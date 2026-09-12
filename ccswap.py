#!/usr/bin/env python3
"""ccswap — alterna entre várias contas do Claude Code na mesma máquina.

Opera sobre os arquivos globais que TODOS os clients do Claude Code leem
(CLI, extensão do VS Code, extensões JetBrains):

    ~/.claude/.credentials.json   -> chave "claudeAiOauth" (o token)
    ~/.claude.json                -> chave "oauthAccount"  (a identidade)

Portanto uma troca vale para a máquina inteira, não só para o terminal.

Só usa a stdlib do Python 3. Sem dependências.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constantes do protocolo OAuth do Claude Code
# ---------------------------------------------------------------------------

OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "ccswap/1.0"

# O Claude Code protege o refresh do token com locks estilo proper-lockfile
# (o "lock" é um diretório; mkdir é o mutex). Os valores abaixo replicam os
# dele: credencial expira o lock em 60s, config em 10s, toque a cada 5s.
CRED_LOCK_STALENESS = 60.0
CONFIG_LOCK_STALENESS = 10.0
TOUCH_INTERVAL = 3.0
LOCK_TIMEOUT = 9.0

# Padrões do auto-switch
DEFAULT_THRESHOLD = 95.0
DEFAULT_INTERVAL = 60
DEFAULT_COOLDOWN = 300.0
DEFAULT_MARGIN = 10.0  # histerese: o alvo precisa estar X pontos melhor

# ---------------------------------------------------------------------------
# Caminhos
# ---------------------------------------------------------------------------


def config_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def credentials_path() -> Path:
    return config_home() / ".credentials.json"


def global_config_path() -> Path:
    legacy = config_home() / ".config.json"
    if legacy.exists():
        return legacy
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(env) if env else Path.home()) / ".claude.json"


def store_root() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg and xdg.startswith("/") else Path.home() / ".local/share"
    return base / "ccswap"


def slot_path(num: int) -> Path:
    return store_root() / "slots" / f"{num}.json"


def state_path() -> Path:
    return store_root() / "state.json"


# ---------------------------------------------------------------------------
# IO utilitário
# ---------------------------------------------------------------------------


def read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_json_atomic(path: Path, data: dict, mode: int = 0o600) -> None:
    """Escreve JSON de forma atômica (tmp no mesmo diretório + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".ccswap-tmp-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Locks compatíveis com os do Claude Code
# ---------------------------------------------------------------------------


class DirLock:
    """Lock por diretório, no mesmo protocolo do proper-lockfile (npm)."""

    def __init__(self, path: Path, staleness: float, timeout: float = LOCK_TIMEOUT):
        self.path = path
        self.staleness = staleness
        self.timeout = timeout
        self._stop = threading.Event()
        self._toucher: threading.Thread | None = None

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                os.mkdir(self.path)
                break
            except FileExistsError:
                try:
                    age = time.time() - os.stat(self.path).st_mtime
                except FileNotFoundError:
                    continue
                if age > self.staleness:
                    # Dono morto: o lock é lixo, remove e tenta de novo.
                    try:
                        os.rmdir(self.path)
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"lock ocupado por outro processo: {self.path}"
                    )
                time.sleep(random.uniform(0.1, 0.3))
        self._stop.clear()
        self._toucher = threading.Thread(target=self._touch_loop, daemon=True)
        self._toucher.start()

    def _touch_loop(self) -> None:
        while not self._stop.wait(TOUCH_INTERVAL):
            try:
                os.utime(self.path, None)
            except OSError:
                return

    def release(self) -> None:
        self._stop.set()
        if self._toucher:
            self._toucher.join(timeout=1)
        try:
            os.rmdir(self.path)
        except OSError:
            pass


@contextmanager
def claude_locks():
    """Segura os mesmos locks que o Claude Code usa ao girar o token.

    Sem isso, um refresh acontecendo no meio da troca sobrescreveria a
    credencial recém-instalada com o token da conta antiga.
    """
    home = config_home()
    gcfg = global_config_path()
    locks = [
        DirLock(home / ".oauth_refresh.lock", CRED_LOCK_STALENESS),
        DirLock(home.parent / (home.name + ".lock"), CRED_LOCK_STALENESS),
        DirLock(gcfg.with_name(gcfg.name + ".lock"), CONFIG_LOCK_STALENESS),
    ]
    with ExitStack() as stack:
        for lock in locks:
            lock.acquire()
            stack.callback(lock.release)
        yield


# ---------------------------------------------------------------------------
# Leitura/escrita da credencial ativa
# ---------------------------------------------------------------------------


def read_active() -> tuple[dict, dict]:
    """Devolve (claudeAiOauth, oauthAccount) do login ativo na máquina."""
    oauth = read_json(credentials_path()).get("claudeAiOauth")
    account = read_json(global_config_path()).get("oauthAccount")
    return (
        oauth if isinstance(oauth, dict) else {},
        account if isinstance(account, dict) else {},
    )


def install_active(oauth: dict, account: dict) -> None:
    """Instala uma credencial como o login ativo, preservando o resto dos arquivos."""
    creds = read_json(credentials_path())
    creds["claudeAiOauth"] = oauth  # mcpOAuth e demais chaves ficam intactos
    write_json_atomic(credentials_path(), creds, mode=0o600)

    if account:
        gpath = global_config_path()
        cfg = read_json(gpath)
        cfg["oauthAccount"] = account
        try:
            mode = os.stat(gpath).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600
        write_json_atomic(gpath, cfg, mode=mode)


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


def load_slots() -> dict[int, dict]:
    out: dict[int, dict] = {}
    d = store_root() / "slots"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            num = int(f.stem)
        except ValueError:
            continue
        data = read_json(f)
        if data:
            out[num] = data
    return dict(sorted(out.items()))


def save_slot(num: int, data: dict) -> None:
    store_root().mkdir(parents=True, exist_ok=True)
    os.chmod(store_root(), 0o700)
    (store_root() / "slots").mkdir(parents=True, exist_ok=True)
    os.chmod(store_root() / "slots", 0o700)
    write_json_atomic(slot_path(num), data, mode=0o600)


def slot_email(slot: dict) -> str:
    return slot.get("account", {}).get("emailAddress") or slot.get("email") or "?"


def fingerprint(oauth: dict) -> str | None:
    tok = oauth.get("refreshToken") or oauth.get("accessToken")
    if not isinstance(tok, str) or not tok:
        return None
    import hashlib

    return hashlib.sha256(tok.encode()).hexdigest()[:16]


def find_active_slot(slots: dict[int, dict]) -> int | None:
    """Qual slot corresponde ao login ativo agora (por e-mail, depois por token)."""
    oauth, account = read_active()
    email = account.get("emailAddress")
    if email:
        for num, slot in slots.items():
            if slot_email(slot).lower() == email.lower():
                return num
    fp = fingerprint(oauth)
    if fp:
        for num, slot in slots.items():
            if fingerprint(slot.get("credentials", {})) == fp:
                return num
    return None


def resolve_slot(slots: dict[int, dict], ref: str) -> int:
    ref = ref.strip()
    if ref.isdigit() and int(ref) in slots:
        return int(ref)
    for num, slot in slots.items():
        if slot_email(slot).lower() == ref.lower():
            return num
    for num, slot in slots.items():
        if slot.get("alias", "").lower() == ref.lower():
            return num
    die(f"conta não encontrada: {ref!r} (veja `ccswap list`)")


# ---------------------------------------------------------------------------
# API OAuth: refresh de token e consulta de uso
# ---------------------------------------------------------------------------


def token_expired(oauth: dict, buffer_s: int = 300) -> bool:
    exp = oauth.get("expiresAt")
    if not isinstance(exp, (int, float)):
        return False
    return now_ms() + buffer_s * 1000 >= int(exp)


def refresh_token(oauth: dict, timeout: float = 10.0) -> tuple[dict | None, str]:
    """Renova o access token. Devolve (novo_oauth|None, motivo)."""
    rt = oauth.get("refreshToken")
    if not isinstance(rt, str) or not rt:
        return None, "sem refresh token — precisa de /login"
    body = json.dumps(
        {"grant_type": "refresh_token", "refresh_token": rt, "client_id": OAUTH_CLIENT_ID}
    ).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        if e.code in (400, 401, 403):
            try:
                kind = json.loads(raw).get("error")
            except (json.JSONDecodeError, AttributeError):
                kind = None
            if kind == "invalid_grant":
                return None, "refresh token morto — precisa de /login nessa conta"
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"rede: {e}"

    new = dict(oauth)
    new["accessToken"] = data["access_token"]
    new["expiresAt"] = now_ms() + int(data["expires_in"]) * 1000
    if data.get("refresh_token"):
        new["refreshToken"] = data["refresh_token"]
    if data.get("scope"):
        new["scopes"] = data["scope"].split()
    return new, "ok"


def fetch_usage(access_token: str, timeout: float = 8.0) -> tuple[dict | None, str]:
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": OAUTH_BETA_HEADER,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return None, "token recusado (401)" if e.code == 401 else f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return None, f"rede: {e}"

    out: dict = {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        win = raw.get(key)
        if isinstance(win, dict) and isinstance(win.get("utilization"), (int, float)):
            out[label] = {
                "pct": float(win["utilization"]),
                "resets_at": win.get("resets_at"),
            }
    scoped = []
    for lim in raw.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        scope = lim.get("scope") or {}
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        if name and isinstance(lim.get("percent"), (int, float)):
            scoped.append(
                {"name": name, "pct": float(lim["percent"]), "resets_at": lim.get("resets_at")}
            )
    if scoped:
        out["scoped"] = scoped
    return (out or None), "ok"


def usage_for_slot(num: int, slot: dict, models: list[str]) -> tuple[dict | None, str]:
    """Uso de um slot, renovando o token guardado se preciso (e persistindo)."""
    oauth = slot.get("credentials", {})
    if not oauth:
        return None, "slot vazio"
    if token_expired(oauth):
        new, why = refresh_token(oauth)
        if not new:
            return None, why
        slot["credentials"] = new
        save_slot(num, slot)
        oauth = new

    usage, why = fetch_usage(oauth.get("accessToken", ""))
    if usage is None and why.startswith("token recusado"):
        new, why2 = refresh_token(oauth)
        if new:
            slot["credentials"] = new
            save_slot(num, slot)
            usage, why = fetch_usage(new.get("accessToken", ""))
    return usage, why


def worst_window(usage: dict | None, models: list[str]) -> tuple[str, float] | None:
    """A janela mais apertada da conta — é ela que manda.

    Devolve (nome, pct). O limite de 5h e o de 7d correm em paralelo e
    resetam em horários diferentes; quem barra primeiro é o maior dos dois,
    então é ele que decide a troca. O nome acompanha o número porque
    "31%" sem dizer de qual janela não significa nada.
    """
    if not usage:
        return None
    wins = [(k, usage[k]["pct"]) for k in ("5h", "7d") if k in usage]
    wanted = {m.lower() for m in models}
    if wanted:
        for sc in usage.get("scoped", []):
            if "all" in wanted or sc["name"].lower() in wanted:
                wins.append((sc["name"], sc["pct"]))
    return max(wins, key=lambda w: w[1]) if wins else None


def worst_pct(usage: dict | None, models: list[str]) -> float | None:
    win = worst_window(usage, models)
    return win[1] if win else None


def fmt_reset(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return ""
    delta = dt - datetime.now().astimezone()
    mins = max(0, int(delta.total_seconds() // 60))
    return f"reset {dt:%H:%M} (em {mins // 60}h{mins % 60:02d})"


def bar(pct: float, width: int = 12) -> str:
    filled = min(width, int(round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"ccswap: {msg}", file=sys.stderr)
    raise SystemExit(1)


def cmd_add(args) -> int:
    oauth, account = read_active()
    if not oauth:
        die("nenhum login OAuth ativo — rode `claude` e faça /login antes")
    email = account.get("emailAddress") or "?"

    slots = load_slots()
    target = args.slot
    if target is None:
        for num, slot in slots.items():
            if slot_email(slot).lower() == email.lower():
                target = num
                print(f"atualizando a conta já cadastrada no slot {num} ({email})")
                break
    if target is None:
        target = next(n for n in range(1, 100) if n not in slots)

    if target in slots and slot_email(slots[target]).lower() != email.lower():
        die(f"slot {target} já é de {slot_email(slots[target])}; use --slot livre ou `remove`")

    save_slot(
        target,
        {
            "email": email,
            "alias": args.alias or slots.get(target, {}).get("alias", ""),
            "credentials": oauth,
            "account": account,
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    write_json_atomic(state_path(), {**read_json(state_path()), "active": target})
    print(f"✓ conta {email} guardada no slot {target}")
    if len(load_slots()) < 2:
        print("\nAgora faça login com a OUTRA conta e rode `ccswap add` de novo:")
        print("  claude  →  /logout não é necessário, use /login e troque de conta")
    return 0


def cmd_remove(args) -> int:
    slots = load_slots()
    num = resolve_slot(slots, args.account)
    email = slot_email(slots[num])
    slot_path(num).unlink(missing_ok=True)
    print(f"✓ slot {num} ({email}) removido")
    return 0


def cmd_alias(args) -> int:
    slots = load_slots()
    num = resolve_slot(slots, args.account)
    slots[num]["alias"] = args.alias
    save_slot(num, slots[num])
    print(f"✓ slot {num} agora atende por {args.alias!r}")
    return 0


def _capture_active_into_slot(slots: dict[int, dict], active: int | None) -> None:
    """Antes de trocar, salva de volta a credencial viva (o Claude Code
    renova o token sozinho; sem isso o slot guardaria um token velho)."""
    if active is None or active not in slots:
        return
    oauth, account = read_active()
    if not oauth:
        return
    slot = slots[active]
    slot["credentials"] = oauth
    if account:
        slot["account"] = account
    save_slot(active, slot)


def do_switch(target: int, slots: dict[int, dict], reason: str = "") -> None:
    slot = slots[target]
    oauth = slot.get("credentials") or {}
    if not oauth:
        die(f"slot {target} não tem credencial guardada")

    with claude_locks():
        active = find_active_slot(slots)
        if active == target:
            print(f"já está em {slot_email(slot)} (slot {target})")
            return
        _capture_active_into_slot(slots, active)
        install_active(oauth, slot.get("account") or {})

    state = read_json(state_path())
    state.update(
        {
            "active": target,
            "last_switch": time.time(),
            "last_reason": reason,
        }
    )
    write_json_atomic(state_path(), state)
    print(f"✓ agora em {slot_email(slot)} (slot {target}){' — ' + reason if reason else ''}")


def cmd_switch(args) -> int:
    slots = load_slots()
    if len(slots) < 2:
        die("cadastre pelo menos duas contas com `ccswap add`")
    models = [m for m in (args.model or "").split(",") if m]

    if args.account:
        target = resolve_slot(slots, args.account)
    else:
        active = find_active_slot(slots)
        if args.best:
            best, best_pct = None, None
            for num, slot in slots.items():
                if num == active:
                    continue
                usage, _ = usage_for_slot(num, slot, models)
                pct = worst_pct(usage, models)
                if pct is not None and (best_pct is None or pct < best_pct):
                    best, best_pct = num, pct
            if best is None:
                die("não consegui ler o uso de nenhuma outra conta")
            target = best
        else:
            nums = list(slots)
            target = nums[(nums.index(active) + 1) % len(nums)] if active in nums else nums[0]

    do_switch(target, slots)
    print("  (vale para o CLI e para a extensão do VS Code — veja `ccswap reload`)")
    return 0


def cmd_status(args) -> int:
    slots = load_slots()
    oauth, account = read_active()
    if not oauth:
        print("nenhum login ativo")
        return 1
    num = find_active_slot(slots)
    label = f"slot {num}" if num else "não cadastrada (`ccswap add`)"
    print(f"{account.get('emailAddress', '?')} — {label}")
    return 0


def cmd_list(args) -> int:
    slots = load_slots()
    if not slots:
        die("nenhuma conta cadastrada — rode `ccswap add` com cada conta logada")
    active = find_active_slot(slots)
    models = [m for m in (args.model or "").split(",") if m]

    for num, slot in slots.items():
        mark = "→" if num == active else " "
        alias = f" [{slot['alias']}]" if slot.get("alias") else ""
        print(f"{mark} {num}. {slot_email(slot)}{alias}")
        if args.no_usage:
            continue
        usage, why = usage_for_slot(num, slot, models)
        if not usage:
            print(f"      uso indisponível: {why}")
            continue
        for key in ("5h", "7d"):
            if key in usage:
                w = usage[key]
                print(f"      {key:<3} {bar(w['pct'])} {w['pct']:5.1f}%  {fmt_reset(w['resets_at'])}")
        for sc in usage.get("scoped", []):
            print(f"      {sc['name'][:8]:<8} {sc['pct']:5.1f}%  {fmt_reset(sc['resets_at'])}")
    return 0


def cmd_reload(args) -> int:
    """Força os clients a reconhecerem a credencial nova."""
    print("CLI: sessões novas já usam a conta nova; uma sessão aberta pega na")
    print("     próxima renovação de token — para valer agora, saia e rode `claude -c`.")
    print("VS Code: feche e reabra a aba do Claude (ou recarregue a janela:")
    print("     Ctrl+Shift+P → 'Developer: Reload Window').")
    return 0


def cmd_auto(args) -> int:
    slots = load_slots()
    if len(slots) < 2:
        die("cadastre pelo menos duas contas com `ccswap add`")
    models = [m for m in (args.model or "").split(",") if m]
    threshold = args.threshold

    def log(msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] {msg}", flush=True)

    def tick() -> str:
        slots_now = load_slots()
        active = find_active_slot(slots_now)
        if active is None:
            log("login ativo não corresponde a nenhum slot — rode `ccswap add`")
            return "erro"

        # A conta ativa é lida do arquivo vivo: é o Claude Code quem renova
        # esse token, então nunca o renovamos por baixo dele.
        oauth, _ = read_active()
        usage, why = fetch_usage(oauth.get("accessToken", ""))
        window = worst_window(usage, models)
        if window is None:
            log(f"uso da conta ativa indisponível ({why}) — mantendo")
            return "nada"
        win, pct = window

        label = slot_email(slots_now[active])
        if pct < threshold:
            outras = " · ".join(
                f"{k} {usage[k]['pct']:.0f}%" for k in ("5h", "7d") if k in usage
            )
            log(f"{label}: {outras} — pior é {win} ({pct:.1f}%), limite {threshold:.0f}% — ok")
            return "nada"

        state = read_json(state_path())
        since = time.time() - float(state.get("last_switch") or 0)
        if since < args.cooldown:
            log(f"{label}: {win} em {pct:.1f}% — em cooldown ({int(args.cooldown - since)}s)")
            return "nada"

        best, best_pct = None, None
        for num, slot in slots_now.items():
            if num == active:
                continue
            u, w = usage_for_slot(num, slot, models)
            cand = worst_window(u, models)
            if cand is None:
                log(f"  slot {num} ({slot_email(slot)}): sem leitura ({w})")
                continue
            cand_win, p = cand
            log(f"  slot {num} ({slot_email(slot)}): pior {cand_win} {p:.1f}%")
            if p < threshold and p <= pct - args.margin and (best_pct is None or p < best_pct):
                best, best_pct = num, p

        if best is None:
            log(f"{label}: {win} em {pct:.1f}% — nenhuma conta alternativa com folga")
            return "bloqueado"

        if args.dry_run:
            log(f"[dry-run] trocaria para {slot_email(slots_now[best])} ({best_pct:.1f}%)")
            return "trocado"

        do_switch(best, slots_now, reason=f"{label} com {win} em {pct:.1f}%")
        notify(
            f"Claude: troquei para {slot_email(slots_now[best])}",
            f"{label} chegou a {pct:.1f}% da janela de {win}",
        )
        return "trocado"

    if args.once:
        return {"trocado": 0, "erro": 1, "nada": 2, "bloqueado": 3}[tick()]

    log(f"monitorando {len(slots)} contas, troca em {threshold:.0f}%, a cada {args.interval}s")
    try:
        while True:
            try:
                tick()
            except Exception as e:  # nunca deixa o loop morrer
                log(f"erro no ciclo: {e!r}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("encerrado")
    return 0


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", "-a", "ccswap", title, body],
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            pass


SERVICE_UNIT = """[Unit]
Description=ccswap - troca automatica de conta do Claude Code
After=network-online.target

[Service]
Type=simple
ExecStart={exe} auto --threshold {threshold} --interval {interval}
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
"""


def cmd_service(args) -> int:
    unit_dir = Path.home() / ".config/systemd/user"
    unit = unit_dir / "ccswap.service"
    if args.action == "install":
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            SERVICE_UNIT.format(
                exe=str(Path(sys.argv[0]).resolve()),
                threshold=int(args.threshold),
                interval=args.interval,
            )
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "ccswap.service"], check=False)
        print(f"✓ serviço instalado: {unit}")
        print("  status: systemctl --user status ccswap")
        print("  log:    journalctl --user -u ccswap -f")
        return 0
    if args.action == "uninstall":
        subprocess.run(["systemctl", "--user", "disable", "--now", "ccswap.service"], check=False)
        unit.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print("✓ serviço removido")
        return 0
    subprocess.run(["systemctl", "--user", "status", "ccswap.service"], check=False)
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="ccswap",
        description="Alterna contas do Claude Code (CLI + extensão VS Code).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="guarda a conta logada agora num slot")
    a.add_argument("--slot", type=int, help="slot de destino")
    a.add_argument("--alias", help="apelido curto")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("remove", help="remove um slot")
    r.add_argument("account")
    r.set_defaults(func=cmd_remove)

    al = sub.add_parser("alias", help="dá um apelido a um slot")
    al.add_argument("account")
    al.add_argument("alias")
    al.set_defaults(func=cmd_alias)

    li = sub.add_parser("list", help="lista as contas com uso 5h/7d")
    li.add_argument("--model", help="também mostra limites semanais por modelo (ex: Fable)")
    li.add_argument("--no-usage", action="store_true", help="não consulta a API")
    li.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="mostra a conta ativa")
    st.set_defaults(func=cmd_status)

    sw = sub.add_parser("switch", help="troca de conta")
    sw.add_argument("account", nargs="?", help="slot, e-mail ou apelido (vazio = próxima)")
    sw.add_argument("--best", action="store_true", help="escolhe a de maior folga")
    sw.add_argument("--model", help="considera o limite semanal desse modelo")
    sw.set_defaults(func=cmd_switch)

    au = sub.add_parser("auto", help="monitora e troca sozinho ao chegar no limite")
    au.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    au.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    au.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN)
    au.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    au.add_argument("--model", help="inclui o limite semanal desse modelo na decisão")
    au.add_argument("--once", action="store_true", help="uma verificação só (cron/systemd)")
    au.add_argument("--dry-run", action="store_true")
    au.set_defaults(func=cmd_auto)

    sv = sub.add_parser("service", help="instala o auto-switch como serviço do systemd")
    sv.add_argument("action", choices=["install", "uninstall", "status"], nargs="?", default="status")
    sv.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    sv.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    sv.set_defaults(func=cmd_service)

    rl = sub.add_parser("reload", help="como fazer os clients pegarem a conta nova")
    rl.set_defaults(func=cmd_reload)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except TimeoutError as exc:
        die(str(exc))

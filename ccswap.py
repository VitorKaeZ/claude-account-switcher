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
HARD_LIMIT = 100.0  # a partir daqui a janela barra de vez
DEFAULT_MARGIN = 10.0  # histerese: o alvo precisa estar X pontos melhor

# O endpoint de uso aceita ~1 leitura a cada 2 min por conta (medido; o
# Retry-After do 429 vem 0). Serviço, mods e CLI dividem esse cache.
USAGE_MIN_AGE = 150.0
USAGE_MAX_STALE = 1800.0  # leitura mais velha que isso não vale como uso

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


def profiles_path() -> Path:
    return store_root() / "profiles.json"


def usage_cache_path() -> Path:
    return store_root() / "usage.json"


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


def slot_label(slot: dict) -> str:
    return slot.get("alias") or slot_email(slot)


# ---------------------------------------------------------------------------
# Perfis: grupos de contas; com um ativo, a troca só circula entre as dele
# ---------------------------------------------------------------------------


def load_profiles() -> dict[str, list[int]]:
    data = read_json(profiles_path())
    return {name: [int(n) for n in nums] for name, nums in data.items() if isinstance(nums, list)}


def save_profiles(profiles: dict[str, list[int]]) -> None:
    write_json_atomic(profiles_path(), profiles, mode=0o600)


def active_profile() -> str | None:
    name = read_json(state_path()).get("profile")
    return name if name in load_profiles() else None


def set_active_profile(name: str | None) -> None:
    state = read_json(state_path())
    state["profile"] = name
    write_json_atomic(state_path(), state)


def scoped_slots(slots: dict[int, dict]) -> dict[int, dict]:
    """Os slots do perfil ativo; sem perfil, todos."""
    name = active_profile()
    if name is None:
        return slots
    members = set(load_profiles()[name])
    return {num: slot for num, slot in slots.items() if num in members}


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


def read_usage(email: str, access_token: str) -> tuple[dict | None, str]:
    """Uso da conta pelo cache compartilhado; só consulta a API quando a
    última leitura passou de USAGE_MIN_AGE e não há 429 recente.

    O uso devolvido traz "read_at" (epoch da leitura). Se a consulta falha,
    devolve a última leitura boa (até USAGE_MAX_STALE) com o motivo.
    """
    key = email.lower()
    entry = read_json(usage_cache_path()).get(key) or {}
    now = time.time()
    is_blocked = now < entry.get("blocked_until", 0)
    why = entry.get("error", "ok") if is_blocked else "ok"
    if not is_blocked and now - entry.get("at", 0) >= USAGE_MIN_AGE:
        usage, why = fetch_usage(access_token)
        if usage:
            entry = {"usage": usage, "at": now}
        elif why == "HTTP 429":
            entry = {**entry, "blocked_until": now + USAGE_MIN_AGE, "error": why}
        else:
            return None, why
        cache = read_json(usage_cache_path())
        cache[key] = entry
        write_json_atomic(usage_cache_path(), cache)
        if usage:
            return {**usage, "read_at": now}, "ok"

    if entry.get("usage") and now - entry["at"] < USAGE_MAX_STALE:
        return {**entry["usage"], "read_at": entry["at"]}, why
    return None, why if why != "ok" else "sem leitura recente"


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

    usage, why = read_usage(slot_email(slot), oauth.get("accessToken", ""))
    if usage is None and why.startswith("token recusado"):
        new, why2 = refresh_token(oauth)
        if new:
            slot["credentials"] = new
            save_slot(num, slot)
            usage, why = read_usage(slot_email(slot), new.get("accessToken", ""))
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


def parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return None


def available_at(usage: dict | None, models: list[str], threshold: float) -> datetime | None:
    """Quando a conta volta a ficar abaixo do limite.

    Uma conta só libera quando TODAS as janelas acima do limite resetam, então
    vale o reset mais tardio entre elas. Sem janela acima do limite, já está
    livre (agora). None quando não dá para saber (sem leitura ou sem horário).
    """
    if not usage:
        return None
    wins = [(usage[k]["pct"], usage[k].get("resets_at")) for k in ("5h", "7d") if k in usage]
    wanted = {m.lower() for m in models}
    if wanted:
        for sc in usage.get("scoped", []):
            if "all" in wanted or sc["name"].lower() in wanted:
                wins.append((sc["pct"], sc.get("resets_at")))
    blocking = [parse_iso(r) for p, r in wins if p >= threshold]
    if not blocking:
        return datetime.now().astimezone()
    if any(b is None for b in blocking):
        return None
    return max(b for b in blocking if b is not None)


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
    profiles = load_profiles()
    if any(num in nums for nums in profiles.values()):
        save_profiles({name: [n for n in nums if n != num] for name, nums in profiles.items()})
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


def least_used_slot(slots: dict[int, dict], skip: int | None, models: list[str]) -> int | None:
    best, best_pct = None, None
    for num, slot in slots.items():
        if num == skip:
            continue
        usage, _ = usage_for_slot(num, slot, models)
        pct = worst_pct(usage, models)
        if pct is not None and (best_pct is None or pct < best_pct):
            best, best_pct = num, pct
    return best


def cmd_switch(args) -> int:
    slots = load_slots()
    if len(slots) < 2:
        die("cadastre pelo menos duas contas com `ccswap add`")
    models = [m for m in (args.model or "").split(",") if m]

    if args.account:
        # Pedida pelo nome, vale qualquer conta, mesmo fora do perfil ativo.
        target = resolve_slot(slots, args.account)
    else:
        pool = scoped_slots(slots)
        active = find_active_slot(slots)
        if len(pool) < 2 and active in pool:
            die(f"o perfil {active_profile()!r} só tem esta conta")
        if args.best:
            target = least_used_slot(pool, active, models)
            if target is None:
                die("não consegui ler o uso de nenhuma outra conta")
        else:
            nums = list(pool)
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
    all_slots = load_slots()
    if not all_slots:
        die("nenhuma conta cadastrada — rode `ccswap add` com cada conta logada")
    active = find_active_slot(all_slots)
    slots = all_slots if args.all else scoped_slots(all_slots)
    profile = active_profile()
    models = [m for m in (args.model or "").split(",") if m]

    if args.json:
        rows = []
        for num, slot in slots.items():
            row = {
                "slot": num,
                "email": slot_email(slot),
                "alias": slot.get("alias", ""),
                "active": num == active,
                "usage": None,
                "error": None,
            }
            if not args.no_usage:
                usage, why = usage_for_slot(num, slot, models)
                row["usage"] = usage
                if usage:
                    row["usage_age"] = int(time.time() - usage["read_at"])
                if why != "ok":
                    row["error"] = why
            rows.append(row)
        print(json.dumps(rows, ensure_ascii=False))
        return 0

    if profile and not args.all:
        print(f"perfil {profile} (`ccswap list --all` mostra todas)")
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
        age = time.time() - usage["read_at"]
        if age >= USAGE_MIN_AGE:
            print(f"      (leitura de {int(age // 60)}min atrás{'; ' + why if why != 'ok' else ''})")
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
        usage, why = read_usage(slot_email(slots_now[active]), oauth.get("accessToken", ""))
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
        others: dict[int, dict | None] = {}
        for num, slot in scoped_slots(slots_now).items():
            if num == active:
                continue
            u, w = usage_for_slot(num, slot, models)
            others[num] = u
            cand = worst_window(u, models)
            if cand is None:
                log(f"  slot {num} ({slot_email(slot)}): sem leitura ({w})")
                continue
            cand_win, p = cand
            log(f"  slot {num} ({slot_email(slot)}): pior {cand_win} {p:.1f}%")
            if p < threshold and p <= pct - args.margin and (best_pct is None or p < best_pct):
                best, best_pct = num, p

        if best is None:
            # Acima do limite não quer dizer sem cota: 98% ainda atende, 100%
            # não. Enquanto a ativa tem cota, fica; sem cota, vai para a outra
            # que ainda tem, a de menor uso.
            if pct < HARD_LIMIT:
                log(f"{label}: {win} em {pct:.1f}% — nenhuma outra abaixo de {threshold:.0f}%, "
                    f"mas a ativa ainda tem cota — mantendo")
                return "bloqueado"
            usable = {num: p for num, u in others.items()
                      if (p := worst_pct(u, models)) is not None and p < HARD_LIMIT}
            if usable:
                target = min(usable, key=lambda num: usable[num])
                if args.dry_run:
                    log(f"[dry-run] ativa esgotada — trocaria para "
                        f"{slot_email(slots_now[target])} ({usable[target]:.1f}%)")
                    return "trocado"
                do_switch(target, slots_now,
                          reason=f"{label} esgotada — esta ainda tem cota ({usable[target]:.1f}%)")
                notify(
                    f"Claude: troquei para {slot_email(slots_now[target])}",
                    f"{label} esgotou a janela de {win}; esta ainda tem cota",
                )
                return "trocado"

            # Todas esgotadas de verdade: fica na que libera primeiro, para a
            # troca automática voltar a ter folga o quanto antes.
            soonest = soonest_release(active, usage, others, models, HARD_LIMIT, log)
            if soonest is None:
                log(f"{label}: {win} em {pct:.1f}% — nenhuma conta alternativa com folga")
                return "bloqueado"
            target, when = soonest
            quando = f"{when:%d/%m %H:%M}"
            if args.dry_run:
                log(f"[dry-run] todas esgotadas — trocaria para "
                    f"{slot_email(slots_now[target])}, que libera em {quando}")
                return "trocado"
            do_switch(target, slots_now,
                      reason=f"todas esgotadas — esta libera primeiro ({quando})")
            notify(
                f"Claude: troquei para {slot_email(slots_now[target])}",
                f"Todas as contas no limite; esta libera primeiro, às {quando}",
            )
            return "trocado"

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


def cmd_profile(args) -> int:
    profiles = load_profiles()
    slots = load_slots()
    action = args.action

    if action == "list":
        current = active_profile()
        if args.json:
            print(json.dumps({"active": current, "profiles": profiles}, ensure_ascii=False))
            return 0
        if not profiles:
            print("nenhum perfil — crie com `ccswap profile set NOME CONTA...`")
            return 0
        for name, nums in profiles.items():
            members = ", ".join(f"{n} ({slot_label(slots[n])})" if n in slots else f"{n} (removido)" for n in nums)
            print(f"{'→' if name == current else ' '} {name}: {members or 'sem contas'}")
        if current is None:
            print("  (nenhum ativo: a troca usa todas as contas)")
        return 0

    if action == "off":
        set_active_profile(None)
        print("✓ sem perfil: a troca volta a usar todas as contas")
        return 0

    if not args.name:
        die(f"diga o nome do perfil: `ccswap profile {action} NOME`")
    name = args.name.strip()

    if action == "set":
        if not args.accounts:
            die("diga as contas do perfil: `ccswap profile set NOME CONTA...` (slot, e-mail ou apelido)")
        nums = list(dict.fromkeys(resolve_slot(slots, ref) for ref in args.accounts))
        save_profiles({**profiles, name: nums})
        print(f"✓ perfil {name}: {', '.join(slot_label(slots[n]) for n in nums)}")
        return 0

    if name not in profiles:
        die(f"perfil não encontrado: {name!r} (veja `ccswap profile`)")

    if action == "delete":
        del profiles[name]
        save_profiles(profiles)
        if read_json(state_path()).get("profile") == name:
            set_active_profile(None)
        print(f"✓ perfil {name} apagado")
        return 0

    # use
    pool = {n: slots[n] for n in profiles[name] if n in slots}
    if not pool:
        die(f"o perfil {name} não tem contas cadastradas")
    set_active_profile(name)
    print(f"✓ perfil {name} ativo")
    if find_active_slot(slots) in pool:
        return 0
    target = least_used_slot(pool, None, []) or next(iter(pool))
    do_switch(target, slots, reason=f"perfil {name}")
    print("  (vale para o CLI e para a extensão do VS Code — veja `ccswap reload`)")
    return 0


def soonest_release(
    active: int,
    active_usage: dict | None,
    others: dict[int, dict | None],
    models: list[str],
    threshold: float,
    log,
) -> tuple[int, datetime] | None:
    """Com todas as contas no limite, a outra conta que libera antes da ativa.

    Devolve (slot, quando) ou None se a ativa já é a que libera primeiro
    (ou se não dá para saber de nenhuma).
    """
    mine = available_at(active_usage, models, threshold)
    log(f"  todas no limite — a ativa libera em {mine:%d/%m %H:%M}" if mine
        else "  todas no limite — horário de liberação da ativa desconhecido")
    best: tuple[int, datetime] | None = None
    for num, u in others.items():
        when = available_at(u, models, threshold)
        if when is None:
            continue
        log(f"  slot {num} libera em {when:%d/%m %H:%M}")
        if best is None or when < best[1]:
            best = (num, when)
    if best is None:
        return None
    if mine is not None and mine <= best[1]:
        return None
    return best


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
    li.add_argument("--json", action="store_true", help="saída em JSON (usada pelo mod)")
    li.add_argument("--all", action="store_true", help="todas as contas, não só as do perfil ativo")
    li.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="mostra a conta ativa")
    st.set_defaults(func=cmd_status)

    sw = sub.add_parser("switch", help="troca de conta")
    sw.add_argument("account", nargs="?", help="slot, e-mail ou apelido (vazio = próxima)")
    sw.add_argument("--best", action="store_true", help="escolhe a de maior folga")
    sw.add_argument("--model", help="considera o limite semanal desse modelo")
    sw.set_defaults(func=cmd_switch)

    pr = sub.add_parser("profile", help="perfis: grupos de contas entre os quais a troca circula")
    pr.add_argument("action", nargs="?", default="list", choices=["list", "set", "use", "off", "delete"])
    pr.add_argument("name", nargs="?", help="nome do perfil")
    pr.add_argument("accounts", nargs="*", help="slots, e-mails ou apelidos (em `set`)")
    pr.add_argument("--json", action="store_true", help="saída em JSON (usada pelo mod)")
    pr.set_defaults(func=cmd_profile)

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

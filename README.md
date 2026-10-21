# ccswap

Alterna entre várias contas do Claude Code na mesma máquina — manualmente ou
sozinho, quando o uso chega perto do limite.

Escrito em Python 3 puro (stdlib), sem dependências. Inspirado no
[claude-swap](https://github.com/realiti4/claude-swap), mas reduzido ao
essencial e sem código de terceiros rodando sobre os seus tokens.

## Por que funciona em todos os clients

O Claude Code guarda o login em dois arquivos globais, lidos pelo CLI, pela
extensão do VS Code e pelas do JetBrains:

| arquivo | chave | conteúdo |
|---|---|---|
| `~/.claude/.credentials.json` | `claudeAiOauth` | access token + refresh token |
| `~/.claude.json` | `oauthAccount` | identidade (e-mail, org) |

O `ccswap` troca **só essas duas chaves**, preservando todo o resto dos
arquivos (`mcpOAuth`, `projects`, `mcpServers`, settings…). Como a troca é no
disco compartilhado, ela vale para a máquina inteira.

Durante a escrita ele segura os mesmos locks que o Claude Code usa ao renovar
o token (`~/.claude/.oauth_refresh.lock`, `~/.claude.lock`,
`~/.claude.json.lock` — diretórios, protocolo do `proper-lockfile`). Sem isso,
um refresh no meio da troca sobrescreveria a credencial recém-instalada com o
token da conta antiga. Se o lock estiver ocupado, ele espera 9s e desiste em
vez de atropelar; se estiver abandonado (mtime > 60s), assume.

Antes de sair de uma conta, o token vivo dela é copiado de volta para o slot —
o Claude Code renova o access token sozinho, e sem essa captura o slot ficaria
com um token velho.

## Uso

```bash
# 1. logado na conta A:
ccswap add
# 2. faça /login com a conta B no Claude Code, depois:
ccswap add

ccswap list              # as duas contas com uso 5h / 7d e horário do reset
ccswap status            # qual está ativa
ccswap switch            # vai para a próxima
ccswap switch 2          # por slot
ccswap switch dev        # por apelido (ccswap alias 2 dev)
ccswap switch --best     # a que tiver mais folga
ccswap remove 2
```

### Troca automática

```bash
ccswap auto                       # loop no terminal, checa a cada 60s
ccswap auto --threshold 90        # troca antes, em 90%
ccswap auto --model Fable         # considera também o limite semanal do modelo
ccswap auto --once --dry-run      # uma checagem, sem trocar (cron/systemd)
```

Regras: só troca se a outra conta estiver **abaixo** do limite e pelo menos
`--margin` (10) pontos melhor que a atual — evita ping-pong; e respeita um
`--cooldown` de 5 min entre trocas.

Se **todas** as contas passaram do limite, acima do limite ainda não é sem
cota (98% atende, 100% não):

1. a ativa ainda tem cota (abaixo de 100%): fica nela;
2. a ativa chegou a 100%: vai para a outra que ainda tem cota, a de menor uso;
3. todas em 100%: vai para a que libera primeiro. Uma conta só libera quando
   todas as janelas em 100% resetam, então uma com o 5h resetando em 1h mas o
   7d em 100% conta pelo reset do 7d. Se a ativa já é a que libera primeiro,
   nada muda.

Códigos de saída do `--once`:
`0` trocou · `1` erro · `2` nada a fazer · `3` sem conta alternativa viável.

Como serviço (sobe no login, reinicia sozinho):

```bash
ccswap service install --threshold 95
systemctl --user status ccswap
journalctl --user -u ccswap -f
ccswap service uninstall
```

### Depois de trocar

- **CLI**: sessões novas já nascem na conta nova. Uma sessão aberta pega na
  próxima renovação de token; para valer na hora, saia e rode `claude -c`.
- **VS Code**: feche e reabra a aba do Claude, ou
  `Ctrl+Shift+P → Developer: Reload Window`.

`ccswap reload` imprime esse lembrete.

### Comando dentro do Claude

`~/.claude/skills/trocar-conta/SKILL.md` expõe isso como `/trocar-conta` no
próprio Claude (CLI e VS Code), sem precisar ir ao terminal:

```
/trocar-conta            # mostra o uso das duas e vai para a próxima
/trocar-conta melhor     # vai para a de maior folga
/trocar-conta 1          # por slot, e-mail ou apelido
```

### Mod do Claude Code

`mod/ccswap/` é um mod (Claude Code v2.1.287+) que usa o `ccswap` por baixo:

- **linha de status** embaixo do rodapé, com cada conta, a pior janela e em
  quanto tempo ela reseta quando passa de 75%:
  `ccswap: → vitorkaez ▰▰▱▱▱ 40%  gabrielfpl40 ▰▰▰▰▱ 80% ↻3h30`. A faixa
  acima do prompt fica livre para outros mods (o savvy-progress usa ela);
- **painel Contas** na barra lateral (`/conta painel` abre e fecha): barras
  5h / 7d em verde, amarelo ou vermelho, um botão `usar` em cada conta
  inativa e `próxima`, `melhor`, `auto` e `atualizar` no pé. Com o painel
  focado, os atalhos são o número do slot, `p`, `m`, `a` e `r`. Abre sozinho
  no início da sessão com a opção *Abrir o painel ao iniciar*;
- o uso é relido a cada 5 min e depois de cada troca; a conta ativa é
  conferida a cada minuto sem consultar a API, então uma troca do
  `ccswap service` aparece logo. Quando a API de uso falha (HTTP 429), o mod
  mantém a última leitura (`~`) e, para a conta da sessão, usa os limites que
  as próprias respostas do Claude trazem (`•`);
- **`/conta`**, que roda na hora, sem gastar turno e mesmo com o Claude
  trabalhando:

  ```
  /conta              # lista as contas com uso
  /conta painel       # abre ou fecha o painel
  /conta proxima      # vai para a próxima
  /conta melhor       # vai para a de maior folga
  /conta 2            # por slot, e-mail ou apelido
  /conta auto         # uma checagem do auto agora
  /conta ocultar      # esconde a linha de status (/conta mostrar traz de volta)
  ```

  Depois de `/conta ` o autocomplete sugere os subcomandos e as contas.

- **troca automática** (desligada por padrão): a cada resposta, se a
  pior janela da conta ativa passou do limite, roda `ccswap auto --once` —
  com o mesmo cooldown e a mesma margem do serviço — e avisa num toast.

A troca em si continua no `ccswap.py`: a API de arquivos dos mods não tem
`mkdir`, `rmdir` nem `chmod`, então os locks e os slots `0600` ficam com ele.
O mod só precisa do `ccswap` no `PATH` (ou do caminho absoluto nas opções).

Instalar:

```
/plugin marketplace add VitorKaeZ/claude-account-switcher
/plugin install ccswap@ccswap
```

As opções (caminho do `ccswap`, troca automática, limite, intervalo, painel) aparecem
no `/plugin` → aba **Installed** → ccswap. O timer e o gatilho só rodam com
uma sessão aberta, então mantenha o `ccswap service` se quiser cobertura o
tempo todo; com os dois ligados, o cooldown evita trocas em dobro. No VS Code
os hooks rodam, mas a linha de status e o painel não aparecem — use `/conta`.
O painel lateral precisa do layout em tela cheia (`/tui fullscreen`) e de um
terminal com 110 colunas ou mais; abaixo disso ele aparece acima do prompt.

Testes do mod: `claude plugin test mod/ccswap`.

## Onde ficam os dados

`~/.local/share/ccswap/` (modo `0700`):

- `slots/N.json` — `{email, alias, credentials, account}` de cada conta, `0600`
- `state.json` — slot ativo, horário da última troca

São tokens OAuth em texto claro, com as mesmas permissões que o próprio Claude
Code usa em `~/.claude/.credentials.json`. Não versione essa pasta.

## Limitações

- Um access token expirado de conta **inativa** é renovado pelo próprio
  ccswap ao consultar o uso. Se o *refresh token* morrer (expira ou é revogado
  por um `/logout`), a conta aparece como ilegível: faça `/login` com ela e
  rode `ccswap add` de novo para atualizar o slot.
- Não rode `/logout` para trocar de conta — o Claude Code pode revogar o
  refresh token guardado. Use `/login` direto, ou o próprio `ccswap switch`.
- A API de uso (`/api/oauth/usage`) não é pública nem documentada; se a
  Anthropic mudar o formato, o `list`/`auto` param de ler o uso (a troca
  manual continua funcionando).

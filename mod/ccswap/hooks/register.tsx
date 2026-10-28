import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionRateLimit } from 'claude-code'

import type { Account, Profiles, ShownAccount, Usage, Window } from '../types'

// A troca em si (locks do proper-lockfile, slots 0600, refresh de token)
// continua no ccswap.py: a API de arquivos dos mods não tem mkdir, rmdir nem
// chmod. O mod só chama o ccswap e cuida da linha de status, do painel, do
// /conta e do gatilho. A faixa acima do prompt fica livre para outros mods.

const accounts = atom({ plugin: 'ccswap', key: 'accounts' } as const, null)
const liveUsage = atom({ plugin: 'ccswap', key: 'liveUsage' } as const, null)
const lastError = atom({ plugin: 'ccswap', key: 'lastError' } as const, null)
const isHidden = atom({ plugin: 'ccswap', key: 'isHidden' } as const, false)
const lastAction = atom({ plugin: 'ccswap', key: 'lastAction' } as const, null)
const profiles = atom({ plugin: 'ccswap', key: 'profiles' } as const, null)

const PANE_ID = 'ccswap'
const PANE_TITLE = 'Contas'
const AUTO_CHECK_EVERY_MS = 60_000
// Sem consultar a API: só percebe a tempo uma troca feita pelo ccswap.service.
const ACTIVE_CHECK_EVERY_MS = 60_000
const WARNING_PCT = 75
const WINDOWS = ['5h', '7d'] as const
const STATUS_BAR_CELLS = 5
// O ccswap guarda o uso num cache compartilhado; leitura mais velha que isso
// aparece como "última leitura".
const STALE_USAGE_S = 300

const SUBCOMMANDS = [
  { text: 'painel', description: 'abre ou fecha o painel com as barras e os botões' },
  { text: 'proxima', description: 'troca para a próxima conta' },
  { text: 'melhor', description: 'troca para a conta com mais folga' },
  { text: 'auto', description: 'troca só se a ativa passou do limite' },
  { text: 'perfil', description: 'lista os perfis; `perfil <nome>` ativa, `perfil off` desliga' },
  { text: 'ocultar', description: 'esconde a linha de status' },
  { text: 'mostrar', description: 'mostra a linha de status' },
]

type Run = { exitCode: number; stdout: string; stderr: string }

const cfg = { bin: 'ccswap', autoSwitch: false, threshold: 95, refreshMs: 5 * 60_000 }

async function ccswap($: EngineInterface, args: string[], timeoutMs = 60_000): Promise<Run> {
  const argv = cfg.bin.endsWith('.py') ? ['python3', cfg.bin, ...args] : [cfg.bin, ...args]
  try {
    const r = await $.process.run(argv, { timeoutMs })
    return { exitCode: r.exitCode, stdout: r.stdout.trim(), stderr: r.stderr.trim() }
  } catch (err) {
    return { exitCode: 127, stdout: '', stderr: `não consegui rodar ${argv.join(' ')}: ${String(err)}` }
  }
}

// Uma leitura que falhou (429 é comum) não apaga a anterior da mesma conta.
function mergeWithPrevious(fresh: Account[], previous: ShownAccount[] | null, withUsage: boolean): ShownAccount[] {
  return fresh.map(account => {
    if (withUsage && account.usage) {
      return { ...account, usageSource: (account.usage_age ?? 0) > STALE_USAGE_S ? 'cache' : 'api' }
    }
    const old = previous?.find(p => p.email === account.email)
    if (old?.usage) return { ...account, usage: old.usage, usageSource: withUsage ? 'cache' : old.usageSource }
    return { ...account, usageSource: null }
  })
}

async function refresh($: EngineInterface, withUsage = true): Promise<ShownAccount[] | null> {
  const [r] = await Promise.all([
    ccswap($, withUsage ? ['list', '--json'] : ['list', '--json', '--no-usage']),
    refreshProfiles($),
  ])
  if (r.exitCode !== 0) {
    await update($, lastError, () => r.stderr || `ccswap saiu com ${r.exitCode}`)
    await syncStatus($)
    return null
  }
  let fresh: Account[]
  try {
    fresh = JSON.parse(r.stdout) as Account[]
  } catch {
    await update($, lastError, () => 'saída do ccswap não é JSON — atualize o ccswap.py (precisa de `list --json`)')
    await syncStatus($)
    return null
  }
  const previous = await read($, accounts)
  const merged = mergeWithPrevious(fresh, previous, withUsage)
  await update($, accounts, () => merged)
  await update($, lastError, () => null)
  await syncStatus($)
  return merged
}

async function refreshProfiles($: EngineInterface) {
  const r = await ccswap($, ['profile', '--json'])
  let parsed: Profiles | null = null
  try {
    const data = JSON.parse(r.stdout) as Profiles
    if (r.exitCode === 0 && data && typeof data.profiles === 'object') parsed = data
  } catch {
    // ccswap antigo, sem perfis
  }
  await update($, profiles, () => parsed)
}

async function afterSwitch($: EngineInterface) {
  // A leitura ao vivo era da conta anterior.
  await update($, liveUsage, () => null)
  await refresh($)
}

// Roda um `ccswap switch …` ou `auto` e devolve o texto para o /conta ou o painel.
async function runSwitch($: EngineInterface, args: string[]): Promise<{ text: string; isSwitched: boolean }> {
  const r = await ccswap($, args)
  if (args[0] === 'auto') {
    if (r.exitCode === 0) await afterSwitch($)
    else await refresh($)
    const out = [r.stdout, r.stderr].filter(Boolean).join('\n')
    return { text: out || `nada a fazer (código ${r.exitCode})`, isSwitched: r.exitCode === 0 }
  }
  if (r.exitCode !== 0) {
    await refresh($)
    return { text: r.stderr || `ccswap saiu com ${r.exitCode}`, isSwitched: false }
  }
  const first = r.stdout.split('\n')[0] ?? ''
  if (first.startsWith('já está')) {
    await refresh($)
    return { text: first, isSwitched: false }
  }
  await afterSwitch($)
  return { text: first.replace(/^✓\s*/, ''), isSwitched: true }
}

async function pressSwitch($: EngineInterface, args: string[]) {
  await update($, lastAction, () => 'trocando…')
  const { text, isSwitched } = await runSwitch($, args)
  const last = text.split('\n').pop() ?? text
  await update($, lastAction, () => last)
  if (isSwitched) $.ui.toast(`Conta trocada: ${last.replace(/^.*✓\s*/, '')}`)
}

async function pressRefresh($: EngineInterface) {
  await update($, lastAction, () => 'atualizando…')
  const list = await refresh($)
  await update($, lastAction, () => (list ? 'atualizado' : 'o ccswap não respondeu'))
}

async function togglePane($: EngineInterface): Promise<string> {
  const panes = await $.ui.panes()
  if (panes.find(p => p.id === PANE_ID)?.isShown) {
    await $.ui.close({ id: PANE_ID })
    return 'Painel fechado.'
  }
  const opened = await $.ui.open({ id: PANE_ID, title: PANE_TITLE })
  return opened.isPlaced ? 'Painel aberto.' : `O painel não coube agora (${opened.reason}).`
}

function toUsage(limits: readonly SessionRateLimit[]): Usage | null {
  const pick = (kind: string): Window | undefined => {
    const limit = limits.find(l => l.kind === kind)
    return limit && { pct: limit.percentUsed, resets_at: limit.resetsAt ?? null }
  }
  const usage: Usage = { '5h': pick('five_hour'), '7d': pick('seven_day') }
  return usage['5h'] || usage['7d'] ? usage : null
}

function worstWindow(usage: Usage | null): Window | null {
  if (!usage) return null
  const [a, b] = [usage['5h'], usage['7d']]
  if (!a || !b) return a ?? b ?? null
  return b.pct > a.pct ? b : a
}

function worstPct(usage: Usage | null): number | null {
  return worstWindow(usage)?.pct ?? null
}

// A sessão só conhece os limites da conta que ela usa; vale como leitura da
// ativa quando a API não respondeu.
function shownUsage(account: ShownAccount, live: Usage | null): { usage: Usage | null; source: string | null } {
  if (account.active && live && account.usageSource !== 'api') return { usage: live, source: 'session' }
  return { usage: account.usage, source: account.usageSource }
}

function formatIn(resetsAt: string | null | undefined, now: number): string | null {
  if (!resetsAt) return null
  const ms = Date.parse(resetsAt) - now
  if (!Number.isFinite(ms)) return null
  if (ms <= 0) return 'agora'
  const minutes = Math.ceil(ms / 60_000)
  if (minutes < 60) return `${minutes}min`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h${String(minutes % 60).padStart(2, '0')}`
  return `${Math.floor(hours / 24)}d${hours % 24}h`
}

function formatUsage(usage: Usage, now: number): string {
  return WINDOWS.filter(k => usage[k])
    .map(k => {
      const w = usage[k]!
      const resetIn = w.pct >= WARNING_PCT ? formatIn(w.resets_at, now) : null
      return `${k} ${w.pct.toFixed(0)}%${resetIn ? ` ↻${resetIn}` : ''}`
    })
    .join(' · ')
}

function barCells(pct: number, cells: number): { full: string; empty: string } {
  const filled = Math.min(cells, Math.max(0, Math.round((pct / 100) * cells)))
  return { full: '▰'.repeat(filled), empty: '▱'.repeat(cells - filled) }
}

function colorFor(pct: number | null): string {
  if (pct === null) return 'inactive'
  if (pct >= cfg.threshold) return 'error'
  return pct >= WARNING_PCT ? 'warning' : 'success'
}

const SOURCE_NOTE: Record<string, string> = { cache: ' (última leitura)', session: ' (desta sessão)' }
const SOURCE_MARK: Record<string, string> = { cache: ' ~', session: ' •' }

function describe(list: ShownAccount[], live: Usage | null, now: number): string {
  return list
    .map(a => {
      const name = a.alias ? `${a.email} [${a.alias}]` : a.email
      const { usage, source } = shownUsage(a, live)
      const text = usage
        ? `${formatUsage(usage, now)}${SOURCE_NOTE[source ?? ''] ?? ''}`
        : `uso indisponível (${a.error ?? '?'})`
      return `${a.active ? '→' : ' '} ${a.slot}. ${name} — ${text}`
    })
    .join('\n')
}

function shortName(a: ShownAccount): string {
  return a.alias || a.email.split('@')[0] || a.email
}

// A linha de status só aceita texto: cada conta com a pior janela, para as
// contas caberem lado a lado.
function statusLine(
  list: ShownAccount[],
  live: Usage | null,
  now: number,
  error: string | null,
  profile: string | null,
): string {
  const parts = list.map(a => {
    const { usage, source } = shownUsage(a, live)
    const worst = worstWindow(usage)
    if (!worst) return `${a.active ? '→ ' : ''}${shortName(a)} sem leitura`
    const { full, empty } = barCells(worst.pct, STATUS_BAR_CELLS)
    const resetIn = worst.pct >= WARNING_PCT ? formatIn(worst.resets_at, now) : null
    return `${a.active ? '→ ' : ''}${shortName(a)} ${full}${empty} ${worst.pct.toFixed(0)}%${resetIn ? ` ↻${resetIn}` : ''}${SOURCE_MARK[source ?? ''] ?? ''}`
  })
  // O Claude Code já desenha o nome do mod na frente da linha.
  return `${profile ? `[${profile}] ` : ''}${parts.join('  ')}${error ? '  ⚠ ccswap não respondeu' : ''}`
}

async function syncStatus($: EngineInterface) {
  const list = await read($, accounts)
  const error = await read($, lastError)
  if (await read($, isHidden)) return $.ui.status(undefined)
  if (!list || list.length === 0) return $.ui.status(error ?? undefined)
  const profile = (await read($, profiles))?.active ?? null
  $.ui.status(statusLine(list, await read($, liveUsage), await $.clock.now(), error, profile))
}

const RELOAD_HINT =
  'Esta sessão continua na conta antiga até a próxima renovação do token; para valer agora, saia e rode `claude -c`.'

export const register: Register = (on, options) => {
  cfg.bin = String(options.ccswapPath ?? 'ccswap')
  cfg.autoSwitch = options.autoSwitch === true
  cfg.threshold = Number(options.threshold ?? 95)
  cfg.refreshMs = Math.max(1, Number(options.refreshMinutes ?? 5)) * 60_000
  const paneAtStart = options.paneAtStart === true
  const { autoSwitch, threshold, refreshMs } = cfg

  let lastAutoCheck = 0

  on('session.start', async ($, e, next) => {
    void refresh($, false).then(() => refresh($))
    $.clock.every(refreshMs, () => void refresh($))
    $.clock.every(ACTIVE_CHECK_EVERY_MS, () => void refresh($, false))

    try {
      await $.command.register({
        name: 'conta',
        description: 'Contas do ccswap: lista, painel, troca (próxima, melhor, slot, apelido ou e-mail)',
        argumentHint: '[painel|proxima|melhor|auto|perfil [nome|off]|<slot>|ocultar|mostrar]',
        immediate: true,
      })
    } catch (err) {
      $.ui.log(`/conta não registrado: ${String(err)}`)
    }
    if (paneAtStart) void $.ui.open({ id: PANE_ID, title: PANE_TITLE })
    return next(e)
  })

  on('prompt.autocomplete', async ($, e, next) => {
    const result = await next(e)
    const profilePrefix = '/conta perfil '
    if (e.text.startsWith(profilePrefix) && e.start === profilePrefix.length) {
      const known = await read($, profiles)
      const typed = e.token.toLowerCase()
      const options = [
        ...Object.entries(known?.profiles ?? {}).map(([name, slots]) => ({
          text: name,
          description: `contas ${slots.join(', ')}${known?.active === name ? ' (ativo)' : ''}`,
        })),
        { text: 'off', description: 'volta a usar todas as contas' },
      ].filter(o => o.text.toLowerCase().startsWith(typed) && o.text.toLowerCase() !== typed)
      return { suggestions: [...result.suggestions, ...options] }
    }
    const prefix = '/conta '
    if (!e.text.startsWith(prefix) || e.start !== prefix.length) return result

    const list = (await read($, accounts)) ?? []
    const typed = e.token.toLowerCase()
    const options = [
      ...SUBCOMMANDS,
      ...list.map(a => ({
        text: a.alias || String(a.slot),
        description: `${a.email}${a.active ? ' (ativa)' : ''}`,
      })),
    ].filter(o => o.text.toLowerCase().startsWith(typed) && o.text.toLowerCase() !== typed)
    return { suggestions: [...result.suggestions, ...options] }
  })

  on('command.run', { command: 'conta' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()

    if (arg === 'ocultar' || arg === 'mostrar') {
      await update($, isHidden, () => arg === 'ocultar')
      await syncStatus($)
      return {}
    }

    if (arg === 'painel' || arg === 'panel') return { text: await togglePane($) }

    if (arg === 'perfil' || arg.startsWith('perfil ')) {
      const name = e.args.trim().slice('perfil'.length).trim()
      if (!name) {
        const r = await ccswap($, ['profile'])
        return { text: r.exitCode === 0 ? r.stdout : r.stderr || `ccswap saiu com ${r.exitCode}` }
      }
      const r = await ccswap($, name.toLowerCase() === 'off' ? ['profile', 'off'] : ['profile', 'use', name])
      const isSwitched = r.stdout.includes('agora em')
      if (isSwitched) await afterSwitch($)
      else await refresh($, false)
      if (r.exitCode !== 0) return { text: r.stderr || `ccswap saiu com ${r.exitCode}` }
      return { text: isSwitched ? `${r.stdout}\n${RELOAD_HINT}` : r.stdout }
    }

    if (arg === '' || arg === 'lista' || arg === 'list') {
      const list = await refresh($)
      const shown = list ?? (await read($, accounts))
      if (!shown) return { text: `ccswap: ${await read($, lastError)}` }
      const text = describe(shown, await read($, liveUsage), await $.clock.now())
      return { text: list ? text : `${text}\n(ccswap falhou agora: ${await read($, lastError)})` }
    }

    const args = arg === 'auto'
      ? ['auto', '--once', '--threshold', String(threshold)]
      : arg === 'proxima' || arg === 'próxima' || arg === 'next'
        ? ['switch']
        : arg === 'melhor' || arg === 'best'
          ? ['switch', '--best']
          : ['switch', e.args.trim()]

    const { text, isSwitched } = await runSwitch($, args)
    if (isSwitched && args[0] === 'switch') $.ui.toast(text)
    return { text: isSwitched ? `${text}\n${RELOAD_HINT}` : text }
  })

  // Cada resposta traz os limites da conta em uso: alimentam o mod sem gastar
  // a API de uso e decidem a troca automática (o ccswap tem cooldown próprio).
  on('session.measure', async ($, e, next) => {
    const result = await next(e)
    if (!result.changed.includes('rateLimits')) return result

    const usage = toUsage(e.rateLimits)
    await update($, liveUsage, () => usage)
    await syncStatus($)

    const worst = worstPct(usage) ?? 0
    if (!autoSwitch || worst < threshold) return result
    const now = await $.clock.now()
    if (now - lastAutoCheck < AUTO_CHECK_EVERY_MS) return result
    lastAutoCheck = now

    void ccswap($, ['auto', '--once', '--threshold', String(threshold)]).then(async r => {
      if (r.exitCode === 0) {
        $.ui.toast(`Conta trocada: ${r.stdout.split('\n').pop()?.replace(/^.*✓\s*/, '') ?? ''}`)
        $.ui.log(RELOAD_HINT)
        await afterSwitch($)
      } else {
        if (r.exitCode === 3) $.ui.toast(`Conta em ${worst.toFixed(0)}% e nenhuma outra com folga`)
        await refresh($)
      }
    })
    return result
  })

  on('ui.render', { component: 'Pane', requestId: PANE_ID }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const list = await read($, accounts)
    const error = await read($, lastError)
    const live = await read($, liveUsage)
    const action = await read($, lastAction)
    const profile = (await read($, profiles))?.active ?? null
    const now = await $.clock.now()
    const cells = e.props.bodyColumns >= 30 ? 10 : 8

    if (!list || list.length === 0) {
      return (
        <Box flexDirection="column">
          <Text dimColor>{error ? `ccswap: ${error}` : 'Lendo as contas…'}</Text>
          <Button key="refresh" label="atualizar" hotkey="r" onPress={() => void pressRefresh($)} />
        </Box>
      )
    }

    return (
      <Box flexDirection="column" rowGap="sm">
        {profile ? <Text dimColor>perfil {profile}</Text> : null}
        {list.map(a => {
          const { usage, source } = shownUsage(a, live)
          return (
            <Box key={`slot-${a.slot}`} flexDirection="column">
              <Box flexDirection="row" columnGap="md">
                <Text bold={a.active} dimColor={!a.active}>
                  {a.active ? '→ ' : '  '}
                  {shortName(a)}
                  {source === 'cache' ? <Text dimColor> ~</Text> : source === 'session' ? <Text dimColor> •</Text> : null}
                </Text>
                {a.active ? null : (
                  <Button
                    key={`use-${a.slot}`}
                    label="usar"
                    hotkey={a.slot < 10 ? String(a.slot) : undefined}
                    variant="primary"
                    onPress={() => void pressSwitch($, ['switch', String(a.slot)])}
                  />
                )}
              </Box>
              {usage ? (
                WINDOWS.filter(k => usage[k]).map(k => {
                  const w = usage[k]!
                  const { full, empty } = barCells(w.pct, cells)
                  const resetIn = w.pct >= WARNING_PCT ? formatIn(w.resets_at, now) : null
                  return (
                    <Text key={`${a.slot}-${k}`}>
                      {'  '}{k} <Text color={colorFor(w.pct)}>{full}</Text><Text dimColor>{empty}</Text>{' '}
                      <Text color={colorFor(w.pct)}>{w.pct.toFixed(0).padStart(3)}%</Text>
                      {resetIn ? <Text dimColor> ↻{resetIn}</Text> : null}
                    </Text>
                  )
                })
              ) : (
                <Text dimColor>  sem leitura{a.error ? ` (${a.error})` : ''}</Text>
              )}
            </Box>
          )
        })}
        <Box flexDirection="row" flexWrap="wrap" columnGap="sm">
          <Button key="next" label="próxima" hotkey="p" onPress={() => void pressSwitch($, ['switch'])} />
          <Button key="best" label="melhor" hotkey="m" onPress={() => void pressSwitch($, ['switch', '--best'])} />
          <Button
            key="auto"
            label="auto"
            hotkey="a"
            onPress={() => void pressSwitch($, ['auto', '--once', '--threshold', String(threshold)])}
          />
          <Button key="refresh" label="atualizar" hotkey="r" dimColor onPress={() => void pressRefresh($)} />
        </Box>
        {action || error ? <Text dimColor>{error ? `⚠ ${error}` : action}</Text> : null}
      </Box>
    )
  })
}

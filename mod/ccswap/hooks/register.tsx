import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Account } from '../types'

// A troca em si (locks do proper-lockfile, slots 0600, refresh de token)
// continua no ccswap.py: a API de arquivos dos mods não tem mkdir, rmdir nem
// chmod. O mod só chama o ccswap e cuida da faixa, do /conta e do gatilho.

const accounts = atom({ plugin: 'ccswap', key: 'accounts' } as const, null)
const lastError = atom({ plugin: 'ccswap', key: 'lastError' } as const, null)
const isHidden = atom({ plugin: 'ccswap', key: 'isHidden' } as const, false)

const AUTO_CHECK_EVERY_MS = 60_000

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

async function refresh($: EngineInterface, withUsage = true): Promise<Account[] | null> {
  const r = await ccswap($, withUsage ? ['list', '--json'] : ['list', '--json', '--no-usage'])
  if (r.exitCode !== 0) {
    await update($, lastError, () => r.stderr || `ccswap saiu com ${r.exitCode}`)
    return null
  }
  try {
    const list = JSON.parse(r.stdout) as Account[]
    await update($, accounts, () => list)
    await update($, lastError, () => null)
    return list
  } catch {
    await update($, lastError, () => 'saída do ccswap não é JSON — atualize o ccswap.py (precisa de `list --json`)')
    return null
  }
}

function describe(list: Account[]): string {
  return list
    .map(a => {
      const name = a.alias ? `${a.email} [${a.alias}]` : a.email
      const u = a.usage
      const usage = u
        ? (['5h', '7d'] as const)
            .filter(k => u[k])
            .map(k => `${k} ${u[k]!.pct.toFixed(0)}%`)
            .join(' · ')
        : `uso indisponível (${a.error ?? '?'})`
      return `${a.active ? '→' : ' '} ${a.slot}. ${name} — ${usage}`
    })
    .join('\n')
}

const RELOAD_HINT =
  'Esta sessão continua na conta antiga até a próxima renovação do token; para valer agora, saia e rode `claude -c`.'


export const register: Register = (on, options) => {
  cfg.bin = String(options.ccswapPath ?? 'ccswap')
  cfg.autoSwitch = options.autoSwitch === true
  cfg.threshold = Number(options.threshold ?? 95)
  cfg.refreshMs = Math.max(1, Number(options.refreshMinutes ?? 5)) * 60_000
  const { autoSwitch, threshold, refreshMs } = cfg

  let lastAutoCheck = 0

  on('session.start', async ($, e, next) => {
    void refresh($, false).then(() => refresh($))
    $.clock.every(refreshMs, () => refresh($))

    try {
      await $.command.register({
        name: 'conta',
        description: 'Contas do ccswap: lista, troca (próxima, melhor, slot, apelido ou e-mail)',
        argumentHint: '[proxima|melhor|auto|<slot>|ocultar|mostrar]',
        immediate: true,
      })
    } catch (err) {
      $.ui.log(`/conta não registrado: ${String(err)}`)
    }
    return next(e)
  })

  on('command.run', { command: 'conta' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()

    if (arg === 'ocultar' || arg === 'mostrar') {
      await update($, isHidden, () => arg === 'ocultar')
      return {}
    }

    if (arg === '' || arg === 'lista' || arg === 'list') {
      const list = await refresh($)
      return { text: list ? describe(list) : `ccswap: ${await read($, lastError)}` }
    }

    if (arg === 'auto') {
      const r = await ccswap($, ['auto', '--once', '--threshold', String(threshold)])
      await refresh($)
      const out = [r.stdout, r.stderr].filter(Boolean).join('\n')
      return { text: r.exitCode === 0 ? `${out}\n${RELOAD_HINT}` : out || `nada a fazer (código ${r.exitCode})` }
    }

    const args = arg === 'proxima' || arg === 'próxima' || arg === 'next'
      ? ['switch']
      : arg === 'melhor' || arg === 'best'
        ? ['switch', '--best']
        : ['switch', e.args.trim()]

    const r = await ccswap($, args)
    await refresh($)
    if (r.exitCode !== 0) return { text: r.stderr || `ccswap saiu com ${r.exitCode}` }
    const first = r.stdout.split('\n')[0] ?? ''
    if (!first.startsWith('já está')) $.ui.toast(first.replace(/^✓\s*/, ''))
    return { text: `${first}\n${RELOAD_HINT}` }
  })

  // O fim de um turno traz os limites da última resposta da API: só chama o
  // ccswap (que tem cooldown e margem próprios) quando a conta passou do limite.
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (!autoSwitch) return result

    const now = await $.clock.now()
    if (now - lastAutoCheck < AUTO_CHECK_EVERY_MS) return result

    const { rateLimits } = await $.session.usage()
    const worst = Math.max(
      0,
      ...rateLimits.filter(l => l.kind === 'five_hour' || l.kind === 'seven_day').map(l => l.percentUsed),
    )
    if (worst < threshold) return result

    lastAutoCheck = now
    void ccswap($, ['auto', '--once', '--threshold', String(threshold)]).then(async r => {
      if (r.exitCode === 0) {
        $.ui.toast(`Conta trocada: ${r.stdout.split('\n').pop()?.replace(/^.*✓\s*/, '') ?? ''}`)
        $.ui.log(RELOAD_HINT)
      } else if (r.exitCode === 3) {
        $.ui.toast(`Conta em ${worst.toFixed(0)}% e nenhuma outra com folga`)
      }
      await refresh($)
    })
    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || (await read($, isHidden))) return next(e)

    const list = await read($, accounts)
    const error = await read($, lastError)
    const { Box, Text } = $.ui.resolve(e)

    if (!list || list.length === 0) {
      return error ? <Text dimColor>ccswap: {error}</Text> : next(e)
    }

    return (
      <Box flexDirection="row" flexWrap="wrap" columnGap="md">
        {list.map(a => {
          const u = a.usage
          const worst = u ? Math.max(u['5h']?.pct ?? 0, u['7d']?.pct ?? 0) : null
          const color = worst === null ? 'inactive' : worst >= threshold ? 'error' : worst >= 75 ? 'warning' : 'success'
          return (
            <Text key={`slot-${a.slot}`} dimColor={!a.active}>
              {a.active ? '→ ' : ''}
              {a.alias || a.email}{' '}
              <Text color={color}>
                {u
                  ? (['5h', '7d'] as const)
                      .filter(k => u[k])
                      .map(k => `${k} ${u[k]!.pct.toFixed(0)}%`)
                      .join(' · ')
                  : 'sem leitura'}
              </Text>
            </Text>
          )
        })}
      </Box>
    )
  })
}

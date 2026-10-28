import { expect, mock, test } from 'claude-code/testing'

const NOW = Date.parse('2026-10-20T12:00:00Z')

const LIST = JSON.stringify([
  { slot: 1, email: 'a@ex.com', alias: 'pessoal', active: true, usage: { '5h': { pct: 40 }, '7d': { pct: 12 } }, error: null },
  { slot: 2, email: 'b@ex.com', alias: '', active: false, usage: { '5h': { pct: 3 }, '7d': { pct: 80, resets_at: '2026-10-20T15:30:00Z' } }, error: null },
])

const LIST_429 = JSON.stringify([
  { slot: 1, email: 'a@ex.com', alias: 'pessoal', active: true, usage: null, error: 'HTTP 429' },
  { slot: 2, email: 'b@ex.com', alias: '', active: false, usage: null, error: 'HTTP 429' },
])

const PANE = {
  plugin: 'ccswap',
  component: 'Pane',
  requestId: 'ccswap',
  props: {
    title: 'Contas',
    isFocused: false,
    bodyColumns: 32,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 20 },
    view: {},
  },
} as const

const START = { cwd: '/', surface: 'terminal', isInteractive: true } as const

test('linha de status, painel com uso e reset, e o botão usar troca de conta', async ($, on) => {
  const calls: string[][] = []
  const status: (string | undefined)[] = []
  mock.clock(on, { now: NOW })
  on('command.register', () => ({ value: { command: 'conta' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.status', (_, e) => (status.push(e.text), { value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('process.run', (_, e) => {
    calls.push([...e.argv])
    const isSwitch = e.argv.includes('switch')
    return { value: { exitCode: 0, stdout: isSwitch ? '✓ agora em b@ex.com (slot 2)' : LIST, stderr: '' } }
  })

  await $.session.start(START)

  const listed = await $.command.run({ command: 'conta', args: '' })
  expect(JSON.stringify(listed)).toContain('7d 80% ↻3h30')
  expect(status.at(-1)).toBe('→ pessoal ▰▰▱▱▱ 40%  b ▰▰▰▰▱ 80% ↻3h30')

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ type: 'Text', text: /pessoal/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /↻3h30/ })).toBeTruthy()
    if (surface === 'terminal') await ui.press({ key: 'use-2' })
    await ui.unmount()
  }

  expect(calls.some(a => a.includes('switch') && a.includes('2'))).toBe(true)

  await $.command.run({ command: 'conta', args: 'ocultar' })
  expect(status.at(-1)).toBe(undefined)
})

test('com HTTP 429 mantém a última leitura e usa os limites da sessão', async ($, on) => {
  let stdout = LIST
  const status: (string | undefined)[] = []
  mock.clock(on, { now: NOW })
  on('command.register', () => ({ value: { command: 'conta' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.status', (_, e) => (status.push(e.text), { value: undefined }))
  on('process.run', () => ({ value: { exitCode: 0, stdout, stderr: '' } }))
  on('session.measure', (_, e) => ({ changed: e.changed }))

  await $.session.start(START)
  await $.command.run({ command: 'conta', args: '' })

  stdout = LIST_429
  const listed = await $.command.run({ command: 'conta', args: '' })
  expect(JSON.stringify(listed)).toContain('5h 40%')
  expect(JSON.stringify(listed)).toContain('última leitura')

  await $.session.measure({
    context: { window: 200_000 } as never,
    rateLimits: [{ kind: 'five_hour', percentUsed: 62 }, { kind: 'seven_day', percentUsed: 20 }],
    changed: ['rateLimits'],
  })
  expect(status.at(-1)).toContain('pessoal ▰▰▰▱▱ 62% •')
  expect(status.at(-1)).toContain('b ▰▰▰▰▱ 80% ↻3h30 ~')

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ type: 'Text', text: /62%/ })).toBeTruthy()
    await ui.unmount()
  }
})

test('perfil ativo aparece na linha de status e /conta perfil troca de perfil', async ($, on) => {
  const calls: string[][] = []
  const status: (string | undefined)[] = []
  let active: string | null = 'empresa'
  mock.clock(on, { now: NOW })
  on('command.register', () => ({ value: { command: 'conta' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.status', (_, e) => (status.push(e.text), { value: undefined }))
  on('process.run', (_, e) => {
    calls.push([...e.argv])
    if (e.argv.includes('--json') && e.argv.includes('profile')) {
      const stdout = JSON.stringify({ active, profiles: { empresa: [1, 2], empresa2: [3] } })
      return { value: { exitCode: 0, stdout, stderr: '' } }
    }
    if (e.argv.includes('use')) {
      active = 'empresa2'
      return { value: { exitCode: 0, stdout: '✓ perfil empresa2 ativo\n✓ agora em c@ex.com (slot 3) — perfil empresa2', stderr: '' } }
    }
    return { value: { exitCode: 0, stdout: LIST, stderr: '' } }
  })

  await $.session.start(START)
  await $.command.run({ command: 'conta', args: '' })
  expect(status.at(-1)).toBe('[empresa] → pessoal ▰▰▱▱▱ 40%  b ▰▰▰▰▱ 80% ↻3h30')

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /perfil empresa/ })).toBeTruthy()
  await ui.unmount()

  const done = await $.command.run({ command: 'conta', args: 'perfil empresa2' })
  expect(JSON.stringify(done)).toContain('agora em c@ex.com')
  expect(calls.some(a => a.includes('profile') && a.includes('use') && a.includes('empresa2'))).toBe(true)
  expect(status.at(-1)).toMatch(/^\[empresa2\] /)
})

test('leitura antiga do cache do ccswap aparece como última leitura', async ($, on) => {
  const stale = JSON.stringify([
    { slot: 1, email: 'a@ex.com', alias: 'pessoal', active: true, usage: { '5h': { pct: 40 } }, usage_age: 900, error: 'HTTP 429' },
  ])
  mock.clock(on, { now: NOW })
  on('command.register', () => ({ value: { command: 'conta' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.status', () => ({ value: undefined }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: stale, stderr: '' } }))

  await $.session.start(START)
  const listed = await $.command.run({ command: 'conta', args: '' })
  expect(JSON.stringify(listed)).toContain('5h 40% (última leitura)')

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /~/ })).toBeTruthy()
  await ui.unmount()
})

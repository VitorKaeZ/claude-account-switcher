import { expect, test } from 'claude-code/testing'

const LIST = JSON.stringify([
  { slot: 1, email: 'a@ex.com', alias: 'pessoal', active: true, usage: { '5h': { pct: 40 }, '7d': { pct: 12 } }, error: null },
  { slot: 2, email: 'b@ex.com', alias: '', active: false, usage: { '5h': { pct: 3 }, '7d': { pct: 55 } }, error: null },
])

const BAND = {
  plugin: 'ccswap',
  component: 'AbovePrompt',
  props: { hasSurvey: false },
} as const

test('/conta lista as contas e a faixa mostra o uso', async ($, on) => {
  const calls: string[][] = []
  on('command.register', () => ({ value: { command: 'conta' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('process.run', (_, e) => {
    calls.push([...e.argv])
    const isSwitch = e.argv.includes('switch')
    return { value: { exitCode: 0, stdout: isSwitch ? '✓ agora em b@ex.com (slot 2)' : LIST, stderr: '' } }
  })
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const listed = await $.command.run({ command: 'conta', args: '' })
  expect(JSON.stringify(listed)).toContain('pessoal')
  expect(JSON.stringify(listed)).toContain('7d 55%')

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect((await ui.find({ type: 'Text', text: /pessoal/ }))).toBeTruthy()
    await ui.unmount()
  }

  const switched = await $.command.run({ command: 'conta', args: 'melhor' })
  expect(JSON.stringify(switched)).toContain('agora em b@ex.com')
  expect(calls.some(a => a.includes('--best'))).toBe(true)
})

export type Window = { pct: number; resets_at?: string | null }

/** Uma linha de `ccswap list --json`. */
export type Account = {
  slot: number
  email: string
  alias: string
  active: boolean
  usage: { '5h'?: Window; '7d'?: Window } | null
  error: string | null
}

declare module 'claude-code' {
  interface PluginState {
    ccswap: {
      accounts: Account[] | null
      lastError: string | null
      isHidden: boolean
    }
  }
}

export type Window = { pct: number; resets_at?: string | null }

export type Usage = { '5h'?: Window; '7d'?: Window }

/** Uma linha de `ccswap list --json`. */
export type Account = {
  slot: number
  email: string
  alias: string
  active: boolean
  usage: Usage | null
  error: string | null
}

/**
 * Uma conta como o mod mostra: o uso pode vir da última leitura boa da API
 * (`cache`, quando a de agora falhou, ex. HTTP 429).
 */
export type ShownAccount = Account & { usageSource: 'api' | 'cache' | null }

declare module 'claude-code' {
  interface PluginState {
    ccswap: {
      accounts: ShownAccount[] | null
      /** Limites que as respostas desta sessão trouxeram (só da conta em uso). */
      liveUsage: Usage | null
      lastError: string | null
      /** A linha de status escondida por `/conta ocultar`. */
      isHidden: boolean
      /** O resultado do último botão do painel. */
      lastAction: string | null
    }
  }
}

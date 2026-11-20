/*
 * @taxueseek/argo-dsh — 会话级联网搜索模式运行时（三层物理守卫 + 抗压缩持久态）
 *
 * 三种模式：
 *  - auto（默认）：智能联网，按需调用搜索与抓取工具；
 *  - required：强制联网，本轮结束前必须完成至少一次检索或抓取，否则纠偏一次，二次中止；
 *  - off：关闭联网，通过 tools.restrict + tools.guard + step-1 声明三层物理隔离。
 *
 * 模式归属 sessionId 而非单个 Agent 实例，避免 compact/clear 重建 Agent 后静默回退。
 */

export const SEARCH_MODES = ['auto', 'required', 'off']

export const MANAGED_WEB_TOOLS = ['web_search', 'web_fetch', 'argo_search', 'argo_fetch']

export const WEB_DISABLED_DENIAL =
  'Web search is switched OFF for this session, so this tool call was denied. Answer from existing context and state what could not be verified.'

export const POLICY_TEXT = {
  auto: 'Web search is available when needed. Call web_search/argo_search (or web_fetch/argo_fetch for a URL) when current or verifiable external information is required.',
  required: 'Web search is REQUIRED for this turn. Complete at least one web_search/argo_search or web_fetch/argo_fetch call before finalizing. If access fails, state clearly what could not be verified.',
  off: 'Web search is switched OFF for this session. Do not call web search or fetch tools; answer from existing context and state what could not be verified.',
  correction: 'Web search is required for this turn. Complete a search or fetch tool call before finalizing.',
}

export function normalizeSearchMode(mode) {
  return SEARCH_MODES.includes(mode) ? mode : 'auto'
}

export function nextSearchMode(mode) {
  const idx = SEARCH_MODES.indexOf(normalizeSearchMode(mode))
  return SEARCH_MODES[(idx + 1) % SEARCH_MODES.length]
}

export function webResearchCompleted(state) {
  return Boolean(state && (state.webSearchCompleted || state.webFetchCompleted))
}

export function shouldAnnounceMode(mode, announced) {
  if (mode === 'required') return true
  return announced !== mode
}

export function searchModeAnnouncement(state, step, messages, announced) {
  if (!state || step !== 1) return undefined
  const mode = state.disabled ? 'off' : state.required ? 'required' : 'auto'
  if (!shouldAnnounceMode(mode, announced)) return undefined
  const factory = mode === 'off' ? messages.disabled : mode === 'required' ? messages.required : messages.auto
  const message = typeof factory === 'function' ? factory() : undefined
  if (message === undefined) return undefined
  return { mode, message }
}

export class SearchModeRuntime {
  constructor(searchAvailable = () => true) {
    this._modes = new Map()
    this._turns = new Map()
    this._announced = new Map()
    this._searchAvailable = searchAvailable
  }

  getMode(sessionId) {
    if (!sessionId) return 'auto'
    return this._modes.get(sessionId) ?? 'auto'
  }

  setMode(sessionId, mode) {
    if (!sessionId) return 'auto'
    const normalized = normalizeSearchMode(mode)
    if (normalized === 'auto') this._modes.delete(sessionId)
    else this._modes.set(sessionId, normalized)
    return normalized
  }

  isOff(sessionId) {
    return this.getMode(sessionId) === 'off'
  }

  available() {
    try {
      return this._searchAvailable() === true
    } catch {
      return true
    }
  }

  announcedMode(sessionId) {
    return this._announced.get(sessionId)
  }

  markAnnounced(sessionId, mode) {
    if (sessionId) this._announced.set(sessionId, mode)
  }

  resetAnnouncement(sessionId) {
    if (sessionId) this._announced.delete(sessionId)
  }

  beginTurn(sessionId, turn) {
    const existing = this._turns.get(sessionId)
    if (existing && existing.turn === turn) return existing
    const mode = this.getMode(sessionId)
    const state = {
      turn,
      required: mode === 'required',
      disabled: mode === 'off',
      webSearchCompleted: false,
      webSearchSucceeded: false,
      webFetchCompleted: false,
      webFetchSucceeded: false,
      correctionCount: 0,
    }
    this._turns.set(sessionId, state)
    return state
  }

  markSearchResult(sessionId, succeeded) {
    const state = this._turns.get(sessionId)
    if (!state) return
    state.webSearchCompleted = true
    state.webSearchSucceeded = state.webSearchSucceeded || Boolean(succeeded)
  }

  markFetchResult(sessionId, succeeded) {
    const state = this._turns.get(sessionId)
    if (!state) return
    state.webFetchCompleted = true
    state.webFetchSucceeded = state.webFetchSucceeded || Boolean(succeeded)
  }

  getTurn(sessionId) {
    return this._turns.get(sessionId)
  }

  clearTurnState(sessionId) {
    this._turns.delete(sessionId)
  }

  clear(sessionId) {
    this._modes.delete(sessionId)
    this._turns.delete(sessionId)
    this._announced.delete(sessionId)
  }

  view(sessionId) {
    return { mode: this.getMode(sessionId), available: this.available() }
  }
}

export function createSearchModeMessages(createUserMessage) {
  const wrap = createUserMessage || ((input) => ({ role: 'user', ...input }))
  const build = (text, form, extra = {}) => wrap({
    content: [{ type: 'text', text }],
    source: { kind: 'plugin:argo-dsh', plugin: 'argo-dsh', form, ...extra },
  })
  return {
    auto: () => build(POLICY_TEXT.auto, 'snapshot', { sections: [{ name: 'argo-search-mode', text: POLICY_TEXT.auto }] }),
    required: () => build(POLICY_TEXT.required, 'snapshot', { sections: [{ name: 'argo-search-mode', text: POLICY_TEXT.required }] }),
    disabled: () => build(POLICY_TEXT.off, 'notice', { summary: 'Web Search disabled' }),
    correction: () => build(POLICY_TEXT.correction, 'notice', { summary: 'Web Search required' }),
  }
}

const SEARCH_TOOL_SET = new Set(['web_search', 'argo_search'])
const FETCH_TOOL_SET = new Set(['web_fetch', 'argo_fetch'])

export function installSearchModeRuntime(ctx, runtime, options = {}) {
  const messages = options.messages || createSearchModeMessages(options.createUserMessage)
  const disposers = []

  if (typeof ctx?.on === 'function') {
    const offCreated = ctx.on('agent/created', (payload) => {
      const agent = payload?.agent
      if (!agent?.id || !agent?.ctx) return undefined
      if (payload.source === 'clear' || payload.source === 'compact') {
        runtime.resetAnnouncement(agent.id)
      }
      const wire = () => {
        let liftRestriction
        const syncRestriction = () => {
          const off = runtime.isOff(agent.id)
          if (off && !liftRestriction && typeof agent.ctx.tools?.restrict === 'function') {
            try {
              liftRestriction = agent.ctx.tools.restrict({ deny: [...MANAGED_WEB_TOOLS] })
            } catch {
              liftRestriction = undefined
            }
          } else if (!off && liftRestriction) {
            try { liftRestriction() } catch { /* ignore */ }
            liftRestriction = undefined
          }
        }
        const stopGuard = typeof agent.ctx.tools?.guard === 'function'
          ? agent.ctx.tools.guard((exec) => (
            runtime.isOff(agent.id) && MANAGED_WEB_TOOLS.includes(exec?.name) ? WEB_DISABLED_DENIAL : undefined
          ))
          : undefined
        const stopPreStep = typeof agent.ctx.on === 'function'
          ? agent.ctx.on('agent/pre-step', async (stepPayload, next) => {
            const decision = await next()
            if (!decision || decision.kind === 'reject' || stepPayload?.signal?.aborted) return decision
            const state = runtime.beginTurn(agent.id, stepPayload.turn)
            syncRestriction()
            const announcement = searchModeAnnouncement(state, stepPayload.step, messages, runtime.announcedMode(agent.id))
            if (!announcement) return decision
            runtime.markAnnounced(agent.id, announcement.mode)
            return { kind: 'enter', messages: [announcement.message, ...(decision.messages ?? [])] }
          })
          : undefined
        const stopResult = typeof agent.ctx.on === 'function'
          ? agent.ctx.on('tools/result', (exec, result) => {
            const sid = exec?.agent?.id || agent.id
            const ok = result?.isError === false
            if (SEARCH_TOOL_SET.has(exec?.name)) runtime.markSearchResult(sid, ok)
            else if (FETCH_TOOL_SET.has(exec?.name)) runtime.markFetchResult(sid, ok)
          })
          : undefined
        const stopStopping = typeof agent.ctx.on === 'function'
          ? agent.ctx.on('agent/turn-stopping', (stopPayload) => {
            if (stopPayload?.signal?.aborted) return
            const state = runtime.getTurn(agent.id)
            if (!state?.required || webResearchCompleted(state)) return
            if (state.correctionCount === 0) {
              state.correctionCount += 1
              agent.steer?.(messages.correction())
              return
            }
            agent.cancel?.({ kind: 'hook', reason: 'required web research was not completed' })
          })
          : undefined
        return () => {
          stopPreStep?.()
          stopResult?.()
          stopStopping?.()
          stopGuard?.()
          if (liftRestriction) {
            try { liftRestriction() } catch { /* ignore */ }
            liftRestriction = undefined
          }
          runtime.clearTurnState(agent.id)
        }
      }
      return typeof agent.ctx.effect === 'function' ? agent.ctx.effect(wire, 'argo-dsh: search-mode') : wire()
    })
    if (typeof offCreated === 'function') disposers.push(offCreated)
  }

  const commands = ctx?.get?.('commands') ?? ctx?.commands
  if (commands && typeof commands.register === 'function') {
    const offCmd = commands.register({
      name: 'search',
      description: '切换当前会话的联网搜索模式（自动 / 强制 / 关闭）',
      handler: (invocation) => {
        const sid = String(invocation?.agent?.id ?? '')
        if (!sid) return { kind: 'error', text: 'no session' }
        const next = nextSearchMode(runtime.getMode(sid))
        runtime.setMode(sid, next)
        const text = next === 'required'
          ? '联网搜索已切换为「始终联网」（每轮必须检索或抓取）'
          : next === 'off'
            ? '联网搜索已切换为「关闭联网」（物理屏蔽联网工具）'
            : '联网搜索已恢复为「智能联网」（按需检索）'
        return { kind: 'success', text }
      },
    })
    if (typeof offCmd === 'function') disposers.push(offCmd)
  }

  const dispose = () => {
    for (const fn of disposers) {
      try { fn() } catch { /* ignore */ }
    }
  }
  if (typeof ctx?.effect === 'function') ctx.effect(() => dispose)
  return dispose
}

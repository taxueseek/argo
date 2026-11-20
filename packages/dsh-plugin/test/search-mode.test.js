import test from 'node:test'
import assert from 'node:assert/strict'
import {
  MANAGED_WEB_TOOLS,
  SearchModeRuntime,
  WEB_DISABLED_DENIAL,
  installSearchModeRuntime,
  nextSearchMode,
  shouldAnnounceMode,
} from '../dsh/search-mode.js'

function createMockHost() {
  const listeners = new Map()
  const commands = new Map()
  return {
    on(event, fn) {
      const list = listeners.get(event) || []
      list.push(fn)
      listeners.set(event, list)
      return () => { listeners.set(event, (listeners.get(event) || []).filter((f) => f !== fn)) }
    },
    emit(event, ...args) {
      return (listeners.get(event) || []).map((fn) => fn(...args))
    },
    effect(fn) {
      const off = fn()
      return typeof off === 'function' ? off : () => {}
    },
    commands: {
      register(def) {
        commands.set(def.name, def)
        return () => { commands.delete(def.name) }
      },
    },
    _commands: commands,
  }
}

function createMockAgent(id) {
  const listeners = new Map()
  const restrictions = []
  const guards = []
  const steered = []
  const cancelled = []
  let disposeEffect = null
  const agent = {
    id,
    steered,
    cancelled,
    restrictions,
    guards,
    steer(msg) { steered.push(msg) },
    cancel(cause) { cancelled.push(cause) },
    ctx: {
      on(event, fn) {
        const list = listeners.get(event) || []
        list.push(fn)
        listeners.set(event, list)
        return () => { listeners.set(event, (listeners.get(event) || []).filter((f) => f !== fn)) }
      },
      effect(fn) {
        disposeEffect = fn()
        return () => { disposeEffect?.() }
      },
      tools: {
        restrict(filter) {
          restrictions.push(filter)
          return () => { restrictions.push({ lifted: true }) }
        },
        guard(fn) {
          guards.push(fn)
          return () => {}
        },
      },
    },
    async runPreStep(turn, step) {
      const list = listeners.get('agent/pre-step') || []
      let decision = { kind: 'enter', messages: [{ role: 'user', text: 'hi' }] }
      for (const fn of list) {
        decision = await fn({ turn, step }, async () => decision)
      }
      return decision
    },
    emitResult(name, isError = false) {
      for (const fn of listeners.get('tools/result') || []) fn({ name, agent }, { isError })
    },
    emitStopping(turn) {
      for (const fn of listeners.get('agent/turn-stopping') || []) fn({ turn })
    },
    dispose() { disposeEffect?.() },
  }
  return agent
}

test('SearchModeRuntime cycles modes and survives compaction while resetting announcements', () => {
  const rt = new SearchModeRuntime()
  assert.equal(rt.getMode('s1'), 'auto')
  assert.equal(nextSearchMode('auto'), 'required')
  assert.equal(nextSearchMode('required'), 'off')
  assert.equal(nextSearchMode('off'), 'auto')

  rt.setMode('s1', 'off')
  rt.markAnnounced('s1', 'off')
  assert.equal(shouldAnnounceMode('off', rt.announcedMode('s1')), false)
  assert.equal(shouldAnnounceMode('required', 'required'), true)

  rt.clearTurnState('s1')
  assert.equal(rt.getMode('s1'), 'off', '清理单轮状态不得丢掉会话级模式')
  rt.resetAnnouncement('s1')
  assert.equal(shouldAnnounceMode('off', rt.announcedMode('s1')), true)
})

test('off mode enforces three-tier barrier and auto mode announces only once until compact', async () => {
  const host = createMockHost()
  const rt = new SearchModeRuntime()
  installSearchModeRuntime(host, rt)

  rt.setMode('s1', 'off')
  const a1 = createMockAgent('s1')
  host.emit('agent/created', { agent: a1, source: 'startup' })

  const d1 = await a1.runPreStep(1, 1)
  assert.equal(d1.messages.length, 2, '首轮应注入一次 off 声明')
  assert.deepEqual(a1.restrictions[0], { deny: MANAGED_WEB_TOOLS })
  assert.equal(a1.guards[0]({ name: 'argo_search' }), WEB_DISABLED_DENIAL)
  assert.equal(a1.guards[0]({ name: 'read_file' }), undefined)

  const d2 = await a1.runPreStep(2, 1)
  assert.equal(d2.messages.length, 1, '常驻 off 态第二轮不得重复注入声明')

  a1.dispose()
  const a2 = createMockAgent('s1')
  host.emit('agent/created', { agent: a2, source: 'compact' })
  assert.equal(rt.getMode('s1'), 'off', 'compact 后模式必须保留')
  const d3 = await a2.runPreStep(3, 1)
  assert.equal(d3.messages.length, 2, 'compact 重建上下文后应重新公告一次')
})

test('required mode steers once then cancels on second violation, and /search toggles host state', async () => {
  const host = createMockHost()
  const rt = new SearchModeRuntime()
  installSearchModeRuntime(host, rt)

  const cmd = host._commands.get('search')
  assert.ok(cmd, '应注册 /search 斜杠命令')
  const agent = createMockAgent('s2')
  host.emit('agent/created', { agent, source: 'startup' })

  const r1 = cmd.handler({ agent })
  assert.equal(r1.kind, 'success')
  assert.equal(rt.getMode('s2'), 'required')

  await agent.runPreStep(1, 1)
  agent.emitStopping(1)
  assert.equal(agent.steered.length, 1)
  assert.equal(agent.cancelled.length, 0)

  agent.emitStopping(1)
  assert.equal(agent.cancelled.length, 1, '二次未检索直接中止防死循环')

  await agent.runPreStep(2, 1)
  agent.emitResult('argo_search', false)
  agent.emitStopping(2)
  assert.equal(agent.steered.length, 1, '已完成检索不再纠偏')
})

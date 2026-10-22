import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { NATIVE_TOOLS } from '../dsh/native-tools.mjs'
import { ARGO_ENV_SPECS } from '../dsh/index.js'

/** 在最小沙箱里执行 lazy-CJS bundle，返回 ModuleLoader 收到的定义。 */
function loadBundle() {
  let definition
  const sandbox = {
    window: { __ModuleLoader__: { load: (value) => { definition = value } } },
    console,
  }
  sandbox.globalThis = sandbox
  vm.runInNewContext(readFileSync(new URL('../dsh/client.js', import.meta.url), 'utf8'), sandbox)
  return definition
}

function factory(definition) {
  const React = {
    createElement: () => null,
    useState: () => [undefined, () => {}],
    useEffect: () => {},
    useRef: () => ({ current: new Set() }),
  }
  return definition.factory((id) => {
    if (id === 'react') return React
    throw new Error(`unexpected require: ${id}`)
  })
}

/** 带状态的 React 替身：单次渲染口径，足够驱动 ArgoPanel 完整执行一遍。
 *  register 测试用无状态 factory（只要模块能加载）；渲染测试要真跑组件函数，
 *  所以 useState/useRef 按调用序记进 hooks，测试可预置 tab 再渲染。 */
function statefulReact() {
  const hooks = []
  let idx = 0
  const react = {
    createElement: (type, props, ...children) => ({ type, props: props || {}, children }),
    useState: (init) => {
      const i = idx++
      if (!(i in hooks)) hooks[i] = typeof init === 'function' ? init() : init
      return [hooks[i], (v) => { hooks[i] = typeof v === 'function' ? v(hooks[i]) : v }]
    },
    useEffect: () => {},
    useRef: (init) => {
      const i = idx++
      if (!(i in hooks)) hooks[i] = { current: init }
      return hooks[i]
    },
  }
  return { react, hooks, reset: () => { idx = 0 } }
}

test('client bundle: registers both settings seats for the wide-research namespace', () => {
  const definition = loadBundle()
  assert.equal(definition.id, '@taxueseek/argo-dsh')
  const mod = factory(definition)
  assert.equal(JSON.stringify(mod.inject), JSON.stringify(['slots', 'locale', 'configForms', 'remote', 'remote.credentials']))
  assert.equal(typeof mod.ArgoPanel, 'function')

  const registered = []
  let served = 0
  const form = {
    getSnapshot: () => ({ status: 'ready', value: {}, writable: true }),
    subscribe: () => () => {},
    set: async () => true,
    mutate: async () => true,
  }
  const ctx = {
    configForms: {
      get: (namespace) => {
        assert.equal(namespace, 'wide-research')
        return form
      },
      whileServed: (namespaces, register) => {
        served += 1
        assert.equal(JSON.stringify(namespaces), JSON.stringify(['wide-research']))
        return register(new Set(namespaces))
      },
    },
    slots: {
      inject: (_name, register) => register(),
      register: (options, component) => {
        registered.push({ options, component })
        return () => {}
      },
    },
    locale: { getSnapshot: () => ({ active: 'zh' }), subscribe: () => () => {} },
    remote: { credentials: {}, $on: () => () => {} },
    effect: (callback) => {
      const off = callback()
      if (typeof off === 'function') off()
    },
  }

  mod.apply(ctx)

  assert.equal(served, 1)
  assert.deepEqual(
    registered.map((entry) => entry.options.name).sort(),
    ['plugins.item', 'settings.section'],
  )
  for (const entry of registered) {
    assert.equal(entry.options.id, 'argo')
    assert.equal(entry.options.order, entry.options.name === 'settings.section' ? 26 : 50)
    assert.equal(typeof entry.component, 'function')
    const injected = entry.options.inject()
    assert.equal(injected.hooks.argoForm, form)
    assert.equal(typeof injected.set, 'function')
    assert.equal(typeof injected.mutate, 'function')
  }
})

test('client TOOLS mirror native-tools.mjs (drift gate)', () => {
  // 面板工具表是 native-tools.mjs（mcp_tools.py 生成产物）的手工镜像：argo
  // 增删工具时两边必须同步，否则面板会给出不存在的勾选或漏掉新工具。
  // 注意 [...mod.TOOLS]：client.js 跑在 vm 沙箱里，其数组带沙箱的
  // Array.prototype，直接 map/deepStrictEqual 会因原型不同假红。
  const mod = factory(loadBundle())
  const specNames = Object.keys(NATIVE_TOOLS).sort()
  const toolIds = [...mod.TOOLS].map((tool) => tool.id).sort()
  assert.deepEqual(toolIds, specNames, '面板 TOOLS 与原生工具规格表漂移，请同步两边')
  for (const tool of mod.TOOLS) {
    assert.ok(tool.zh && tool.en, `tool ${tool.id} 缺双语标签`)
    assert.ok(tool.group === 'core' || tool.group === 'long', `tool ${tool.id} 分组非法`)
  }
})

test('client ENGINE_TOGGLES mirror ARGO_ENV_SPECS (drift gate)', () => {
  // 面板引擎开关与 index.js 白名单同一份名单：名字、双语标签、数值域都要对得上。
  const mod = factory(loadBundle())
  const specNames = ARGO_ENV_SPECS.map((spec) => spec.name).sort()
  const toggleNames = [...mod.ENGINE_TOGGLES].map((spec) => spec.name).sort()
  assert.deepEqual(toggleNames, specNames, '面板 ENGINE_TOGGLES 与 index.js ARGO_ENV_SPECS 漂移，请同步两边')
  const specByName = new Map(ARGO_ENV_SPECS.map((spec) => [spec.name, spec]))
  for (const toggle of mod.ENGINE_TOGGLES) {
    const spec = specByName.get(toggle.name)
    assert.equal(toggle.kind, spec.kind, `${toggle.name} kind 不一致`)
    assert.equal(toggle.group, spec.group, `${toggle.name} group 不一致`)
    assert.equal(toggle.invert, spec.invert, `${toggle.name} invert 不一致`)
    assert.equal(toggle.def, spec.def, `${toggle.name} 默认值不一致`)
    assert.ok(toggle.zh && toggle.en && toggle.hintZh && toggle.hintEn, `${toggle.name} 缺双语文案`)
    if (toggle.kind === 'num') {
      assert.ok(toggle.min <= toggle.max, `${toggle.name} min>max`)
      assert.ok(toggle.def >= toggle.min && toggle.def <= toggle.max, `${toggle.name} 默认值越界`)
    }
  }
})

test('cache switch defaults to cache enabled and never inverts', () => {
  const spec = ARGO_ENV_SPECS.find((s) => s.name === 'ARGO_NO_CACHE')
  assert.ok(spec, 'ARGO_NO_CACHE 不在白名单')
  assert.equal(spec.def, false, '缓存默认必须是开（NO_CACHE 默认 0）')
  assert.match(spec.zh, /测试模式/, '缓存开关文案应说明这是测试模式')
  assert.equal(spec.invert, undefined, '负向开关不再用 invert 反转')
  assert.deepEqual(ARGO_ENV_SPECS.filter((s) => s.invert).map((s) => s.name), [],
    '任何开关都不应再依赖 invert：文案与 env 必须同向')
})

test('paid quota switches distinguish daily and monthly limits', () => {
  const byName = new Map(ARGO_ENV_SPECS.map((s) => [s.name, s]))
  const daily = ['ARGO_QUOTA_ANYSEARCH_LIMIT', 'ARGO_QUOTA_ZHIHU_LIMIT']
  const monthly = ['ARGO_QUOTA_YUANBAO_LIMIT', 'ARGO_QUOTA_TAVILY_LIMIT']
  for (const name of daily) {
    const spec = byName.get(name)
    assert.ok(spec, `${name} 不在白名单`)
    assert.match(spec.zh, /每日/, `${name} 的文案必须写明按日`)
  }
  for (const name of monthly) {
    const spec = byName.get(name)
    assert.ok(spec, `${name} 不在白名单`)
    assert.match(spec.zh, /每月/, `${name} 的文案必须写明按月`)
  }
  for (const name of [...daily, ...monthly]) {
    const spec = byName.get(name)
    assert.equal(spec.kind, 'num', `${name} 必须是数字`)
    assert.equal(spec.group, 'paid', `${name} 应归入付费搜索组`)
    assert.equal(spec.min, 0, `${name} 允许 0=不限制`)
  }
  assert.equal(byName.get('ARGO_QUOTA_YUANBAO_LIMIT').def, 2000,
    '元宝默认 2000/月')
  // 博查按量付费：不应再有固定次数项（2026-10-22 用户更正）
  assert.equal(byName.has('ARGO_QUOTA_BOCHA_LIMIT'), false,
    '博查按量付费，不应暴露次数上限')
})

test('Exa uses a monthly USD budget, not a call count', () => {
  const byName = new Map(ARGO_ENV_SPECS.map((s) => [s.name, s]))
  const exa = byName.get('ARGO_BUDGET_EXA')
  assert.ok(exa, 'ARGO_BUDGET_EXA 不在白名单')
  assert.equal(exa.kind, 'num')
  assert.equal(exa.group, 'paid')
  assert.equal(exa.def, 10, 'Exa 默认 $10/月')
  assert.match(exa.zh, /美元|USD/)
  assert.equal(byName.has('ARGO_QUOTA_EXA_LIMIT'), false,
    'Exa 是金额额度，不应再暴露成次数上限')
})

test('keys tab exposes the Yuanbao credential ref', () => {
  // KEYS 是面板「密钥」页的清单；元宝已成为中文通用域第 2 位对冲源，
  // key 不在清单里就只能手改 ~/.config/argo/env（2026-10-22 用户反馈）。
  const src = readFileSync(new URL('../dsh/client.js', import.meta.url), 'utf8')
  assert.match(src, /ref: 'ARGO_YUANBAO_API_KEY'/, '密钥页缺腾讯元宝')
})

test('ArgoPanel renders every tab without undefined references', () => {
  // node --check 只查语法，查不出渲染路径里的未定义引用；注册测试又不跑组件。
  // 这里用带状态的 React 替身把六个 tab 各完整渲染一遍：任何 tab 的渲染函数
  // 抛错（拼错变量、漏定义助手）都会在这里红，而不是等用户在浏览器里点开。
  const definition = loadBundle()
  const { react, hooks, reset } = statefulReact()
  const mod = definition.factory((id) => {
    if (id === 'react') return react
    throw new Error(`unexpected require: ${id}`)
  })
  const value = {
    nativeTools: ['argo_search', 'argo_fetch'],
    keyRefs: ['DEEPSEEK_API_KEY'],
    argoEnv: ['ARGO_NO_CACHE=1', 'ARGO_PROXY=http://127.0.0.1:7890', 'ARGO_MINHASH_THRESHOLD=0.9'],
    searchProviderEnabled: true,
    fetchProviderEnabled: true,
    searchCommand: 'python3',
    searchArgs: ['mcp_server.py'],
  }
  const props = {
    useArgoForm: (select) => select({ status: 'ready', writable: true, value }),
    set: async () => true,
    mutate: async () => true,
    credentials: {
      describe: async () => ({ ok: true, value: { DEEPSEEK_API_KEY: { configured: true, writable: true, source: 'env' } } }),
      set: async () => ({ ok: true }),
      unset: async () => ({ ok: true }),
    },
    remote: { $on: () => () => {} },
    locale: { getSnapshot: () => ({ active: 'zh' }), subscribe: () => () => {} },
    view: 'page',
  }
  for (const tab of ['dashboard', 'access', 'tools', 'engines', 'keys', 'research', 'advanced']) {
    reset()
    hooks.length = 0
    hooks[1] = tab // tab 是 ArgoPanel 的第 2 个 useState
    const tree = mod.ArgoPanel(props)
    assert.ok(tree && tree.type === 'div', `${tab} 应渲染出面板根节点`)
  }
  // 摘要视图（plugins.item 卡片的一行摘要）
  reset()
  hooks.length = 0
  assert.equal(typeof mod.ArgoPanel({ ...props, view: 'summary' }), 'string')

  // 引擎 tab 真实产出：七个分组卡 + 输入控件（防「渲染不抛错但内容是空」）
  reset()
  hooks.length = 0
  hooks[1] = 'engines'
  const flat = []
  // 迷你渲染器：createElement 替身不调用函数组件，这里补上——Card/Switch/
  // Field/PrimaryButton 都要真跑一遍，section/input 节点才存在，组件体里的
  // 未定义引用也才会在这里暴露。注意 React 语义：children 在 props.children
  // 上递给组件（单个 child 不是数组），替身只把 children 挂在节点上，这里补映射。
  const walk = (node) => {
    if (node === null || node === undefined) return
    if (Array.isArray(node)) { node.forEach(walk); return }
    if (typeof node !== 'object') return
    if (typeof node.type === 'function') {
      const props = { ...node.props }
      if (node.children && node.children.length > 0) {
        props.children = node.children.length === 1 ? node.children[0] : node.children
      }
      walk(node.type(props))
      return
    }
    flat.push(node)
    ;(node.children || []).forEach(walk)
  }
  walk(mod.ArgoPanel(props))
  const sections = flat.filter((node) => node.type === 'section')
  assert.ok(sections.length >= 7, `引擎 tab 应渲染 7 个分组卡，实际 ${sections.length}`)
  assert.ok(flat.some((node) => node.type === 'input'), '引擎 tab 应渲染输入控件')
  assert.ok(flat.some((node) => node.type === 'button'), '引擎 tab 应渲染保存按钮')

  // 只读态：表单不可写时渲染不崩（控件禁用由 props 下沉）
  reset()
  hooks.length = 0
  hooks[1] = 'engines'
  const roTree = mod.ArgoPanel({ ...props, useArgoForm: (select) => select({ status: 'ready', writable: false, value }) })
  assert.ok(roTree && roTree.type === 'div')
})

test('engines tab renders a control for every ENGINE_TOGGLES switch (no invisible group)', () => {
  // 上面的 mirror 测试只核对「index.js 白名单 ↔ client.js 镜像」同名同字段，
  // 核对不到「这个 group 到底渲不渲染」。2026-10-21 实锤：`paid` 分组没进渲染
  // 清单，ARGO_PAID_SEARCH 与 ARGO_BUDGET_DEEPSEEK_SEARCH 在白名单、镜像、
  // 双语文案三处都齐全，面板上却完全不显示——用户配了预算却找不到入口，
  // 只会以为插件没装。
  //
  // 判据取「每个开关的标签都必须出现在渲染树里」，而不是「分组数量对得上」：
  // 后者在两组互相吞并、或多渲染一个空组时仍可能凑巧通过，前者直接锁住
  // 「用户能看见并可操作这一项」这件事本身。
  const definition = loadBundle()
  const { react, hooks, reset } = statefulReact()
  const mod = definition.factory((id) => {
    if (id === 'react') return react
    throw new Error(`unexpected require: ${id}`)
  })
  const props = {
    useArgoForm: (select) => select({
      status: 'ready',
      writable: true,
      value: { nativeTools: [], keyRefs: [], argoEnv: [] },
    }),
    set: async () => true,
    mutate: async () => true,
    credentials: {
      describe: async () => ({ ok: true, value: {} }),
      set: async () => ({ ok: true }),
      unset: async () => ({ ok: true }),
    },
    remote: { $on: () => () => {} },
    locale: { getSnapshot: () => ({ active: 'zh' }), subscribe: () => () => {} },
    view: 'page',
  }
  reset()
  hooks.length = 0
  hooks[1] = 'engines'
  const texts = new Set()
  // 与「七个分组卡」那条测试同一套迷你渲染器（函数组件要真跑，children 要补映射）
  const walk = (node) => {
    if (node === null || node === undefined) return
    if (Array.isArray(node)) { node.forEach(walk); return }
    if (typeof node !== 'object') {
      if (typeof node === 'string' || typeof node === 'number') texts.add(String(node))
      return
    }
    if (typeof node.type === 'function') {
      const nextProps = { ...node.props }
      if (node.children && node.children.length > 0) {
        nextProps.children = node.children.length === 1 ? node.children[0] : node.children
      }
      walk(node.type(nextProps))
      return
    }
    ;(node.children || []).forEach(walk)
  }
  walk(mod.ArgoPanel(props))
  // 标签带 suffix（未配置时追加「（默认）」），故用前缀匹配而非全等。
  const visible = (label) => [...texts].some((text) => String(text).startsWith(label))
  const missing = [...mod.ENGINE_TOGGLES]
    .filter((spec) => !visible(spec.zh))
    .map((spec) => `${spec.name}(${spec.group})`)
  assert.deepEqual(
    missing, [],
    `这些开关定义了但面板上没有控件（分组未进渲染清单？）：${missing.join(', ')}`,
  )
})

test('dashboard tab renders metrics, workflow presets, and engine topology matrix', () => {
  const definition = loadBundle()
  const { react, hooks, reset } = statefulReact()
  const mod = definition.factory((id) => {
    if (id === 'react') return react
    throw new Error(`unexpected require: ${id}`)
  })
  const props = {
    useArgoForm: (select) => select({
      status: 'ready',
      writable: true,
      value: { nativeTools: ['argo_search'], keyRefs: ['DEEPSEEK_API_KEY'], argoEnv: [] },
    }),
    set: async () => true,
    mutate: async () => true,
    credentials: {
      describe: async () => ({ ok: true, value: { DEEPSEEK_API_KEY: { configured: true } } }),
      set: async () => ({ ok: true }),
      unset: async () => ({ ok: true }),
    },
    remote: { $on: () => () => {} },
    locale: { getSnapshot: () => ({ active: 'zh' }), subscribe: () => () => {} },
    view: 'page',
  }
  reset()
  hooks.length = 0
  hooks[1] = 'dashboard'
  const flat = []
  const walk = (node) => {
    if (node === null || node === undefined) return
    if (Array.isArray(node)) { node.forEach(walk); return }
    if (typeof node !== 'object') return
    flat.push(node)
    if (typeof node.type === 'function') {
      const nextProps = { ...node.props }
      if (node.children && node.children.length > 0) {
        nextProps.children = node.children.length === 1 ? node.children[0] : node.children
      }
      walk(node.type(nextProps))
      return
    }
    ;(node.children || []).forEach(walk)
  }
  walk(mod.ArgoPanel(props))
  const sections = flat.filter((node) => node.type === 'section')
  assert.ok(sections.length >= 2, `dashboard tab 应渲染预设和矩阵卡，实际 ${sections.length}`)
  assert.ok(flat.some((node) => node.props && node.props.key === 'metrics'), 'dashboard 应包含 KPI 指标卡')
  assert.ok(flat.some((node) => node.props && node.props.key === 'presets'), 'dashboard 应包含工作流预设')
  assert.ok(flat.some((node) => node.props && node.props.key === 'matrix'), 'dashboard 应包含核心引擎拓扑矩阵')
})

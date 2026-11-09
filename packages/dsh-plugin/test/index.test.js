import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { apply } from '../dsh/index.js'
import { buildChildToolFilters, resolveArgoEnv } from '../dsh/specs.js'
import {
  normalizeTracks,
  stageTracks,
  evaluateGates,
  normalizeSource,
  normalizeSourceUrl,
  buildLocalSources,
  parseJsonArray,
  parseJsonObject,
  extractValues,
  startStructured,
  providerSupportsStructured,
} from '../dsh/wide-research.js'
import {
  mapArgoToWebResult,
  resolveNativeSpawn,
  makeNativeTool,
  nativeViaMcpPool,
  disposeSharedMcp,
  fetchViaArgo,
  mapArgoToWebFetch,
  classifyFetchError,
  runNativeCall,
  searchViaArgoMCP,
} from '../dsh/runner.js'
import { NATIVE_TOOLS } from '../dsh/native-tools.mjs'

test('mapArgoToWebResult maps compact argo payload to web sources', () => {
  const mapped = mapArgoToWebResult({
    query: 'q',
    engine: 'octen',
    results: [
      { title: '甲', url: 'https://a.com/1', snippet: 's1', source: 'octen', score: 0.9 },
      { title: '', url: 'https://b.com/2', snippet: null },
      { url: 'https://c.com/3' },
      { title: '无url', snippet: 'no' }
    ]
  })
  assert.equal(mapped.sources.length, 3)
  assert.deepEqual(mapped.sources[0], { url: 'https://a.com/1', title: '甲', snippet: 's1' })
  assert.deepEqual(mapped.sources[1], { url: 'https://b.com/2' })
  assert.equal(mapped.truncated, false)
  assert.equal(mapped.engine, 'octen')
})

test('mapArgoToWebResult tolerates empty and malformed payloads', () => {
  const empty = mapArgoToWebResult({}, 'q')
  assert.deepEqual(empty.sources, [])
  const junk = mapArgoToWebResult({ results: 'nope' }, 'q')
  assert.deepEqual(junk.sources, [])
  const nonArray = mapArgoToWebResult({ results: [42, { url: 7 }] }, 'q')
  assert.deepEqual(nonArray.sources, [])
})

test('normalizeTracks keeps depends_on and dedupes ids', () => {
  const tracks = normalizeTracks({
    tracks: [
      { id: 'def', title: '定义', question: '如何分类', rationale: 'r1', depends_on: [] },
      { id: 'risk', title: '风险', question: '瓶颈', rationale: 'r2', depends_on: ['def'] },
      { id: 'def', title: '重复', question: '重复', rationale: 'r3' },
    ],
  }, 9)
  assert.equal(tracks.length, 2)
  assert.deepEqual(tracks[0].depends_on, [])
  assert.deepEqual(tracks[1].depends_on, ['def'])
})

test('normalizeTracks caps at maximum and clips long fields', () => {
  const tracks = normalizeTracks({
    tracks: [1, 2, 3, 4].map((n, i) => ({
      id: `t${i}`, title: 't', question: 'q', rationale: 'r',
    })),
  }, 2)
  assert.equal(tracks.length, 2)
})

test('stageTracks: no deps means a single parallel stage', () => {
  const { stages, warnings } = stageTracks([
    { id: 'a', depends_on: [] },
    { id: 'b', depends_on: [] },
  ])
  assert.equal(warnings.length, 0)
  assert.equal(stages.length, 1)
  assert.deepEqual(stages[0].map(t => t.id).sort(), ['a', 'b'])
})

test('stageTracks: depends_on splits into sequential stages', () => {
  const { stages, warnings } = stageTracks([
    { id: 'risk', depends_on: ['def'] },
    { id: 'def', depends_on: [] },
    { id: 'market', depends_on: ['def'] },
  ])
  assert.equal(warnings.length, 0)
  assert.deepEqual(stages.map(s => s.map(t => t.id)), [['def'], ['market', 'risk']])
})

test('stageTracks: missing dependency is a warning, not a crash', () => {
  const { stages, warnings } = stageTracks([
    { id: 'a', depends_on: ['ghost'] },
  ])
  assert.equal(warnings.length, 1)
  assert.match(warnings[0], /ghost/)
  assert.equal(stages.length, 1)
})

test('stageTracks: cycle merges into last stage with warning', () => {
  const { stages, warnings } = stageTracks([
    { id: 'a', depends_on: ['b'] },
    { id: 'b', depends_on: ['a'] },
  ])
  assert.equal(stages.length, 1)
  assert.equal(stages[0].length, 2)
  assert.ok(warnings.some(w => w.includes('cycle')))
})

test('evaluateGates: no sources fails to low', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 2, failedTracks: 0, sourceCount: 0 },
    sources: [], caveats: [], unansweredQuestions: [],
  })
  assert.equal(gates.passed, false)
  assert.equal(gates.conclusion_cap, 'low')
  assert.ok(gates.failures.some(f => f.id === 'no_sources'))
})

test('evaluateGates: all tracks failed fails to low', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 0, failedTracks: 3, sourceCount: 0 },
    sources: [], caveats: [], unansweredQuestions: [],
  })
  assert.equal(gates.conclusion_cap, 'low')
  assert.ok(gates.failures.some(f => f.id === 'all_tracks_failed'))
})

test('evaluateGates: partial track failure caps at medium', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 2, failedTracks: 1, sourceCount: 5 },
    sources: [
      { confidence: 'high', url: 'https://a.com' },
      { confidence: 'medium', url: 'https://b.com' },
    ],
    caveats: [], unansweredQuestions: [],
  })
  assert.equal(gates.passed, true)
  assert.equal(gates.conclusion_cap, 'medium')
  assert.ok(gates.warnings.some(w => w.id === 'partial_track_failure'))
})

test('evaluateGates: no high-confidence sources warns to medium', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 2, failedTracks: 0, sourceCount: 3 },
    sources: [
      { confidence: 'low', url: 'https://a.com' },
      { confidence: 'low', url: 'https://b.com' },
    ],
    caveats: [], unansweredQuestions: [],
  })
  assert.equal(gates.conclusion_cap, 'medium')
  assert.ok(gates.warnings.some(w => w.id === 'no_high_confidence_sources'))
})

test('evaluateGates: clean run caps at high', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 3, failedTracks: 0, sourceCount: 8 },
    sources: [
      { confidence: 'high', url: 'https://a.com' },
      { confidence: 'high', url: 'https://b.com' },
      { confidence: 'medium', url: 'https://c.com' },
    ],
    caveats: ['one caveat'], unansweredQuestions: [],
  })
  assert.equal(gates.passed, true)
  assert.equal(gates.conclusion_cap, 'high')
  assert.equal(gates.failures.length, 0)
})

test('normalizeSource: only http(s) urls enter the ledger', () => {
  const http = normalizeSource({ title: 't', url: 'https://a.com/x', claim: 'c' })
  assert.equal(http.url, 'https://a.com/x')
  const javascript = normalizeSource({ title: 't', url: 'javascript:alert(1)', claim: 'c' })
  assert.equal(javascript, undefined)
  const ftp = normalizeSource({ title: 't', url: 'ftp://a.com/file', claim: 'c' })
  assert.equal(ftp, undefined)
  const httpNoScheme = normalizeSource({ title: 't', url: 'example.com/x', claim: 'c' })
  assert.equal(httpNoScheme, undefined)
})

test('normalizeSource: confidence defaults to low, limits high to high/medium', () => {
  const low = normalizeSource({ title: 't', url: 'https://a.com', claim: 'c' })
  assert.equal(low.confidence, 'low')
  const high = normalizeSource({ title: 't', url: 'https://a.com', claim: 'c', confidence: 'high' })
  assert.equal(high.confidence, 'high')
  const bogus = normalizeSource({ title: 't', url: 'https://a.com', claim: 'c', confidence: 'certain' })
  assert.equal(bogus.confidence, 'low')
})

test('persistReport writes to <dir>/<ts>-<slug>.md and returns absolute path', async () => {
  const { mkdtemp, readFile } = await import('node:fs/promises')
  const { tmpdir } = await import('node:os')
  const { join } = await import('node:path')
  const { persistReport } = await import('../dsh/wide-research.js')
  const dir = await mkdtemp(join(tmpdir(), 'argo-report-'))
  const file = await persistReport(dir, '测试 报告？问题', '# Report\ncontent')
  assert.ok(file.startsWith(dir))
  assert.ok(file.endsWith('.md'))
  const body = await readFile(file, 'utf8')
  assert.equal(body, '# Report\ncontent')
  // 目录自动创建
  const nested = await persistReport(join(dir, 'a', 'b'), 'q', 'x')
  assert.ok(nested.startsWith(join(dir, 'a', 'b')))
})

test('buildLocalSources registers lineage without content', () => {
  const out = buildLocalSources([
    { path: '~/data/company.xlsx', role: '原始数据', sha256: 'abc', size: 100, mtime: 123 },
    { path: '', role: 'x' },
    { path: '~/notes/a.md' },
  ])
  assert.equal(out.length, 2)
  assert.equal(out[0].ref, '[L1]')
  assert.equal(out[0].type, 'file')
  assert.equal(out[0].role, '原始数据')
  assert.equal(out[0].note.includes('内容未入库'), true)
  assert.equal(out[1].kind, '')
  assert.equal(out[1].role, 'data')
})

test('parseJsonArray tolerates empty & malformed', () => {
  assert.deepEqual(parseJsonArray(undefined, 'fi'), [])
  assert.deepEqual(parseJsonArray('', 'fi'), [])
  const good = parseJsonArray('[{"path":"a"}]', 'fi')
  assert.equal(good.length, 1)
  assert.deepEqual(parseJsonArray('nope', 'fi'), [])
  assert.deepEqual(parseJsonArray([1, 2], 'fi'), [])
})

test('parseJsonObject tolerates empty & malformed', () => {
  assert.deepEqual(parseJsonObject(undefined, 'rc'), {})
  assert.deepEqual(parseJsonObject('', 'rc'), {})
  const good = parseJsonObject('{"script":"1+1","budget":{"timeout_s":30}}', 'rc')
  assert.equal(good.script, '1+1')
  assert.equal(good.budget.timeout_s, 30)
  assert.deepEqual(parseJsonObject('oops', 'rc'), {})
})

test('extractValues pulls numbers and percentages', () => {
  assert.deepEqual(extractValues('营收 1,234.5 万，同比 23%'), [1234.5, 0.23])
  assert.deepEqual(extractValues(''), [])
  assert.deepEqual(extractValues('无数字文本'), [])
})

test('evaluateGates: recompute_skipped caps at medium', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 2, failedTracks: 0, sourceCount: 3 },
    sources: [
      { confidence: 'high', url: 'https://a.com' },
      { confidence: 'high', url: 'https://b.com' },
    ],
    caveats: [], unansweredQuestions: [],
    recomputeExpected: true, recomputedValues: [], localSources: [],
  })
  assert.equal(gates.conclusion_cap, 'medium')
  assert.ok(gates.warnings.some(w => w.id === 'recompute_skipped'))
})

test('evaluateGates: recompute_conflict when no numeric overlap', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 2, failedTracks: 0, sourceCount: 3 },
    sources: [
      { confidence: 'high', url: 'https://a.com', title: '甲', claim: '营收 100 万元' },
      { confidence: 'high', url: 'https://b.com', title: '乙', claim: '利润 5 万元' },
    ],
    caveats: [], unansweredQuestions: [],
    recomputeExpected: true,
    recomputedValues: [{ ok: true, values: [42], stdout_tail: '', stderr_tail: '' }],
    localSources: [],
  })
  assert.ok(gates.warnings.some(w => w.id === 'recompute_conflict'))
})

test('evaluateGates: local first-hand source counts as evidence', () => {
  // 无网络源但含本地一手文件时，不应触发 no_sources 假阴性
  const gates = evaluateGates({
    stats: { completedTracks: 1, failedTracks: 0, sourceCount: 0 },
    sources: [], caveats: [], unansweredQuestions: [],
    localSources: [{ path: '~/company.xlsx', ref: '[L1]' }],
  })
  assert.ok(!gates.failures.some(f => f.id === 'no_sources'), '本地一手存在时不应判 no_sources')
})

// ── 原生一等工具（CLI 单发形态）──────────────────────────────────────────

test('resolveNativeSpawn: npx mode appends call subcommand', () => {
  const config = { searchCommand: 'npx', searchArgs: ['-y', 'github:taxueseek/argo'] }
  const spec = resolveNativeSpawn(config, 'argo_search', '{"query":"q"}')
  assert.equal(spec.command, 'npx')
  assert.deepEqual(spec.args.slice(-3), ['call', 'argo_search', '{"query":"q"}'])
})

test('resolveNativeSpawn: local mcp_server.py mode uses --call flag', () => {
  const config = { searchCommand: 'python3', searchArgs: ['/opt/argo/scripts/mcp_server.py'] }
  const spec = resolveNativeSpawn(config, 'argo_fetch', '{"url":"https://x"}')
  assert.equal(spec.command, 'python3')
  assert.deepEqual(spec.args, ['/opt/argo/scripts/mcp_server.py', '--call', 'argo_fetch', '{"url":"https://x"}'])
})

test('resolveNativeSpawn: mcp_server.py mode detected with extra args, not just a single arg', () => {
  // 旧实现按 args.length===1 判定本机模式，searchArgs 一带附加参数就静默走错
  // 分支（把 call 当脚本参数喂给 python）。现按「是否出现 mcp_server.py」判定。
  const config = { searchCommand: 'python3', searchArgs: ['-u', '/opt/argo/scripts/mcp_server.py'] }
  const spec = resolveNativeSpawn(config, 'argo_search', '{}')
  assert.deepEqual(spec.args, ['-u', '/opt/argo/scripts/mcp_server.py', '--call', 'argo_search', '{}'])
})

test('resolveNativeSpawn: empty config falls back to defaults with call tail', () => {
  // DEFAULTS.searchCommand 读环境变量，不硬断言具体命令值；
  // 两种部署路径的 args 尾部统一是 [tool, payload]。
  const spec = resolveNativeSpawn({}, 'argo_search', '{"query":"q"}')
  assert.equal(typeof spec.command, 'string')
  assert.ok(spec.command.length > 0)
  assert.deepEqual(spec.args.slice(-2), ['argo_search', '{"query":"q"}'])
})

test('makeNativeTool: execute forwards picked args and render unwraps MCP payload', async () => {
  const captured = []
  const run = async (spawnSpec) => {
    captured.push(spawnSpec)
    return JSON.stringify({ content: [{ type: 'text', text: 'hello argo' }] })
  }
  const tool = makeNativeTool({ nativeMaxChars: 24_000 }, 'argo_search', { run })
  assert.equal(tool.name, 'argo_search')
  assert.deepEqual(tool.parameters.required, ['query'])

  const value = await tool.execute({ query: 'q1', evil: 'injected', engine: '' })
  // junk 键被 pickArgs 丢弃、空串过滤；payload 只含合法非空参数
  const payload = JSON.parse(captured[0].args[captured[0].args.length - 1])
  assert.deepEqual(payload, { query: 'q1' })

  const blocks = tool.output.render({}, value)
  assert.equal(blocks[0].type, 'text')
  assert.equal(blocks[0].text, 'hello argo')
})

test('makeNativeTool: render clips to nativeMaxChars and tolerates raw stdout', () => {
  const run = async () => JSON.stringify({ content: [{ type: 'text', text: 'x'.repeat(100) }] })
  const tool = makeNativeTool({ nativeMaxChars: 10 }, 'argo_fetch', { run })
  const blocks = tool.output.render({}, { stdout: JSON.stringify({ content: [{ type: 'text', text: 'x'.repeat(100) }] }) })
  assert.ok(blocks[0].text.length <= 10)
  // 非 JSON stdout 原样透传（同样受 clip 约束）
  const raw = makeNativeTool({ nativeMaxChars: 5 }, 'argo_fetch', { run: async () => 'plain text!' })
  assert.ok(raw.output.render({}, { stdout: 'plain text!' })[0].text.length <= 5)
})

test('makeNativeTool: prefers persistent MCP connection and passes picked args object', async () => {
  const seen = []
  // mcpCall 收到的是参数对象（不是 JSON 字符串），与 stdio tools/call 的 arguments 对齐
  const mcpCall = async (config, kind, argsObj, timeoutMs) => {
    seen.push({ kind, argsObj, timeoutMs })
    return JSON.stringify({ content: [{ type: 'text', text: 'via-persistent' }] })
  }
  // 常驻连接可用时，单发 runner 不应被调用
  const run = async () => { throw new Error('should not spawn when persistent MCP works') }
  const tool = makeNativeTool(
    { nativeTimeoutMs: 1234 }, 'argo_search', { mcpCall, run })
  const value = await tool.execute({ query: 'q', engine: '', bogus: 1 })
  assert.deepEqual(seen[0].argsObj, { query: 'q' })
  assert.equal(seen[0].kind, 'argo_search')
  assert.equal(seen[0].timeoutMs, 1234)
  assert.equal(tool.output.render({}, value)[0].text, 'via-persistent')
})

test('makeNativeTool: falls back to single-shot spawn when persistent connection fails', async () => {
  const spawned = []
  const mcpCall = async () => { throw new Error('connection closed') }
  const run = async (spawnSpec) => {
    spawned.push(spawnSpec)
    return JSON.stringify({ content: [{ type: 'text', text: 'via-spawn' }] })
  }
  const tool = makeNativeTool(
    { searchCommand: 'python3', searchArgs: ['/opt/argo/scripts/mcp_server.py'] },
    'argo_fetch', { mcpCall, run })
  const value = await tool.execute({ url: 'https://x' })
  assert.equal(spawned.length, 1, '连接失败后应回退单发一次')
  assert.equal(tool.output.render({}, value)[0].text, 'via-spawn')
})

test('makeNativeTool: unknown tool name fails loudly', () => {
  assert.throws(() => makeNativeTool({}, 'argo_nope'), /unknown native tool/)
})

function makeCtx() {
  const registered = []
  const providers = []
  const fetchProviders = []
  const guards = []
  const ctx = {
    tools: {
      register: (t) => {
        registered.push(t)
        // 与宿主一致：注册返回 disposer，供 nativeTools 热更新时撤销。
        return () => {
          const index = registered.indexOf(t)
          if (index >= 0) registered.splice(index, 1)
        }
      },
      guard: (g) => guards.push(g),
    },
    subagents: { getProvider: () => null, list: () => [] },
    // 与 cordis 一致：effect 回调同步执行并返回 disposer（mock 忽略返回值）。
    effect: (cb) => cb(),
    get: (svc) => svc === 'web' ? {
      registerSearchProvider: (p) => providers.push(p),
      registerFetchProvider: (p) => fetchProviders.push(p),
    } : null,
  }
  return { ctx, registered, providers, fetchProviders, guards }}

test('searchViaArgoMCP: pool path resolves keyRefs from caller credentials (issue #41)', async () => {
  // 回归：search 走 MCP 池，连接 envResolver 只认 options.config/env。web 搜索
  // provider 经 registerSearchProvider 进来，手里有 config、无独立顶层
  // credentials；此前 acquireMcp 只传 options.credentials（undefined），于是
  // resolveArgoEnv 的 provider 恒为 undefined，keyRefs 解析不出来，所有需
  // API key 的引擎长期 skipped-missing-env。锁住 config.credentials 这条路径：
  // 记录凭据被解析过 + 池确实按该 config 开了一条连接（桩不回 JSON，忽略载荷）。
  stubEnv()
  let resolved = 0
  const fake = { resolve: async (ref) => { resolved += 1; return ref === 'DEEPSEEK_API_KEY' ? { value: 'sk-host', source: 'file' } : undefined } }
  const fakeConn = { release() {}, request: async () => ({ content: [{ type: 'text', text: '{"success":true,"results":[]}' }] }) }
  try {
    await searchViaArgoMCP('hello', 5, undefined, {
      command: process.execPath, args: [STUB_SERVER], timeoutMs: 5000,
      credentials: fake, config: { keyRefs: ['DEEPSEEK_API_KEY'] },
    })
  } catch { /* 桩载荷非 JSON，忽略；只验凭据解析 */ }
  assert.ok(resolved >= 1, 'caller 传入的 credentials 应被用来解析 keyRefs')
  assert.equal(spawnCount(), 1, '应按该 config 开一条池连接')
})

test('providerSupportsStructured: rc.2 capabilities no longer include toolFilter', () => {
  assert.equal(providerSupportsStructured({
    capabilities: { agentOptions: true, agentPreset: true, outputSchema: true, depthLimit: true }
  }), true, '缺 toolFilter 能力不应再判失败')
  assert.equal(providerSupportsStructured({
    capabilities: { outputSchema: false, depthLimit: true }
  }), false)
  assert.equal(providerSupportsStructured({}), false)
})

function structuredCtx(overrides = {}) {
  const seen = { activation: [], legacy: [] }
  const ctx = {
    subagents: {
      ...(overrides.legacyOnly ? {} : {
        startActivation: async (spec) => {
          seen.activation.push(spec)
          return {
            childId: 'child-1',
            result: Promise.resolve({ stopReason: 'completed', structured: { ok: true } }),
            dispose: async () => { seen.disposed = true }
          }
        }
      }),
      ...(overrides.legacyOnly ? {
        start: async (provider, request) => {
          seen.legacy.push({ provider, request })
          return {
            result: Promise.resolve({ stopReason: 'completed', structured: { ok: true } }),
            dispose: async () => { seen.disposed = true }
          }
        }
      } : {})
    }
  }
  return { ctx, seen }
}

test('startStructured: rc.2 uses startActivation envelope and tracks the child', async () => {
  const { ctx, seen } = structuredCtx()
  const tracked = []
  const untracked = []
  const value = await startStructured(
    ctx, 'spawn', { id: 'parent' }, new AbortController().signal, 'lbl',
    'prompt', { type: 'object' }, 1234, { allow: ['argo_search'] },
    (id) => { tracked.push(id); return () => untracked.push(id) })
  assert.deepEqual(value, { ok: true })
  assert.equal(seen.activation.length, 1)
  const spec = seen.activation[0]
  assert.equal(spec.provider, 'spawn')
  assert.equal(spec.label, 'lbl')
  assert.equal(spec.delivery, 'caller', '结果只返回给调用方，不重复推给模型')
  assert.equal(spec.request.label, undefined, 'rc.2 的 request 不含 label')
  assert.equal(spec.request.signal, undefined, 'rc.2 的 request 不含 signal')
  assert.equal(spec.request.toolFilter, undefined,
    'rc.2 的 request 不再接受 toolFilter（工具范围由 preset/guard 承担）')
  assert.equal(spec.request.agentOptions.maxTokens, 1234)
  assert.deepEqual(tracked, ['child-1'])
  assert.deepEqual(untracked, ['child-1'], 'dispose 后应解除追踪')
})

test('startStructured: falls back to legacy start(provider, request)', async () => {
  const { ctx, seen } = structuredCtx({ legacyOnly: true })
  const value = await startStructured(
    ctx, 'spawn', { id: 'parent' }, new AbortController().signal, 'lbl',
    'prompt', { type: 'object' }, 999, { allow: ['argo_search'] }, () => () => {})
  assert.deepEqual(value, { ok: true })
  assert.equal(seen.legacy.length, 1)
  assert.equal(seen.legacy[0].provider, 'spawn')
  assert.equal(seen.legacy[0].request.label, 'lbl')
  assert.equal(seen.legacy[0].request.signal instanceof AbortSignal, true)
  assert.deepEqual(seen.legacy[0].request.toolFilter, { allow: ['argo_search'] },
    '旧宿主仍按 alpha.x 契约收 toolFilter')
})

test('apply: registers a guard that blocks wide_research inside a tracked worker', () => {
  const { ctx, guards } = makeCtx()
  const handle = apply(ctx, {})
  assert.equal(guards.length, 1)
  assert.equal(guards[0]({ name: 'wide_research', agent: { id: 'outside' } }), undefined)
  handle.childIds.add('worker-1')
  assert.match(guards[0]({ name: 'wide_research', agent: { id: 'worker-1' } }),
    /not available inside a research worker/)
  assert.equal(guards[0]({ name: 'argo_search', agent: { id: 'worker-1' } }), undefined)
})

test('apply: default config registers native tools and an available web provider', () => {
  const { ctx, registered, providers } = makeCtx()
  apply(ctx, {})
  const names = registered.map(t => t.name)
  assert.ok(names.includes('argo_search'), 'argo_search 应注册')
  assert.ok(names.includes('argo_fetch'), 'argo_fetch 应注册')
  assert.ok(names.includes('wide_research'))
  assert.equal(providers.length, 1)
  assert.equal(providers[0].id, 'argo')
  assert.equal(providers[0].available(), true)
})

test('apply: default config also registers an available web fetch provider', () => {
  const { ctx, fetchProviders } = makeCtx()
  apply(ctx, {})
  assert.equal(fetchProviders.length, 1, '宿主有 registerFetchProvider 时应注册 fetch provider')
  assert.equal(fetchProviders[0].id, 'argo')
  assert.equal(fetchProviders[0].available(), true)
})

test('apply: fetchProviderEnabled=false keeps the fetch provider registered but unavailable', () => {
  const { ctx, fetchProviders } = makeCtx()
  apply(ctx, { fetchProviderEnabled: false })
  assert.equal(fetchProviders.length, 1, '与 search seam 同 pattern：注册但不可选')
  assert.equal(fetchProviders[0].available(), false)
})

test('apply: old host without registerFetchProvider does not break search registration', () => {
  const registered = []
  const providers = []
  const ctx = {
    tools: { register: (t) => registered.push(t), guard: () => {} },
    subagents: { getProvider: () => null, list: () => [] },
    effect() {},
    get: (svc) => svc === 'web' ? { registerSearchProvider: (p) => providers.push(p) } : null,
  }
  apply(ctx, {})
  assert.equal(providers.length, 1, 'search seam 不受宿主缺 fetch seam 影响')
  assert.equal(registered.filter(t => t.name === 'argo_fetch').length, 1, '原生 argo_fetch 工具仍在')
})

test('apply: old host without ctx.effect does not block activation', () => {
  // 7022fa2 修过同一类病（可选宿主服务缺席被当成必需，整个 entry 激活失败，
  // argo 工具与设置页一起消失），并在设置块留了「旧宿主无 ctx.effect」的
  // try/catch。但 apply 里另有两处**无条件** ctx.effect 先炸，那句容忍从未
  // 兑现——实测裸 ctx 下 apply 直接抛 "ctx.effect is not a function"。
  // 这条钉住：缺 effect 只丢卸载清理，不丢功能、不激活失败。
  const registered = []
  const ctx = {
    tools: { register: (t) => registered.push(t.name), guard: () => {} },
    subagents: { getProvider: () => null, list: () => [] },
    get: (svc) => svc === 'web' ? { registerSearchProvider: () => {}, registerFetchProvider: () => {} } : null,
    // 注意：没有 effect、没有 on、没有 fiber
  }
  let threw = null
  try { apply(ctx, { nativeTools: ['argo_search'] }) } catch (e) { threw = e }
  assert.equal(threw, null, `旧宿主缺 ctx.effect 不该阻断激活：${threw && threw.message}`)
  assert.ok(registered.includes('argo_search'), '原生工具仍要注册')
  assert.ok(registered.includes('wide_research'), '研究工具仍要注册')
})

test('apply: fetchProviderId override changes the registered id', () => {
  const { ctx, fetchProviders } = makeCtx()
  apply(ctx, { fetchProviderId: 'argo-fetch' })
  assert.equal(fetchProviders[0].id, 'argo-fetch')
})

test('apply: nativeTools=[] and searchProviderEnabled=false disables both seams', () => {
  const { ctx, registered, providers } = makeCtx()
  apply(ctx, { nativeTools: [], searchProviderEnabled: false })
  const names = registered.map(t => t.name)
  assert.ok(!names.includes('argo_search'))
  assert.ok(!names.includes('argo_fetch'))
  assert.ok(names.includes('wide_research'), 'wide_research 不受搜索开关影响')
  assert.equal(providers.length, 1, 'provider 仍注册（headless 一致性）')
  assert.equal(providers[0].available(), false)
})

test('apply: unknown native tool name fails loudly', () => {
  const { ctx } = makeCtx()
  assert.throws(() => apply(ctx, { nativeTools: ['argo_nope'] }), /unknown native tool/)
})

test('apply: disables the auto-form lazily and never reads ctx.settings directly', () => {
  // cordis 的 ctx 是 Proxy：未 inject 就访问 ctx.settings 会抛
  // `cannot get property "settings" without inject`（可选链救不了 getter
  // 抛错）。回归 2026-10-20 的启动崩溃：上一轮用 ctx.inject(['settings'],
  // cb) 取服务，子 fiber 在本插件 apply 期对 settings 仍 inactive，cb 里
  // child.settings 一样抛，整个 entry 激活失败。现在改为 effect 内惰性
  // ctx.get('settings')（get 对未提供/未激活服务返回 undefined，不抛）。
  const throwing = makeCtx().ctx
  Object.defineProperty(throwing, 'settings', {
    get() { throw new Error('cannot get property "settings" without inject') },
  })
  assert.doesNotThrow(() => apply(throwing, {}), 'apply 不得因 settings 不可用而抛')

  // settings 服务可用时应经 ctx.get 拿到并 configure({auto:false})
  const { ctx } = makeCtx()
  let configured = 0
  ctx.get = (name) => (name === 'settings' ? { configure: () => { configured += 1 } } : undefined)
  apply(ctx, {})
  assert.equal(configured, 1, '应经 ctx.get 取到 settings 并关闭自动表单')

  // configure 抛错（旧宿主 API 不兼容）时静默跳过，不阻断插件
  const { ctx: ctx2 } = makeCtx()
  ctx2.get = (name) => (name === 'settings' ? { configure: () => { throw new Error('nope') } } : undefined)
  assert.doesNotThrow(() => apply(ctx2, {}))
})

test('apply: nativeTools follows a live settings update without remounting', () => {
  const { ctx, registered } = makeCtx()
  const config = { nativeTools: ['argo_search'] }
  const handle = apply(ctx, config)
  assert.deepEqual(registered.map(t => t.name).sort(), ['argo_search', 'wide_research'])

  config.nativeTools = ['argo_fetch']
  handle.syncNativeTools()
  assert.deepEqual(registered.map(t => t.name).sort(), ['argo_fetch', 'wide_research'],
    '旧工具应撤销、新工具应注册、wide_research 保留')
})

test('apply: volatile refs are unwrapped and stay live after apply', () => {
  const VOLATILE_WRITE = Symbol.for('cosmokit.volatile.write')
  let enabled = true
  const ref = () => ({
    get: () => enabled,
    [VOLATILE_WRITE]: (next) => { enabled = next },
  })
  const { ctx, providers, fetchProviders } = makeCtx()
  apply(ctx, { searchProviderEnabled: ref(), fetchProviderEnabled: ref() })
  assert.equal(providers[0].available(), true, 'volatile 引用必须解成当前值')
  assert.equal(fetchProviders[0].available(), true)

  enabled = false
  assert.equal(providers[0].available(), false, '引用更新后 provider 立即可见新值')
  assert.equal(fetchProviders[0].available(), false)
})
test('resolveArgoEnv: resolves host credential refs into the child env', async () => {
  const fake = {
    resolve: async (ref) => (ref === 'DEEPSEEK_API_KEY' ? { value: 'sk-host', source: 'file' } : undefined),
  }
  const env = await resolveArgoEnv({ keyRefs: ['DEEPSEEK_API_KEY'] }, { credentials: fake })
  assert.equal(env.DEEPSEEK_API_KEY, 'sk-host')
  assert.equal(env.PATH, process.env.PATH, '整份 env 继承，不是只带密钥的裸对象')
})

test('resolveArgoEnv: the credential service wins over the process env (file layer)', async () => {
  // 凭证服务契约：存储文件层 > 启动环境 > .env。用户经面板/Models 页轮换 key
  // 后，argo 子进程必须拿到新值，不能抱着启动时的旧值不放。
  const saved = process.env.DEEPSEEK_API_KEY
  process.env.DEEPSEEK_API_KEY = 'sk-env'
  try {
    const fake = { resolve: async () => ({ value: 'sk-file', source: 'file' }) }
    const env = await resolveArgoEnv({ keyRefs: ['DEEPSEEK_API_KEY'] }, { credentials: fake })
    assert.equal(env.DEEPSEEK_API_KEY, 'sk-file')
  } finally {
    if (saved === undefined) delete process.env.DEEPSEEK_API_KEY
    else process.env.DEEPSEEK_API_KEY = saved
  }
})

test('resolveArgoEnv: an unconfigured ref falls through to the inherited process env', async () => {
  // 服务在场但该引用哪层都没有值：跳过它，spawn 继承 process.env（旧宿主/
  // 未配置场景的保底路径）。
  const saved = process.env.DEEPSEEK_API_KEY
  process.env.DEEPSEEK_API_KEY = 'sk-env'
  try {
    const fake = { resolve: async () => undefined }
    assert.equal(await resolveArgoEnv({ keyRefs: ['DEEPSEEK_API_KEY'] }, { credentials: fake }), undefined)
  } finally {
    if (saved === undefined) delete process.env.DEEPSEEK_API_KEY
    else process.env.DEEPSEEK_API_KEY = saved
  }
})

test('resolveArgoEnv: empty keyRefs, missing service and failed resolve all degrade to undefined', async () => {
  assert.equal(await resolveArgoEnv({ keyRefs: [] }, { credentials: { resolve: async () => ({ value: 'x' }) } }), undefined)
  assert.equal(await resolveArgoEnv({ keyRefs: ['DEEPSEEK_API_KEY'] }, {}), undefined)
  const boom = { resolve: async () => { throw new Error('locked keychain') } }
  assert.equal(await resolveArgoEnv({ keyRefs: ['DEEPSEEK_API_KEY'] }, { credentials: boom }), undefined)
})

test('apply: captures the host credentials service for child env resolution', async () => {
  const fake = {
    resolve: async (ref) => (ref === 'DEEPSEEK_API_KEY' ? { value: 'sk-host', source: 'file' } : undefined),
  }
  const { ctx } = makeCtx()
  ctx.get = (svc) => (svc === 'credentials' ? fake : null)
  apply(ctx, {})
  const env = await resolveArgoEnv()
  assert.equal(env.DEEPSEEK_API_KEY, 'sk-host')
  // 收尾复位模块状态，不污染后续用例
  apply(makeCtx().ctx, { nativeTools: [], searchProviderEnabled: false })
})

test('apply: a keyRefs entry that is not an identifier fails loudly', () => {
  const { ctx } = makeCtx()
  assert.throws(() => apply(ctx, { keyRefs: ['has space'] }), /POSIX-style identifier/)
})

test('makeNativeTool: single-spawn spec carries the host-resolved key env', async () => {
  const saved = process.env.DEEPSEEK_API_KEY
  delete process.env.DEEPSEEK_API_KEY
  const captured = []
  try {
    const fake = {
      resolve: async (ref) => (ref === 'DEEPSEEK_API_KEY' ? { value: 'sk-host', source: 'file' } : undefined),
    }
    const tool = makeNativeTool(
      {
        searchCommand: 'python3',
        searchArgs: ['mcp_server.py'],
        keyRefs: ['DEEPSEEK_API_KEY'],
        credentials: fake,
      },
      'argo_search',
      { run: async (spec) => { captured.push(spec); return '{"ok":true}' } },
    )
    await tool.execute({ query: 'q' })
    assert.equal(captured.length, 1)
    assert.equal(captured[0].env.DEEPSEEK_API_KEY, 'sk-host')
  } finally {
    if (saved !== undefined) process.env.DEEPSEEK_API_KEY = saved
  }
})

test('makeNativeTool: spawn spec shape is unchanged when no key resolves', async () => {
  const captured = []
  const tool = makeNativeTool(
    { searchCommand: 'python3', searchArgs: ['mcp_server.py'], keyRefs: [] },
    'argo_search',
    { run: async (spec) => { captured.push(spec); return '{"ok":true}' } },
  )
  await tool.execute({ query: 'q' })
  assert.equal(captured.length, 1)
  assert.equal(captured[0].env, undefined, '无增量时不改 spec 形状')
})

test('buildChildToolFilters: default allow list dual-writes MCP and native tools', () => {
  const { allow, deny } = buildChildToolFilters({}, 'wide_research')
  for (const tool of ['argo_search', 'argo_fetch', 'web_search', 'web_fetch', 'mcp__argo__argo_search']) {
    assert.ok(allow.includes(tool), `默认白名单应含 ${tool}（MCP/原生两态自洽）`)
  }
  assert.ok(!allow.includes('wide_research'), '自身不入白名单')
  assert.ok(!allow.includes('mcp__argo__argo_research'), '研究不套研究')
  assert.ok(deny.includes('wide_research'))
})

test('buildChildToolFilters: explicit empty allow falls back to deny path', () => {
  const { allow, deny } = buildChildToolFilters({ childToolAllow: [] }, 'wide_research')
  assert.deepEqual(allow, [])
  assert.ok(deny.includes('wide_research'))
})

test('buildChildToolFilters: explicit list filters blocked names and dedupes', () => {
  const { allow } = buildChildToolFilters({
    childToolAllow: ['argo_search', 'argo_search', 'mcp__argo__argo_research', 'web_search'],
  }, 'wide_research')
  assert.deepEqual(allow, ['argo_search', 'web_search'])
})

test('buildChildToolFilters: default MCP allow list covers the full tool face (minus argo_research)', () => {
  // 门禁：worker 白名单的 MCP 侧必须等于原生规格表。native-tools.mjs 是
  // mcp_tools.py 的生成产物（= 全量面去掉 argo_research，由
  // tests/test_native_tools_sync.py 把关），因此这条断言把插件白名单与
  // MCP 真源串成一条链——此前手写列表在工具面涨到 19 后漏掉了 5 个。
  const { allow } = buildChildToolFilters({}, 'wide_research')
  const mcpInAllow = allow.filter(tool => tool.startsWith('mcp__argo__'))
  assert.deepEqual(
    mcpInAllow,
    Object.keys(NATIVE_TOOLS).map(name => `mcp__argo__${name}`),
    'worker 白名单的 MCP 侧必须等于原生规格表（= mcp_tools.py 全量面 − argo_research）',
  )
  assert.ok(!allow.includes('mcp__argo__argo_research'), '研究不套研究')
})

test('buildChildToolFilters: deny entries are subtracted from the allow list', () => {
  // allow 非空时调用方只把 allow 交给宿主 toolFilter（单一键），deny 必须
  // 在这里就扣除，否则用户同时设 allow + deny 时 deny 被静默忽略。
  const { allow, deny } = buildChildToolFilters(
    { childToolDeny: ['argo_search', 'mcp__argo__argo_pdf'] }, 'wide_research')
  assert.ok(!allow.includes('argo_search'))
  assert.ok(!allow.includes('mcp__argo__argo_pdf'))
  assert.ok(deny.includes('argo_search'))
  assert.ok(allow.includes('argo_fetch'), '未被 deny 的工具应保留')
})

// ── 连接池（真实 stdio 桩）─────────────────────────────────────────────────
// 上面注入的 mcpCall 是替身，测不到池本身。这里起一个真进程讲 NDJSON：每次
// 启动往计数文件追加一行，「开了几条连接」于是成为可断言的观测值——复用、
// 按需增长、并发是否真并行、dispose 是否收干净，都能被证明而不是被假设。

const STUB_DIR = mkdtempSync(join(tmpdir(), 'argo-dsh-stub-'))
const STUB_SERVER = join(STUB_DIR, 'stub-mcp.js')

writeFileSync(STUB_SERVER, `
const fs = require('node:fs')
if (process.env.ARGO_STUB_COUNTER) fs.appendFileSync(process.env.ARGO_STUB_COUNTER, 'spawn\\n')
const delayMs = Number(process.env.ARGO_STUB_DELAY_MS || 0)
const toolError = process.env.ARGO_STUB_TOOL_ERROR === '1'
const fetchError = process.env.ARGO_STUB_FETCH_ERROR === '1'
let buf = ''
process.stdin.setEncoding('utf8')
const write = (obj) => process.stdout.write(JSON.stringify(obj) + '\\n')
process.stdin.on('data', (chunk) => {
  buf += chunk
  let nl
  while ((nl = buf.indexOf('\\n')) >= 0) {
    const line = buf.slice(0, nl).trim()
    buf = buf.slice(nl + 1)
    if (line === '') continue
    let msg
    try { msg = JSON.parse(line) } catch { continue }
    if (msg.id === undefined) continue
    if (msg.method === 'initialize') {
      write({ jsonrpc: '2.0', id: msg.id, result: { protocolVersion: '2025-06-18', capabilities: {}, serverInfo: { name: 'stub', version: '0' } } })
      continue
    }
    setTimeout(() => {
      const isFetch = msg.params?.name === 'argo_fetch'
      if (isFetch && fetchError) {
        write({
          jsonrpc: '2.0',
          id: msg.id,
          result: { content: [{ type: 'text', text: JSON.stringify({ error: 'blocked by robots' }) }], isError: true },
        })
        return
      }
      write({
        jsonrpc: '2.0',
        id: msg.id,
        result: toolError
          ? { content: [{ type: 'text', text: 'stub tool failed' }], isError: true }
          : isFetch
            ? { content: [{ type: 'text', text: JSON.stringify({
                success: true,
                url: 'https://stub.example/final',
                title: 'Stub page',
                content: 'stub fetched body (max_chars=' + (msg.params && msg.params.arguments ? msg.params.arguments.max_chars : 'none') + ')',
                length: 17,
                truncated: false,
              }) }] }
            : { content: [{ type: 'text', text: 'pooled-ok' }] },
      })
    }, delayMs)
  }
})
`, 'utf8')

let spawnLog = ''
let envRestore = {}

function stubEnv(vars = {}) {
  spawnLog = join(mkdtempSync(join(tmpdir(), 'argo-dsh-count-')), 'spawns.log')
  envRestore = {}
  for (const [key, value] of Object.entries({ ARGO_STUB_COUNTER: spawnLog, ...vars })) {
    envRestore[key] = process.env[key]
    process.env[key] = value
  }
}

/** 桩进程启动次数 === 池开出的连接数。 */
function spawnCount() {
  if (!existsSync(spawnLog)) return 0
  return readFileSync(spawnLog, 'utf8').split('\n').filter(Boolean).length
}

function stubTool(config = {}) {
  return makeNativeTool(
    { searchCommand: process.execPath, searchArgs: [STUB_SERVER], ...config },
    'argo_search',
  )
}

afterEach(() => {
  disposeSharedMcp()
  for (const [key, value] of Object.entries(envRestore)) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
  envRestore = {}
})

test('pool: sequential calls reuse one connection (no extra resident process)', async () => {
  stubEnv()
  const tool = stubTool({ searchConcurrency: 3 })
  await tool.execute({ query: 'a' })
  await tool.execute({ query: 'b' })
  assert.equal(spawnCount(), 1, '顺序调用只该有一条常驻连接，池化不该带来固定开销')
})

test('pool: concurrent calls grow the pool without exceeding the cap', async () => {
  stubEnv({ ARGO_STUB_DELAY_MS: '150' })
  const tool = stubTool({ searchConcurrency: 3 })
  await Promise.all([1, 2, 3].map(i => tool.execute({ query: `q${i}` })))
  const spawned = spawnCount()
  assert.ok(spawned > 1, `并发调用应补开连接，实际只有 ${spawned} 条`)
  assert.ok(spawned <= 3, `不得越过 searchConcurrency，实际开了 ${spawned} 条`)
})

test('pool: concurrent calls run in parallel instead of queueing on one connection', async () => {
  stubEnv({ ARGO_STUB_DELAY_MS: '300' })
  const tool = stubTool({ searchConcurrency: 3 })
  const started = Date.now()
  await Promise.all([1, 2, 3].map(i => tool.execute({ query: `q${i}` })))
  const elapsed = Date.now() - started
  // 单连接串行会累加到 ~900ms；池化后应贴近单次 300ms。阈值放宽到 700ms 抗抖动。
  assert.ok(elapsed < 700, `3 个 300ms 调用耗时 ${elapsed}ms，说明仍在同一连接上串行`)
})

test('pool: tool-level error is NOT retried through single-shot spawn', async () => {
  stubEnv({ ARGO_STUB_TOOL_ERROR: '1' })
  const spawned = []
  const tool = makeNativeTool(
    { searchCommand: process.execPath, searchArgs: [STUB_SERVER] },
    'argo_search',
    { mcpCall: nativeViaMcpPool, run: async (spec) => { spawned.push(spec); return 'unreachable' } },
  )
  await assert.rejects(() => tool.execute({ query: 'q' }), /stub tool failed/)
  assert.equal(spawned.length, 0, '工具级错误不得回退单发（那是双倍引擎调用与配额）')
  assert.equal(spawnCount(), 1, '工具级错误不得新起进程重跑')
})

test('pool: transport failure still falls back to single-shot spawn', async () => {
  const spawned = []
  const tool = makeNativeTool(
    { searchCommand: process.execPath, searchArgs: [join(STUB_DIR, 'does-not-exist.js')] },
    'argo_search',
    {
      mcpCall: nativeViaMcpPool,
      run: async (spec) => {
        spawned.push(spec)
        return JSON.stringify({ content: [{ type: 'text', text: 'via-spawn' }] })
      },
    },
  )
  const value = await tool.execute({ query: 'q' })
  assert.equal(spawned.length, 1, '连接建立失败应回退单发一次，功能不丢')
  assert.equal(tool.output.render({}, value)[0].text, 'via-spawn')
})

test('pool: disposeSharedMcp closes every connection; next call rebuilds', async () => {
  stubEnv()
  const tool = stubTool({ searchConcurrency: 2 })
  await Promise.all([tool.execute({ query: 'a' }), tool.execute({ query: 'b' })])
  const before = spawnCount()
  assert.ok(before >= 1)
  disposeSharedMcp()
  await tool.execute({ query: 'c' })
  assert.equal(spawnCount(), before + 1, 'dispose 后池应为空，下次调用重新建立连接')
})

// --- web_fetch seam：fetchViaArgo / mapArgoToWebFetch / classifyFetchError ---

test('mapArgoToWebFetch: maps payload, prefers the final url, and carries the truncated flag', () => {
  const mapped = mapArgoToWebFetch({
    success: true,
    url: 'https://final.example/doc',
    title: 'T',
    content: 'body text',
    truncated: true,
  }, 'https://request.example/doc', 100_000)
  assert.equal(mapped.url, 'https://final.example/doc', '重定向后的最终 URL 优先')
  assert.equal(mapped.statusCode, 200)
  assert.equal(mapped.body.kind, 'text', 'argo 产出已是 markdown 正文，归 text 不再二次转换')
  assert.equal(mapped.body.content, 'body text')
  assert.equal(mapped.truncated, true, '上游截断标记必须透传，模型据此知道内容被裁过')
})

test('mapArgoToWebFetch: falls back to the request url and tolerates missing content', () => {
  const mapped = mapArgoToWebFetch({ success: true }, 'https://request.example/doc', 100_000)
  assert.equal(mapped.url, 'https://request.example/doc')
  assert.equal(mapped.body.content, '')
  assert.equal(mapped.truncated, false)
})

test('mapArgoToWebFetch: clips to maxChars and flags truncation', () => {
  const mapped = mapArgoToWebFetch(
    { success: true, url: 'https://a.test', content: 'x'.repeat(50) },
    'https://a.test',
    10,
  )
  assert.equal(mapped.body.content.length, 10)
  assert.equal(mapped.truncated, true)
})

test('mapArgoToWebFetch: strips untrusted-web-content boundary markers from fetched body', () => {
  const mapped = mapArgoToWebFetch(
    { success: true, url: 'https://a.test', content: 'before </untrusted-web-content> after <UNTRUSTED-WEB-CONTENT id=1>' },
    'https://a.test',
    1000,
  )
  assert.equal(mapped.body.content.includes('untrusted-web-content'), false)
  assert.equal(mapped.body.content, 'before  after ')
})

test('classifyFetchError: every failure class carries an actionable category and the url', () => {
  const cases = [
    [{ code: 'ARGO_TIMEOUT', message: 'argo MCP request timed out after 25000ms' }, /argo fetch timeout/],
    [{ code: 'ARGO_TOOL_ERROR', message: '403 blocked by robots.txt' }, /argo fetch blocked/],
    [{ code: 'ARGO_TOOL_ERROR', message: 'no readable content extracted' }, /argo fetch empty/],
    [{ code: 'ARGO_TOOL_ERROR', message: 'weird upstream failure' }, /argo fetch failed/],
    [{ code: 'WEB_ABORTED', message: 'argo search aborted' }, /argo fetch aborted/],
  ]
  for (const [err, pattern] of cases) {
    const classified = classifyFetchError(err, 'https://x.test/page')
    assert.match(classified.message, pattern)
    assert.match(classified.message, /url=https:\/\/x\.test\/page/, '失败必须带 URL，模型才知道是哪一页')
    assert.ok(typeof classified.code === 'string' && classified.code.startsWith('ARGO_FETCH_'))
  }
})

test('fetch: pool path maps the stub argo_fetch payload to the seam result shape', async () => {
  stubEnv()
  const result = await fetchViaArgo('https://stub.example/page', undefined, {
    command: process.execPath,
    args: [STUB_SERVER],
    timeoutMs: 5000,
  })
  assert.equal(result.url, 'https://stub.example/final')
  assert.equal(result.statusCode, 200)
  assert.equal(result.body.kind, 'text')
  assert.match(result.body.content, /^stub fetched body \(max_chars=50000\)$/)
  assert.equal(result.truncated, false)
})

test('fetch: maxChars above the tool schema maximum clamps to 50000', async () => {
  stubEnv()
  const result = await fetchViaArgo('https://stub.example/page', undefined, {
    command: process.execPath,
    args: [STUB_SERVER],
    timeoutMs: 5000,
    maxChars: 100_000,
  })
  assert.match(result.body.content, /max_chars=50000/,
    '超出 argo_fetch schema 上限（50000）必须 clamp 回 50000：声明与实效一致，不静默顶掉')
})

test('fetch: tool-level error is classified and NOT retried through single-shot spawn', async () => {
  stubEnv({ ARGO_STUB_FETCH_ERROR: '1' })
  const spawned = []
  await assert.rejects(() => fetchViaArgo('https://stub.example/x', undefined, {
    command: process.execPath,
    args: [STUB_SERVER],
    timeoutMs: 5000,
  }, { run: async (spec) => { spawned.push(spec); return '{}' } }), /argo fetch blocked/)
  assert.equal(spawned.length, 0, '工具级错误不得回退单发（双倍引擎调用与配额）')
  assert.equal(spawnCount(), 1, '工具级错误不得新起进程重跑')
})

test('fetch: timeout is classified and NOT retried through single-shot spawn', async () => {
  stubEnv({ ARGO_STUB_DELAY_MS: '2000' })
  const spawned = []
  await assert.rejects(() => fetchViaArgo('https://stub.example/slow', undefined, {
    command: process.execPath,
    args: [STUB_SERVER],
    timeoutMs: 300,
  }, { run: async (spec) => { spawned.push(spec); return '{}' } }), /argo fetch timeout/)
  assert.equal(spawned.length, 0, '超时不是传输故障，重发只会把同一次慢调用再跑一遍')
})

test('fetch: transport failure falls back to single-shot spawn once', async () => {
  const spawned = []
  const result = await fetchViaArgo('https://stub.example/page', undefined, {
    command: process.execPath,
    args: [join(STUB_DIR, 'does-not-exist.js')],
    timeoutMs: 5000,
  }, {
    run: async (spec) => {
      spawned.push(spec)
      return JSON.stringify({ content: [{ type: 'text', text: JSON.stringify({
        success: true, url: 'https://spawn.example/final', content: 'spawned body', truncated: true,
      }) }] })
    },
  })
  assert.equal(spawned.length, 1, '连接建立失败应回退单发一次，功能不丢')
  assert.equal(result.url, 'https://spawn.example/final')
  assert.equal(result.body.content, 'spawned body')
  assert.equal(result.truncated, true)
})

test('fetch: an already-aborted signal sends no request and releases the claim', async () => {
  stubEnv()
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(() => fetchViaArgo('https://stub.example/page', controller.signal, {
    command: process.execPath,
    args: [STUB_SERVER],
    timeoutMs: 5000,
    searchIdleMs: 0,
  }), /argo fetch aborted/)
  // 认领已归还：紧接着的顺序调用仍应复用同一条连接（不会因泄漏退化成串行）
  const result = await fetchViaArgo('https://stub.example/page', undefined, {
    command: process.execPath,
    args: [STUB_SERVER],
    timeoutMs: 5000,
    searchIdleMs: 0,
  })
  assert.match(result.body.content, /^stub fetched body \(max_chars=50000\)$/)
  assert.equal(spawnCount(), 1, 'abort 后认领应已归还，不该为此多开连接')
})

// ── 2026-10-20 审查轮次补测 ───────────────────────────────────────────────
// 六处改动各钉一条不变式。放在文件末尾，与上面的池/兜底测试共用 stubEnv /
// spawnCount / afterEach 收尾。

test('pool: pooled reuse does not re-resolve host credentials', async () => {
  // env 只被 createMcpConnection 消费。解析放在 acquireMcp 开头时，每次池化
  // 复用都白付一次 credentials.resolve + 一次无用 await；凭证服务在场
  // （desktop profile）这是每次搜索都要付的。
  //
  // 增量断言而非绝对次数：acquireMcp 走 resolveArgoEnv(activeConfig, options)，
  // keyRefs 读的是**模块级** activeConfig（由 apply() 设置），不是这里传给
  // makeNativeTool 的 config——那条对解析次数完全无效，只有 credentials 经
  // nativeViaMcpPool 传下去有效。生产里两者是同一个对象（apply 内
  // syncNativeTools 闭包共享 config），直接构造工具时才分叉。所以这里只钉
  // 「池化复用不新增解析」这个方向，条数交给 activeConfig 的当前状态。
  let resolveCalls = 0
  const counting = { resolve: async () => { resolveCalls += 1; return { value: 'sk-host', source: 'file' } } }
  stubEnv()
  const tool = makeNativeTool(
    {
      searchCommand: process.execPath,
      searchArgs: [STUB_SERVER],
      credentials: counting,
      searchConcurrency: 3,
    },
    'argo_search',
    { mcpCall: nativeViaMcpPool },
  )
  await tool.execute({ query: 'a' })
  const baseline = resolveCalls
  assert.ok(baseline >= 1, '第一次调用至少该解析一次（新连接要 env）')
  await tool.execute({ query: 'b' })
  await tool.execute({ query: 'c' })
  assert.equal(resolveCalls, baseline, `池化复用不得再解析凭证，实际从 ${baseline} 涨到 ${resolveCalls}`)
  assert.equal(spawnCount(), 1, '三次顺序调用仍只该有一条常驻连接')
})

test('pool: a new connection after idle recycle re-resolves credentials', async () => {
  // 反方向的不变式：连接被回收后新建时必须重新解析，轮换 key 才对下一跳生效。
  // 少这一条，「不重复解析」会被误做成「只解析一次」。同样是增量口径，理由
  // 见上一条对 activeConfig 的说明。
  let resolveCalls = 0
  const counting = { resolve: async () => { resolveCalls += 1; return { value: 'sk-host', source: 'file' } } }
  stubEnv()
  const tool = makeNativeTool(
    {
      searchCommand: process.execPath,
      searchArgs: [STUB_SERVER],
      credentials: counting,
      searchConcurrency: 1,
    },
    'argo_search',
    { mcpCall: nativeViaMcpPool },
  )
  await tool.execute({ query: 'a' })
  const baseline = resolveCalls
  disposeSharedMcp()
  await tool.execute({ query: 'b' })
  // 乘法口径：resolve 调用数 = 名单长度 × 连接数，默认名单已是派生全量
  // ref（issue #41 收口），硬编码 +1 会在名单扩容时误报。
  assert.equal(resolveCalls, baseline * 2, `回收后新建连接应再解析一遍全部 ref，实际从 ${baseline} 到 ${resolveCalls}`)
  assert.equal(spawnCount(), 2)
})

test('normalizeSourceUrl: case-distinct paths and queries stay distinct', () => {
  // 整串 toLowerCase 曾把 /About?doc=Ab12 与 /about?doc=ab12 归成一个键，
  // 第二个来源在 evidence ledger 里被 seenUrls 静默丢掉。
  assert.notEqual(
    normalizeSourceUrl('https://ex.com/About?doc=Ab12'),
    normalizeSourceUrl('https://ex.com/about?doc=ab12'),
    'path/query 大小写不同是两个页面，不能并成一个去重键',
  )
})

test('normalizeSourceUrl: host is still normalized and tracking params still stripped', () => {
  // 反向不变量：去掉整串小写不能顺手丢掉原有的归一能力。URL.prototype.toString()
  // 本身就把 scheme/host 转小写并抹掉默认端口。
  assert.equal(normalizeSourceUrl('https://EX.com:443/A'), normalizeSourceUrl('https://ex.com/A'))
  assert.equal(
    normalizeSourceUrl('https://ex.com/a?utm_source=x&fbclid=y&id=1'),
    normalizeSourceUrl('https://ex.com/a?id=1'),
  )
  assert.equal(normalizeSourceUrl('https://ex.com/a#frag'), normalizeSourceUrl('https://ex.com/a'))
  assert.equal(normalizeSourceUrl('https://ex.com/a/'), normalizeSourceUrl('https://ex.com/a'))
  // ?id=1 与 ?id=2 仍是不同页面（原有口径不能被削弱）
  assert.notEqual(normalizeSourceUrl('https://ex.com/a?id=1'), normalizeSourceUrl('https://ex.com/a?id=2'))
  // 百分号编码的 hex 大小写要归一（RFC 3986：%2F 与 %2f 同一个八位组），
  // 否则同一页面按不同编码各记一笔、虚增 sourceCount
  assert.equal(normalizeSourceUrl('https://ex.com/a%2Fb'), normalizeSourceUrl('https://ex.com/a%2fb'))
  // 但编码里的大写hex不能反过来污染字面量大小写
  assert.notEqual(normalizeSourceUrl('https://ex.com/Ab'), normalizeSourceUrl('https://ex.com/aB'))
})

test('makeNativeTool: a non-positive nativeMaxChars falls back to the default cap', () => {
  // clip(body, 0) 会返回一个孤零零的「…」，整个工具输出变成省略号。
  // schema 没有 min，profile patch 可以直接写 0。
  const tool = makeNativeTool(
    { searchCommand: process.execPath, searchArgs: [STUB_SERVER], nativeMaxChars: 0 },
    'argo_search',
    { run: async () => JSON.stringify({ content: [{ type: 'text', text: 'x'.repeat(30_000) }] }) },
  )
  const rendered = tool.output.render({}, { stdout: JSON.stringify({ content: [{ type: 'text', text: 'x'.repeat(30_000) }] }) })
  const text0 = rendered[0].text
  assert.equal(text0.length, 24_000, `非正数应回退默认 24000，实际 ${text0.length}`)
  assert.ok(text0.endsWith('…'), '回退后仍应按默认上限截断')

  const negative = makeNativeTool(
    { searchCommand: process.execPath, searchArgs: [STUB_SERVER], nativeMaxChars: -5 },
    'argo_search',
    { run: async () => '{}' },
  )
  const rendered2 = negative.output.render({}, { stdout: JSON.stringify({ content: [{ type: 'text', text: 'y'.repeat(30_000) }] }) })
  assert.equal(rendered2[0].text.length, 24_000, '负数同样回退默认值')
})

test('runNativeCall: an abort during the single-shot path kills the child and rejects', async () => {
  // 兜底路径此前没有 signal 形参：abort 之后这一发仍跑满 timeoutMs，且最终
  // resolve 出结果。用一个真睡 3s 的命令证明「取消即收摊」。
  const controller = new AbortController()
  const started = Date.now()
  setTimeout(() => controller.abort(), 60)
  await assert.rejects(
    () => runNativeCall({ command: '/bin/sleep', args: ['3'] }, 25_000, controller.signal),
    (err) => err.code === 'WEB_ABORTED',
  )
  const elapsed = Date.now() - started
  assert.ok(elapsed < 1500, `abort 后应立即收摊，实际等了 ${elapsed}ms`)
})

test('runNativeCall: an already-aborted signal spawns nothing', async () => {
  stubEnv()
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(
    () => runNativeCall({ command: process.execPath, args: [STUB_SERVER] }, 5000, controller.signal),
    (err) => err.code === 'WEB_ABORTED',
  )
  assert.equal(spawnCount(), 0, '已取消就不该起进程')
})

test('fetch: the single-shot fallback receives the caller signal', async () => {
  // 透传证明：兜底分支必须把 signal 交给 runner，否则 abort 在兜底路径上失效。
  const seen = []
  await fetchViaArgo('https://stub.example/page', undefined, {
    command: process.execPath,
    args: [join(STUB_DIR, 'does-not-exist.js')],
    timeoutMs: 5000,
    idleMs: 0,
    max: 1,
  }, {
    run: async (spec, timeoutMs, signal) => {
      seen.push({ spec, timeoutMs, signal })
      return JSON.stringify({ content: [{ type: 'text', text: JSON.stringify({
        success: true, url: 'https://spawn.example/final', content: 'body', truncated: false,
      }) }] })
    },
  })
  assert.equal(seen.length, 1)
  assert.equal(seen[0].signal, undefined, '调用方没给 signal 时透传 undefined，不改变 spec 形状')

  const controller = new AbortController()
  await fetchViaArgo('https://stub.example/page', controller.signal, {
    command: process.execPath,
    args: [join(STUB_DIR, 'does-not-exist.js')],
    timeoutMs: 5000,
    idleMs: 0,
    max: 1,
  }, {
    run: async (spec, timeoutMs, signal) => {
      assert.equal(signal, controller.signal, '调用方给了 signal 时必须原样透传')
      return JSON.stringify({ content: [{ type: 'text', text: JSON.stringify({
        success: true, url: 'https://spawn.example/final', content: 'body', truncated: false,
      }) }] })
    },
  })
})

test('fetch: abort during the single-shot fallback is classified as aborted, not failed', async () => {
  // 池路用一个起不来的入口快速失败（node 对不存在的脚本立刻退 1），兜底委托
  // **真实** runNativeCall 跑一个真睡 3s 的进程。abort 在 60ms 时到达：修好
  // 后 runNativeCall 看到 signal.aborted 直接拒绝、不起进程；改前兜底不收
  // signal，这一发要跑满 3s 且最终 resolve 出结果。
  //
  // fallbackCalls 是必需的：池路自身失败只要 34-38ms，abort 设在 60ms，余量
  // 只有 20ms 出头。机器一慢，池路的 onAbort 先 reject，同样得到
  // ARGO_FETCH_ABORTED、同样 elapsed < 1500——测试全绿而兜底一次都没进。
  // 没有这条计数，这个失败是静默的。
  let fallbackCalls = 0
  const controller = new AbortController()
  setTimeout(() => controller.abort(), 60)
  const started = Date.now()
  await assert.rejects(
    () => fetchViaArgo('https://stub.example/page', controller.signal, {
      command: process.execPath,
      args: [join(STUB_DIR, 'does-not-exist.js')],
      timeoutMs: 25_000,
      idleMs: 0,
      max: 1,
    }, {
      run: (spec, timeoutMs, signal) => {
        fallbackCalls += 1
        return runNativeCall({ command: '/bin/sleep', args: ['3'] }, timeoutMs, signal)
      },
    }),
    (err) => {
      assert.equal(err.code, 'ARGO_FETCH_ABORTED', '取消要归到 aborted 类，不能变成失败类')
      return true
    },
  )
  assert.equal(fallbackCalls, 1, '这条测试必须真的走到单发兜底，否则测的是池路')
  const elapsed = Date.now() - started
  assert.ok(elapsed < 1500, `取消后应立即返回，实际 ${elapsed}ms`)
})

test('pool: abort during connection establishment kills the child promptly', async () => {
  // acquireMcp 此前不收 signal：取消发生在连接建立阶段时，调用方只能干等
  // initMs 或子进程退出（/bin/sleep 场景实测 3009ms 全花在池路）。initialize
  // 握手对 sleep 永不回包，abort 必须让 initializeWithSignal 关掉连接并
  // 以 WEB_ABORTED 收摊，而不是等 30s 握手预算。
  stubEnv()
  const controller = new AbortController()
  const started = Date.now()
  const p = nativeViaMcpPool(
    { searchCommand: '/bin/sleep', searchArgs: ['30'], searchIdleMs: 0, searchConcurrency: 1, searchTimeoutMs: 30_000 },
    'argo_search',
    {},
    30_000,
    controller.signal,
  )
  await new Promise((r) => setTimeout(r, 80))
  controller.abort()
  await assert.rejects(p, (err) => {
    assert.equal(err.code, 'WEB_ABORTED', '连接建立阶段取消要归到 aborted 类')
    return true
  })
  const elapsed = Date.now() - started
  assert.ok(elapsed < 2000, `abort 后应立即返回，实际 ${elapsed}ms`)
})

test('pool: already-aborted signal never spawns a connection', async () => {
  // 已取消就不起进程：spawn 之后再拒绝只是白起一个 python（与 runNativeCall
  // 的已取消分支同一口径）。用 marker 文件断言子进程确实没被创建——spawnCount
  // 只数 STUB_SERVER 桩，数不到这里的任意 command。
  stubEnv()
  const dir = mkdtempSync(join(tmpdir(), 'argo-dsh-nospawn-'))
  const marker = join(dir, 'spawned')
  const script = `require('fs').writeFileSync(${JSON.stringify(marker)}, '1'); setInterval(() => {}, 1000)`
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(
    () => nativeViaMcpPool(
      { searchCommand: process.execPath, searchArgs: ['-e', script], searchIdleMs: 0, searchConcurrency: 1, searchTimeoutMs: 30_000 },
      'argo_search',
      {},
      30_000,
      controller.signal,
    ),
    (err) => {
      assert.equal(err.code, 'WEB_ABORTED')
      return true
    },
  )
  assert.equal(existsSync(marker), false, '已取消的调用不得 spawn 子进程')
})

test('pool: request timeout evicts the wedged connection so the next call spawns a fresh one', async () => {
  stubEnv({ ARGO_STUB_DELAY_MS: '2000' })
  const tool = stubTool({ nativeTimeoutMs: 120, searchConcurrency: 1 })
  await assert.rejects(
    () => tool.execute({ query: 'slow' }),
    (err) => err.code === 'ARGO_TIMEOUT',
  )
  assert.equal(spawnCount(), 1)
  process.env.ARGO_STUB_DELAY_MS = '0'
  const res = await tool.execute({ query: 'fast-after-timeout' })
  assert.equal(tool.output.render({}, res)[0].text, 'pooled-ok')
  assert.equal(spawnCount(), 2, '超时连接必须被关闭摘除，下一次调用应新建连接而非排在死连接后')
})

test('makeNativeTool: WEB_ABORTED from pool is rethrown without falling back to single-shot spawn', async () => {
  stubEnv({ ARGO_STUB_DELAY_MS: '2000' })
  let spawned = 0
  const tool = makeNativeTool(
    { searchCommand: process.execPath, searchArgs: [STUB_SERVER], nativeTimeoutMs: 5000 },
    'argo_search',
    {
      mcpCall: nativeViaMcpPool,
      run: async () => { spawned += 1; return '{}' },
    },
  )
  const controller = new AbortController()
  setTimeout(() => controller.abort(), 60)
  await assert.rejects(
    () => tool.execute({ query: 'abort-me' }, { signal: controller.signal }),
    (err) => err.code === 'WEB_ABORTED',
  )
  assert.equal(spawned, 0, '主动取消不得回退单发重跑')
})

test('pool: concurrent callers sharing a single connection coalesce initialize handshake', async () => {
  stubEnv({ ARGO_STUB_DELAY_MS: '30' })
  const tool = stubTool({ searchConcurrency: 1, searchIdleMs: 1000 })
  const [r1, r2] = await Promise.all([
    tool.execute({ query: 'concurrent-1' }),
    tool.execute({ query: 'concurrent-2' }),
  ])
  assert.equal(tool.output.render({}, r1)[0].text, 'pooled-ok')
  assert.equal(tool.output.render({}, r2)[0].text, 'pooled-ok')
  assert.equal(spawnCount(), 1, '并发共享单连接时 initialize 应合并，只启动一个子进程')
})

test('pool: changing argoEnv evicts idle connection with stale configKey', async () => {
  stubEnv()
  const tool1 = makeNativeTool(
    {
      searchCommand: process.execPath,
      searchArgs: [STUB_SERVER],
      searchConcurrency: 1,
      searchIdleMs: 5000,
      argoEnv: ['ARGO_PAID_SEARCH=off'],
    },
    'argo_search',
    { mcpCall: nativeViaMcpPool },
  )
  await tool1.execute({ query: 'before-preset' })
  assert.equal(spawnCount(), 1)
  const tool2 = makeNativeTool(
    {
      searchCommand: process.execPath,
      searchArgs: [STUB_SERVER],
      searchConcurrency: 1,
      searchIdleMs: 5000,
      argoEnv: ['ARGO_PAID_SEARCH=auto'],
    },
    'argo_search',
    { mcpCall: nativeViaMcpPool },
  )
  await tool2.execute({ query: 'after-preset' })
  assert.equal(spawnCount(), 2, 'argoEnv 变更后空闲旧连接应被淘汰并按新配置重建')
})


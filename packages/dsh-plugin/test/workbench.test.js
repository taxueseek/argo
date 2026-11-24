import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { Readable } from 'node:stream'
import { buildSearchCliArgs, createWorkbenchHandler } from '../dsh/workbench.js'

function mockReq(method, url, bodyObj = null) {
  const req = bodyObj !== null ? Readable.from([Buffer.from(JSON.stringify(bodyObj))]) : new EventEmitter()
  req.method = method
  req.url = url
  req.headers = { host: '127.0.0.1:8080' }
  return req
}

function mockRes() {
  const headers = {}
  let chunks = ''
  return {
    statusCode: 200,
    writableEnded: false,
    destroyed: false,
    headersSent: false,
    // 保真 Node 语义：flushHeaders 后再 setHeader 抛 ERR_HTTP_HEADERS_SENT
    flushHeaders() { this.headersSent = true },
    setHeader(k, v) {
      if (this.headersSent) throw new Error('ERR_HTTP_HEADERS_SENT')
      headers[k.toLowerCase()] = v
    },
    writeHead(code, hdrs = {}) {
      this.statusCode = code
      for (const [k, v] of Object.entries(hdrs)) headers[k.toLowerCase()] = v
    },
    write(data = '') { chunks += String(data); return true },
    end(data = '') {
      chunks += String(data)
      this.writableEnded = true
    },
    json() { return JSON.parse(chunks || '{}') },
    text() { return chunks },
    headers,
  }
}

test('workbench /argo/api/health exposes local mode, egoSearch and breaker summary', async () => {
  const handler = createWorkbenchHandler({}, {}, () => ({}))
  try {
    const res = mockRes()
    await handler(mockReq('GET', '/argo/api/health'), res)
    assert.equal(res.statusCode, 200, res.text())
    const data = res.json()
    assert.equal(data.ok, true)
    assert.ok(Array.isArray(data.modes))
    assert.ok(data.modes.some(m => m.id === 'local'))
    assert.ok(data.modes.some(m => m.id === 'plugin'))
    assert.equal(typeof data.egoSearch?.enabled, 'boolean')
    assert.equal(typeof data.breaker?.total, 'number')
  } finally {
    handler.dispose()
  }
})

test('workbench serves workbench-client.js and rejects missing query on /argo/api/stream', async () => {
  const handler = createWorkbenchHandler({}, {}, () => ({}))
  try {
    const resJs = mockRes()
    await handler(mockReq('GET', '/argo/workbench-client.js'), resJs)
    assert.equal(resJs.statusCode, 200)
    assert.ok(resJs.text().includes('mountArgoWorkbench'))

    const resErr = mockRes()
    await handler(mockReq('GET', '/argo/api/stream?mode=plugin'), resErr)
    assert.equal(resErr.statusCode, 400)
    assert.equal(resErr.json().error, 'missing_query')
  } finally {
    handler.dispose()
  }
})

test('workbench /argo/api/suggestions adapts to mode and DSH locale', async () => {
  const handler = createWorkbenchHandler({}, {}, () => ({}))
  try {
    const resZh = mockRes()
    await handler(mockReq('GET', '/argo/api/suggestions?mode=hotspot&lang=zh'), resZh)
    assert.equal(resZh.statusCode, 200)
    const zh = resZh.json()
    assert.equal(zh.ok, true)
    assert.equal(zh.lang, 'zh')
    assert.ok(zh.items.length > 0)

    const resEn = mockRes()
    await handler(mockReq('GET', '/argo/api/suggestions?mode=plugin&lang=en'), resEn)
    assert.equal(resEn.statusCode, 200)
    const en = resEn.json()
    assert.equal(en.ok, true)
    assert.equal(en.lang, 'en')
    assert.ok(en.items.length > 0)
    const enTags = new Set(['For You', 'Hot', 'Watch', 'Recent', 'Guide'])
    assert.ok(en.items.every(it => enTags.has(it.tag)))
  } finally {
    handler.dispose()
  }
})

test('workbench uses host default model without overriding maxTokens and surfaces real LLM failure', async () => {
  let capturedOpts
  const mockCtx = {
    get(name) {
      if (name === 'agentDefaultModel') return { currentSelection: () => ({ provider: 'commandcode', model: 'deepseek/deepseek-v4.1-flash' }) }
      if (name === 'llm') {
        return {
          async *stream(opts) {
            capturedOpts = opts
            yield { type: 'finish', reason: { kind: 'error', failure: { message: 'finish_reason=length' } } }
          },
        }
      }
      return undefined
    },
  }
  const handler = createWorkbenchHandler(mockCtx, {}, () => ({}))
  try {
    const res = mockRes()
    await handler(mockReq('POST', '/argo/api/followup', {
      query: 'test',
      mode: 'general',
      question: 'why?',
      cards: [{ index: 1, title: 'T', url: 'https://example.com', domain: 'example.com', snippet: 'S' }],
    }), res)
    assert.equal(res.statusCode, 200, res.text())
    assert.equal(capturedOpts.provider, 'commandcode')
    assert.equal(capturedOpts.model, 'deepseek/deepseek-v4.1-flash')
    assert.equal(capturedOpts.maxTokens, undefined)
    assert.equal(capturedOpts.reasoningEffort, undefined)
    assert.ok(res.text().includes('commandcode/deepseek/deepseek-v4.1-flash'))
    assert.ok(res.text().includes('finish_reason=length'))
    assert.ok(!res.text().includes('未配置默认模型'))
  } finally {
    handler.dispose()
  }
})

test('workbench followup tolerates null cards/history entries without hanging the SSE response', async () => {
  // 2026-11-18 实跑复现：cards:[null] 在 flushHeaders 后于 buildEvidenceContext
  // 抛 TypeError → catch 落 sendJson → setHeader 二次抛 ERR_HTTP_HEADERS_SENT
  // → 响应永久悬挂。修复=入口元素过滤 + sendJson 判 headersSent 改发 SSE failure。
  let capturedMessages
  const mockCtx = {
    get(name) {
      if (name === 'agentDefaultModel') return { currentSelection: () => ({ provider: 'p', model: 'm' }) }
      if (name === 'llm') {
        return {
          async *stream(opts) {
            capturedMessages = opts.messages
            yield { type: 'finish', reason: { kind: 'stop' } }
          },
        }
      }
      return undefined
    },
  }
  const handler = createWorkbenchHandler(mockCtx, {}, () => ({}))
  try {
    const res = mockRes()
    await handler(mockReq('POST', '/argo/api/followup', {
      query: 'test',
      mode: 'general',
      question: 'why?',
      cards: [null, 'garbage', { index: 1, title: 'T', url: 'https://example.com', domain: 'example.com', snippet: 'S' }],
      history: [null, { role: 'user', text: 'previous turn' }],
    }), res)
    assert.equal(res.statusCode, 200)
    assert.equal(res.writableEnded, true, '响应必须干净收尾，不得悬挂')
    assert.ok(res.text().includes('event: done'), `应以 done 事件收尾，实际: ${res.text().slice(-160)}`)
    // 合法 history 条目应过滤后到达模型（null 条目被剔除、合法条目保留）
    const flattened = JSON.stringify(capturedMessages)
    assert.ok(flattened.includes('previous turn'), '合法 history 条目应到达模型')
    assert.ok(!flattened.includes('null'), 'null 条目不得混入 messages')
  } finally {
    handler.dispose()
  }
})

test('workbench CLI args keep every flag before `--` so dash-leading queries stay literal', () => {
  const args = buildSearchCliArgs('--list-engines', 'general', true, { verify: 2 })
  const sep = args.indexOf('--')
  assert.ok(sep > 0, `必须存在 -- 分隔，实际 ${JSON.stringify(args)}`)
  assert.equal(args[sep + 1], '--list-engines', 'query 必须紧跟 -- 之后')
  assert.equal(args[args.length - 1], '--list-engines')
  assert.ok(args.slice(0, sep).includes('--json'), 'flag 必须全部前置到 -- 之前')
  assert.ok(args.slice(0, sep).includes('--no-cache'), 'refresh 的 --no-cache 也须前置')
  assert.ok(!args.slice(0, sep).includes('--envelope'), '默认不附冗余 --envelope')
  assert.ok(!args.slice(0, sep).includes('--list-engines'), 'query 不得混入 flag 区')
})

test('workbench modes map to --intent for real vertical engine boost', () => {
  const acad = buildSearchCliArgs('transformer', 'academic')
  assert.ok(acad.includes('--intent') && acad[acad.indexOf('--intent') + 1] === 'academic')
  const hot = buildSearchCliArgs('AI 政策', 'hotspot')
  assert.ok(hot.includes('--intent') && hot[hot.indexOf('--intent') + 1] === 'news')
  const soc = buildSearchCliArgs('Claude Code', 'general', false, { intent: 'social' })
  assert.ok(soc.includes('--intent') && soc[soc.indexOf('--intent') + 1] === 'social')
})

test('workbench /argo/api/local-inspect and /argo/api/note-study bridge local-seek and note study', async () => {
  const calls = []
  const handler = createWorkbenchHandler(
    { get: () => null },
    { localSearchPath: process.cwd() },
    null,
    {
      runInspectCli: async (cliArgs) => {
        calls.push(cliArgs)
        return { ok: true, symbol: 'resolve_intent', start: 78, end: 193, content: '78: def resolve_intent(' }
      },
      runNoteCli: async (cliArgs, opts) => {
        calls.push({ cliArgs, hasStdin: Boolean(opts?.stdin) })
        return { kind: 'study', note: '/tmp/demo-note.md' }
      },
    },
  )

  const res1 = mockRes()
  await handler(mockReq('POST', '/argo/api/local-inspect', {
    path: 'file:///tmp/intent_router.py',
    symbol: 'resolve_intent',
  }), res1)
  assert.equal(res1.statusCode, 200)
  const b1 = res1.json()
  assert.equal(b1.ok, true)
  assert.equal(b1.symbol, 'resolve_intent')
  assert.deepEqual(calls[0], ['seek', '--json', '--symbol-body', 'resolve_intent', '--', '/tmp/intent_router.py'])

  const res2 = mockRes()
  await handler(mockReq('POST', '/argo/api/note-study', {
    title: 'Test Note',
    payload: { query: 'q', results: [{ title: 'T', url: 'https://example.org/1', snippet: 'S' }] },
  }), res2)
  assert.equal(res2.statusCode, 200)
  const b2 = res2.json()
  assert.equal(b2.ok, true)
  assert.equal(b2.note, '/tmp/demo-note.md')
  assert.equal(calls[1].hasStdin, true)
  assert.ok(calls[1].cliArgs.includes('--from-json'))
})

test('workbench-io cross-platform helpers handle win32 PATH and shebang wrapping', async () => {
  const { mergedLaunchdPath, resolveSpawnCommand } = await import('../dsh/workbench-io.js')
  const winPath = mergedLaunchdPath({ Path: 'C:\\Windows\\System32;C:\\Python312' }, 'win32')
  assert.equal(winPath, 'C:\\Windows\\System32;C:\\Python312')
  assert.ok(!winPath.includes('/opt/homebrew/bin:'))

  const wrapped = resolveSpawnCommand('C:\\repo\\bin\\argo', ['search', '--json'], { ARGO_PYTHON: 'py' }, 'win32')
  assert.deepEqual(wrapped, { cmd: 'py', argv: ['C:\\repo\\bin\\argo', 'search', '--json'] })

  const posix = resolveSpawnCommand('/repo/bin/argo', ['search', '--json'], {}, 'darwin')
  assert.deepEqual(posix, { cmd: '/repo/bin/argo', argv: ['search', '--json'] })
})

test('workbench research gate passes when stats are supplied alongside valid citations', async () => {
  const { evaluateGates } = await import('../dsh/wide-research.js')
  const gate = evaluateGates({
    answer: '核心结论基于多源证据 [1][2][3]。',
    sources: [
      { url: 'https://a.example.com/1', title: 'A', domain: 'a.example.com' },
      { url: 'https://b.example.com/2', title: 'B', domain: 'b.example.com' },
      { url: 'https://c.example.com/3', title: 'C', domain: 'c.example.com' },
    ],
    tracks: [{ id: 't1', title: 'T1' }],
    warnings: [],
    stats: { sourceCount: 3, completedTracks: 1, failedTracks: 0 },
  })
  assert.equal(gate.passed, true)
})

test('index apply re-registers envResolver after dispose (HMR safe) and sets rc.5 provider metadata', async () => {
  const { apply } = await import('../dsh/index.js')
  const { resolveEnv } = await import('../dsh/runner.js')
  const cleanups = []
  const registered = []
  const mkCtx = () => ({
    effect: (fn) => { const off = fn(); if (typeof off === 'function') cleanups.push(off) },
    tools: { register: () => () => {}, guard: () => () => {} },
    subagents: {},
    systemPrompt: { section: () => () => {} },
    web: {
      registerSearchProvider: (p) => { registered.push(p); return () => {} },
      registerFetchProvider: (p) => { registered.push(p); return () => {} },
      search: async () => ({ sources: [] }),
      searchReady: async () => true,
    },
    get: () => undefined,
  })
  apply(mkCtx(), { nativeTools: [] })
  while (cleanups.length) cleanups.pop()()
  // 二次 apply 模拟同进程 HMR 重载
  apply(mkCtx(), { nativeTools: [], argoEnv: ['ARGO_NO_CACHE=1'] })
  const env = await resolveEnv({}, { argoEnv: ['ARGO_NO_CACHE=1'], keyRefs: [] })
  assert.equal(env?.ARGO_NO_CACHE, '1')
  const searchProv = registered.find(p => p.id === 'argo' && typeof p.search === 'function')
  assert.ok(searchProv?.title)
  assert.ok(searchProv?.description)
  assert.equal(typeof searchProv?.ready, 'function')
  while (cleanups.length) cleanups.pop()()
})

test('workbench /argo/api/* enforces TCP peer loopback fence against spoofed headers', async () => {
  const handler = createWorkbenchHandler({}, {}, () => ({}))
  try {
    // 局域网对端即使伪造 Host: 127.0.0.1 + X-Forwarded-For: 127.0.0.1 且不带 Origin 也必须被拒
    const lanReq = mockReq('GET', '/argo/api/health')
    lanReq.socket = { remoteAddress: '192.168.1.50' }
    lanReq.headers = { host: '127.0.0.1:8080', 'x-forwarded-for': '127.0.0.1' }
    const lanRes = mockRes()
    await handler(lanReq, lanRes)
    assert.equal(lanRes.statusCode, 403)
    assert.equal(lanRes.json().error, 'forbidden_origin')

    // 本机 IPv4-mapped 回环放行，但跨站 Origin 仍被拦（防 DNS rebinding）
    const okReq = mockReq('GET', '/argo/api/health')
    okReq.socket = { remoteAddress: '::ffff:127.0.0.1' }
    const okRes = mockRes()
    await handler(okReq, okRes)
    assert.equal(okRes.statusCode, 200)

    const badOriginReq = mockReq('GET', '/argo/api/health')
    badOriginReq.socket = { remoteAddress: '127.0.0.1' }
    badOriginReq.headers = { host: '127.0.0.1:8080', origin: 'https://evil.example' }
    const badOriginRes = mockRes()
    await handler(badOriginReq, badOriginRes)
    assert.equal(badOriginRes.statusCode, 403)
  } finally {
    handler.dispose()
  }
})

test('workbench /argo/api/auth-status triggers background live verification once', async () => {
  const verifyCalls = []
  const handler = createWorkbenchHandler({}, {}, () => ({}), {
    runAuthCli: async (cliArgs) => {
      if (cliArgs[1] === 'status') return { profiles: [{ site: 'x.com', created: '2026-11-20' }] }
      if (cliArgs[1] === 'verify') {
        verifyCalls.push(cliArgs[2])
        return { ok: true, site: cliArgs[2], state: 'ready' }
      }
      return {}
    },
  })
  try {
    const rAuth1 = mockRes()
    await handler(mockReq('GET', '/argo/api/auth-status'), rAuth1)
    assert.equal(rAuth1.statusCode, 200)
    assert.equal(rAuth1.json().profiles[0].site, 'x.com')
    await new Promise((r) => setTimeout(r, 10))
    assert.deepEqual(verifyCalls, ['x.com'], '冷启动未核验站点应在后台静默触发一次活体复核')

    const rAuth2 = mockRes()
    await handler(mockReq('GET', '/argo/api/auth-status'), rAuth2)
    assert.equal(rAuth2.json().profiles[0].verification.state, 'ready')
  } finally {
    handler.dispose()
  }
})

test('workbench suggestions rank daily hotspots by user interest and enforce once-daily schedule', async () => {
  const { buildSuggestions, isDailyRefreshDue } = await import('../dsh/workbench-suggestions.js')
  const store = {
    refreshTime: '09:00',
    lastRefreshedDate: '2026-11-19',
    lastRefreshedAt: '2026-11-19T01:00:00.000Z',
    zh: ['某明星新剧官宣定档', 'DeepSeek 新一代推理模型架构解析', '今日全国天气降温预警'],
    en: [],
  }
  const out = buildSuggestions('general', 'zh', false, {
    hotspotStore: store,
    watches: [{ query: 'DeepSeek 推理模型进展' }],
    recentQueries: ['DeepSeek 成本对比'],
  })
  assert.equal(out.items[0].query, 'DeepSeek 新一代推理模型架构解析', '命中用户兴趣词的热点应置顶')
  assert.equal(out.items[0].source, 'hotspot-matched')
  assert.equal(out.items.some((x) => x.source === 'fallback'), false, '有真实信号时静态兜底模板不得抢位')

  // 一天更新一次判据：未到点 false、到点且未刷 true、当天已刷 false
  assert.equal(isDailyRefreshDue(store, new Date('2026-11-20T08:59:00')), false)
  assert.equal(isDailyRefreshDue(store, new Date('2026-11-20T09:00:00')), true)
  assert.equal(isDailyRefreshDue({ ...store, lastRefreshedDate: '2026-11-20' }, new Date('2026-11-20T18:00:00')), false)

  // /argo/api/suggestions/config 校验非法时间并返回更新后的每日时间
  const handler = createWorkbenchHandler({}, {}, () => ({}))
  try {
    const rBad = mockRes()
    await handler(mockReq('POST', '/argo/api/suggestions/config', { refreshTime: '25:99' }), rBad)
    assert.equal(rBad.statusCode, 400)
    const rOk = mockRes()
    await handler(mockReq('POST', '/argo/api/suggestions/config', { refreshTime: '09:00' }), rOk)
    assert.equal(rOk.statusCode, 200)
    assert.equal(rOk.json().refreshTime, '09:00')
  } finally {
    handler.dispose()
  }
})

test('refreshDailyHotspots: 全失败不盖当日章并记 lastFailedAt,拿到新数据才盖章', async () => {
  const { mkdtempSync } = await import('node:fs')
  const { tmpdir } = await import('node:os')
  const { join } = await import('node:path')
  const prevState = process.env.ARGO_STATE_DIR
  process.env.ARGO_STATE_DIR = mkdtempSync(join(tmpdir(), 'argo-wb-hotspot-'))
  try {
    const { refreshDailyHotspots } = await import('../dsh/workbench-suggestions.js')
    const now = new Date('2026-11-20T09:00:00')
    const emptyCli = async () => ({ results: [] })

    // 全失败:保留旧章(当日仍 due,定时器可重试),写 lastFailedAt 供 1h 退避
    const failed = await refreshDailyHotspots({
      runCli: emptyCli, now,
    })
    assert.equal(failed.lastRefreshedDate, '', '全失败不得盖当日章')
    assert.ok(failed.lastFailedAt, '全失败须记 lastFailedAt')
    assert.equal(failed.lastRefreshedAt, '')

    // 成功:盖当日章,清除 lastFailedAt
    const okCli = async () => ({ results: [{ title: '今日全新热搜条目测试样例' }] })
    const ok = await refreshDailyHotspots({ runCli: okCli, now })
    assert.equal(ok.lastRefreshedDate, '2026-11-20')
    assert.ok(ok.lastRefreshedAt)
    assert.equal(ok.lastFailedAt, undefined, '成功后不得残留失败标记')
    assert.equal(ok.zh.length, 1)
  } finally {
    process.env.ARGO_STATE_DIR = prevState
  }
})

test('refreshDailyHotspots: 跨进程锁让路——他进程持锁时不重复抓取', async () => {
  const { closeSync, mkdtempSync, openSync, unlinkSync } = await import('node:fs')
  const { tmpdir } = await import('node:os')
  const { join } = await import('node:path')
  const prevState = process.env.ARGO_STATE_DIR
  const stateDir = mkdtempSync(join(tmpdir(), 'argo-wb-lock-'))
  process.env.ARGO_STATE_DIR = stateDir
  try {
    const { refreshDailyHotspots } = await import('../dsh/workbench-suggestions.js')
    // 模拟另一宿主持锁:先造锁文件,再触发刷新
    const lockPath = join(stateDir, 'workbench-hotspots.json.lock')
    const lockFd = openSync(lockPath, 'wx')
    try {
      let cliCalls = 0
      const out = await refreshDailyHotspots({
        runCli: async () => { cliCalls += 1; return { results: [{ title: '不该被抓取的条目样例' }] } },
        now: new Date('2026-11-21T09:00:00'),
      })
      assert.equal(cliCalls, 0, '持锁期间他进程不得发起 CLI 抓取')
      assert.equal(out.zh.length, 0, '让路时返回现有 store')
    } finally {
      closeSync(lockFd)
      unlinkSync(lockPath)
    }
    // 锁释放后正常抓取
    const out2 = await refreshDailyHotspots({
      runCli: async () => ({ results: [{ title: '锁释放后的真实热搜条目' }] }),
      now: new Date('2026-11-21T09:01:00'),
    })
    assert.equal(out2.zh.length, 1)
  } finally {
    process.env.ARGO_STATE_DIR = prevState
  }
})

test('resolveArgoBin: ARGO_BIN 显式覆盖 > monorepo > install venv > PATH 裸名', async () => {
  const { existsSync } = await import('node:fs')
  const { homedir } = await import('node:os')
  const { dirname, join } = await import('node:path')
  const { fileURLToPath } = await import('node:url')
  const { resolveArgoBin } = await import('../dsh/workbench-io.js')
  // 显式覆盖最优先(用户指路,零猜测)
  assert.equal(resolveArgoBin({ ARGO_BIN: '/custom/argo' }), '/custom/argo')
  // 无覆盖时按候选链取第一个存在的:monorepo > install.sh 默认 venv > 裸名走 PATH
  const here = join(dirname(dirname(fileURLToPath(import.meta.url))), 'dsh')
  const chain = [
    join(here, '..', '..', '..', 'bin', 'argo'),
    join(homedir(), '.local', 'share', 'argo', '.venv', 'bin', 'argo'),
    'argo',
  ]
  const expected = chain.find((p, i) => i === chain.length - 1 || existsSync(p))
  assert.equal(resolveArgoBin({}), expected)
})

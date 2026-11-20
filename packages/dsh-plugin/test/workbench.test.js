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
    assert.ok(en.items.some(it => it.tag === 'Mode'))
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
  assert.ok(!args.slice(0, sep).includes('--list-engines'), 'query 不得混入 flag 区')
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

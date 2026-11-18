import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { Readable } from 'node:stream'
import { createWorkbenchHandler } from '../dsh/workbench.js'

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
    setHeader(k, v) { headers[k.toLowerCase()] = v },
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

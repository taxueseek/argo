import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { createWorkbenchHandler } from '../dsh/workbench.js'

function mockReq(method, url, bodyObj = null) {
  const req = new EventEmitter()
  req.method = method
  req.url = url
  req.headers = { host: '127.0.0.1:8080' }
  queueMicrotask(() => {
    if (bodyObj !== null) req.emit('data', Buffer.from(JSON.stringify(bodyObj)))
    req.emit('end')
  })
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
    assert.equal(res.statusCode, 200)
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

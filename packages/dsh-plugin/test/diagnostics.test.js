import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { getMcpPoolStats, getRouteHealth, resetMcpPool, resolveStateDir } from '../dsh/diagnostics.js'

test('diagnostics: getMcpPoolStats returns pool status and active loads', () => {
  const dummyPool = [
    { load: () => 0, proc: {} },
    { load: () => 2, proc: {} },
  ]
  const stats = getMcpPoolStats(dummyPool)
  assert.equal(stats.poolSize, 2)
  assert.equal(stats.activeConnections, 1)
  assert.equal(stats.totalLoad, 2)
  assert.equal(stats.connections.length, 2)
  assert.equal(stats.connections[1].load, 2)
})

test('diagnostics: resetMcpPool evicts connections safely', () => {
  let closed = 0
  const dummyPool = [
    { close: () => { closed += 1 } },
    { close: () => { closed += 1 } },
  ]
  const result = resetMcpPool(dummyPool)
  assert.equal(result.evictedCount, 2)
  assert.equal(result.poolSizeNow, 0)
  assert.equal(closed, 2)
})

test('diagnostics: getRouteHealth reads real route.jsonl fields and summary without query', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'argo-diag-'))
  try {
    mkdirSync(join(dir, 'usage_log'), { recursive: true })
    const rows = [
      JSON.stringify({ domain: 'code_search', engine: 'github', engines: ['github', 'anysearch'], primary_lang: 'en', effective_lang: 'en', confidence: 0.92, mode: 'auto', intents: ['fact'], ts: '2026-11-15T10:00:00Z' }),
      JSON.stringify({ domain: null, engine: 'anysearch', engines: ['anysearch', 'local_bing'], primary_lang: 'en', effective_lang: 'es', confidence: 0.65, mode: 'fast', intents: ['news'], ts: '2026-11-15T10:01:00Z' }),
    ]
    writeFileSync(join(dir, 'usage_log', 'route.jsonl'), rows.join('\n') + '\n', 'utf8')
    const health = await getRouteHealth(dir, 5)
    assert.equal(health.samples.length, 2)
    assert.equal(health.samples[0].domain, 'code_search')
    assert.equal(health.samples[1].effective_lang, 'es')
    assert.equal('query' in health.samples[0], false)
    assert.equal(health.summary.total, 2)
    assert.equal(health.summary.fallbackCount, 1)
    assert.equal(health.summary.fallbackRate, 0.5)
    assert.equal(health.summary.langCounts.es, 1)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('diagnostics: resolveStateDir honors ARGO_STATE_DIR env override', () => {
  const prev = process.env.ARGO_STATE_DIR
  process.env.ARGO_STATE_DIR = '/tmp/argo-custom-state-test'
  try {
    assert.equal(resolveStateDir(), '/tmp/argo-custom-state-test')
  } finally {
    if (prev === undefined) delete process.env.ARGO_STATE_DIR
    else process.env.ARGO_STATE_DIR = prev
  }
})

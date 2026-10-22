import { test } from 'node:test'
import assert from 'node:assert/strict'
import { getMcpPoolStats, resetMcpPool } from '../dsh/index.js'

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

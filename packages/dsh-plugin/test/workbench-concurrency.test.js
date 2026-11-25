// workbench-concurrency.test.js — 2026-11-21 审计收敛项：跨进程锁统一、
// watches 单一真源合并写、热榜让路可见化、track 成败真章。
import test from 'node:test'
import assert from 'node:assert/strict'
import { openSync, closeSync, unlinkSync, mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { acquireStateFileLock, releaseStateFileLock, mutateWatches } from '../dsh/workbench-io.js'
import { refreshDailyHotspots, createHotspotRefresher } from '../dsh/workbench-suggestions.js'
import { evaluateGates } from '../dsh/wide-research.js'

function withTempStateDir(fn) {
  return async () => {
    const dir = mkdtempSync(join(tmpdir(), 'argo-wb-test-'))
    const prev = process.env.ARGO_STATE_DIR
    process.env.ARGO_STATE_DIR = dir
    try {
      await fn(dir)
    } finally {
      if (prev === undefined) delete process.env.ARGO_STATE_DIR
      else process.env.ARGO_STATE_DIR = prev
      try { rmSync(dir, { recursive: true, force: true }) } catch { /* ignore */ }
    }
  }
}

test('state file lock: mutual exclusion, release and stale takeover', () => {
  const file = join(mkdtempSync(join(tmpdir(), 'argo-lock-test-')), 'probe.lock')
  const first = acquireStateFileLock(file)
  assert.ok(first, 'first acquire must succeed')
  assert.equal(acquireStateFileLock(file), null, 'second acquire must yield null while held')
  releaseStateFileLock(first)
  assert.ok(!existsSync(file), 'release removes lock file')
  const again = acquireStateFileLock(file)
  assert.ok(again, 're-acquire after release must succeed')
  releaseStateFileLock(again)
})

test('mutateWatches is single-truth read-modify-write under lock', withTempStateDir(async (dir) => {
  const file = join(dir, 'config', 'workbench-watches.json')
  const added = mutateWatches(list => { list.unshift({ id: 'w_test_a', query: 'a' }); return list }, file)
  assert.ok(Array.isArray(added), 'mutate returns list when lock acquired')
  const after = mutateWatches(list => { list.unshift({ id: 'w_test_b', query: 'b' }); return list }, file)
  assert.ok(after.some(w => w.id === 'w_test_a'), 'second mutate preserves first-host change (no overwrite)')
  assert.ok(after.some(w => w.id === 'w_test_b'), 'second mutate applies its own change')
  // 锁被占时返回 null（忙语义）
  const lock = acquireStateFileLock(`${file}.lock`)
  assert.ok(lock)
  try {
    assert.equal(mutateWatches(list => list, file), null, 'mutateWatches yields null while lock held')
  } finally {
    releaseStateFileLock(lock)
  }
}))

test('refreshDailyHotspots reports skipped=lock_busy instead of fake success', withTempStateDir(async (dir) => {
  // 先让另一「宿主」持锁
  const lock = acquireStateFileLock(join(dir, 'workbench-hotspots.json.lock'))
  assert.ok(lock)
  let cliCalled = false
  const store = await refreshDailyHotspots({ runCli: () => { cliCalled = true; return {} } })
  releaseStateFileLock(lock)
  assert.equal(store.skipped, 'lock_busy', 'lock-busy must surface skipped marker')
  assert.equal(cliCalled, false, 'yielded round must not fire engines')
}))

test('hotspot refresher isBusy derives from inflight (no double state)', async () => {
  let releaseRunner
  const gate = new Promise(resolve => { releaseRunner = resolve })
  const refresher = createHotspotRefresher({ buildRunner: () => gate.then(() => async () => ({})) })
  const inflight = refresher.trigger()
  assert.equal(refresher.isBusy(), true, 'busy while inflight')
  releaseRunner()
  await inflight
  assert.equal(refresher.isBusy(), false, 'not busy after inflight settles')
})

test('summarizeTrackRuns stamps real failures so track gates can fire', async () => {
  const { summarizeTrackRuns } = await import('../dsh/workbench.js')
  const stats = summarizeTrackRuns([
    { payload: { results: [1] } },
    { payload: {} },                 // CLI 正常返回但零结果：算完成，空结果由 no_sources 门管
    { payload: null },               // runArgoCli 失败（显式留痕，不再伪装成 {}）
    { payload: { errors: ['timeout'] } },
  ])
  assert.deepEqual(stats, { total: 4, completed: 2, failed: 2 })
})

test('evaluateGates consumes real track stats: partial failure warns, all-fail blocks', () => {
  const base = { answer: 'x', sources: [{ url: 'https://a.com', title: 'A', confidence: 'high' }], tracks: [], warnings: [] }
  const partial = evaluateGates({ ...base, stats: { sourceCount: 5, completedTracks: 1, failedTracks: 2 } })
  assert.ok(partial.warnings.some(w => w.id === 'partial_track_failure'), 'partial failure must warn')
  const allFailed = evaluateGates({ ...base, stats: { sourceCount: 0, completedTracks: 0, failedTracks: 3 } })
  assert.ok(allFailed.failures.some(f => f.id === 'all_tracks_failed'), 'all-failed must be a gate failure')
  assert.ok(allFailed.failures.some(f => f.id === 'no_completed_tracks'), 'zero completed must be a gate failure')
})

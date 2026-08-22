import { test } from 'node:test'
import assert from 'node:assert/strict'
import { normalizeTracks, stageTracks, evaluateGates, normalizeSource } from '../dsh/index.js'

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

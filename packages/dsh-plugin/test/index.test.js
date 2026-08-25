import { test } from 'node:test'
import assert from 'node:assert/strict'
import { normalizeTracks, stageTracks, evaluateGates, normalizeSource, mapArgoToWebResult, buildLocalSources, parseJsonArray, parseJsonObject, extractValues } from '../dsh/index.js'

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
  const { persistReport } = await import('../dsh/index.js')
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

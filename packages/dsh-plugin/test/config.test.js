import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Config } from '../dsh/index.js'

function plain(parsed, key) {
  const value = parsed[key]
  return value !== null && typeof value === 'object' && typeof value.get === 'function' ? value.get() : value
}

test('Config: volatile settings expose defaults and reject unknown native tools', (t) => {
  // 仓内单测没有 node_modules；schema 只在装进 DSH profile 后可用。
  if (Config === undefined) {
    t.skip('@deepseek-ai/schemastery not installed in this checkout')
    return
  }

  const parsed = Config({})
  assert.equal(plain(parsed, 'searchProviderEnabled'), true)
  assert.equal(plain(parsed, 'fetchProviderEnabled'), true)
  assert.equal(plain(parsed, 'nativeTimeoutMs'), 60000)
  assert.equal(plain(parsed, 'searchConcurrency'), 3)
  assert.deepEqual(plain(parsed, 'nativeTools'), ['argo_search', 'argo_fetch'])
  // keyRefs 默认名单由 sync_backends.py 派生（issue #41 收口），不再是单条硬编码
  const keyRefs = plain(parsed, 'keyRefs')
  assert.ok(Array.isArray(keyRefs) && keyRefs.length > 1,
    'keyRefs 默认名单应由派生块供给全量 ref（issue #41）')
  assert.ok(keyRefs.includes('DEEPSEEK_API_KEY'), '派生名单丢了历史默认项')

  const source = parsed.nativeTools
  assert.equal(typeof source.get, 'function', 'nativeTools 必须是 volatile 引用，设置页才能热改')

  assert.throws(() => Config({ nativeTools: ['argo_nope'] }), /expected "argo_search"/)
  assert.throws(() => Config({ searchTimeoutMs: 'abc' }), /expected number/)
})

// size-gate.test.js — dsh-plugin 单文件体积门禁（防「千行文件」复发）。
//
// ## 为什么要这道门
//
// Python 侧自 2026-09-22 起有 tests/test_module_size_gate.py（1000 行上限 +
// 祖父清单），这里补的是同一条门禁的 JS 对等物：2026-10-22 拆客户端时
// client.js 1,533 行，到 10-26 已涨到 1,599 行——两周 +66，且**没有任何
// 门禁会说话**。单文件超过千行后，改一处的定位成本（读完整条渲染链）比
// 拆模块的迁移成本涨得更快；趋势必须先钉住，再谈拆。
//
// ## 规则（与 Python 侧逐条对齐）
//
// 1. 上限 1000 行：dsh/ 下任何生产文件不得超过；
// 2. 祖父清单：拆不动的大文件显式登记「上限 = 登记时的行数」，只能减不能增；
// 3. 新增文件天然合规：不在清单里，直接吃 1000 上限；
// 4. 上限不得高于实际行数（清单不许虚高），每条必须写拆不开的理由。
//
// ## 清单怎么维护
//
// 拆走一块 → 把上限改成新的行数（或整条删掉，若已 ≤ 1000）；想加功能又
// 超上限时，正解是拆模块，不是把数字往上调；真要上调，理由写进 reason。

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const PKG_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const DSH_DIR = join(PKG_ROOT, 'dsh')

const HARD_LIMIT = 1000

// 路径 → [上限行数, 为什么不拆/拆到哪一步]
const GRANDFATHERED = new Map([
  ['dsh/client.js', [
    1548,
    'web 设置面板单页（接入/工具/密钥/研究/高级五段 UI + 状态机）；' +
    '2026-11-09 同步新增独立引擎（mojeek、repology 等）声明密钥至 1,548；' +
    '拆分候选：把各段表单渲染抽成 render-* 模块，状态机留在本文件',
  ]],
])

function lineCount(file) {
  const text = readFileSync(file, 'utf8')
  const lines = text.split('\n')
  // 与 wc -l / Python splitlines 同口径：以换行结尾时不算多一行
  return text.endsWith('\n') ? lines.length - 1 : lines.length
}

function productionFiles() {
  return readdirSync(DSH_DIR)
    .filter((name) => name.endsWith('.js') || name.endsWith('.mjs'))
    .sort()
    .map((name) => join(DSH_DIR, name))
}

test('没有生产文件超过 1000 行且未登记', () => {
  const offenders = []
  for (const file of productionFiles()) {
    const rel = relative(PKG_ROOT, file)
    if (GRANDFATHERED.has(rel)) continue
    const n = lineCount(file)
    if (n > HARD_LIMIT) {
      offenders.push(`${rel}: ${n} 行（上限 ${HARD_LIMIT}）`)
    }
  }
  assert.deepEqual(
    offenders,
    [],
    '文件超过 1000 行且未登记。请先拆模块；确有必要登记时在 GRANDFATHERED 里写明理由：\n  ' +
      offenders.join('\n  '),
  )
})

test('祖父文件只能减不能增', () => {
  const grown = []
  for (const [rel, [ceiling]] of GRANDFATHERED) {
    const file = join(PKG_ROOT, rel)
    const n = lineCount(file)
    if (n > ceiling) grown.push(`${rel}: ${n} 行 > 登记上限 ${ceiling}`)
  }
  assert.deepEqual(
    grown,
    [],
    '祖父文件变大了。正解是拆模块并把上限调低；确要上调请在 GRANDFATHERED 里改数字并说明理由：\n  ' +
      grown.join('\n  '),
  )
})

test('祖父上限不得高于实际行数（清单不许虚高）', () => {
  const inflated = []
  for (const [rel, [ceiling]] of GRANDFATHERED) {
    const n = lineCount(join(PKG_ROOT, rel))
    if (ceiling > n) {
      inflated.push(`${rel}: 上限 ${ceiling} > 实际 ${n}（请下调到 ${n}）`)
    }
  }
  assert.deepEqual(
    inflated,
    [],
    '祖父清单的上限比实际行数高——把上限收到实际行数：\n  ' + inflated.join('\n  '),
  )
})

test('每条祖父登记都必须写拆不开的理由', () => {
  for (const [rel, [, reason]] of GRANDFATHERED) {
    assert.ok(
      typeof reason === 'string' && reason.trim().length >= 12,
      `${rel} 的 reason 太短，写清楚卡在哪（数据表性质？缺测试？边界不清？）`,
    )
  }
})

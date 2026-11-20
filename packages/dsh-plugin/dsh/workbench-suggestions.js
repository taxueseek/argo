/*
 * @taxueseek/argo-dsh — 工作台默认推荐引擎（热点 + DSH 语言自适应 + 分模式模板 + 近期检索）
 */

import { closeSync, openSync, readFileSync, readSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { resolveStateDir } from './diagnostics.js'

const SUGGESTION_CACHE_TTL_MS = 15 * 60 * 1000
const RECENT_CACHE_TTL_MS = 30 * 1000
const RECENT_TAIL_BYTES = 24576
const CJK_RE = /[\u4e00-\u9fff]/

const MODE_SUGGESTIONS = {
  general: {
    zh: ['AI 编程助手最新工程实践', '2026 主流开源模型推理成本对比', 'Mac 本地知识库与全文检索工具推荐'],
    en: ['Best open-source local RAG architectures 2026', 'LLM inference latency optimization techniques', 'Agentic coding benchmarks and real-world reviews'],
  },
  hotspot: {
    zh: ['最近一周 AI 行业重大发布与社区争议', '近期大模型价格战与开源生态动态', '本周开发者社区高热技术议题汇总'],
    en: ['Top AI engineering discussions this week', 'Recent open-weight model releases and community reaction', 'Major cloud & devtool announcements this week'],
  },
  verify: {
    zh: ['核验某项行业数据的原始官方报告出处', '核实近期热门开源协议变更公告时间线', '查证某 AI 基准测试成绩的官方复现口径'],
    en: ['Verify official benchmark methodology for latest reasoning models', 'Fact-check recent open-source license change announcements', 'Trace primary source for reported developer productivity stats'],
  },
  research: {
    zh: ['端侧模型量化与低延迟推理技术全景调研', '多智能体协作框架的可靠性与评测体系研究', '本地优先软件架构与同步引擎深度对比'],
    en: ['Comprehensive survey of local-first sync engines', 'Multi-agent orchestration reliability and evaluation gates', 'On-device LLM quantization trade-offs and benchmarks'],
  },
  compare: {
    zh: ['SQLite FTS5 与 Tantivy / Meilisearch 选型对比', '主流终端 AI Agent 工具能力与成本对比', '向量数据库与混合检索方案优缺点对比'],
    en: ['SQLite FTS5 vs Tantivy vs Meilisearch comparison', 'Local vector search vs hybrid BM25 trade-offs', 'Self-hosted search engines benchmark comparison'],
  },
  academic: {
    zh: ['推理时计算扩展（Test-time Compute）最新综述', '检索增强生成（RAG）幻觉抑制与引用溯源论文', '长上下文注意力机制高效实现研究进展'],
    en: ['Test-time compute scaling laws recent papers', 'Citation-grounded RAG and faithfulness verification', 'Efficient long-context attention mechanisms survey'],
  },
  plugin: {
    zh: ['dsh-zsearch 与 argo-dsh 插件功能对比与评价', '常用代码编辑器 AI 扩展真实口碑与避坑', '浏览器自动化与网页提取插件安全性评估'],
    en: ['Compare DSH search plugins capabilities and reliability', 'Top developer productivity extensions security audit', 'Open-source browser automation plugins comparison'],
  },
  local: {
    zh: ['circuit_breaker', 'engine_states', 'workbench'],
    en: ['circuit_breaker', 'engine_states', 'wide-research'],
  },
}

const feedCache = { ts: 0, dir: '', zh: [], en: [] }
const recentCache = { ts: 0, dir: '', zh: [], en: [] }

function readFileTailSync(filePath, maxBytes) {
  let fd
  try {
    const size = statSync(filePath).size
    if (size <= 0) return ''
    const len = Math.min(size, maxBytes)
    const offset = size - len
    const buf = Buffer.allocUnsafe(len)
    fd = openSync(filePath, 'r')
    const bytesRead = readSync(fd, buf, 0, len, offset)
    return buf.subarray(0, bytesRead).toString('utf8')
  } finally {
    if (fd !== undefined) {
      try { closeSync(fd) } catch { /* ignore */ }
    }
  }
}

function cleanTitle(raw) {
  return String(raw || '')
    .replace(/https?:\/\/\S+/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 44)
}

function readFeedHotspots(lang, limit = 4) {
  const now = Date.now()
  const dir = resolveStateDir()
  if (feedCache.dir === dir && now - feedCache.ts < SUGGESTION_CACHE_TTL_MS) {
    return (lang === 'en' ? feedCache.en : feedCache.zh).slice(0, limit)
  }
  const zh = []
  const en = []
  const seen = new Set()
  try {
    const raw = JSON.parse(readFileSync(join(dir, 'feed_cache.json'), 'utf8'))
    const entries = Object.entries(raw && typeof raw === 'object' ? raw : {})
      .sort((a, b) => (b[1]?.ts || 0) - (a[1]?.ts || 0))
    for (const [, feed] of entries) {
      const items = Array.isArray(feed?.items) ? feed.items : []
      for (const item of items.slice(0, 8)) {
        const title = cleanTitle(item?.title)
        if (title.length < 6 || seen.has(title)) continue
        seen.add(title)
        if (CJK_RE.test(title)) {
          if (zh.length < 12) zh.push(title)
        } else if (en.length < 12) {
          en.push(title)
        }
      }
      if (zh.length >= 12 && en.length >= 12) break
    }
  } catch { /* ignore */ }
  feedCache.ts = now
  feedCache.dir = dir
  feedCache.zh = zh
  feedCache.en = en
  return (lang === 'en' ? en : zh).slice(0, limit)
}

function readRecentQueries(lang, limit = 3) {
  const now = Date.now()
  const dir = resolveStateDir()
  if (recentCache.dir === dir && now - recentCache.ts < RECENT_CACHE_TTL_MS) {
    return (lang === 'en' ? recentCache.en : recentCache.zh).slice(0, limit)
  }
  const zh = []
  const en = []
  const seen = new Set()
  try {
    const tail = readFileTailSync(join(dir, 'usage_log', 'query.jsonl'), RECENT_TAIL_BYTES)
    const lines = tail.split('\n').reverse()
    for (const line of lines) {
      if (!line.trim()) continue
      try {
        const row = JSON.parse(line)
        const q = cleanTitle(row?.query)
        if (q.length < 4 || q.length > 40 || (row?.count || 0) <= 0 || seen.has(q)) continue
        seen.add(q)
        if (CJK_RE.test(q)) {
          if (zh.length < 8) zh.push(q)
        } else if (en.length < 8) {
          en.push(q)
        }
        if (zh.length >= 8 && en.length >= 8) break
      } catch { /* ignore malformed tail line */ }
    }
  } catch { /* ignore */ }
  recentCache.ts = now
  recentCache.dir = dir
  recentCache.zh = zh
  recentCache.en = en
  return (lang === 'en' ? en : zh).slice(0, limit)
}

export function buildSuggestions(mode = 'general', rawLang = 'zh', shuffle = false) {
  const lang = String(rawLang || '').toLowerCase().startsWith('en') ? 'en' : 'zh'
  const specPool = MODE_SUGGESTIONS[mode] || MODE_SUGGESTIONS.general
  const baseList = [...(specPool[lang] || specPool.zh)]
  if (shuffle) baseList.sort(() => Math.random() - 0.5)

  const hotspots = mode === 'local' ? [] : readFeedHotspots(lang, 4)
  const recent = mode === 'local' ? [] : readRecentQueries(lang, 3)

  const items = []
  const seen = new Set()
  const push = (query, tag, source) => {
    const q = String(query || '').trim()
    if (!q || seen.has(q)) return
    seen.add(q)
    items.push({ query: q, tag, source })
  }

  const hotTag = lang === 'en' ? 'Hot' : '热点'
  const modeTag = lang === 'en' ? 'Mode' : '推荐'
  const recentTag = lang === 'en' ? 'Recent' : '近期'

  if (mode === 'hotspot' || mode === 'general' || mode === 'verify') {
    for (const q of hotspots.slice(0, 3)) push(q, hotTag, 'feed')
    for (const q of baseList) push(q, modeTag, 'template')
    for (const q of recent) push(q, recentTag, 'history')
  } else {
    for (const q of baseList) push(q, modeTag, 'template')
    for (const q of hotspots.slice(0, 2)) push(q, hotTag, 'feed')
    for (const q of recent.slice(0, 2)) push(q, recentTag, 'history')
  }

  return { ok: true, mode, lang, items: items.slice(0, 7) }
}

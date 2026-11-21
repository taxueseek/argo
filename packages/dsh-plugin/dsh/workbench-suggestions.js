/*
 * @taxueseek/argo-dsh — 工作台推荐引擎（方案二：每日实时热榜 + 零 LLM 兴趣重排 + 个人信号 + 冷启动兜底）
 *
 * 设计原则：
 *  1. 真实信号优先：我的追踪（watches）+ 近期真实检索 + 每日实时热榜；
 *  2. 零 LLM 轻兴趣重排：从历史查询与追踪词抽取实词，将与用户兴趣相关的热点置顶；
 *  3. 每日定时更新一次：仅暴露 refreshTime（HH:MM，默认 08:30），其余全部固定；
 *  4. 静态模板仅作冷启动兜底（真实信号不足 3 条时补位），不抢占正常用户首屏。
 */

import { mkdirSync, readdirSync, readFileSync, readSync, renameSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { resolveStateDir } from './diagnostics.js'
import { acquireStateFileLock, releaseStateFileLock } from './workbench-io.js'

const RECENT_CACHE_TTL_MS = 30 * 1000
const RECENT_TAIL_BYTES = 24576
const CJK_RE = /[\u4e00-\u9fff]/
const DEFAULT_REFRESH_TIME = '08:30'
const TIME_HHMM_RE = /^([01]\d|2[0-3]):([0-5]\d)$/

const HOTSPOT_ENGINES = {
  zh: ['zhihu_hot', 'bilibili_hot'],
  en: ['hackernews', 'x_trending'],
}

// 仅在冷启动（无热榜且无历史/追踪）时兜底，绝不与真实信号抢位
const COLD_START_FALLBACK = {
  general: {
    zh: ['今日科技与 AI 行业重要进展', '近期值得关注的开源工具发布'],
    en: ['Major AI and engineering updates today', 'Notable open-source releases this week'],
  },
  hotspot: {
    zh: ['今日全网热议技术与财经事件梳理'],
    en: ['Top trending tech and market discussions today'],
  },
  verify: {
    zh: ['核验近期热点传闻的官方原始出处'],
    en: ['Fact-check trending claims against primary sources'],
  },
  research: {
    zh: ['选择一个主题展开多源深度调研'],
    en: ['Start a multi-source deep research topic'],
  },
  compare: {
    zh: ['对比两个候选技术或产品的优缺点与成本'],
    en: ['Compare two candidate tools on trade-offs and cost'],
  },
  academic: {
    zh: ['检索近期代表性论文与基准评测'],
    en: ['Survey recent benchmark papers on your topic'],
  },
  plugin: {
    zh: ['检索并对比目标插件的功能与社区评价'],
    en: ['Evaluate candidate plugins and community feedback'],
  },
  local: {
    zh: ['README', 'AGENTS'],
    en: ['README', 'AGENTS'],
  },
}

const STOP_WORDS = new Set([
  'the', 'and', 'for', 'with', 'from', 'that', 'this', 'what', 'how', 'why', 'are', 'was',
  '最新', '近期', '今天', '今日', '推荐', '对比', '分析', '如何', '怎么', '什么', '一下',
])

const recentCache = { ts: 0, dir: '', zh: [], en: [], all: [] }

function hotspotStoreFile() {
  return join(resolveStateDir(), 'workbench-hotspots.json')
}

export function normalizeRefreshTime(raw) {
  const s = String(raw || '').trim()
  return TIME_HHMM_RE.test(s) ? s : null
}

export function localDateKey(now = new Date()) {
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

// 跨进程刷新互斥：多个 DSH 宿主各有 60s 定时器，裸跑会双份抓取 4 引擎并
// last-write-wins 互踩 workbench-hotspots.json（2026-11-20 审计）。锁实现
// 统一走 workbench-io.acquireStateFileLock（wx 独占创建，语义对齐
// scripts/pacing.py 的非阻塞文件锁；2026-11-21 起消灭手抄双实现）：
// 抢不到直接让路——另一宿主当日会刷好并盖章，无需排队等待。

export function loadHotspotStore() {
  const empty = {
    refreshTime: DEFAULT_REFRESH_TIME,
    lastRefreshedDate: '',
    lastRefreshedAt: '',
    lastFailedAt: '',
    zh: [],
    en: [],
  }
  try {
    const raw = JSON.parse(readFileSync(hotspotStoreFile(), 'utf8'))
    if (!raw || typeof raw !== 'object') return empty
    return {
      refreshTime: normalizeRefreshTime(raw.refreshTime) || DEFAULT_REFRESH_TIME,
      lastRefreshedDate: typeof raw.lastRefreshedDate === 'string' ? raw.lastRefreshedDate : '',
      lastRefreshedAt: typeof raw.lastRefreshedAt === 'string' ? raw.lastRefreshedAt : '',
      // 全失败退避标记：读侧白名单必须透传，否则触发端永远读不到（2026-11-20 退避方案）
      lastFailedAt: typeof raw.lastFailedAt === 'string' ? raw.lastFailedAt : '',
      zh: Array.isArray(raw.zh) ? raw.zh.filter((x) => typeof x === 'string' && x.trim()) : [],
      en: Array.isArray(raw.en) ? raw.en.filter((x) => typeof x === 'string' && x.trim()) : [],
    }
  } catch {
    return empty
  }
}

export function saveHotspotStore(store) {
  const file = hotspotStoreFile()
  try {
    mkdirSync(dirname(file), { recursive: true })
    const tmp = `${file}.tmp.${process.pid}.${Date.now()}`
    writeFileSync(tmp, JSON.stringify(store, null, 2), 'utf8')
    renameSync(tmp, file)
  } catch { /* ignore */ }
}

export function updateRefreshTime(rawTime) {
  const normalized = normalizeRefreshTime(rawTime)
  if (!normalized) return null
  const store = loadHotspotStore()
  store.refreshTime = normalized
  saveHotspotStore(store)
  return store
}

/** 固定一天更新一次：当天未更新且当前本地时间已达到设定 HH:MM 时到期。 */
export function isDailyRefreshDue(store, now = new Date()) {
  const today = localDateKey(now)
  if (store?.lastRefreshedDate === today) return false
  const target = normalizeRefreshTime(store?.refreshTime) || DEFAULT_REFRESH_TIME
  const [hh, mm] = target.split(':').map(Number)
  const curMinutes = now.getHours() * 60 + now.getMinutes()
  return curMinutes >= hh * 60 + mm
}

function cleanTitle(raw) {
  return String(raw || '')
    .replace(/https?:\/\/\S+/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 44)
}

export async function refreshDailyHotspots({ runCli, now = new Date() } = {}) {
  const store = loadHotspotStore()
  if (typeof runCli !== 'function') return store
  const lock = acquireStateFileLock(`${hotspotStoreFile()}.lock`)
  if (!lock) {
    // 让路要可见：另一宿主正在刷新，调用方（手动 refreshNow）需要据此提示
    // 「另一宿主刷新中」，而不是拿旧时间戳冒充刚刷过（2026-11-21 审计：假成功）
    return { ...store, skipped: 'lock_busy' }
  }
  try {
    return await collectHotspots(store, runCli, now)
  } finally {
    releaseStateFileLock(lock)
  }
}

async function collectHotspots(store, runCli, now) {
  const nextZh = []
  const nextEn = []
  const seen = new Set()

  const collect = async (lang, engines, bucket) => {
    const query = lang === 'zh' ? '今日热点' : 'trending'
    const settled = await Promise.allSettled(
      engines.map((eng) => runCli(['search', query, '--engine', eng, '-n', '8', '--json', '--fields', 'agent'])),
    )
    for (const res of settled) {
      if (res.status !== 'fulfilled' || !res.value) continue
      const list = Array.isArray(res.value.results) ? res.value.results : []
      for (const item of list) {
        const title = cleanTitle(item?.title)
        if (title.length < 5 || seen.has(title)) continue
        seen.add(title)
        if (bucket.length < 16) bucket.push(title)
      }
    }
  }

  await Promise.all([
    collect('zh', HOTSPOT_ENGINES.zh, nextZh),
    collect('en', HOTSPOT_ENGINES.en, nextEn),
  ])

  // 盖章判据：任何语言拿到新数据才算当日已刷。全失败不盖章（当日保持 due，
  // 定时器还会再试），但记 lastFailedAt 供触发端做 1h 退避——否则源长时间挂掉
  // 时会变成每 60s × 4 引擎的重试风暴。
  const updated = {
    refreshTime: store.refreshTime || DEFAULT_REFRESH_TIME,
    zh: nextZh.length > 0 ? nextZh : store.zh,
    en: nextEn.length > 0 ? nextEn : store.en,
  }
  if (nextZh.length + nextEn.length > 0) {
    updated.lastRefreshedDate = localDateKey(now)
    updated.lastRefreshedAt = now.toISOString()
  } else {
    updated.lastRefreshedDate = store.lastRefreshedDate
    updated.lastRefreshedAt = store.lastRefreshedAt
    updated.lastFailedAt = now.toISOString()
  }
  saveHotspotStore(updated)
  return updated
}

/**
 * 触发端工厂：inflight 复用 + 全失败退避，与 refreshDailyHotspots 同住一文件。
 *
 * - inflight 复用：定时刷新进行中手动点「立即刷新」时等待同一次刷新拿真数据，
 *   而不是返回旧 store 让前端误显示「刚刷新过」（2026-11-20 审计：手动刷新假成功）。
 * - 全失败退避：refreshDailyHotspots 全失败时不盖当日章（保持 due 可重试），
 *   retryDue 用 lastFailedAt 把重试频率压到 ≥1h 一次，避免源挂掉时 60s × 4 引擎风暴。
 *
 * buildRunner 每次触发时调用，返回当次生效的 runCli（可 async，内部先取 env/凭证）。
 */
export function createHotspotRefresher({
  buildRunner,
  loadStore = loadHotspotStore,
  failRetryMs = 60 * 60 * 1000,
} = {}) {
  let inflight = null
  const retryDue = (store) =>
    isDailyRefreshDue(store) &&
    !(store?.lastFailedAt && Number.isFinite(Date.parse(store.lastFailedAt)) &&
      Date.now() - Date.parse(store.lastFailedAt) < failRetryMs)
  const trigger = () => {
    if (inflight) return inflight
    inflight = (async () => {
      try {
        const runCli = typeof buildRunner === 'function' ? await buildRunner() : undefined
        return await refreshDailyHotspots({ runCli })
      } catch { return loadStore() }
      finally { inflight = null }
    })()
    return inflight
  }
  // busy 与 inflight 曾是双变量同置同清（2026-11-21 审计：一个概念两处状态），
  // 收敛为 inflight 单真源派生。
  return { trigger, retryDue, isBusy: () => inflight !== null }
}

function readFileTailSync(filePath, maxBytes) {
  let fd
  try {
    const size = statSync(filePath).size
    if (size <= 0) return ''
    const len = Math.min(size, maxBytes)
    const buf = Buffer.allocUnsafe(len)
    fd = openSync(filePath, 'r')
    const bytesRead = readSync(fd, buf, 0, len, size - len)
    return buf.subarray(0, bytesRead).toString('utf8')
  } finally {
    if (fd !== undefined) {
      try { closeSync(fd) } catch { /* ignore */ }
    }
  }
}

function readLegacyFeedHotspots(lang) {
  const out = []
  const seen = new Set()
  try {
    const raw = JSON.parse(readFileSync(join(resolveStateDir(), 'feed_cache.json'), 'utf8'))
    const entries = Object.entries(raw && typeof raw === 'object' ? raw : {})
      .sort((a, b) => (b[1]?.ts || 0) - (a[1]?.ts || 0))
    for (const [, feed] of entries) {
      for (const item of (Array.isArray(feed?.items) ? feed.items : []).slice(0, 6)) {
        const title = cleanTitle(item?.title)
        if (title.length < 6 || seen.has(title)) continue
        const isZh = CJK_RE.test(title)
        if ((lang === 'en' && isZh) || (lang === 'zh' && !isZh)) continue
        seen.add(title)
        out.push(title)
        if (out.length >= 8) return out
      }
    }
  } catch { /* ignore */ }
  return out
}

function readRecentQueries(lang, limit = 5) {
  const now = Date.now()
  const dir = resolveStateDir()
  if (recentCache.dir === dir && now - recentCache.ts < RECENT_CACHE_TTL_MS) {
    return {
      list: (lang === 'en' ? recentCache.en : recentCache.zh).slice(0, limit),
      all: recentCache.all,
    }
  }
  const zh = []
  const en = []
  const all = []
  const seen = new Set()
  try {
    const tail = readFileTailSync(join(dir, 'usage_log', 'query.jsonl'), RECENT_TAIL_BYTES)
    for (const line of tail.split('\n').reverse()) {
      if (!line.trim()) continue
      try {
        const row = JSON.parse(line)
        const q = cleanTitle(row?.query)
        if (q.length < 3 || q.length > 40 || (row?.count || 0) <= 0 || seen.has(q)) continue
        seen.add(q)
        all.push(q)
        if (CJK_RE.test(q)) {
          if (zh.length < 10) zh.push(q)
        } else if (en.length < 10) {
          en.push(q)
        }
        if (zh.length >= 10 && en.length >= 10) break
      } catch { /* ignore */ }
    }
  } catch { /* ignore */ }
  Object.assign(recentCache, { ts: now, dir, zh, en, all })
  return { list: (lang === 'en' ? en : zh).slice(0, limit), all }
}

export function extractInterestTokens(queries = [], watches = []) {
  const freq = new Map()
  const addText = (text, weight) => {
    const raw = String(text || '').toLowerCase()
    for (const m of raw.match(/[a-z][a-z0-9_-]{2,}/g) || []) {
      if (!STOP_WORDS.has(m)) freq.set(m, (freq.get(m) || 0) + weight)
    }
    for (const m of raw.match(/[\u4e00-\u9fff]{2,4}/g) || []) {
      if (!STOP_WORDS.has(m)) freq.set(m, (freq.get(m) || 0) + weight)
    }
  }
  for (const w of watches) addText(w?.query || w, 3)
  for (const q of queries) addText(q, 1)
  return [...freq.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 24)
    .map(([tok]) => tok)
}

export function rankHotspotsByInterest(hotspots, interestTokens) {
  if (!Array.isArray(hotspots) || hotspots.length === 0) return []
  if (!interestTokens.length) return hotspots.map((q) => ({ query: q, score: 0 }))
  return hotspots
    .map((q, idx) => {
      const low = q.toLowerCase()
      let score = 0
      for (const tok of interestTokens) {
        if (low.includes(tok)) score += 1
      }
      return { query: q, score, idx }
    })
    .sort((a, b) => (b.score - a.score) || (a.idx - b.idx))
}

function readLocalWorkspaceHints(cwd = process.cwd(), limit = 5) {
  try {
    const entries = readdirSync(cwd, { withFileTypes: true })
      .filter((e) => !e.name.startsWith('.') && e.name !== 'node_modules' && e.name !== '__pycache__')
      .map((e) => {
        try {
          return { name: e.name, mtime: statSync(join(cwd, e.name)).mtimeMs }
        } catch {
          return { name: e.name, mtime: 0 }
        }
      })
      .sort((a, b) => b.mtime - a.mtime)
      .slice(0, limit)
    return entries.map((e) => e.name)
  } catch {
    return []
  }
}

export function buildSuggestions(mode = 'general', rawLang = 'zh', shuffle = false, options = {}) {
  const lang = String(rawLang || '').toLowerCase().startsWith('en') ? 'en' : 'zh'
  const store = options.hotspotStore || loadHotspotStore()
  const watches = Array.isArray(options.watches) ? options.watches : []
  const items = []
  const seen = new Set()

  const push = (query, tag, source) => {
    const q = String(query || '').trim()
    if (!q || seen.has(q)) return
    seen.add(q)
    items.push({ query: q, tag, source })
  }

  const meta = {
    refreshTime: store.refreshTime || DEFAULT_REFRESH_TIME,
    lastRefreshedAt: store.lastRefreshedAt || '',
    lastRefreshedDate: store.lastRefreshedDate || '',
  }

  if (mode === 'local') {
    const localTag = lang === 'en' ? 'Workspace' : '工作区'
    const dirs = readLocalWorkspaceHints(options.cwd, 5)
    for (const d of dirs) push(d, localTag, 'workspace')
    if (items.length < 3) {
      const fb = (COLD_START_FALLBACK.local[lang] || COLD_START_FALLBACK.local.zh)
      for (const q of fb) push(q, lang === 'en' ? 'Hint' : '引导', 'fallback')
    }
    return { ok: true, mode, lang, items: items.slice(0, 7), meta }
  }

  const { list: recentList, all: recentAll } = options.recentQueries
    ? { list: options.recentQueries, all: options.recentQueries }
    : readRecentQueries(lang, 4)

  const rawHotspots = (lang === 'en' ? store.en : store.zh)
  const hotspotPool = rawHotspots.length > 0 ? [...rawHotspots] : readLegacyFeedHotspots(lang)
  const interestTokens = extractInterestTokens(recentAll, watches)
  const rankedHotspots = rankHotspotsByInterest(hotspotPool, interestTokens)
  if (shuffle && rankedHotspots.length > 2) {
    const head = rankedHotspots.slice(0, 2)
    const rest = rankedHotspots.slice(2).sort(() => Math.random() - 0.5)
    rankedHotspots.splice(0, rankedHotspots.length, ...head, ...rest)
  }

  const relatedTag = lang === 'en' ? 'For You' : '相关热点'
  const hotTag = lang === 'en' ? 'Hot' : '实时热点'
  const watchTag = lang === 'en' ? 'Watch' : '我的追踪'
  const recentTag = lang === 'en' ? 'Recent' : '近期常搜'
  const fallbackTag = lang === 'en' ? 'Guide' : '引导'

  // 1. 优先展示与用户兴趣重合的实时热点（最多 3 条）
  for (const h of rankedHotspots.filter((x) => x.score > 0).slice(0, 3)) {
    push(h.query, relatedTag, 'hotspot-matched')
  }
  // 2. 用户正在追踪的 Watch 主题（最多 2 条）
  for (const w of watches.slice(0, 2)) {
    if (w?.query) push(w.query, watchTag, 'watch')
  }
  // 3. 其余实时热点（补足到 4 条热点位）
  for (const h of rankedHotspots.filter((x) => x.score === 0).slice(0, 4)) {
    if (items.length >= 5) break
    push(h.query, hotTag, 'hotspot')
  }
  // 4. 用户近期真实搜索记录
  for (const q of recentList) {
    if (items.length >= 7) break
    push(q, recentTag, 'history')
  }
  // 5. 仅在冷启动（真实信号不足 3 条）时用极简引导补位
  if (items.length < 3) {
    const fbPool = COLD_START_FALLBACK[mode] || COLD_START_FALLBACK.general
    for (const q of fbPool[lang] || fbPool.zh) {
      push(q, fallbackTag, 'fallback')
    }
  }

  return { ok: true, mode, lang, items: items.slice(0, 7), meta }
}

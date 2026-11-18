/*
 * @taxueseek/argo-dsh — 搜索分析工作台 Host 端（webServer + SSE + 宿主 LLM）
 *
 * 独立于 Agent 工具链（nativeTools / web seam / wide_research）：
 * 仅在宿主提供 webServer 时挂载 `/argo/*` 路由，为普通用户提供类网页版
 * 搜索分析工作台（5 种专题模式 + 左答右证 SSE + 原文核验 + 多轮追问）。
 */

import { execFile } from 'node:child_process'
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { DEFAULTS, acquireMcp, resolveEntry, stripBoundary } from './runner.js'
import { resolveArgoEnv } from './specs.js'
import { boundedMap, buildCrossLingualTracks, evaluateGates } from './wide-research.js'
import { buildSuggestions } from './workbench-suggestions.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const CLIENT_FILE = join(HERE, 'workbench-client.js')
const MD_FILE = join(HERE, 'workbench-md.js')
const WATCH_STORE_FILE = join(homedir(), '.config', 'argo', 'workbench-watches.json')
const BREAKER_FILE = join(homedir(), '.cache', 'unified-search', 'circuit_breaker.json')

const CACHE_TTL_MS = 5 * 60 * 1000
const MAX_CACHE_ENTRIES = 32
const MAX_DIAGNOSTICS = 20
const MAX_WATCHES = 20

const CITATION_RULE = [
  '严格基于提供的检索证据回答，所有事实判断、数据与观点必须在句末使用方括号编号引用，例如 [1] 或 [2][3]。',
  '严禁编造未出现在证据中的数字、链接或来源；如果证据不足或存在冲突，直接指出并并列呈现不同来源。',
  '使用清晰的中文 Markdown 结构（小标题、要点列表或对比表格），先给核心结论，再展开细节。',
].join('\n')

const MODE_SPECS = {
  general: {
    label: '全网搜索',
    searchArgs: { max_results: 12, verify: 2, depth: 'balanced' },
    awaitVerify: false,
    prompt: '当前模式为「全网搜索」：先用 2 到 3 句话给出直接结论，再用清晰小标题展开关键依据与延伸信息。',
  },
  hotspot: {
    label: '热点分析',
    searchArgs: { max_results: 14, sort: 'newest', since: '14d', depth: 'balanced' },
    awaitVerify: false,
    prompt: '当前模式为「热点分析」：按「一、核心结论与现状」「二、事件脉络」「三、核心争议与各方立场」「四、影响与后续关注点」组织输出。',
  },
  verify: {
    label: '真假核验',
    searchArgs: { max_results: 12, verify: 5, depth: 'deep' },
    awaitVerify: true,
    prompt: '当前模式为「真假核验」：按「一、核验结论（明确给出可信度判断：可信 / 存疑 / 失实 / 证据不足）」「二、关键证据链（优先引用已核验正文）」「三、冲突点与核验边界」组织输出。',
  },
  research: {
    label: '深度研究',
    searchArgs: { max_results: 16, verify: 5, depth: 'deep' },
    awaitVerify: true,
    prompt: '当前模式为「深度研究」：按「一、执行摘要」「二、多维拆解与关键发现」「三、数据与案例支撑」「四、风险、局限与待验证项」组织详尽研究报告。',
  },
  compare: {
    label: '选型对比',
    searchArgs: { max_results: 14, verify: 3, depth: 'balanced' },
    awaitVerify: true,
    prompt: '当前模式为「选型对比」：按「一、选型结论与推荐」「二、核心维度对比表（Markdown 表格）」「三、各自优缺点与适用人群」「四、替代方案与踩坑提示」组织输出。',
  },
  academic: {
    label: '学术文献',
    searchArgs: { max_results: 14, domain: 'academic', depth: 'deep', verify: 3 },
    awaitVerify: false,
    prompt: '当前模式为「学术文献」：按「一、核心研究结论与共识」「二、代表性论文与方法拆解」「三、实验证据与关键数据」「四、局限性与延伸方向」组织输出。',
  },
  plugin: {
    label: '插件评测',
    searchArgs: { max_results: 14, domain: 'dsh_plugin_search', verify: 3, depth: 'balanced' },
    awaitVerify: true,
    prompt: '当前模式为「插件评测」：按「一、是否值得安装（结论）」「二、核心能力与亮点」「三、社区真实反馈与局限」「四、替代方案与安装配置注意」组织输出。',
  },
  local: {
    label: '本机检索',
    searchArgs: { max_results: 16 },
    awaitVerify: false,
    prompt: '当前模式为「本机检索」：基于本机文件与代码命中证据回答，标明文件路径、行号与上下文关联，禁止编造未命中的本地文件。',
  },
}

const MODES = new Set(Object.keys(MODE_SPECS))
const MODE_LABELS = Object.fromEntries(Object.entries(MODE_SPECS).map(([k, v]) => [k, v.label]))

function sendJson(res, status, data) {
  if (res.writableEnded) return
  res.statusCode = status
  res.setHeader('Content-Type', 'application/json; charset=utf-8')
  res.setHeader('Cache-Control', 'no-store')
  res.end(JSON.stringify(data))
}

function sendSse(res, event, data) {
  if (res.writableEnded || res.destroyed) return
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
}

function checkSameOrigin(ctx, req) {
  const origin = req.headers?.origin
  if (!origin) return true
  try {
    const expected = ctx.webServer?.getOrigin?.()
    if (expected && origin === expected) return true
    const parsed = new URL(origin)
    return parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost' || parsed.hostname === '::1'
  } catch {
    return false
  }
}

async function readJsonBody(req, maxBytes = 256 * 1024) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > maxBytes) throw new Error('request body too large')
    chunks.push(chunk)
  }
  const raw = Buffer.concat(chunks).toString('utf8').trim()
  return raw ? JSON.parse(raw) : {}
}

function extractDomain(url) {
  try {
    return new URL(url).hostname.replace(/^www\./i, '')
  } catch {
    return ''
  }
}

function normalizeCandidates(payload) {
  const rawList = Array.isArray(payload?.results) ? payload.results : []
  const seen = new Set()
  const cards = []
  for (const item of rawList) {
    if (!item || typeof item.url !== 'string' || !item.url.startsWith('http')) continue
    const key = item.url.trim()
    if (seen.has(key)) continue
    seen.add(key)
    const index = cards.length + 1
    const title = stripBoundary(item.title) || key
    const snippet = stripBoundary(item.snippet || item.content || '') || ''
    const verifiedText = stripBoundary(item.verified_excerpt || item.fetched_excerpt || '') || ''
    const source = item.engine || item.source || item.provider || 'web'
    cards.push({
      id: `c${index}`,
      index,
      title,
      url: key,
      domain: extractDomain(key),
      source,
      snippet,
      verifiedText,
      verified: Boolean(item.verified || verifiedText),
      credibility: typeof item.credibility_fast === 'number'
        ? item.credibility_fast
        : (typeof item.credibility === 'number' ? item.credibility : null),
      publishedAt: item.published_at || item.publishedAt || item.date || '',
    })
    if (cards.length >= 16) break
  }
  return cards
}

function buildSearchArgs(query, mode, overrideDomain) {
  const spec = MODE_SPECS[mode] || MODE_SPECS.general
  const args = { query, summary: false, ...spec.searchArgs }
  if (overrideDomain) args.domain = overrideDomain
  return args
}

function buildSystemPrompt(mode) {
  const spec = MODE_SPECS[mode] || MODE_SPECS.general
  return `${CITATION_RULE}\n${spec.prompt}`
}

function normalizeLocalCards(payload) {
  const rawList = Array.isArray(payload?.matches)
    ? payload.matches
    : (Array.isArray(payload?.results) ? payload.results : [])
  const seen = new Set()
  const cards = []
  for (const item of rawList) {
    const filePath = String(item?.path || item?.file || item?.url || '').trim()
    if (!filePath) continue
    const line = item?.line ? `:${item.line}` : ''
    const dedupKey = `${filePath}${line}`
    if (seen.has(dedupKey)) continue
    seen.add(dedupKey)
    const index = cards.length + 1
    const snippet = stripBoundary(item?.snippet || item?.text || item?.preview || (Array.isArray(item?.lines) ? item.lines.join('\n') : '')) || ''
    cards.push({
      id: `c${index}`,
      index,
      title: `${filePath.split('/').slice(-2).join('/')}${line}`,
      url: filePath.startsWith('file://') ? filePath : `file://${filePath}`,
      domain: 'local-seek',
      source: 'local-seek',
      snippet,
      verifiedText: '',
      verified: true,
      credibility: 1,
      publishedAt: item?.mtime || '',
    })
    if (cards.length >= 16) break
  }
  return cards
}

function readEgoSearchStatus() {
  const statePath = process.env.EGO_SEARCH_STATE || join(process.env.XDG_STATE_HOME || join(homedir(), '.local', 'state'), 'ego-search.json')
  try {
    const parsed = JSON.parse(readFileSync(statePath, 'utf8'))
    return { enabled: Boolean(parsed?.enabled), enabledAt: parsed?.enabled_at || null }
  } catch {
    return { enabled: false, enabledAt: null }
  }
}

function readBreakerSummary() {
  try {
    const raw = JSON.parse(readFileSync(BREAKER_FILE, 'utf8'))
    const engines = raw?.engines && typeof raw.engines === 'object' ? raw.engines : {}
    const tripped = []
    let total = 0
    for (const [name, info] of Object.entries(engines)) {
      if (!info || typeof info !== 'object') continue
      total += 1
      if (info.state && info.state !== 'closed') {
        tripped.push({
          engine: name,
          state: info.state,
          opens: info.opens || 0,
          lastError: String(info.last_error || '').slice(0, 60),
        })
      }
    }
    tripped.sort((a, b) => (b.opens || 0) - (a.opens || 0))
    return { total, nonClosedCount: tripped.length, topTripped: tripped.slice(0, 8) }
  } catch {
    return { total: 0, nonClosedCount: 0, topTripped: [] }
  }
}

function buildEvidenceContext(cards, maxChars = 14000) {
  const lines = []
  let used = 0
  for (const c of cards) {
    const body = c.verifiedText || c.snippet || '(无摘要)'
    const block = [
      `[${c.index}] 标题：${c.title}`,
      `来源：${c.source} (${c.domain})${c.publishedAt ? ` · 时间：${c.publishedAt}` : ''}${c.verified ? ' · [已核验正文]' : ''}`,
      `链接：${c.url}`,
      `内容：${body.slice(0, 900)}`,
    ].join('\n')
    if (used + block.length > maxChars) break
    lines.push(block)
    used += block.length
  }
  return lines.join('\n\n')
}

function buildFallbackSummary(query, mode, cards) {
  const label = MODE_LABELS[mode] || '全网综合'
  if (cards.length === 0) {
    return `### 未检索到相关结果\n\n针对「${query}」在 **${label}** 模式下未命中有效网页结果，建议精简关键词或点击右上角「强制刷新」重试。`
  }
  const rows = cards.slice(0, 8).map((c) => {
    const text = (c.verifiedText || c.snippet || '').slice(0, 180).replace(/\n+/g, ' ')
    return `- **${c.title}** [${c.index}]（${c.domain || c.source}）\n  ${text || '点击右侧卡片查看原文'}`
  })
  return `### ${label} · 聚合检索结果（零模型直出模式）\n\n当前宿主未配置可用默认模型，已为你整理最相关的 ${cards.length} 条多源证据，可直接在右侧核对原文：\n\n${rows.join('\n\n')}`
}

async function runMcpTool(config, credentials, toolName, args, timeoutMs, signal, normalizer = normalizeCandidates) {
  const entry = resolveEntry(config)
  const conn = await acquireMcp({
    command: entry.command,
    args: entry.args,
    idleMs: config.searchIdleMs ?? DEFAULTS.searchIdleMs,
    max: config.searchConcurrency ?? DEFAULTS.searchConcurrency,
    initMs: config.searchTimeoutMs ?? DEFAULTS.searchTimeoutMs,
    credentials,
    config,
  }, signal)
  try {
    const result = await conn.request('tools/call', {
      name: toolName,
      arguments: args,
    }, timeoutMs)
    const rawText = result?.content?.[0]?.text
    const payload = typeof rawText === 'string' ? JSON.parse(rawText) : {}
    return { payload, cards: normalizer(payload) }
  } finally {
    conn.release?.()
  }
}

async function callLocalSeek(config, credentials, query, maxResults = 16, signal) {
  const searchPath = config?.localSearchPath || process.env.ARGO_LOCAL_SEARCH_PATH || join(homedir(), 'Documents')
  try {
    return await runMcpTool(
      config,
      credentials,
      'argo_local_search',
      { query, path: searchPath, max_results: Math.min(20, Math.max(1, maxResults)) },
      15000,
      signal,
      normalizeLocalCards,
    )
  } catch (err) {
    return { payload: { errors: [String(err?.message ?? err)] }, cards: [] }
  }
}

async function runMcpSearch(config, credentials, args, timeoutMs, signal) {
  return runMcpTool(config, credentials, 'argo_search', args, timeoutMs, signal, normalizeCandidates)
}

async function callArgoSearch(config, credentials, query, mode, refresh, signal, tracks = null) {
  const extraEnv = refresh ? { ARGO_NO_CACHE: '1' } : undefined
  if (mode === 'local') {
    const res = await callLocalSeek(config, credentials, query, MODE_SPECS.local.searchArgs.max_results, signal)
    return { ...res, extraEnv }
  }

  const timeoutMs = mode === 'research'
    ? Math.max(config.searchTimeoutMs ?? 60000, 90000)
    : (config.searchTimeoutMs ?? DEFAULTS.searchTimeoutMs)

  if (mode === 'research' && Array.isArray(tracks) && tracks.length > 1) {
    const trackRuns = await boundedMap(tracks, 3, async (track) => {
      const args = buildSearchArgs(track.question || query, 'research')
      args.max_results = 10
      if (refresh) args.no_cache = true
      return runMcpSearch(config, credentials, args, timeoutMs, signal).catch(() => ({ payload: {}, cards: [] }))
    })
    const seen = new Set()
    const merged = []
    for (const run of trackRuns) {
      for (const card of run.cards || []) {
        if (seen.has(card.url)) continue
        seen.add(card.url)
        merged.push({ ...card, id: `c${merged.length + 1}`, index: merged.length + 1 })
        if (merged.length >= 16) break
      }
      if (merged.length >= 16) break
    }
    return { payload: trackRuns[0]?.payload || {}, cards: merged, extraEnv }
  }

  const args = buildSearchArgs(query, mode)
  if (refresh) args.no_cache = true
  const primary = await runMcpSearch(config, credentials, args, timeoutMs, signal)

  // 插件评测：主域 dsh_plugin_search 命中不足时补发通用评测检索，避免单域冷场或仅出元宝/知乎
  if (mode === 'plugin' && primary.cards.length < 6) {
    const fallbackArgs = buildSearchArgs(`${query} 评测 替代`, 'general')
    if (refresh) fallbackArgs.no_cache = true
    const extra = await runMcpSearch(config, credentials, fallbackArgs, timeoutMs, signal).catch(() => ({ cards: [] }))
    const seen = new Set(primary.cards.map(c => c.url))
    const combined = [...primary.cards]
    for (const c of extra.cards || []) {
      if (seen.has(c.url)) continue
      seen.add(c.url)
      combined.push({ ...c, id: `c${combined.length + 1}`, index: combined.length + 1 })
      if (combined.length >= 16) break
    }
    return { payload: primary.payload, cards: combined, extraEnv }
  }

  return { ...primary, extraEnv }
}

async function verifyCardWithFetch(config, credentials, card, signal) {
  const entry = resolveEntry(config)
  const conn = await acquireMcp({
    command: entry.command,
    args: entry.args,
    idleMs: config.searchIdleMs ?? DEFAULTS.searchIdleMs,
    max: config.searchConcurrency ?? DEFAULTS.searchConcurrency,
    initMs: config.fetchTimeoutMs ?? DEFAULTS.fetchTimeoutMs,
    credentials,
    config,
  }, signal)

  try {
    const result = await conn.request('tools/call', {
      name: 'argo_fetch',
      arguments: { url: card.url, max_chars: 2500 },
    }, Math.min(config.fetchTimeoutMs ?? 30000, 20000))
    const rawText = result?.content?.[0]?.text
    const payload = typeof rawText === 'string' ? JSON.parse(rawText) : {}
    const excerpt = stripBoundary(payload?.content || payload?.text || payload?.markdown || '')
    if (!excerpt || excerpt.length < 40) return null
    return {
      ...card,
      verified: true,
      verifiedText: excerpt.slice(0, 700),
    }
  } catch {
    return null
  } finally {
    conn.release?.()
  }
}

async function streamLlmAnswer(ctx, { system, messages, maxTokens, signal, onDelta }) {
  const llm = ctx.get?.('llm')
  const modelService = ctx.get?.('agentDefaultModel')
  if (!llm || !modelService) return { ok: false, reason: 'no_model' }

  let selection
  try {
    selection = modelService.currentSelection?.()
  } catch {
    selection = null
  }
  if (!selection?.provider || !selection?.model) {
    return { ok: false, reason: 'no_model' }
  }

  let text = ''
  let usage = null
  let finish = null
  try {
    const stream = llm.stream({
      provider: selection.provider,
      model: selection.model,
      maxTokens,
      signal,
      system,
      messages,
    })
    for await (const chunk of stream) {
      if (signal?.aborted) break
      if (chunk?.type === 'text-delta' && typeof chunk.text === 'string') {
        text += chunk.text
        onDelta(chunk.text)
      } else if (chunk?.type === 'usage') {
        usage = chunk.usage
      } else if (chunk?.type === 'finish') {
        finish = chunk.reason
      } else if (chunk?.type === 'error') {
        const msg = chunk.error?.message || chunk.message || 'model stream error'
        return { ok: false, reason: 'error', message: String(msg), text, model: `${selection.provider}/${selection.model}` }
      }
    }
    if (finish && finish.kind !== 'stop') {
      const msg = finish.failure?.message || finish.failure?.code || `finish=${finish.kind}`
      console.warn(`[argo-workbench] llm finish=${finish.kind} model=${selection.provider}/${selection.model} msg=${msg}`)
      return { ok: false, reason: finish.kind, message: String(msg), text, usage, finish, model: `${selection.provider}/${selection.model}` }
    }
    return { ok: true, text, usage, finish, model: `${selection.provider}/${selection.model}` }
  } catch (err) {
    return { ok: false, reason: err?.name === 'AbortError' ? 'aborted' : 'error', message: String(err?.message ?? err), text }
  }
}

function extractCitations(text, cards) {
  const used = new Set()
  const re = /\[(\d+)\]/g
  let m
  while ((m = re.exec(text)) !== null) {
    const n = Number(m[1])
    if (n >= 1 && n <= cards.length) used.add(n)
  }
  return {
    citedIndices: [...used].sort((a, b) => a - b),
    totalCards: cards.length,
  }
}

function loadWatches() {
  try {
    const raw = readFileSync(WATCH_STORE_FILE, 'utf8')
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function saveWatches(items) {
  try {
    mkdirSync(dirname(WATCH_STORE_FILE), { recursive: true })
    const tmp = `${WATCH_STORE_FILE}.tmp.${process.pid}`
    writeFileSync(tmp, JSON.stringify(items, null, 2), 'utf8')
    renameSync(tmp, WATCH_STORE_FILE)
  } catch { /* ignore */ }
}

export function createWorkbenchHandler(ctx, config, getCredentials) {
  const cache = new Map()
  const inflight = new Map()
  const diagnostics = []
  let watches = loadWatches()
  let watchBusy = false

  const executeWatch = async (watch) => {
    const started = Date.now()
    const credentials = typeof getCredentials === 'function' ? getCredentials() : undefined
    try {
      await resolveArgoEnv(config, { credentials }).catch(() => ({}))
      const { cards } = await callArgoSearch(config, credentials, watch.query, watch.mode, true, AbortSignal.timeout(45000))
      let summary = ''
      if (cards.length > 0) {
        const evidence = buildEvidenceContext(cards, 9000)
        const llmRes = await streamLlmAnswer(ctx, {
          system: buildSystemPrompt(watch.mode),
          messages: [{
            role: 'user',
            content: [{ type: 'text', text: `定时追踪任务：${watch.query}\n\n最新检索证据：\n\n${evidence}\n\n请用简明结构概括最新动态，并在句末标注 [1]、[2] 引用编号。` }],
          }],
          maxTokens: 1800,
          signal: AbortSignal.timeout(35000),
          onDelta: () => {},
        })
        summary = llmRes.text || buildFallbackSummary(watch.query, watch.mode, cards)
      } else {
        summary = buildFallbackSummary(watch.query, watch.mode, cards)
      }
      watch.lastRunAt = new Date().toISOString()
      watch.nextRunAt = new Date(Date.now() + watch.intervalMinutes * 60000).toISOString()
      watch.lastStatus = 'ok'
      watch.lastCardCount = cards.length
      watch.lastSummary = summary
      watch.lastCards = cards.slice(0, 10)
      watch.lastDurationMs = Date.now() - started
    } catch (err) {
      watch.lastRunAt = new Date().toISOString()
      watch.nextRunAt = new Date(Date.now() + watch.intervalMinutes * 60000).toISOString()
      watch.lastStatus = `error: ${String(err?.message ?? err)}`
    }
    saveWatches(watches)
    return watch
  }

  const timer = setInterval(async () => {
    if (watchBusy) return
    const now = Date.now()
    const due = watches.find(w => w.enabled !== false && (!w.nextRunAt || Date.parse(w.nextRunAt) <= now))
    if (!due) return
    watchBusy = true
    try { await executeWatch(due) } finally { watchBusy = false }
  }, 60 * 1000)
  timer.unref?.()

  const recordDiag = (entry) => {
    diagnostics.unshift({ ...entry, ts: new Date().toISOString() })
    if (diagnostics.length > MAX_DIAGNOSTICS) diagnostics.length = MAX_DIAGNOSTICS
  }

  const getCached = (key) => {
    const hit = cache.get(key)
    if (!hit) return null
    if (Date.now() - hit.ts > CACHE_TTL_MS) {
      cache.delete(key)
      return null
    }
    return hit
  }

  const setCached = (key, value) => {
    if (cache.size >= MAX_CACHE_ENTRIES) {
      const oldest = cache.keys().next().value
      if (oldest) cache.delete(oldest)
    }
    cache.set(key, { ...value, ts: Date.now() })
  }

  const workbenchHandler = async (req, res) => {
    try {
      const url = new URL(req.url || '/', 'http://127.0.0.1')
      const path = url.pathname

      if (path === '/argo/workbench-client.js' && req.method === 'GET') {
        const source = readFileSync(CLIENT_FILE, 'utf8')
        res.statusCode = 200
        res.setHeader('Content-Type', 'application/javascript; charset=utf-8')
        res.setHeader('Cache-Control', 'no-store')
        res.end(source)
        return
      }

      if (path === '/argo/workbench-md.js' && req.method === 'GET') {
        const source = readFileSync(MD_FILE, 'utf8')
        res.statusCode = 200
        res.setHeader('Content-Type', 'application/javascript; charset=utf-8')
        res.setHeader('Cache-Control', 'no-store')
        res.end(source)
        return
      }

      if (path === '/argo/api/watches' && req.method === 'GET') {
        sendJson(res, 200, { ok: true, items: watches })
        return
      }

      if (path === '/argo/api/watches' && req.method === 'POST') {
        if (!checkSameOrigin(ctx, req)) { sendJson(res, 403, { error: 'forbidden_origin' }); return }
        const body = await readJsonBody(req)
        const query = String(body.query || '').trim()
        const mode = MODES.has(body.mode) ? body.mode : 'general'
        const intervalMinutes = Math.max(60, Math.min(43200, Number(body.intervalMinutes) || 1440))
        if (!query) { sendJson(res, 400, { error: 'missing_query' }); return }
        const item = {
          id: `w_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`,
          query,
          mode,
          modeLabel: MODE_LABELS[mode] || mode,
          intervalMinutes,
          enabled: true,
          createdAt: new Date().toISOString(),
          nextRunAt: new Date(Date.now() + intervalMinutes * 60000).toISOString(),
          lastRunAt: null,
          lastStatus: 'pending',
          lastCardCount: 0,
          lastSummary: '',
          lastCards: [],
        }
        watches.unshift(item)
        if (watches.length > MAX_WATCHES) watches.length = MAX_WATCHES
        saveWatches(watches)
        sendJson(res, 200, { ok: true, item, items: watches })
        return
      }

      if (path === '/argo/api/watches/run' && req.method === 'POST') {
        if (!checkSameOrigin(ctx, req)) { sendJson(res, 403, { error: 'forbidden_origin' }); return }
        const body = await readJsonBody(req)
        const target = watches.find(w => w.id === body.id)
        if (!target) { sendJson(res, 404, { error: 'watch_not_found' }); return }
        const updated = await executeWatch(target)
        sendJson(res, 200, { ok: true, item: updated, items: watches })
        return
      }

      if (path === '/argo/api/watches' && req.method === 'DELETE') {
        if (!checkSameOrigin(ctx, req)) { sendJson(res, 403, { error: 'forbidden_origin' }); return }
        const id = url.searchParams.get('id') || ''
        watches = watches.filter(w => w.id !== id)
        saveWatches(watches)
        sendJson(res, 200, { ok: true, items: watches })
        return
      }

      if (path === '/argo/api/health' && req.method === 'GET') {
        const hasLlm = Boolean(ctx.get?.('llm') && ctx.get?.('agentDefaultModel')?.currentSelection?.()?.model)
        sendJson(res, 200, {
          ok: true,
          modes: Object.entries(MODE_LABELS).map(([id, label]) => ({ id, label })),
          hasLlm,
          egoSearch: readEgoSearchStatus(),
          breaker: readBreakerSummary(),
        })
        return
      }

      if (path === '/argo/api/suggestions' && req.method === 'GET') {
        const reqMode = MODES.has(url.searchParams.get('mode')) ? url.searchParams.get('mode') : 'general'
        const lang = url.searchParams.get('lang') || 'zh'
        const shuffle = url.searchParams.get('shuffle') === '1'
        sendJson(res, 200, buildSuggestions(reqMode, lang, shuffle))
        return
      }

      if (path === '/argo/api/ego-search' && req.method === 'POST') {
        if (!checkSameOrigin(ctx, req)) { sendJson(res, 403, { error: 'forbidden_origin' }); return }
        const body = await readJsonBody(req)
        const action = body.enabled ? 'enable' : 'disable'
        await new Promise((resolve) => {
          execFile('argo', ['ego-search', action], { timeout: 8000 }, () => resolve())
        })
        sendJson(res, 200, { ok: true, egoSearch: readEgoSearchStatus() })
        return
      }

      if (path === '/argo/api/diagnostics' && req.method === 'GET') {
        sendJson(res, 200, { ok: true, items: diagnostics })
        return
      }

      if (path === '/argo/api/stream' && req.method === 'GET') {
        if (!checkSameOrigin(ctx, req)) {
          sendJson(res, 403, { error: 'forbidden_origin' })
          return
        }
        const query = (url.searchParams.get('q') || '').trim()
        const rawMode = (url.searchParams.get('mode') || 'general').trim()
        const mode = MODES.has(rawMode) ? rawMode : 'general'
        const refresh = url.searchParams.get('refresh') === '1'

        if (!query) {
          sendJson(res, 400, { error: 'missing_query' })
          return
        }

        res.statusCode = 200
        res.setHeader('Content-Type', 'text/event-stream; charset=utf-8')
        res.setHeader('Cache-Control', 'no-cache, no-transform')
        res.setHeader('Connection', 'keep-alive')
        res.flushHeaders?.()

        const ac = new AbortController()
        req.on('close', () => ac.abort())

        const startedAt = Date.now()
        const cacheKey = `${mode}::${query.toLowerCase()}`

        try {
          sendSse(res, 'status', { stage: 'searching', message: `正在通过 Argo 多源检索（${MODE_LABELS[mode]}）…` })
          const researchTracks = mode === 'research' ? buildCrossLingualTracks(query, 3) : null
          sendSse(res, 'plan', {
            query,
            mode,
            modeLabel: MODE_LABELS[mode],
            tracks: researchTracks
              ? researchTracks.map(t => t.title)
              : (mode === 'local' ? ['本机 FTS/rg 索引检索'] : ['智能路由检索', '证据梳理与引用']),
          })

          let cards
          let payload
          let cached = !refresh ? getCached(cacheKey) : null
          if (cached) {
            cards = cached.cards
            payload = cached.payload
          } else {
            let pending = !refresh ? inflight.get(cacheKey) : null
            if (!pending) {
              const credentials = typeof getCredentials === 'function' ? getCredentials() : undefined
              // 同步一次环境变量，避免池化前首次调用缺省环境
              await resolveArgoEnv(config, { credentials }).catch(() => ({}))
              pending = callArgoSearch(config, credentials, query, mode, refresh, ac.signal, researchTracks)
                .finally(() => inflight.delete(cacheKey))
              if (!refresh) inflight.set(cacheKey, pending)
            }
            const resData = await pending
            cards = resData.cards
            payload = resData.payload
            if (cards.length > 0) setCached(cacheKey, { cards, payload })
          }

          if (ac.signal.aborted) return

          sendSse(res, 'cards', {
            cards,
            funnel: payload?.funnel || null,
            engines: [...new Set(cards.map(c => c.source))],
          })

          // 对未核验的 top-3 卡片做原文核验；需等待核验完成再进入合成，确保 LLM 上下文拿到正文摘录且无 SSE 竞态
          const credentials = typeof getCredentials === 'function' ? getCredentials() : undefined
          if (MODE_SPECS[mode]?.awaitVerify && cards.length > 0) {
            const toVerify = cards.filter(c => !c.verified).slice(0, 3)
            if (toVerify.length > 0) {
              sendSse(res, 'status', { stage: 'verifying', message: '正在并行核验高优先原文…' })
              await Promise.allSettled(
                toVerify.map(async (card) => {
                  const updated = await verifyCardWithFetch(config, credentials, card, ac.signal)
                  if (updated && !ac.signal.aborted) {
                    const idx = cards.findIndex(c => c.id === updated.id)
                    if (idx >= 0) cards[idx] = updated
                    sendSse(res, 'verify-update', { card: updated })
                  }
                }),
              )
            }
          }
          if (ac.signal.aborted) return

          sendSse(res, 'status', { stage: 'synthesizing', message: '正在结合检索证据生成结构化分析…' })

          const system = buildSystemPrompt(mode)
          const evidence = buildEvidenceContext(cards)
          const userPrompt = cards.length > 0
            ? `问题：${query}\n\n以下是检索到的多源证据：\n\n${evidence}\n\n请严格依据上述证据回答，并在句末标注 [1]、[2] 等引用编号。`
            : `问题：${query}\n\n当前未检索到外部网页结果，请如实说明并给出可行的关键词调整建议。`

          const llmRes = await streamLlmAnswer(ctx, {
            system,
            messages: [{ role: 'user', content: [{ type: 'text', text: userPrompt }] }],
            maxTokens: mode === 'research' ? 6000 : 3500,
            signal: ac.signal,
            onDelta: (delta) => sendSse(res, 'answer', { delta }),
          })

          let finalText = llmRes.text || ''
          if (!llmRes.ok && finalText === '') {
            if (llmRes.reason !== 'no_model') {
              sendSse(res, 'answer-status', {
                status: 'fallback',
                message: `默认模型（${llmRes.model || 'unknown'}）暂不可用：${llmRes.message || llmRes.reason}，已自动切换为多源聚合直出视图`,
              })
            }
            finalText = buildFallbackSummary(query, mode, cards)
            sendSse(res, 'answer', { delta: finalText })
          } else if (!llmRes.ok && llmRes.reason !== 'aborted') {
            sendSse(res, 'answer-status', {
              status: 'interrupted',
              message: llmRes.message || '模型输出中断，已保留已生成内容',
            })
          }

          const citations = extractCitations(finalText, cards)
          sendSse(res, 'citations', citations)
          if (mode === 'research') {
            const gate = evaluateGates({
              answer: finalText,
              sources: cards.map(c => ({ url: c.url, title: c.title, domain: c.domain })),
              tracks: researchTracks || [],
              warnings: [],
            })
            sendSse(res, 'gate', gate)
          }
          sendSse(res, 'done', {
            durationMs: Date.now() - startedAt,
            cardCount: cards.length,
            verifiedCount: cards.filter(c => c.verified).length,
            model: llmRes.model || 'none',
            usage: llmRes.usage || null,
          })

          recordDiag({
            query,
            mode,
            durationMs: Date.now() - startedAt,
            cardCount: cards.length,
            verifiedCount: cards.filter(c => c.verified).length,
            citedCount: citations.citedIndices.length,
            status: llmRes.ok ? 'ok' : (llmRes.reason || 'fallback'),
            usage: llmRes.usage || null,
          })
        } catch (err) {
          if (!ac.signal.aborted) {
            sendSse(res, 'failure', { message: String(err?.message ?? err) })
            recordDiag({
              query,
              mode,
              durationMs: Date.now() - startedAt,
              cardCount: 0,
              status: 'error',
              error: String(err?.message ?? err),
            })
          }
        } finally {
          if (!res.writableEnded) res.end()
        }
        return
      }

      if (path === '/argo/api/followup' && req.method === 'POST') {
        if (!checkSameOrigin(ctx, req)) {
          sendJson(res, 403, { error: 'forbidden_origin' })
          return
        }
        const body = await readJsonBody(req)
        const question = String(body.question || '').trim()
        const cards = Array.isArray(body.cards) ? body.cards : []
        const history = Array.isArray(body.history) ? body.history : []
        const mode = MODES.has(body.mode) ? body.mode : 'general'

        if (!question) {
          sendJson(res, 400, { error: 'missing_question' })
          return
        }

        res.statusCode = 200
        res.setHeader('Content-Type', 'text/event-stream; charset=utf-8')
        res.setHeader('Cache-Control', 'no-cache, no-transform')
        res.setHeader('Connection', 'keep-alive')
        res.flushHeaders?.()

        const ac = new AbortController()
        req.on('close', () => ac.abort())

        const evidence = buildEvidenceContext(cards)
        const messages = [
          {
            role: 'user',
            content: [{ type: 'text', text: `原始问题：${body.query || ''}\n\n已检索证据：\n${evidence}` }],
          },
          ...history.slice(-6).map(item => ({
            role: item.role === 'assistant' ? 'assistant' : 'user',
            content: [{ type: 'text', text: String(item.text || '') }],
          })),
          {
            role: 'user',
            content: [{ type: 'text', text: `基于上述已检索证据继续追问：${question}\n请继续使用 [1]、[2] 引用编号；若当前证据未覆盖，请明确说明。` }],
          },
        ]

        const llmRes = await streamLlmAnswer(ctx, {
          system: buildSystemPrompt(mode),
          messages,
          maxTokens: 2500,
          signal: ac.signal,
          onDelta: (delta) => sendSse(res, 'answer', { delta }),
        })

        if (!llmRes.ok && llmRes.reason === 'no_model') {
          sendSse(res, 'answer', { delta: '当前宿主未配置默认模型，无法进行多轮追问推理。' })
        }
        sendSse(res, 'citations', extractCitations(llmRes.text || '', cards))
        sendSse(res, 'done', { ok: llmRes.ok })
        if (!res.writableEnded) res.end()
        return
      }

      sendJson(res, 404, { error: 'not_found', path })
    } catch (err) {
      sendJson(res, 500, { error: 'internal_error', message: String(err?.message ?? err) })
    }
  }
  workbenchHandler.dispose = () => clearInterval(timer)
  return workbenchHandler
}

export function registerArgoWorkbench(ctx, config, getCredentials) {
  const attach = (targetCtx) => {
    let webServer
    try {
      webServer = targetCtx?.get?.('webServer') ?? targetCtx?.webServer
    } catch {
      return undefined
    }
    if (!webServer || typeof webServer.register !== 'function') return undefined
    const handler = createWorkbenchHandler(targetCtx, config, getCredentials)
    const off = webServer.register({ kind: 'prefix', path: '/argo', handler })
    return () => {
      handler.dispose?.()
      if (typeof off === 'function') off()
    }
  }

  if (typeof ctx?.inject === 'function') {
    try {
      ctx.inject(['webServer'], (subCtx) => {
        const off = attach(subCtx)
        if (off && typeof subCtx.effect === 'function') subCtx.effect(() => off)
      })
      return undefined
    } catch {
      /* 测试环境或无 webServer 时静默跳过 */
    }
  }

  const direct = attach(ctx)
  if (direct && typeof ctx?.effect === 'function') ctx.effect(() => direct)
  return direct
}

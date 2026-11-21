/*
 * @taxueseek/argo-dsh — 搜索分析工作台 Host 端（webServer + SSE + 宿主 LLM）
 *
 * 独立于 Agent 工具链（nativeTools / web seam / wide_research）：
 * 仅在宿主提供 webServer 时挂载 `/argo/*` 路由，为普通用户提供类网页版
 * 搜索分析工作台（5 种专题模式 + 左答右证 SSE + 原文核验 + 多轮追问）。
 */

import { execFile } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { resolveArgoEnv } from './specs.js'
import { buildCrossLingualTracks, evaluateGates } from './wide-research.js'
import {
  buildEvidenceContext,
  buildFallbackSummary,
  buildSystemPrompt,
  callArgoSearch,
  extractCitations,
  MODES,
  MODE_LABELS,
  resolveLocalSearchPath,
  runClarifyIfShort,
  runEvidenceScoring,
  streamLlmAnswer,
} from './workbench-search.js'
import {
  buildSuggestions,
  createHotspotRefresher,
  loadHotspotStore,
  updateRefreshTime,
} from './workbench-suggestions.js'
import { checkLoopbackAndOrigin, resolveAuthStatusWithBgVerify } from './route-guard.js'
import {
  acquireStateFileLock,
  breakerFilePath,
  loadWatches,
  mergedLaunchdPath,
  mutateWatches,
  readStaticAssetCached,
  releaseStateFileLock,
  resolveArgoBin,
  runArgoCli,
  WATCH_STORE_FILE,
} from './workbench-io.js'

// 供既有测试与外部调用方沿旧入口取用（真源在 workbench-search.js）
export { buildSearchCliArgs, summarizeTrackRuns } from './workbench-search.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const CLIENT_FILE = join(HERE, 'workbench-client.js')
const MD_FILE = join(HERE, 'workbench-md.js')

const CACHE_TTL_MS = 5 * 60 * 1000
const MAX_CACHE_ENTRIES = 32
const MAX_DIAGNOSTICS = 20
const MAX_WATCHES = 20

function sendJson(res, status, data) {
  if (res.writableEnded) return
  if (res.headersSent) {
    // SSE 头已 flush 后再 setHeader 会抛 ERR_HTTP_HEADERS_SENT（2026-11-18
    // 实跑复现：followup 传 cards:[null] 在 flushHeaders 后抛 TypeError，
    // 落到这里二次抛 → 响应悬挂 + 异常逃出 async handler）。改发 SSE
    // failure 事件收尾，让客户端与连接都能干净结束。
    sendSse(res, 'failure', data)
    if (!res.writableEnded) res.end()
    return
  }
  res.statusCode = status
  res.setHeader('Content-Type', 'application/json; charset=utf-8')
  res.setHeader('Cache-Control', 'no-store')
  res.end(JSON.stringify(data))
}

function sendSse(res, event, data) {
  if (res.writableEnded || res.destroyed) return
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
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
    const raw = JSON.parse(readFileSync(breakerFilePath(), 'utf8'))
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

export function createWorkbenchHandler(ctx, config, getCredentials, options = {}) {
  const authVerifyCache = new Map()
  const authVerifyInflight = new Set()
  const cache = new Map()
  const inflight = new Map()
  const diagnostics = []
  let watches = loadWatches()
  let watchBusy = false

  // 热榜触发端（inflight 复用 + 全失败 1h 退避）实现在 workbench-suggestions.js
  const hotspotRefresher = createHotspotRefresher({
    buildRunner: async () => {
      const env = await resolveArgoEnv(config, { credentials: getCredentials?.() })
      return (cliArgs) => runArgoCli(cliArgs, { env, timeoutMs: 12000 })
    },
  })
  const triggerDailyHotspotRefresh = hotspotRefresher.trigger
  const hotspotRetryDue = hotspotRefresher.retryDue

  const executeWatch = async (watch) => {
    const started = Date.now()
    const credentials = typeof getCredentials === 'function' ? getCredentials() : undefined
    try {
      const env = await resolveArgoEnv(config, { credentials }).catch(() => ({}))
      const { cards } = await callArgoSearch(config, env, watch.query, watch.mode, true, AbortSignal.timeout(45000))
      let summary = ''
      if (cards.length > 0) {
        const evidence = buildEvidenceContext(cards, 9000)
        const llmRes = await streamLlmAnswer(ctx, {
          system: buildSystemPrompt(watch.mode),
          messages: [{
            role: 'user',
            content: [{ type: 'text', text: `定时追踪任务：${watch.query}\n\n最新检索证据：\n\n${evidence}\n\n请用简明结构概括最新动态，并在句末标注 [1]、[2] 引用编号。` }],
          }],
          signal: AbortSignal.timeout(35000),
          onDelta: () => {},
        })
        summary = llmRes.text || buildFallbackSummary(watch.query, watch.mode, cards, llmRes)
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
    // 按 id upsert 合并写：磁盘是唯一真源，另一宿主对其他 watch 的增删改不被抹掉
    // （旧 saveWatches(watches) 全量覆盖写，2026-11-21 审计 P1）
    const updated = mutateWatches((list) => {
      const idx = list.findIndex(w => w.id === watch.id)
      if (idx >= 0) list[idx] = watch
      else list.unshift(watch)
      return list
    })
    if (Array.isArray(updated)) watches = updated
    return watch
  }

  const timer = setInterval(async () => {
    if (!hotspotRefresher.isBusy() && hotspotRetryDue(loadHotspotStore())) {
      triggerDailyHotspotRefresh()
    }
    if (watchBusy) return
    // 每 tick 重读磁盘：另一宿主刚增删的 watch 立即可见，不靠 60s 前的内存快照
    watches = loadWatches()
    const now = Date.now()
    const due = watches.find(w => w.enabled !== false && (!w.nextRunAt || Date.parse(w.nextRunAt) <= now))
    if (!due) return
    // 跨宿主执行互斥：单锁串行化全部 watch 执行（最坏 ~80s << 5min stale 阈值），
    // 抢不到说明另一宿主在跑，本轮让路——被让路的 watch 下轮仍 due 会重试
    const execLock = acquireStateFileLock(`${WATCH_STORE_FILE}.exec.lock`)
    if (!execLock) return
    watchBusy = true
    try { await executeWatch(due) } finally { watchBusy = false; releaseStateFileLock(execLock) }
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
        const source = readStaticAssetCached(CLIENT_FILE)
        res.statusCode = 200
        res.setHeader('Content-Type', 'application/javascript; charset=utf-8')
        res.setHeader('Cache-Control', 'no-store')
        res.end(source)
        return
      }

      if (path === '/argo/workbench-md.js' && req.method === 'GET') {
        const source = readStaticAssetCached(MD_FILE)
        res.statusCode = 200
        res.setHeader('Content-Type', 'application/javascript; charset=utf-8')
        res.setHeader('Cache-Control', 'no-store')
        res.end(source)
        return
      }

      if (path.startsWith('/argo/api/') && !checkLoopbackAndOrigin(ctx, req)) {
        sendJson(res, 403, { error: 'forbidden_origin' })
        return
      }

      if (path === '/argo/api/auth-status' && req.method === 'GET') {
        const runCli = options.runAuthCli || (async (cliArgs) => {
          const env = await resolveArgoEnv(config, { credentials: getCredentials?.() })
          return runArgoCli(cliArgs, { env, timeoutMs: 8000 })
        })
        const items = await resolveAuthStatusWithBgVerify({
          runCli, verifyCache: authVerifyCache, inflight: authVerifyInflight,
          allowVerify: url.searchParams.get('verify') !== '0',
        })
        sendJson(res, 200, { ok: true, profiles: items })
        return
      }

      if (path === '/argo/api/watches' && req.method === 'GET') {
        sendJson(res, 200, { ok: true, items: watches })
        return
      }

      if (path === '/argo/api/watches' && req.method === 'POST') {
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
        const updatedList = mutateWatches((list) => {
          list.unshift(item)
          if (list.length > MAX_WATCHES) list.length = MAX_WATCHES
          return list
        })
        if (!Array.isArray(updatedList)) { sendJson(res, 409, { error: 'watch_busy' }); return }
        watches = updatedList
        sendJson(res, 200, { ok: true, item, items: watches })
        return
      }

      if (path === '/argo/api/watches/run' && req.method === 'POST') {
        const body = await readJsonBody(req)
        watches = loadWatches()
        const target = watches.find(w => w.id === body.id)
        if (!target) { sendJson(res, 404, { error: 'watch_not_found' }); return }
        if (watchBusy) { sendJson(res, 409, { error: 'watch_busy' }); return }
        // 与定时器同锁：手动触发与另一宿主的执行互斥
        const execLock = acquireStateFileLock(`${WATCH_STORE_FILE}.exec.lock`)
        if (!execLock) { sendJson(res, 409, { error: 'watch_busy' }); return }
        watchBusy = true
        try {
          const updated = await executeWatch(target)
          sendJson(res, 200, { ok: true, item: updated, items: watches })
        } finally {
          watchBusy = false
          releaseStateFileLock(execLock)
        }
        return
      }

      if (path === '/argo/api/watches' && req.method === 'DELETE') {
        const id = url.searchParams.get('id') || ''
        const updatedList = mutateWatches((list) => list.filter(w => w.id !== id))
        if (!Array.isArray(updatedList)) { sendJson(res, 409, { error: 'watch_busy' }); return }
        watches = updatedList
        sendJson(res, 200, { ok: true, items: watches })
        return
      }

      if (path === '/argo/api/health' && req.method === 'GET') {
        const hasLlm = Boolean(ctx.get?.('llm') && ctx.get?.('agentDefaultModel')?.currentSelection?.()?.model)
        const hs = loadHotspotStore()
        sendJson(res, 200, {
          ok: true,
          modes: Object.entries(MODE_LABELS).map(([id, label]) => ({ id, label })),
          hasLlm,
          egoSearch: readEgoSearchStatus(),
          breaker: readBreakerSummary(),
          hotspotSchedule: { refreshTime: hs.refreshTime, lastRefreshedAt: hs.lastRefreshedAt },
        })
        return
      }

      if (path === '/argo/api/suggestions' && req.method === 'GET') {
        const reqMode = MODES.has(url.searchParams.get('mode')) ? url.searchParams.get('mode') : 'general'
        const lang = url.searchParams.get('lang') || 'zh'
        const shuffle = url.searchParams.get('shuffle') === '1'
        const store = loadHotspotStore()
        if (hotspotRetryDue(store)) triggerDailyHotspotRefresh()
        // 本机模式建议词与 callLocalSeek 同源同路径
        sendJson(res, 200, buildSuggestions(reqMode, lang, shuffle, {
          watches,
          hotspotStore: store,
          cwd: resolveLocalSearchPath(config),
        }))
        return
      }

      if (path === '/argo/api/suggestions/config' && req.method === 'POST') {
        const body = await readJsonBody(req)
        let store = loadHotspotStore()
        if (body.refreshTime !== undefined) {
          const updated = updateRefreshTime(body.refreshTime)
          if (!updated) { sendJson(res, 400, { error: 'invalid_refresh_time' }); return }
          store = updated
        }
        if (body.refreshNow === true) {
          store = await triggerDailyHotspotRefresh()
        }
        sendJson(res, 200, {
          ok: true,
          refreshTime: store.refreshTime,
          lastRefreshedAt: store.lastRefreshedAt,
          hotspotCount: (store.zh?.length || 0) + (store.en?.length || 0),
          refreshing: hotspotRefresher.isBusy(),
          // 锁被另一宿主占时如实回传 skipped，前端提示「另一宿主刷新中」，
          // 不再拿旧时间戳冒充刚刷过（2026-11-21 审计：假成功边界）
          skipped: store.skipped || null,
        })
        return
      }

      if (path === '/argo/api/ego-search' && req.method === 'POST') {
        const body = await readJsonBody(req)
        const action = body.enabled ? 'enable' : 'disable'
        await new Promise((resolve) => {
          execFile(
            resolveArgoBin({ PATH: mergedLaunchdPath() }),
            ['ego-search', action],
            { timeout: 8000, env: { ...process.env, PATH: mergedLaunchdPath() } },
            () => resolve(),
          )
        })
        sendJson(res, 200, { ok: true, egoSearch: readEgoSearchStatus() })
        return
      }

      if (path === '/argo/api/diagnostics' && req.method === 'GET') {
        sendJson(res, 200, { ok: true, items: diagnostics })
        return
      }

      if (path === '/argo/api/stream' && req.method === 'GET') {
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

          const credentials = typeof getCredentials === 'function' ? getCredentials() : undefined
          const env = await resolveArgoEnv(config, { credentials }).catch(() => ({}))
          const clarifyPromise = runClarifyIfShort(query, mode, env, ac.signal)

          let cards
          let payload
          let trackStats
          let cached = !refresh ? getCached(cacheKey) : null
          if (cached) {
            cards = cached.cards
            payload = cached.payload
            trackStats = cached.trackStats
          } else {
            let pending = !refresh ? inflight.get(cacheKey) : null
            if (!pending) {
              // 共享去重任务使用独立 signal，防止客户端 A 断连误杀同 key 并发等待的客户端 B
              const searchSignal = refresh ? ac.signal : new AbortController().signal
              pending = callArgoSearch(config, env, query, mode, refresh, searchSignal, researchTracks)
                .finally(() => inflight.delete(cacheKey))
              if (!refresh) inflight.set(cacheKey, pending)
            }
            const resData = await pending
            cards = resData.cards
            payload = resData.payload
            trackStats = resData.trackStats
            if (cards.length > 0) setCached(cacheKey, { cards, payload, trackStats })
          }

          if (ac.signal.aborted) return

          const clarify = await clarifyPromise
          if (clarify) sendSse(res, 'clarify', clarify)

          if (mode !== 'local' && cards.length > 0) {
            sendSse(res, 'status', { stage: 'verifying', message: '正在计算证据可信度与交叉验证…' })
            const scored = await runEvidenceScoring(query, cards, env, ac.signal)
            cards = scored.cards
            if (scored.evidenceSummary) sendSse(res, 'evidence', scored.evidenceSummary)
          }

          sendSse(res, 'cards', {
            cards,
            funnel: payload?.funnel || null,
            engines: [...new Set(cards.map(c => c.source))],
          })
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
            finalText = buildFallbackSummary(query, mode, cards, llmRes)
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
            const total = (researchTracks || []).length
            // 真实 track 成败统计进门禁；旧缓存条目无 trackStats 时按结果有无推断，
            // 不再恒注入 completedTracks/failedTracks: 0 的假章（2026-11-21 审计）
            const stats = trackStats || {
              total,
              completed: cards.length > 0 ? total : 0,
              failed: 0,
            }
            const gate = evaluateGates({
              answer: finalText,
              sources: cards.map(c => ({ url: c.url, title: c.title, domain: c.domain })),
              tracks: researchTracks || [],
              warnings: [],
              stats: {
                sourceCount: cards.length,
                completedTracks: stats.completed,
                failedTracks: stats.failed,
              },
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
        const body = await readJsonBody(req)
        const question = String(body.question || '').trim()
        // 元素级过滤：null/非对象条目会在 buildEvidenceContext 与 history
        // 映射处抛 TypeError（2026-11-18 实跑复现 cards:[null] 崩溃链）
        const cards = Array.isArray(body.cards)
          ? body.cards.filter(c => c && typeof c === 'object')
          : []
        const history = Array.isArray(body.history)
          ? body.history.filter(h => h && typeof h === 'object')
          : []
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
        const onClose = () => { if (!res.writableEnded) ac.abort() }
        if (typeof res.on === 'function') res.on('close', onClose)
        else if (typeof req.on === 'function') req.on('close', onClose)

        try {
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
            signal: ac.signal,
            onDelta: (delta) => sendSse(res, 'answer', { delta }),
          })

          if (!llmRes.ok && !llmRes.text) {
            const msg = llmRes.reason === 'no_model'
              ? '当前宿主未配置默认模型，无法进行多轮追问推理。'
              : `默认模型（${llmRes.model || 'unknown'}）调用未完成：${llmRes.message || llmRes.reason}`
            sendSse(res, 'answer', { delta: msg })
          }
          sendSse(res, 'citations', extractCitations(llmRes.text || '', cards))
          sendSse(res, 'done', { ok: llmRes.ok })
        } catch (err) {
          if (!ac.signal.aborted) {
            sendSse(res, 'failure', { message: String(err?.message ?? err) })
          }
        } finally {
          if (!res.writableEnded) res.end()
        }
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

export function registerArgoWorkbench(ctx, config, getCredentials, options = {}) {
  const attach = (targetCtx) => {
    let webServer
    try {
      webServer = targetCtx?.get?.('webServer') ?? targetCtx?.webServer
    } catch {
      return undefined
    }
    if (!webServer || typeof webServer.register !== 'function') return undefined
    const handler = createWorkbenchHandler(targetCtx, config, getCredentials, options)
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

/*
 * @taxueseek/argo-dsh — 工作台检索管线：模式规格、卡片归一、CLI 编排与 LLM 直流。
 * 自 workbench.js 拆出以守 1000 行门禁（2026-11-21）；HTTP 骨架与宿主 handler 留在 workbench.js。
 */

import { realpathSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { DEFAULTS, stripBoundary } from './runner.js'
import { runArgoCli } from './workbench-io.js'
import { boundedMap } from './wide-research.js'

export const CITATION_RULE = [
  '严格基于提供的检索证据回答，所有事实判断、数据与观点必须在句末使用方括号编号引用，例如 [1] 或 [2][3]。',
  '严禁编造未出现在证据中的数字、链接或来源；如果证据不足或存在冲突，直接指出并并列呈现不同来源。',
  '使用清晰的中文 Markdown 结构（小标题、要点列表或对比表格），先给核心结论，再展开细节。',
].join('\n')

export const MODE_SPECS = {
  general: { label: '全网搜索', searchArgs: { max_results: 12, verify: 2, depth: 'balanced' }, prompt: '当前模式为「全网搜索」：先用 2 到 3 句话给出直接结论，再用清晰小标题展开关键依据与延伸信息。' },
  hotspot: { label: '热点分析', searchArgs: { max_results: 14, intent: 'news', sort: 'newest', since: '14d', depth: 'balanced', verify: 2 }, socialSupplement: true, prompt: '当前模式为「热点分析」：按「一、核心结论与现状」「二、事件脉络」「三、核心争议与各方立场」「四、影响与后续关注点」组织输出。' },
  verify: { label: '真假核验', searchArgs: { max_results: 12, verify: 5, depth: 'deep', mode: 'deep' }, prompt: '当前模式为「真假核验」：按「一、核验结论（明确给出可信度判断：可信 / 存疑 / 失实 / 证据不足）」「二、关键证据链（优先引用已核验正文）」「三、冲突点与核验边界」组织输出。' },
  research: { label: '深度研究', searchArgs: { max_results: 16, verify: 5, depth: 'deep', mode: 'deep' }, prompt: '当前模式为「深度研究」：按「一、执行摘要」「二、多维拆解与关键发现」「三、数据与案例支撑」「四、风险、局限与待验证项」组织详尽研究报告。' },
  compare: { label: '选型对比', searchArgs: { max_results: 14, verify: 3, depth: 'balanced' }, prompt: '当前模式为「选型对比」：按「一、选型结论与推荐」「二、核心维度对比表（Markdown 表格）」「三、各自优缺点与适用人群」「四、替代方案与踩坑提示」组织输出。' },
  academic: { label: '学术文献', searchArgs: { max_results: 14, intent: 'academic', depth: 'deep', mode: 'deep', verify: 3 }, prompt: '当前模式为「学术文献」：按「一、核心研究结论与共识」「二、代表性论文与方法拆解」「三、实验证据与关键数据」「四、局限性与延伸方向」组织输出。' },
  plugin: { label: '插件评测', searchArgs: { max_results: 14, verify: 3, depth: 'balanced' }, socialSupplement: true, prompt: '当前模式为「插件评测」：按「一、是否值得安装（结论）」「二、核心能力与亮点」「三、社区真实反馈与局限」「四、替代方案与安装配置注意」组织输出。' },
  local: { label: '本机检索', searchArgs: { max_results: 16 }, prompt: '当前模式为「本机检索」：基于本机文件与代码命中证据回答，标明文件路径、行号与上下文关联，禁止编造未命中的本地文件。' },
}

export const MODES = new Set(Object.keys(MODE_SPECS))
export const MODE_LABELS = Object.fromEntries(Object.entries(MODE_SPECS).map(([k, v]) => [k, v.label]))

function extractDomain(url) {
  try {
    return new URL(url).hostname.replace(/^www\./i, '')
  } catch {
    return ''
  }
}

export function normalizeCandidates(payload) {
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

export function buildSystemPrompt(mode) {
  const spec = MODE_SPECS[mode] || MODE_SPECS.general
  return `${CITATION_RULE}\n${spec.prompt}`
}

export function normalizeLocalCards(payload) {
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

export function buildEvidenceContext(cards, maxChars = 14000) {
  const lines = []
  let used = 0
  for (const c of cards) {
    if (!c || typeof c !== 'object') continue
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

export function buildFallbackSummary(query, mode, cards, failure = null) {
  const label = MODE_LABELS[mode] || '全网综合'
  if (cards.length === 0) {
    return `### 未检索到相关结果\n\n针对「${query}」在 **${label}** 模式下未命中有效网页结果，建议精简关键词或点击右上角「强制刷新」重试。`
  }
  const rows = cards.slice(0, 8).map((c) => {
    const text = (c.verifiedText || c.snippet || '').slice(0, 180).replace(/\n+/g, ' ')
    return `- **${c.title}** [${c.index}]（${c.domain || c.source}）\n  ${text || '点击右侧卡片查看原文'}`
  })
  const hasRealFailure = failure && failure.reason && failure.reason !== 'no_model'
  const header = hasRealFailure
    ? `### ${label} · 聚合检索结果（模型调用未完成，已切回直出视图）\n\n默认模型（\`${failure.model || 'unknown'}\`）暂不可用：${failure.message || failure.reason}。已为你整理最相关的 ${cards.length} 条多源证据，可直接在右侧核对原文：`
    : `### ${label} · 聚合检索结果（零模型直出模式）\n\n当前宿主未配置可用默认模型，已为你整理最相关的 ${cards.length} 条多源证据，可直接在右侧核对原文：`
  return `${header}\n\n${rows.join('\n\n')}`
}

// 榜单词识别：对齐 config.yaml hot_trending，供 hotspot 模式跳过 --intent news 单引擎锁定
const HOT_LIST_QUERY_RE = /(热搜|热榜|热搜榜|实时热搜|今日热搜|今天热搜|微博热搜|百度热搜|B站热搜|bilibili热搜|头条热榜|抖音热榜|知乎热榜|知乎热搜|热门话题|trending now|hot search|top trends)/i

export function isHotListQuery(query) {
  return HOT_LIST_QUERY_RE.test(String(query || ''))
}

export function mergeDedupCards(lists, maxCards = 16) {
  const nonEmpty = (lists || []).filter(l => Array.isArray(l) && l.length > 0)
  if (nonEmpty.length === 0) return []
  // 多路合并时为次路（如 socialSupplement）保留保底席位，避免主路满 16 条直接挤空次路
  const reservedTail = nonEmpty.length === 2 ? Math.min(4, nonEmpty[1].length) : 0
  const primaryCap = reservedTail > 0 ? Math.max(1, maxCards - reservedTail) : maxCards
  const seen = new Set()
  const merged = []
  const pushFrom = (list, limit) => {
    for (const card of list) {
      if (!card?.url || seen.has(card.url)) continue
      seen.add(card.url)
      merged.push({ ...card, id: `c${merged.length + 1}`, index: merged.length + 1 })
      if (merged.length >= limit) return
    }
  }
  if (reservedTail > 0) {
    pushFrom(nonEmpty[0], primaryCap)
    pushFrom(nonEmpty[1], maxCards)
    if (merged.length < maxCards) pushFrom(nonEmpty[0], maxCards)
    return merged
  }
  for (const list of nonEmpty) {
    pushFrom(list, maxCards)
    if (merged.length >= maxCards) return merged
  }
  return merged
}

// 本机检索根目录唯一出口：callLocalSeek 与本机模式建议词必须同源——
// 旧版建议词读宿主 cwd、实际搜索另一路径，点了搜不到（2026-11-21 审计）
export function resolveLocalSearchPath(config) {
  return config?.localSearchPath || process.env.ARGO_LOCAL_SEARCH_PATH || join(homedir(), 'Documents')
}

// track 成败真章：payload 缺失或带 errors 即失败。旧版恒注入
// completedTracks/failedTracks: 0，no_completed_tracks、all_tracks_failed、
// partial_track_failure 三类门禁永久失效（2026-11-21 审计：与热榜假成功
// 同根因——写章条件与读章消费不对齐，凡盖章处必须有契约对拍）。
export function summarizeTrackRuns(trackRuns) {
  const total = trackRuns.length
  const failed = trackRuns.filter(r => r.payload === null || r.payload === undefined
    || (Array.isArray(r.payload.errors) && r.payload.errors.length > 0)).length
  return { total, completed: total - failed, failed }
}

export async function callLocalSeek(config, env, query, maxResults = 16, signal) {
  const searchPath = resolveLocalSearchPath(config)
  try {
    const payload = await runArgoCli(
      ['seek', '--path', searchPath, '--json', '--max', String(Math.min(20, Math.max(1, maxResults))), '--', query],
      { env, timeoutMs: 15000, signal },
    )
    return { payload, cards: normalizeLocalCards(payload) }
  } catch (err) {
    return { payload: { errors: [String(err?.message ?? err)] }, cards: [] }
  }
}

export async function callArgoSearch(config, env, query, mode, refresh, signal, tracks = null) {
  if (mode === 'local') {
    return callLocalSeek(config, env, query, MODE_SPECS.local.searchArgs.max_results, signal)
  }

  const timeoutMs = mode === 'research'
    ? Math.max(config.searchTimeoutMs ?? 60000, 90000)
    : (config.searchTimeoutMs ?? DEFAULTS.searchTimeoutMs)

  if (mode === 'research' && Array.isArray(tracks) && tracks.length > 1) {
    const trackRuns = await boundedMap(tracks, 3, async (track) => {
      const cliArgs = buildSearchCliArgs(track.question || query, 'research', refresh, { max_results: 10, verify: 3 })
      try {
        const payload = await runArgoCli(cliArgs, { env, timeoutMs, signal })
        return { payload, cards: normalizeCandidates(payload) }
      } catch (err) {
        // 失败要留痕：旧 .catch(() => ({})) 把失败伪装成空 payload，
        // track 成败从此不可判（2026-11-21 审计：假章同根因）
        return { payload: null, error: String(err?.message ?? err), cards: [] }
      }
    })
    return {
      payload: trackRuns[0]?.payload || {},
      cards: mergeDedupCards(trackRuns.map(r => r.cards), 16),
      trackStats: summarizeTrackRuns(trackRuns),
    }
  }

  const primaryTask = runArgoCli(buildSearchCliArgs(query, mode, refresh), { env, timeoutMs, signal })
    .then(payload => ({ payload, cards: normalizeCandidates(payload) }))

  // 热点分析与插件评测并行补充社区/社交域召回（榜单词已原生多平台聚合，跳过冗余补搜）
  const needSocial = Boolean(MODE_SPECS[mode]?.socialSupplement) && !(mode === 'hotspot' && isHotListQuery(query))
  const socialTask = needSocial
    ? runArgoCli(buildSearchCliArgs(query, 'general', refresh, { intent: 'social', max_results: 6, verify: 0 }), { env, timeoutMs: Math.min(timeoutMs, 15000), signal })
        .then(payload => normalizeCandidates(payload))
        .catch(() => [])
    : Promise.resolve([])

  const [primary, socialCards] = await Promise.all([primaryTask, socialTask])
  let combined = mergeDedupCards([primary.cards, socialCards], 16)

  if (mode === 'plugin' && combined.length < 6) {
    const fallbackPayload = await runArgoCli(
      buildSearchCliArgs(`${query} 评测 替代`, 'general', refresh, { verify: 2 }),
      { env, timeoutMs, signal },
    ).catch(() => ({}))
    combined = mergeDedupCards([combined, normalizeCandidates(fallbackPayload)], 16)
  }

  return { payload: primary.payload, cards: combined, trackStats: summarizeTrackRuns([{ payload: primary.payload }]) }
}

export async function runClarifyIfShort(query, mode, env, signal) {
  if (mode === 'local' || query.length > 18) return null
  try {
    const data = await runArgoCli(['clarify', '--json', '--', query], { env, timeoutMs: 3000, signal })
    if (!data || (!data.ambiguities?.length && !data.routing?.explanation)) return null
    return {
      ambiguities: data.ambiguities || [],
      explanation: data.routing?.explanation || '',
      recommendedStrategy: data.recommended_strategy || '',
    }
  } catch {
    return null
  }
}

export async function runEvidenceScoring(query, cards, env, signal) {
  if (!cards.length) return { cards, evidenceSummary: null }
  try {
    const payload = await runArgoCli(
      ['evidence', '--', query],
      {
        env,
        stdin: {
          results: cards.map(c => ({
            title: c.title,
            url: c.url,
            source: c.source,
            snippet: c.verifiedText || c.snippet,
            published_at: c.publishedAt,
          })),
        },
        timeoutMs: 5000,
        signal,
      },
    )
    const scored = Array.isArray(payload?.results) ? payload.results : []
    const enriched = cards.map((c, idx) => {
      const ev = scored[idx]?.credibility
      if (!ev) return c
      return {
        ...c,
        credibility: typeof ev.final === 'number' ? ev.final : c.credibility,
        authorityTier: ev.authority?.tier || null,
        authorityReason: ev.authority?.reason || '',
      }
    })
    return {
      cards: enriched,
      evidenceSummary: {
        crossValidation: payload?.cross_validation || null,
        summary: payload?.summary || null,
      },
    }
  } catch {
    return { cards, evidenceSummary: null }
  }
}

export async function streamLlmAnswer(ctx, { system, messages, signal, onDelta }) {
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
      ...(selection.reasoningEffort ? { reasoningEffort: selection.reasoningEffort } : {}),
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

export function extractCitations(text, cards) {
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

// 供测试断言「flag 全部前置、query 经 `--` 分隔殿后」的参数形态
export function buildSearchCliArgs(query, mode, refresh = false, overrides = {}) {
  const spec = MODE_SPECS[mode] || MODE_SPECS.general
  const baseArgs = { ...spec.searchArgs }
  // hotspot 模式下查询热榜/热搜榜时，直通 Argo 原生 hot_trending 多平台热榜聚合，
  // 避免 --intent news 将其锁死到单引擎 deepseek_search 且免除热榜无 published_at 的时间窗告警
  if (mode === 'hotspot' && isHotListQuery(query)) {
    delete baseArgs.intent
    delete baseArgs.since
  }
  const merged = { ...baseArgs, ...overrides }
  const args = ['search', '--json']
  if (merged.envelope) args.push('--envelope')
  if (merged.max_results) args.push('-n', String(merged.max_results))
  if (merged.depth) args.push('--depth', String(merged.depth))
  if (merged.mode) args.push('--mode', String(merged.mode))
  if (merged.intent) args.push('--intent', String(merged.intent))
  if (merged.domain) args.push('--domain', String(merged.domain))
  if (merged.since) args.push('--since', String(merged.since))
  if (merged.sort) args.push('--sort', String(merged.sort))
  if (merged.verify) args.push('--verify', String(merged.verify))
  if (refresh) args.push('--no-cache')
  // `--` 后的全部 token 被 argparse 当位置参数：flag 必须全部前置，否则
  // 以 `-` 开头的 query（如 `--list-engines`）会被 CLI 当 flag 吃掉
  // （2026-11-18 实跑复现：`argo search "--list-engines"` 返回引擎清单）
  args.push('--', query)
  return args
}

function _safeRealpath(p) {
  const abs = resolve(p)
  try {
    return realpathSync(abs)
  } catch {
    // 文件尚未存在时对其父目录解析符号链接，避免通过目录 symlink 越界
    const parent = dirname(abs)
    if (parent && parent !== abs) {
      try {
        return join(realpathSync(parent), relative(parent, abs))
      } catch { /* ignore */ }
    }
    return abs
  }
}

export function isPathWithinRoot(targetPath, allowedRoot) {
  if (!targetPath || !allowedRoot) return false
  const rootReal = _safeRealpath(allowedRoot)
  const resolvedTarget = isAbsolute(targetPath) ? targetPath : join(rootReal, targetPath)
  const targetReal = _safeRealpath(resolvedTarget)
  const rel = relative(rootReal, targetReal)
  return rel === '' || (!rel.startsWith('..' + sep) && rel !== '..' && !isAbsolute(rel))
}

// 本地代码卡片一跳精读：复用 local-seek 的 --symbol-body / --lines / --outline JSON 输出
export async function inspectLocalFile(
  env,
  { filePath, allowedRoot = '', symbol = '', lines = '', signal = null, runner = runArgoCli } = {},
) {
  const target = String(filePath || '').replace(/^file:\/\//, '').trim()
  if (!target || target.includes('\0')) return { ok: false, error: 'invalid_path' }
  const root = String(allowedRoot || '').trim() || resolveLocalSearchPath({})
  if (!isPathWithinRoot(target, root)) {
    return { ok: false, error: 'path_outside_root' }
  }
  const resolvedTarget = isAbsolute(target) ? resolve(target) : resolve(root, target)
  const cliArgs = ['seek', '--json']
  if (symbol && String(symbol).trim()) {
    cliArgs.push('--symbol-body', String(symbol).trim())
  } else if (lines && String(lines).trim()) {
    cliArgs.push('--lines', String(lines).trim())
  } else {
    cliArgs.push('--outline')
  }
  cliArgs.push('--', resolvedTarget)
  return runner(cliArgs, { env, timeoutMs: 8000, signal })
}

// 工作台一键沉淀研报：通过 stdin JSON 管道直连 `argo note study --from-json`
export async function saveNoteStudy(env, { payload, title = '', signal = null, runner = runArgoCli } = {}) {
  if (!payload || typeof payload !== 'object') return { ok: false, error: 'missing_payload' }
  const cliArgs = ['note', 'study', '--from-json']
  if (title && String(title).trim()) cliArgs.push('--title', String(title).trim())
  return runner(cliArgs, { env, stdin: payload, timeoutMs: 10000, signal })
}

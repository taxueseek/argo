/*
 * @taxueseek/argo-dsh — 搜索分析工作台前端模块（ES Module，零构建依赖）
 *
 * 通过 /argo/workbench-client.js 由 Host 提供，挂载到 DSH：
 *   - main slot（key: 'argo-workbench'）
 *   - sidebar.panellist（id: 'argo-workbench'）
 *   - 全局划词浮层（选中对话内容一键进入工作台分析）
 */

import { CSS, renderMarkdown } from '/argo/workbench-md.js'

const STATE_KEY = 'argo-workbench:state.v1'
const PREFS_KEY = 'argo-workbench:prefs.v1'

const CORE_MODES = [
  { id: 'general', label: '全网搜索', desc: '多源快查，先给结论再列出处', ph: '搜任何问题、概念、报错或资料（Enter 发送，Shift+Enter 换行）…' },
  { id: 'hotspot', label: '热点分析', desc: '梳理近期事件脉络、争议与各方立场', ph: '输入热点事件、行业动态或争议话题，看多方观点…' },
  { id: 'research', label: '深度研究', desc: '多维拆解、原文核验并生成研究报告', ph: '输入需要系统调研的复杂问题，生成带来源账本的报告…' },
]

const TOPIC_CARDS = [
  {
    id: 'verify',
    label: '真假核验',
    desc: '抓取原文核对出处，给出可信度判断',
    ph: '输入需要核验真假的传闻、数据或说法…',
    samples: [
      '核验某项公开行业统计数据的原始官方出处',
      '核实近期热门科技传闻的官方公告与时间线',
      '核对某软件开源协议或最新收费政策的官方条款',
    ],
  },
  {
    id: 'compare',
    label: '选型对比',
    desc: '多维度对比产品、方案优缺点与替代',
    ph: '输入要对比的两个或多个产品、工具或方案…',
    samples: [
      '主流本地知识库方案对比：架构、成本与适用场景',
      '常见全文检索与向量数据库选型优缺点对比',
      '主流个人知识管理工具功能与迁移成本对比',
    ],
  },
  {
    id: 'academic',
    label: '学术文献',
    desc: '学术多源深挖，梳理方法与实验结论',
    ph: '输入论文主题、研究方向或具体文献关键词…',
    samples: [
      '推理时计算扩展方法最新研究综述与实验对比',
      '多智能体协作与检索增强生成前沿论文梳理',
      '端侧模型量化与低延迟推理优化技术进展',
    ],
  },
  {
    id: 'plugin',
    label: '插件评测',
    desc: '汇总官方说明、社区口碑与安装注意',
    ph: '输入想评估的工具、扩展或插件类型…',
    samples: [
      '热门效率工具与浏览器扩展的真实口碑与安全评估',
      '常用代码编辑器高星插件生态对比与避坑指南',
      '开源自动化工作流插件的局限性与替代方案',
    ],
  },
  {
    id: 'local',
    label: '本机检索',
    desc: '基于 local-seek 检索本机代码与文档',
    ph: '输入要在本机检索的关键词、符号或文件名…',
    samples: [
      'circuit_breaker',
      'engine_states',
      'wide-research',
    ],
  },
]

const pickSample = (samples) => samples[Math.floor(Math.random() * samples.length)]

const ALL_MODES = [...CORE_MODES, ...TOPIC_CARDS]

function injectStyle() {
  if (typeof document === 'undefined') return
  if (document.getElementById('argo-wb-style')) return
  const el = document.createElement('style')
  el.id = 'argo-wb-style'
  el.textContent = CSS
  document.head.appendChild(el)
}

function readStorage(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key)
    return raw ? JSON.parse(raw) : fallback
  } catch {
    return fallback
  }
}

function writeStorage(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch { /* ignore */ }
}

function formatInterval(mins) {
  if (mins >= 43200) return '每月一次'
  if (mins >= 10080) return '每周一次'
  if (mins >= 1440) return '每日一次'
  return mins >= 60 ? `每 ${Math.round(mins / 60)} 小时` : `每 ${mins} 分钟`
}

function formatTime(iso) {
  if (!iso) return '尚未运行'
  try {
    const d = new Date(iso)
    return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  } catch {
    return iso
  }
}

export function mountArgoWorkbench(ctx, react) {
  injectStyle()
  const h = react.createElement
  const { useState, useEffect, useRef } = react

  let pendingExternal = null
  const listeners = new Set()

  const requestSearch = (query, mode = 'general') => {
    const clean = String(query || '').trim()
    if (!clean) return false
    pendingExternal = { query: clean, mode }
    for (const fn of listeners) fn(pendingExternal)
    return true
  }

  const resolveLang = () => {
    const loc = ctx?.locale ?? (typeof ctx?.get === 'function' ? ctx.get('locale') : null)
    const active = loc?.getSnapshot?.()?.active || (typeof navigator !== 'undefined' ? navigator.language : 'zh')
    return String(active).toLowerCase().startsWith('en') ? 'en' : 'zh'
  }

  function WorkbenchApp() {
    const [initialSaved] = useState(() => readStorage(STATE_KEY, null))
    const [prefs, setPrefs] = useState(() => readStorage(PREFS_KEY, { selection: true }))
    const [lang, setLang] = useState(resolveLang)
    const [view, setView] = useState('home')
    const [input, setInput] = useState('')
    const [activeQuery, setActiveQuery] = useState(() => initialSaved?.query || '')
    const [mode, setMode] = useState('general')
    const [topicSamples, setTopicSamples] = useState(() => Object.fromEntries(TOPIC_CARDS.map(t => [t.id, pickSample(t.samples)])))
    const [suggestions, setSuggestions] = useState([])
    const [cards, setCards] = useState(() => initialSaved?.cards || [])
    const [turns, setTurns] = useState(() => initialSaved?.turns || [])
    const [stageText, setStageText] = useState('')
    const [loading, setLoading] = useState(false)
    const [filter, setFilter] = useState('all')
    const [flashId, setFlashId] = useState('')
    const [followup, setFollowup] = useState('')
    const [popover, setPopover] = useState(null) // null | 'debug' | 'settings' | 'watch'
    const [diagItems, setDiagItems] = useState([])
    const [healthInfo, setHealthInfo] = useState(null)
    const [gateResult, setGateResult] = useState(null)
    const [clarifyInfo, setClarifyInfo] = useState(null)
    const [evidenceSummary, setEvidenceSummary] = useState(null)
    const [watches, setWatches] = useState([])
    const [watchQuery, setWatchQuery] = useState('')
    const [watchInterval, setWatchInterval] = useState(1440)
    const [watchBusyId, setWatchBusyId] = useState('')
    const inputRef = useRef(null)
    const esRef = useRef(null)
    const abortRef = useRef(null)
    const modeRef = useRef(mode)
    const startSearchRef = useRef(null)
    const suggestSeqRef = useRef(0)
    modeRef.current = mode

    useEffect(() => {
      if (loading) return undefined
      const t = setTimeout(() => {
        writeStorage(STATE_KEY, { query: activeQuery, mode, cards, turns })
      }, 400)
      return () => clearTimeout(t)
    }, [activeQuery, mode, cards, turns, loading])

    const refreshWatches = async () => {
      try {
        const res = await fetch('/argo/api/watches')
        const data = await res.json()
        if (Array.isArray(data.items)) setWatches(data.items)
      } catch { /* ignore */ }
    }

    const refreshSuggestions = async (targetMode = mode, targetLang = lang, shuffle = false) => {
      const seq = ++suggestSeqRef.current
      try {
        const res = await fetch(`/argo/api/suggestions?mode=${encodeURIComponent(targetMode)}&lang=${encodeURIComponent(targetLang)}&shuffle=${shuffle ? '1' : '0'}`)
        const data = await res.json()
        if (seq === suggestSeqRef.current && Array.isArray(data?.items)) setSuggestions(data.items)
      } catch { /* ignore */ }
    }

    useEffect(() => {
      const loc = ctx?.locale ?? (typeof ctx?.get === 'function' ? ctx.get('locale') : null)
      if (!loc?.subscribe) return undefined
      return loc.subscribe(() => setLang(resolveLang()))
    }, [])

    useEffect(() => {
      refreshSuggestions(mode, lang, false)
    }, [mode, lang])

    useEffect(() => { refreshWatches() }, [])

    const startSearch = (qText, targetMode = mode, refresh = false) => {
      const q = String(qText || '').trim()
      if (!q) return
      if (esRef.current) { esRef.current.close(); esRef.current = null }
      if (abortRef.current) { abortRef.current.abort(); abortRef.current = null }

      setView('result')
      setActiveQuery(q)
      setInput(q)
      setMode(targetMode)
      setCards([])
      setTurns([{ role: 'assistant', text: '' }])
      setStageText('正在连接 Argo 多源检索引擎…')
      setLoading(true)
      setFilter('all')
      setPopover(null)
      setGateResult(null)
      setClarifyInfo(null)
      setEvidenceSummary(null)

      const url = `/argo/api/stream?q=${encodeURIComponent(q)}&mode=${encodeURIComponent(targetMode)}&refresh=${refresh ? '1' : '0'}`
      const es = new EventSource(url)
      esRef.current = es

      const parse = (ev) => { try { return JSON.parse(ev.data) } catch { return {} } }
      es.addEventListener('status', (ev) => { const d = parse(ev); if (d.message) setStageText(d.message) })
      es.addEventListener('clarify', (ev) => { const d = parse(ev); if (d && (d.explanation || d.ambiguities?.length)) setClarifyInfo(d) })
      es.addEventListener('evidence', (ev) => { const d = parse(ev); if (d && (d.crossValidation || d.summary)) setEvidenceSummary(d) })
      es.addEventListener('cards', (ev) => { const d = parse(ev); if (Array.isArray(d.cards)) setCards(d.cards) })
      es.addEventListener('gate', (ev) => { const d = parse(ev); if (typeof d.passed === 'boolean') setGateResult(d) })
      es.addEventListener('answer', (ev) => {
        const d = parse(ev)
        if (!d.delta) return
        setTurns(prev => {
          const next = [...prev]
          const last = next[next.length - 1]
          if (last && last.role === 'assistant') next[next.length - 1] = { ...last, text: last.text + d.delta }
          return next
        })
      })
      es.addEventListener('answer-status', (ev) => { const d = parse(ev); if (d.message) setStageText(d.message) })
      es.addEventListener('done', () => { setLoading(false); setStageText(''); es.close(); esRef.current = null })
      es.addEventListener('failure', (ev) => { const d = parse(ev); setLoading(false); setStageText(`检索出错：${d.message || '未知错误'}`); es.close(); esRef.current = null })
      es.onerror = () => {
        es.close()
        if (esRef.current === es) esRef.current = null
        setLoading(false)
      }
    }
    startSearchRef.current = startSearch

    useEffect(() => {
      const handler = (req) => {
        if (!req) return
        pendingExternal = null
        startSearchRef.current?.(req.query, req.mode || modeRef.current)
      }
      listeners.add(handler)
      if (pendingExternal) handler(pendingExternal)
      return () => {
        listeners.delete(handler)
        if (esRef.current) esRef.current.close()
        if (abortRef.current) abortRef.current.abort()
      }
    }, [])

    const handleFollowup = async (e) => {
      e.preventDefault()
      const q = followup.trim()
      if (!q || loading) return
      setFollowup('')
      setLoading(true)
      setStageText('正在结合当前证据回答追问…')

      const historySnapshot = [...turns]
      setTurns(prev => [...prev, { role: 'user', text: q }, { role: 'assistant', text: '' }])

      const ac = new AbortController()
      abortRef.current = ac
      try {
        const res = await fetch('/argo/api/followup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query: activeQuery, mode, cards, history: historySnapshot, question: q }),
          signal: ac.signal,
        })
        if (!res.ok || !res.body) {
          setStageText(`追问失败（HTTP ${res.status}）`)
          return
        }
        const reader = res.body.getReader()
        const dec = new TextDecoder()
        let buf = ''
        while (true) {
          const { value, done } = await reader.read()
          if (done) break
          buf += dec.decode(value, { stream: true })
          const parts = buf.split('\n\n')
          buf = parts.pop() || ''
          for (const chunk of parts) {
            const evMatch = chunk.match(/^event:\s*(.+)$/m)
            const dataMatch = chunk.match(/^data:\s*(.+)$/m)
            if (!dataMatch) continue
            const evName = evMatch ? evMatch[1].trim() : 'message'
            let payload
            try { payload = JSON.parse(dataMatch[1]) } catch { continue }
            if (evName === 'answer' && payload.delta) {
              setTurns(prev => {
                const next = [...prev]
                const last = next[next.length - 1]
                if (last && last.role === 'assistant') next[next.length - 1] = { ...last, text: last.text + payload.delta }
                return next
              })
            } else if (evName === 'failure' && payload.message) {
              setStageText(`追问出错：${payload.message}`)
            }
          }
        }
      } catch { /* ignore */ } finally {
        setLoading(false)
        setStageText('')
        abortRef.current = null
      }
    }

    const createWatch = async (qStr, targetMode = mode, mins = watchInterval) => {
      const q = String(qStr || '').trim()
      if (!q) return
      try {
        const res = await fetch('/argo/api/watches', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query: q, mode: targetMode, intervalMinutes: mins }),
        })
        const data = await res.json()
        if (Array.isArray(data.items)) setWatches(data.items)
        setWatchQuery('')
        setPopover('watch') // 抽屉可见性由 popover 状态管（此前引用未定义的 setWatchOpen）
      } catch { /* ignore */ }
    }

    const runWatchNow = async (id) => {
      setWatchBusyId(id)
      try {
        const res = await fetch('/argo/api/watches/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id }),
        })
        const data = await res.json()
        if (Array.isArray(data.items)) setWatches(data.items)
      } finally {
        setWatchBusyId('')
      }
    }

    const deleteWatch = async (id) => {
      try {
        const res = await fetch(`/argo/api/watches?id=${encodeURIComponent(id)}`, { method: 'DELETE' })
        const data = await res.json()
        if (Array.isArray(data.items)) setWatches(data.items)
      } catch { /* ignore */ }
    }

    const openWatchResult = (w) => {
      setView('result')
      setActiveQuery(w.query)
      setInput(w.query)
      setMode(w.mode || 'general')
      setCards(Array.isArray(w.lastCards) ? w.lastCards : [])
      setTurns([{ role: 'assistant', text: w.lastSummary || '该定时任务尚未生成摘要，可点击「立即执行」或「强制刷新」。' }])
      setPopover(null)
    }

    const scrollToCard = (cardId) => {
      const el = document.getElementById(`aw-${cardId}`)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        setFlashId(cardId)
        setTimeout(() => setFlashId(''), 1400)
      }
    }

    const refreshHealth = async () => {
      try {
        const res = await fetch('/argo/api/health')
        const data = await res.json()
        if (data?.ok) setHealthInfo(data)
      } catch { /* ignore */ }
    }

    const openDebug = async () => {
      const next = popover === 'debug' ? null : 'debug'
      setPopover(next)
      if (next === 'debug') {
        refreshHealth()
        try {
          const res = await fetch('/argo/api/diagnostics')
          const data = await res.json()
          setDiagItems(data.items || [])
        } catch { /* ignore */ }
      }
    }

    const toggleWatchPanel = () => {
      const next = popover === 'watch' ? null : 'watch'
      setPopover(next)
      if (next === 'watch') refreshWatches()
    }

    const toggleSettingsPanel = () => {
      const next = popover === 'settings' ? null : 'settings'
      setPopover(next)
      if (next === 'settings') refreshHealth()
    }

    const toggleEgoSearch = async () => {
      const current = Boolean(healthInfo?.egoSearch?.enabled)
      try {
        const res = await fetch('/argo/api/ego-search', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: !current }),
        })
        const data = await res.json()
        if (data?.ok) setHealthInfo(prev => ({ ...(prev || {}), egoSearch: data.egoSearch }))
      } catch { /* ignore */ }
    }

    const toggleSelectionPref = () => {
      const next = { ...prefs, selection: !prefs.selection }
      setPrefs(next)
      writeStorage(PREFS_KEY, next)
    }

    const currentModeObj = ALL_MODES.find(m => m.id === mode) || CORE_MODES[0]
    const lockedTopic = TOPIC_CARDS.find(t => t.id === mode) || null
    const verifiedCount = cards.filter(c => c.verified).length
    const visibleCards = filter === 'verified'
      ? cards.filter(c => c.verified)
      : (filter === 'all' ? cards : cards.filter(c => c.source === filter))
    const sources = [...new Set(cards.map(c => c.source))]

    const renderWatchDrawer = () => popover !== 'watch' ? null : h('div', { className: 'aw-popover' },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' } },
        h('strong', null, '定时搜索与追踪'),
        h('button', { type: 'button', className: 'aw-filter', onClick: () => setPopover(null) }, '关闭')),
      h('form', {
        style: { display: 'flex', gap: '6px', marginBottom: '10px' },
        onSubmit: (e) => { e.preventDefault(); createWatch(watchQuery || activeQuery || input, mode, watchInterval) },
      },
        h('input', {
          className: 'aw-top-input',
          style: { border: '1px solid var(--aw-border)', borderRadius: '8px', padding: '5px 8px', background: 'var(--aw-sunken)' },
          placeholder: '输入定时追踪关键词…',
          value: watchQuery,
          onChange: (e) => setWatchQuery(e.target.value),
        }),
        h('select', {
          className: 'aw-select',
          style: { height: '28px' },
          value: watchInterval,
          onChange: (e) => setWatchInterval(Number(e.target.value)),
        },
          h('option', { value: 1440 }, '每日一次'),
          h('option', { value: 10080 }, '每周一次'),
          h('option', { value: 43200 }, '每月一次')),
        h('button', { type: 'submit', className: 'aw-filter active' }, '添加')),
      watches.length === 0
        ? h('div', { style: { color: 'var(--aw-text-3)', padding: '8px 0' } }, '暂无定时搜索任务')
        : watches.map(w => h('div', { key: w.id, className: 'aw-watch-row' },
            h('div', { style: { fontWeight: 600 } }, `[${w.modeLabel || w.mode}] ${w.query}`),
            h('div', { style: { color: 'var(--aw-text-3)' } }, `${formatInterval(w.intervalMinutes)} · 上次：${formatTime(w.lastRunAt)} · 来源 ${w.lastCardCount || 0}`),
            h('div', { className: 'aw-watch-actions' },
              w.lastSummary ? h('button', { type: 'button', className: 'aw-filter active', onClick: () => openWatchResult(w) }, '查看结果') : null,
              h('button', {
                type: 'button',
                className: 'aw-filter',
                disabled: watchBusyId === w.id,
                onClick: () => runWatchNow(w.id),
              }, watchBusyId === w.id ? '执行中…' : '立即执行'),
              h('button', { type: 'button', className: 'aw-filter', onClick: () => deleteWatch(w.id) }, '删除')))))

    if (view === 'home') {
      return h('div', { className: 'aw-root' },
        h('div', { className: 'aw-hero' },
          h('div', { className: 'aw-hero-top' },
            h('button', { type: 'button', className: 'aw-icon-btn', onClick: toggleWatchPanel }, `定时搜索 (${watches.length})`)),
          renderWatchDrawer(),
          h('div', { className: 'aw-hero-inner' },
            h('h1', { className: 'aw-slogan' }, lang === 'en' ? 'Search Wide, Verify Fast' : '搜得到，查得快'),
            h('p', { className: 'aw-sub' }, lang === 'en' ? 'Multi-source search, hotspot brief, deep research & scheduled watch' : '全网搜索，热点分析，深度研究，定时搜索'),
            h('form', {
              className: 'aw-box',
              onSubmit: (e) => { e.preventDefault(); startSearch(input, mode) },
            },
              h('textarea', {
                ref: inputRef,
                className: 'aw-textarea',
                placeholder: currentModeObj.ph,
                value: input,
                onChange: (e) => setInput(e.target.value),
                onKeyDown: (e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    startSearch(input, mode)
                  }
                },
              }),
              h('div', { className: 'aw-box-foot' },
                h('div', { className: 'aw-modes' },
                  CORE_MODES.map(m => h('button', {
                    key: m.id,
                    type: 'button',
                    title: m.desc,
                    className: `aw-mode-btn ${mode === m.id ? 'active' : ''}`,
                    onClick: () => setMode(m.id),
                  }, m.label)),
                  lockedTopic ? h('span', { className: 'aw-topic-chip' },
                    `${lang === 'en' ? 'Topic' : '主题'}：${lockedTopic.label}`,
                    h('button', { type: 'button', title: '取消特定主题', onClick: () => setMode('general') }, '×')) : null),
                h('button', {
                  type: 'submit',
                  className: 'aw-send',
                  disabled: !input.trim(),
                  title: '开始检索分析',
                }, '→'))),
            suggestions.length > 0 ? h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px', marginTop: '14px', alignItems: 'center' } },
              suggestions.map((s, idx) => h('button', {
                key: `${s.source}-${idx}`,
                type: 'button',
                className: 'aw-filter',
                style: { display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '12px', maxWidth: '100%' },
                onClick: () => startSearch(s.query, mode),
              },
                h('span', { style: { opacity: 0.7, fontSize: '11px' } }, `[${s.tag}]`),
                h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '240px' } }, s.query))),
              h('button', {
                type: 'button',
                className: 'aw-preset-try',
                style: { color: 'var(--aw-text-3)', marginLeft: 'auto' },
                onClick: () => refreshSuggestions(mode, lang, true),
              }, lang === 'en' ? 'Shuffle' : '换一批')) : null,
            h('div', { className: 'aw-sec-head' },
              h('span', null, '特定主题'),
              h('div', { style: { display: 'flex', gap: '10px', alignItems: 'center' } },
                activeQuery && cards.length > 0 ? h('button', {
                  type: 'button',
                  className: 'aw-preset-try',
                  onClick: () => setView('result'),
                }, `继续上次：${activeQuery.slice(0, 14)}${activeQuery.length > 14 ? '…' : ''} ↗`) : null,
                h('button', {
                  type: 'button',
                  className: 'aw-preset-try',
                  style: { color: 'var(--aw-text-3)' },
                  onClick: () => setTopicSamples(Object.fromEntries(TOPIC_CARDS.map(t => [t.id, pickSample(t.samples)]))),
                }, '换一批引导'))),
            h('div', { className: 'aw-presets' },
              TOPIC_CARDS.map(item => h('div', {
                key: item.id,
                className: `aw-preset ${mode === item.id ? 'selected' : ''}`,
                onClick: () => { setMode(item.id); inputRef.current?.focus?.() },
              },
                h('div', null,
                  h('div', { className: 'aw-preset-tag' }, item.label),
                  h('div', { className: 'aw-preset-desc', style: { marginTop: '4px' } }, item.desc)),
                h('button', {
                  type: 'button',
                  className: 'aw-preset-try',
                  onClick: (e) => { e.stopPropagation(); startSearch(topicSamples[item.id] || pickSample(item.samples), item.id) },
                }, '试一试 ↗')))))))
    }

    return h('div', { className: 'aw-root' },
      h('div', { className: 'aw-topbar' },
        h('button', { type: 'button', className: 'aw-icon-btn', onClick: () => setView('home') }, '← 首页'),
        h('select', {
          className: 'aw-select',
          value: mode,
          onChange: (e) => {
            const nextMode = e.target.value
            setMode(nextMode)
            if (activeQuery) startSearch(activeQuery, nextMode)
          },
        }, ALL_MODES.map(m => h('option', { key: m.id, value: m.id }, m.label))),
        h('form', {
          className: 'aw-top-form',
          onSubmit: (e) => { e.preventDefault(); startSearch(input, mode) },
        },
          h('input', {
            className: 'aw-top-input',
            value: input,
            onChange: (e) => setInput(e.target.value),
            placeholder: '输入新的查询…',
          }),
          h('button', {
            type: 'button',
            className: 'aw-filter',
            title: '跳过缓存重新检索',
            onClick: () => startSearch(input || activeQuery, mode, true),
          }, '强制刷新'),
          h('button', {
            type: 'submit',
            className: 'aw-send',
            style: { width: '28px', height: '28px' },
            disabled: !input.trim(),
          }, '→')),
        h('div', { className: 'aw-stat-pill' }, `${cards.length} 条来源，${verifiedCount} 条已核验`),
        h('button', {
          type: 'button',
          className: 'aw-icon-btn',
          title: '把当前查询加入定时追踪',
          onClick: () => createWatch(activeQuery, mode, watchInterval),
        }, '定时追踪'),
        h('button', { type: 'button', className: 'aw-icon-btn', onClick: toggleWatchPanel }, `定时 (${watches.length})`),
        h('button', { type: 'button', className: 'aw-icon-btn', onClick: openDebug }, 'DEBUG'),
        h('button', {
          type: 'button',
          className: 'aw-icon-btn',
          onClick: toggleSettingsPanel,
        }, '设置')),

      renderWatchDrawer(),

      popover === 'debug' ? h('div', { className: 'aw-popover' },
        h('div', { style: { fontWeight: 600, marginBottom: '8px' } }, '最近 20 次工作台检索诊断'),
        healthInfo?.breaker ? h('div', { style: { padding: '6px 8px', marginBottom: '8px', borderRadius: '8px', background: 'var(--aw-sunken)', fontSize: '12px' } },
          `熔断器：${healthInfo.breaker.total} 源 · 非闭合 ${healthInfo.breaker.nonClosedCount} 源` +
          (healthInfo.breaker.topTripped?.length ? `（${healthInfo.breaker.topTripped.slice(0, 3).map(t => `${t.engine}:${t.state}`).join(', ')}）` : '')) : null,
        diagItems.length === 0
          ? h('div', { style: { color: 'var(--aw-text-3)' } }, '暂无记录')
          : diagItems.map((it, idx) => h('div', {
              key: idx,
              style: { padding: '6px 0', borderBottom: '1px solid var(--aw-border)' },
            },
              h('div', { style: { fontWeight: 600 } }, `[${it.mode}] ${it.query}`),
              h('div', { style: { color: 'var(--aw-text-3)' } }, `${it.durationMs}ms · 来源 ${it.cardCount} · 核验 ${it.verifiedCount ?? 0} · ${it.status}`)))) : null,

      popover === 'settings' ? h('div', { className: 'aw-popover', style: { width: '280px' } },
        h('div', { style: { fontWeight: 600, marginBottom: '10px' } }, '工作台偏好与能力'),
        h('label', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', cursor: 'pointer', marginBottom: '8px' } },
          h('span', null, '对话划词浮层（Argo 分析）'),
          h('input', { type: 'checkbox', checked: Boolean(prefs.selection), onChange: toggleSelectionPref })),
        h('label', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', cursor: 'pointer' } },
          h('span', null, '登录态专业搜索（ego-search）'),
          h('input', { type: 'checkbox', checked: Boolean(healthInfo?.egoSearch?.enabled), onChange: toggleEgoSearch })),
        h('button', {
          type: 'button',
          className: 'aw-icon-btn',
          style: { marginTop: '12px', width: '100%', justifyContent: 'center' },
          onClick: () => {
            window.localStorage.removeItem(STATE_KEY)
            setActiveQuery(''); setCards([]); setTurns([]); setGateResult(null); setView('home'); setPopover(null)
          },
        }, '清空当前工作台记录')) : null,

      h('div', { className: 'aw-main' },
        h('div', null,
          h('div', { className: 'aw-q-head' },
            h('h2', { className: 'aw-q-title' }, activeQuery),
            h('span', { className: 'aw-badge' }, currentModeObj.label),
            evidenceSummary?.crossValidation ? h('span', {
              className: 'aw-badge',
              title: evidenceSummary.crossValidation.detail || '',
            }, `交叉验证：${evidenceSummary.crossValidation.corroboration_level || 'unknown'}${evidenceSummary.summary ? ` · 高可信 ${evidenceSummary.summary.high_credibility || 0}` : ''}`) : null,
            gateResult ? h('span', {
              className: 'aw-badge',
              title: (gateResult.warnings || []).map(w => (typeof w === 'string' ? w : (w?.detail || w?.id || ''))).filter(Boolean).join('；'),
              style: { background: gateResult.passed ? 'var(--aw-sunken)' : '#fef2f2', color: gateResult.passed ? 'var(--aw-text-2)' : '#b91c1c' },
            }, gateResult.passed ? '质量门禁通过' : '质量门禁未达标') : null),
          clarifyInfo ? h('div', { style: { fontSize: '12px', color: 'var(--aw-text-2)', padding: '6px 10px', marginBottom: '10px', borderRadius: '8px', background: 'var(--aw-sunken)' } },
            clarifyInfo.explanation || '',
            Array.isArray(clarifyInfo.ambiguities) && clarifyInfo.ambiguities.length > 0
              ? ` · 歧义提示：${clarifyInfo.ambiguities.slice(0, 2).map(a => a.term || a).join(' / ')}`
              : '') : null,
          loading || stageText ? h('div', { className: 'aw-progress' },
            loading ? h('span', { className: 'aw-dot' }) : null,
            h('span', null, stageText || '处理中…')) : null,
          turns.map((turn, idx) => h('div', { key: idx, className: 'aw-turn' },
            turn.role === 'user'
              ? h('div', { className: 'aw-turn-q' }, turn.text)
              : h('div', { className: 'aw-md' }, renderMarkdown(h, turn.text, cards, scrollToCard)))),
          !loading && cards.length > 0 ? h('form', { className: 'aw-followup', onSubmit: handleFollowup },
            h('input', {
              value: followup,
              onChange: (e) => setFollowup(e.target.value),
              placeholder: '基于当前检索证据继续追问细节…',
            }),
            h('button', { type: 'submit', className: 'aw-send', disabled: !followup.trim() }, '→')) : null),

        h('div', null,
          h('div', { className: 'aw-right-head' },
            h('strong', { style: { fontSize: '13px' } }, '证据与原文出处'),
            h('div', { className: 'aw-filters' },
              h('button', {
                type: 'button',
                className: `aw-filter ${filter === 'all' ? 'active' : ''}`,
                onClick: () => setFilter('all'),
              }, `全部 (${cards.length})`),
              verifiedCount > 0 ? h('button', {
                type: 'button',
                className: `aw-filter ${filter === 'verified' ? 'active' : ''}`,
                onClick: () => setFilter('verified'),
              }, `已核验 (${verifiedCount})`) : null,
              sources.slice(0, 4).map(src => h('button', {
                key: src,
                type: 'button',
                className: `aw-filter ${filter === src ? 'active' : ''}`,
                onClick: () => setFilter(src),
              }, src)))),
          h('div', { className: 'aw-cards' },
            visibleCards.length === 0
              ? h('div', { style: { color: 'var(--aw-text-3)', fontSize: '13px', padding: '12px' } }, loading ? '正在拉取多源证据…' : '暂无匹配卡片')
              : visibleCards.map(c => h('div', {
                  key: c.id,
                  id: `aw-${c.id}`,
                  className: `aw-card ${flashId === c.id ? 'flash' : ''}`,
                },
                  h('div', { className: 'aw-card-top' },
                    h('span', { className: 'aw-card-src' },
                      h('span', { className: 'aw-idx' }, String(c.index)),
                      h('span', null, `${c.source}${c.domain ? ` · ${c.domain}` : ''}`)),
                    c.verified ? h('span', { className: 'aw-verified' }, '已核验原文') : null),
                  h('p', { className: 'aw-card-quote' }, (c.verifiedText || c.snippet || '暂无摘录').slice(0, 220)),
                  h('a', {
                    className: 'aw-card-link',
                    href: c.url,
                    target: '_blank',
                    rel: 'noopener noreferrer',
                  },
                    h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, c.title),
                    h('span', { style: { flexShrink: 0 } }, '↗'))))))))
  }

  function SelectionPill() {
    const [hit, setHit] = useState(null)

    useEffect(() => {
      let timerId = null
      const checkSelection = () => {
        const prefs = readStorage(PREFS_KEY, { selection: true })
        if (!prefs.selection) { setHit(null); return }
        const sel = window.getSelection?.()
        const text = sel ? String(sel.toString() || '').trim() : ''
        if (text.length < 4 || text.length > 200) { setHit(null); return }
        const range = sel.rangeCount > 0 ? sel.getRangeAt(0) : null
        if (!range) { setHit(null); return }
        const rect = range.getBoundingClientRect()
        if (!rect || (rect.width === 0 && rect.height === 0)) { setHit(null); return }
        setHit({ text, top: Math.max(12, rect.top - 38), left: Math.min(window.innerWidth - 130, Math.max(12, rect.left)) })
      }
      const onUp = () => {
        if (timerId) clearTimeout(timerId)
        timerId = setTimeout(checkSelection, 20)
      }
      document.addEventListener('mouseup', onUp)
      document.addEventListener('keyup', onUp)
      return () => {
        if (timerId) clearTimeout(timerId)
        document.removeEventListener('mouseup', onUp)
        document.removeEventListener('keyup', onUp)
      }
    }, [])

    if (!hit) return null
    return h('button', {
      type: 'button',
      className: 'aw-sel-pill',
      style: { top: `${hit.top}px`, left: `${hit.left}px` },
      onMouseDown: (e) => {
        e.preventDefault()
        const text = hit.text
        setHit(null)
        requestSearch(text, 'general')
        try {
          ctx.get?.('layout')?.selectPanel?.('argo-workbench')
        } catch { /* ignore */ }
      },
    }, 'Argo 分析')
  }

  function SearchModeControl({ sessionId }) {
    const sid = String(sessionId || 'default')
    const [mode, setMode] = useState('auto')
    const [busy, setBusy] = useState(false)

    useEffect(() => {
      let alive = true
      fetch(`/argo/api/search-mode?sessionId=${encodeURIComponent(sid)}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => { if (alive && d && d.mode) setMode(d.mode) })
        .catch(() => {})
      return () => { alive = false }
    }, [sid])

    const cycle = async () => {
      if (busy) return
      const next = mode === 'auto' ? 'required' : mode === 'required' ? 'off' : 'auto'
      setBusy(true)
      try {
        const res = await fetch('/argo/api/search-mode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sessionId: sid, mode: next }),
        })
        if (res.ok) {
          const data = await res.json()
          setMode(data.mode || next)
        }
      } catch {
        /* 保持原态 */
      } finally {
        setBusy(false)
      }
    }

    const label = mode === 'required' ? '始终联网' : mode === 'off' ? '关闭联网' : '智能联网'
    const color = mode === 'required' ? '#ea580c' : mode === 'off' ? '#64748b' : 'currentColor'
    return h('button', {
      type: 'button',
      title: `Argo 联网策略：${label}（点击切换）`,
      disabled: busy,
      onClick: cycle,
      style: {
        display: 'inline-flex', alignItems: 'center', gap: '4px',
        padding: '3px 8px', borderRadius: '6px', border: '1px solid currentColor',
        background: 'transparent', color, fontSize: '12px', cursor: busy ? 'wait' : 'pointer',
        opacity: mode === 'off' ? 0.65 : 0.92,
      },
    }, label)
  }

  const offs = [
    ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'argo-workbench' }, WorkbenchApp)),
    ctx.slots.inject('sidebar.panellist', () => ctx.slots.register(
      { name: 'sidebar.panellist', id: 'argo-workbench', order: 5, label: 'Argo 搜索' },
      ({ size = 20, active }) => h('svg', {
        width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
        stroke: active ? '#ea580c' : 'currentColor', strokeWidth: 1.8,
        strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true,
      },
        h('circle', { cx: 11, cy: 11, r: 7 }),
        h('path', { d: 'M16.5 16.5 21 21' }),
        h('path', { d: 'M11 7.2 13.8 13.8 11 12.3 8.2 13.8 11 7.2Z', fill: '#ea580c', stroke: 'none' })),
    )),
    ctx.slots.inject('global', () => ctx.slots.register({ name: 'global', key: 'argo-workbench-pill' }, SelectionPill)),
    ctx.slots.inject('conversation.input.left', () => ctx.slots.register(
      { name: 'conversation.input.left', id: 'argo-search-mode', order: 28, label: () => 'Argo 联网模式' },
      SearchModeControl,
    )),
  ]

  return () => {
    for (const off of offs) if (typeof off === 'function') off()
  }
}

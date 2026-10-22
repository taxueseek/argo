/*
 * runner.js — argo-dsh 执行与连接池层（Runner & Connection Pool）
 *
 * 负责：
 *   1. 常驻 stdio MCP 连接池（按需增长、并发独立、空闲回收、零泄漏）
 *   2. 原生一等工具（makeNativeTool / runNativeCall）单发兜底与 MCP 优先调度
 *   3. 宿主 web_search & web_fetch seam 结果映射与错误分类
 */

import { spawn } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { NATIVE_TOOLS as NATIVE_TOOL_SPECS } from './native-tools.mjs'

function resolvePluginVersion() {
  try {
    const here = dirname(fileURLToPath(import.meta.url))
    const raw = readFileSync(join(here, '..', 'package.json'), 'utf8')
    const parsed = JSON.parse(raw)
    if (typeof parsed?.version === 'string' && parsed.version !== '') return parsed.version
  } catch { /* 打包裁剪/权限异常：回退，不阻断插件加载 */ }
  return '0.0.0-unknown'
}

export const PLUGIN_VERSION = resolvePluginVersion()

export const MCP_CHILD_TOOLS = Object.keys(NATIVE_TOOL_SPECS).map(name => `mcp__argo__${name}`)
export const DEFAULT_CHILD_TOOLS = Object.freeze([
  ...MCP_CHILD_TOOLS,
  'argo_search',
  'argo_fetch',
  'web_search',
  'web_fetch',
])

export const DEFAULTS = Object.freeze({
  toolName: 'wide_research',
  provider: 'spawn',
  defaultWorkers: 6,
  maxWorkers: 9,
  maxTracks: 9,
  maxSourcesPerTrack: 5,
  workerMaxTokens: 5_000,
  synthesisMaxTokens: 7_000,
  childToolDeny: [],
  childToolAllow: [...DEFAULT_CHILD_TOOLS],
  searchProviderEnabled: true,
  nativeTools: ['argo_search', 'argo_fetch'],
  nativeTimeoutMs: 60_000,
  nativeMaxChars: 24_000,
  searchProviderId: 'argo',
  fetchProviderEnabled: true,
  fetchProviderId: 'argo',
  fetchTimeoutMs: 25_000,
  fetchMaxChars: 50_000,
  searchCommand: process.env.ARGO_SEARCH_PYTHON || 'npx',
  searchArgs: process.env.ARGO_SEARCH_MCP_SERVER
    ? [process.env.ARGO_SEARCH_MCP_SERVER]
    : ['-y', 'github:taxueseek/argo'],
  searchTimeoutMs: 30_000,
  searchIdleMs: 60_000,
  searchConcurrency: 3,
  reportDir: join(homedir(), '.dsh-research'),
  keyRefs: ['DEEPSEEK_API_KEY'],
  argoEnv: [],
})

const text = (value) => [{ type: 'text', text: value }]
const clip = (value, maximum) => value.length <= maximum ? value : `${value.slice(0, maximum - 1)}…`
const clamp = (value, fallback, minimum, maximum) => Number.isFinite(value)
  ? Math.max(minimum, Math.min(maximum, Math.floor(value)))
  : fallback

export function argoAborted() {
  const err = new Error('argo search aborted')
  err.code = 'WEB_ABORTED'
  return err
}

export function argoToolError(message) {
  const err = new Error(message)
  err.code = 'ARGO_TOOL_ERROR'
  return err
}

export const stripBoundary = (v) => typeof v === 'string'
  ? v.replace(/<\s*\/?\s*untrusted-web-content\b[^>]*>/gi, '')
  : v

export function mapArgoToWebResult(payload, query) {
  const results = Array.isArray(payload?.results)
    ? payload.results.filter(r => r && typeof r.url === 'string' && r.url !== '')
    : []
  const sources = results.map(r => {
    const source = { url: r.url }
    const title = stripBoundary(r.title)
    const snippet = stripBoundary(r.snippet)
    if (typeof title === 'string' && title !== '') source.title = title
    if (typeof snippet === 'string' && snippet !== '') source.snippet = snippet
    return source
  })
  return { content: undefined, sources, truncated: false, engine: payload?.engine ?? 'argo' }
}

let activeEnvResolver = null

export function setEnvResolver(fn) {
  activeEnvResolver = fn
}

export async function resolveEnv(options = {}, config = undefined) {
  if (typeof activeEnvResolver === 'function') {
    return activeEnvResolver(options, config)
  }
  return undefined
}

export const mcpPool = []

export function dropFromPool(conn) {
  const index = mcpPool.indexOf(conn)
  if (index >= 0) mcpPool.splice(index, 1)
}

export function createMcpConnection(options) {
  const { command, args, idleMs, env } = options
  let proc = null
  let buffer = ''
  let nextId = 1
  let chain = Promise.resolve()
  let idleTimer = null
  let disposed = false
  let claims = 0
  const pending = new Map()

  const touchIdle = () => {
    if (idleTimer !== null) clearTimeout(idleTimer)
    if (idleMs > 0) {
      idleTimer = setTimeout(() => { close() }, idleMs)
      if (idleTimer.unref) idleTimer.unref()
    }
  }

  const close = () => {
    if (disposed) return
    disposed = true
    if (idleTimer !== null) clearTimeout(idleTimer)
    if (proc !== null) {
      try { proc.kill() } catch { /* already gone */ }
      proc = null
    }
    const err = new Error('argo MCP connection closed')
    for (const entry of pending.values()) entry.rej(err)
    pending.clear()
    dropFromPool(conn)
  }

  const send = (method, params, timeoutMs = 0) => {
    touchIdle()
    const run = () => new Promise((res, rej) => {
      if (proc === null || proc.stdin.destroyed) {
        rej(new Error('argo MCP process not running'))
        return
      }
      const id = nextId
      nextId += 1
      const entry = { res, rej }
      pending.set(id, entry)

      let reqTimer = null
      if (timeoutMs > 0) {
        reqTimer = setTimeout(() => {
          pending.delete(id)
          const err = new Error(`argo MCP request timed out after ${timeoutMs}ms: ${method}`)
          err.code = 'ARGO_TIMEOUT'
          rej(err)
          close()
        }, timeoutMs)
        if (reqTimer.unref) reqTimer.unref()
      }
      const settle = (fn, value) => {
        if (reqTimer !== null) clearTimeout(reqTimer)
        fn(value)
      }
      entry.res = (v) => settle(res, v)
      entry.rej = (e) => settle(rej, e)
      try {
        proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n')
      } catch (err) {
        pending.delete(id)
        rej(err)
      }
    })
    const result = chain.then(run)
    chain = result.catch(() => { /* 失败不中断后续请求 */ })
    return result
  }

  const conn = {
    initialize: async (timeoutMs = 0) => {
      if (proc !== null) return
      proc = spawn(command, args, { stdio: ['pipe', 'pipe', 'pipe'], env: env ?? process.env })
      proc.on('error', () => close())
      proc.on('exit', () => close())
      proc.stderr.on('data', () => { /* 预热日志忽略 */ })
      proc.stdout.setEncoding('utf8')
      proc.stdout.on('data', (chunk) => {
        buffer += chunk
        let nl
        while ((nl = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, nl).trim()
          buffer = buffer.slice(nl + 1)
          if (line === '') continue
          let msg
          try {
            msg = JSON.parse(line)
          } catch {
            continue
          }
          const entry = pending.get(msg.id)
          if (entry === undefined) continue
          pending.delete(msg.id)
          if (msg.error !== undefined) entry.rej(new Error(msg.error.message ?? 'argo MCP error'))
          else entry.res(msg.result)
        }
      })
      await new Promise((res, rej) => {
        proc.once('spawn', res)
        proc.once('error', rej)
      })
      const init = await send('initialize', {
        protocolVersion: '2025-06-18',
        capabilities: {},
        clientInfo: { name: 'argo-dsh', version: PLUGIN_VERSION }
      }, timeoutMs)
      conn.notify('notifications/initialized', {})
      return init
    },
    request: (method, params, timeoutMs = 0) => {
      if (claims > 0) claims -= 1
      return send(method, params, timeoutMs)
    },
    claim: () => {
      claims += 1
      touchIdle()
    },
    release: () => {
      if (claims > 0) claims -= 1
      touchIdle()
    },
    load: () => pending.size + claims,
    notify: (method, params) => {
      if (proc !== null && !proc.stdin.destroyed) {
        try {
          proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n')
        } catch { /* ignore */ }
      }
    },
    close
  }
  return conn
}

export function initializeWithSignal(conn, initMs, signal) {
  if (signal?.aborted) {
    conn.close()
    return Promise.reject(argoAborted())
  }
  if (signal === undefined) return conn.initialize(initMs)
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      conn.close()
      reject(argoAborted())
    }
    signal.addEventListener('abort', onAbort, { once: true })
    conn.initialize(initMs).then(
      (value) => { signal.removeEventListener('abort', onAbort); resolve(value) },
      (err) => { signal.removeEventListener('abort', onAbort); reject(err) },
    )
  })
}

export async function acquireMcp(options, signal) {
  const max = Math.max(1, Number(options.max) || DEFAULTS.searchConcurrency)
  const initMs = Math.max(0, Number(options.initMs) || 0)
  const idle = mcpPool.find(entry => entry.load() === 0)
  if (idle !== undefined) {
    idle.claim()
    try {
      await initializeWithSignal(idle, initMs, signal)
      return idle
    } catch (err) {
      dropFromPool(idle)
      idle.close()
      if (mcpPool.length >= max) throw err
    }
  }
  if (mcpPool.length < max) {
    const env = await resolveEnv(options)
    const conn = createMcpConnection({ ...options, env })
    mcpPool.push(conn)
    conn.claim()
    try {
      await initializeWithSignal(conn, initMs, signal)
    } catch (err) {
      dropFromPool(conn)
      conn.close()
      throw err
    }
    return conn
  }
  const least = mcpPool.reduce((a, b) => (a.load() <= b.load() ? a : b))
  least.claim()
  try {
    await initializeWithSignal(least, initMs, signal)
  } catch (err) {
    dropFromPool(least)
    least.close()
    throw err
  }
  return least
}

export function disposeSharedMcp() {
  for (const conn of [...mcpPool]) conn.close()
  mcpPool.length = 0
}

export function classifyFetchError(err, url) {
  const raw = String(err?.message ?? err ?? '')
  const lower = raw.toLowerCase()
  const prefix = (label, hint) => {
    const e = new Error(`argo fetch ${label}: ${hint} (url=${url})`)
    e.code = `ARGO_FETCH_${label.toUpperCase().replace(/-/g, '_')}`
    return e
  }
  if (err?.code === 'WEB_ABORTED' || /abort/i.test(raw)) {
    return prefix('aborted', 'cancelled by caller')
  }
  if (err?.code === 'ARGO_TIMEOUT' || /timed out|timeout/i.test(lower)) {
    return prefix('timeout', 'the page took too long; try a more specific URL or retry')
  }
  if (/blocked|forbidden|403|robots|captcha|cloudflare|denied/i.test(lower)) {
    return prefix('blocked', 'the source refused automated access; use a search result URL instead')
  }
  if (/no readable|no content|nothing extracted|no results|empty/i.test(lower)) {
    return prefix('empty', 'no readable content was extracted from the page')
  }
  const e = new Error(`argo fetch failed: ${clip(raw, 300)} (url=${url})`)
  e.code = 'ARGO_FETCH_FAILED'
  return e
}

export function mapArgoToWebFetch(payload, requestUrl, maxChars) {
  const url = typeof payload?.url === 'string' && payload.url !== '' ? payload.url : requestUrl
  const content = typeof payload?.content === 'string' ? stripBoundary(payload.content) : ''
  const clipped = content.length > maxChars
  return {
    url,
    statusCode: 200,
    body: { kind: 'text', content: clipped ? content.slice(0, maxChars) : content },
    truncated: Boolean(payload?.truncated) || clipped,
  }
}

export async function fetchViaArgo(url, signal, options = {}, deps = {}) {
  const command = options.command ?? DEFAULTS.searchCommand
  const args = options.args ?? DEFAULTS.searchArgs
  const timeoutMs = options.timeoutMs ?? DEFAULTS.fetchTimeoutMs
  const maxChars = clamp(options.maxChars ?? DEFAULTS.fetchMaxChars,
    DEFAULTS.fetchMaxChars, 500, 50_000)
  const argsObj = { url, max_chars: maxChars }
  const spawnRun = deps.run ?? runNativeCall

  const acquireOptions = {
    command,
    args,
    idleMs: options.idleMs ?? DEFAULTS.searchIdleMs,
    max: options.max ?? DEFAULTS.searchConcurrency,
    initMs: timeoutMs,
    credentials: options.credentials,
    config: options.config,
  }

  let raw
  try {
    const conn = await acquireMcp(acquireOptions, signal)
    raw = await new Promise((resolve, reject) => {
      const onAbort = () => reject(argoAborted())
      if (signal?.aborted) {
        conn.release()
        reject(argoAborted())
        return
      }
      if (signal !== undefined) signal.addEventListener('abort', onAbort, { once: true })
      conn.request('tools/call', { name: 'argo_fetch', arguments: argsObj }, timeoutMs).then((value) => {
        if (signal !== undefined) signal.removeEventListener('abort', onAbort)
        if (value?.isError) {
          const body = value?.content?.[0]?.text
          reject(argoToolError(typeof body === 'string' && body !== ''
            ? body
            : `argo tool argo_fetch returned an error`))
          return
        }
        const text = value?.content?.[0]?.text
        resolve(typeof text === 'string' ? JSON.parse(text) : {})
      }, (err) => {
        if (signal !== undefined) signal.removeEventListener('abort', onAbort)
        reject(err)
      })
    })
  } catch (err) {
    if (err?.code === 'ARGO_TOOL_ERROR' || err?.code === 'ARGO_TIMEOUT' || err?.code === 'WEB_ABORTED') {
      throw classifyFetchError(err, url)
    }
    try {
      const spawnSpec = resolveNativeSpawn(
        { searchCommand: command, searchArgs: args },
        'argo_fetch',
        JSON.stringify(argsObj),
      )
      const env = await resolveEnv(options)
      const stdout = await spawnRun(env !== undefined ? { ...spawnSpec, env } : spawnSpec, timeoutMs, signal)
      const parsed = JSON.parse(stdout)
      if (parsed?.isError) {
        const body = parsed?.content?.[0]?.text
        throw argoToolError(typeof body === 'string' && body !== '' ? body : 'argo_fetch failed')
      }
      raw = JSON.parse(parsed?.content?.[0]?.text ?? '{}')
    } catch (spawnErr) {
      throw classifyFetchError(spawnErr, url)
    }
  }

  if (raw?.success === false) {
    throw classifyFetchError(new Error(String(raw?.error ?? 'fetch failed')), url)
  }
  return mapArgoToWebFetch(raw, url, maxChars)
}

export async function searchViaArgoMCP(query, maxResults = 5, signal, options = {}) {
  const command = options.command ?? DEFAULTS.searchCommand
  const args = options.args ?? DEFAULTS.searchArgs
  const timeoutMs = options.timeoutMs ?? DEFAULTS.searchTimeoutMs
  const count = clamp(maxResults ?? 5, 5, 1, 20)

  const conn = await acquireMcp({
    command,
    args,
    idleMs: options.idleMs ?? DEFAULTS.searchIdleMs,
    max: options.max ?? DEFAULTS.searchConcurrency,
    initMs: timeoutMs,
    credentials: options.credentials,
    config: options.config,
  }, signal)

  const result = await new Promise((resolve, reject) => {
    const onAbort = () => reject(argoAborted())
    if (signal !== undefined) {
      if (signal.aborted) {
        conn.release()
        reject(argoAborted())
        return
      }
      signal.addEventListener('abort', onAbort, { once: true })
    }
    conn.request('tools/call', {
      name: 'argo_search',
      arguments: { query, max_results: count, summary: true }
    }, timeoutMs).then((value) => {
      if (signal !== undefined) signal.removeEventListener('abort', onAbort)
      resolve(value)
    }, (err) => {
      if (signal !== undefined) signal.removeEventListener('abort', onAbort)
      reject(err)
    })
  })

  let payload
  try {
    const raw = result?.content?.[0]?.text
    payload = typeof raw === 'string' ? JSON.parse(raw) : {}
  } catch (err) {
    throw new Error(`argo search returned an unprocessable response: ${String(err)}`)
  }
  return mapArgoToWebResult(payload, query)
}

export function resolveNativeSpawn(config, tool, payload) {
  const command = typeof config.searchCommand === 'string' && config.searchCommand !== ''
    ? config.searchCommand
    : DEFAULTS.searchCommand
  const args = Array.isArray(config.searchArgs) && config.searchArgs.length > 0
    ? config.searchArgs
    : DEFAULTS.searchArgs
  if (args.some(arg => typeof arg === 'string' && arg.endsWith('mcp_server.py'))) {
    return { command, args: [...args, '--call', tool, payload] }
  }
  return { command, args: [...args, 'call', tool, payload] }
}

export function nativeErrorText(stdout, stderr) {
  try {
    const parsed = JSON.parse(stdout)
    const body = parsed?.content?.[0]?.text
    if (typeof body === 'string' && body !== '') return body
  } catch { /* ignore */ }
  return stderr.trim() !== '' ? stderr : stdout
}

export function runNativeCall({ command, args, env }, timeoutMs, signal) {
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) {
      reject(argoAborted())
      return
    }
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'], env: env ?? process.env })
    let stdout = ''
    let stderr = ''
    let settled = false
    let timer = null
    let detachAbort = null

    const settle = (fn, value) => {
      if (settled) return
      settled = true
      if (timer !== null) clearTimeout(timer)
      if (detachAbort !== null) detachAbort()
      fn(value)
    }
    const onAbort = () => {
      try { child.kill('SIGKILL') } catch { /* already gone */ }
      settle(reject, argoAborted())
    }
    if (signal) {
      signal.addEventListener('abort', onAbort, { once: true })
      detachAbort = () => signal.removeEventListener('abort', onAbort)
    }
    timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch { /* already gone */ }
      settle(reject, new Error(`argo native call timed out after ${timeoutMs}ms`))
    }, timeoutMs)
    child.stdout.on('data', (chunk) => { stdout += chunk })
    child.stderr.on('data', (chunk) => { stderr += chunk })
    child.on('error', (err) => settle(reject, err))
    child.on('close', (code) => {
      if (code === 0) { settle(resolve, stdout); return }
      settle(reject, new Error(`argo native call exited ${code}: ${clip(nativeErrorText(stdout, stderr), 500)}`))
    })
  })
}

export function resolveEntry(config) {
  const command = typeof config.searchCommand === 'string' && config.searchCommand !== ''
    ? config.searchCommand
    : DEFAULTS.searchCommand
  const args = Array.isArray(config.searchArgs) && config.searchArgs.length > 0
    ? config.searchArgs
    : DEFAULTS.searchArgs
  return { command, args }
}

export function pickArgs(args, allowed) {
  const out = {}
  for (const [key, value] of Object.entries(args ?? {})) {
    if (allowed.includes(key) && value !== undefined && value !== null && value !== '') {
      out[key] = value
    }
  }
  return out
}

export async function nativeViaMcpPool(config, kind, argsObj, timeoutMs, signal) {
  const { command, args } = resolveEntry(config)
  const conn = await acquireMcp({
    command,
    args,
    idleMs: config.searchIdleMs ?? DEFAULTS.searchIdleMs,
    max: config.searchConcurrency ?? DEFAULTS.searchConcurrency,
    initMs: timeoutMs,
    credentials: config.credentials,
    config,
  }, signal)
  const result = await new Promise((resolve, reject) => {
    const onAbort = () => reject(argoAborted())
    if (signal !== undefined) {
      if (signal.aborted) {
        conn.release()
        reject(argoAborted())
        return
      }
      signal.addEventListener('abort', onAbort, { once: true })
    }
    conn.request('tools/call', { name: kind, arguments: argsObj }, timeoutMs).then((value) => {
      if (signal !== undefined) signal.removeEventListener('abort', onAbort)
      resolve(value)
    }, (err) => {
      if (signal !== undefined) signal.removeEventListener('abort', onAbort)
      reject(err)
    })
  })
  if (result?.isError) {
    const body = result?.content?.[0]?.text
    throw argoToolError(typeof body === 'string' && body !== ''
      ? body
      : `argo tool ${kind} returned an error`)
  }
  return JSON.stringify(result ?? {})
}

export function makeNativeTool(config, kind, deps = {}) {
  const spec = NATIVE_TOOL_SPECS[kind]
  if (!spec) {
    throw new Error(`argo-dsh: unknown native tool "${kind}" (known: ${Object.keys(NATIVE_TOOL_SPECS).join(', ')})`)
  }
  const spawnRun = deps.run ?? runNativeCall
  const mcpCall = deps.mcpCall ?? nativeViaMcpPool
  const forceSpawn = deps.run !== undefined && deps.mcpCall === undefined

  const currentTimeout = () => {
    const value = Number(config.nativeTimeoutMs)
    return Number.isFinite(value) && value > 0 ? value : DEFAULTS.nativeTimeoutMs
  }
  const currentMaxChars = () => {
    const value = Number(config.nativeMaxChars)
    return Number.isFinite(value) && value > 0 ? value : DEFAULTS.nativeMaxChars
  }
  return {
    name: kind,
    description: spec.description,
    parameters: spec.parameters,
    isConcurrencySafe: () => true,
    output: {
      schema: { type: 'object' },
      render: (_args, value) => {
        let body = ''
        try {
          const parsed = JSON.parse(value.stdout)
          body = parsed?.content?.[0]?.text ?? value.stdout
        } catch {
          body = value.stdout
        }
        return text(clip(body, currentMaxChars()))
      },
    },
    async execute(args, exec) {
      const argsObj = pickArgs(args, spec.allowed)
      const payload = JSON.stringify(argsObj)
      const timeoutMs = currentTimeout()
      const signal = exec?.signal
      if (!forceSpawn) {
        try {
          return { stdout: await mcpCall(config, kind, argsObj, timeoutMs, signal) }
        } catch (err) {
          if (err?.code === 'ARGO_TOOL_ERROR' || err?.code === 'ARGO_TIMEOUT' || err?.code === 'WEB_ABORTED') throw err
        }
      }
      const spawnSpec = resolveNativeSpawn(config, kind, payload)
      const env = await resolveEnv(undefined, config)
      const stdout = await spawnRun(env !== undefined ? { ...spawnSpec, env } : spawnSpec, timeoutMs, signal)
      return { stdout }
    },
  }
}

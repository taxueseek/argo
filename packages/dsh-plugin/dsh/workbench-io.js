/*
 * @taxueseek/argo-dsh — 工作台 I/O 与进程执行辅助（CLI 定位、静态资源缓存、定时监控持久化）
 */

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { resolveStateDir } from './diagnostics.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const MONOREPO_ARGO_BIN = join(HERE, '..', '..', '..', 'bin', 'argo')
const WATCH_STORE_FILE = join(homedir(), '.config', 'argo', 'workbench-watches.json')

export function mergedLaunchdPath(env = {}) {
  const extraPath = ['/opt/homebrew/bin', '/usr/local/bin', join(homedir(), '.local', 'bin')].join(':')
  const basePath = env.PATH || process.env.PATH || ''
  return basePath ? `${extraPath}:${basePath}` : extraPath
}

export function resolveArgoBin() {
  if (existsSync(MONOREPO_ARGO_BIN)) return MONOREPO_ARGO_BIN
  return 'argo'
}

export function runArgoCli(args, { env = {}, stdin = null, timeoutMs = 30000, signal = null } = {}) {
  const mergedPath = mergedLaunchdPath(env)
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new Error('aborted')); return }
    const child = spawn(resolveArgoBin(), args, {
      env: { ...process.env, ...env, PATH: mergedPath },
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    const out = []
    const err = []
    const timer = setTimeout(() => {
      child.kill('SIGTERM')
      reject(new Error(`argo ${args[0] || ''} timeout (${timeoutMs}ms)`))
    }, timeoutMs)
    if (typeof timer.unref === 'function') timer.unref()
    const onAbort = () => {
      clearTimeout(timer)
      child.kill('SIGTERM')
      reject(new Error('aborted'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.on('data', d => out.push(d))
    child.stderr.on('data', d => err.push(d))
    child.on('error', (e) => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      reject(e)
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      const text = Buffer.concat(out).toString('utf8').trim()
      if (code !== 0 && !text) {
        reject(new Error(Buffer.concat(err).toString('utf8').trim() || `argo exit ${code}`))
        return
      }
      try {
        resolve(JSON.parse(text || '{}'))
      } catch {
        reject(new Error(`invalid json from argo ${args[0] || ''}`))
      }
    })
    if (stdin !== null && stdin !== undefined) {
      child.stdin.end(typeof stdin === 'string' ? stdin : JSON.stringify(stdin))
    } else {
      child.stdin.end()
    }
  })
}

const staticAssetCache = new Map()

export function readStaticAssetCached(filePath) {
  const st = statSync(filePath)
  const hit = staticAssetCache.get(filePath)
  if (hit && hit.mtimeMs === st.mtimeMs && hit.size === st.size) {
    return hit.text
  }
  const text = readFileSync(filePath, 'utf8')
  staticAssetCache.set(filePath, { mtimeMs: st.mtimeMs, size: st.size, text })
  return text
}

export function loadWatches() {
  try {
    const raw = readFileSync(WATCH_STORE_FILE, 'utf8')
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

export function saveWatches(items) {
  try {
    mkdirSync(dirname(WATCH_STORE_FILE), { recursive: true })
    const uniq = `${process.pid}.${Date.now()}.${Math.random().toString(36).slice(2, 6)}`
    const tmp = `${WATCH_STORE_FILE}.tmp.${uniq}`
    writeFileSync(tmp, JSON.stringify(items, null, 2), 'utf8')
    renameSync(tmp, WATCH_STORE_FILE)
  } catch { /* ignore */ }
}

export function breakerFilePath() {
  return join(resolveStateDir(), 'circuit_breaker.json')
}

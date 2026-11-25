/*
 * @taxueseek/argo-dsh — 工作台 I/O 与进程执行辅助（CLI 定位、静态资源缓存、定时监控持久化）
 */

import { spawn } from 'node:child_process'
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { resolveStateDir } from './diagnostics.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const MONOREPO_ARGO_BIN = join(HERE, '..', '..', '..', 'bin', 'argo')
// install.sh 默认安装根（ARGO_HOME 可改，此处只探测默认路径——探测不到就走
// console script 的 PATH 兜底，不猜 site-packages 里的 wheel _data 布局）
const INSTALL_VENV_ARGO_BIN = join(homedir(), '.local', 'share', 'argo', '.venv', 'bin', 'argo')
const INSTALL_VENV_ARGO_BIN_WIN = join(
  process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local'),
  'argo', '.venv', 'Scripts', 'argo.exe',
)
export const WATCH_STORE_FILE = join(homedir(), '.config', 'argo', 'workbench-watches.json')

export function mergedLaunchdPath(env = {}, platform = process.platform) {
  const basePath = env.PATH || env.Path || process.env.PATH || process.env.Path || ''
  if (platform === 'win32') return basePath
  const extraPath = ['/opt/homebrew/bin', '/usr/local/bin', join(homedir(), '.local', 'bin')].join(':')
  return basePath ? `${extraPath}:${basePath}` : extraPath
}

// 2026-11-20 审计：npm 发布版插件装在 DSH 缓存目录时 MONOREPO_ARGO_BIN 不存在，
// 裸名 'argo' 又依赖 PATH——launchd/受限 PATH 下 venv console script 可能不可达。
// 修法加候选不重排：显式 ARGO_BIN 最优先（用户指路零猜测），其次 monorepo、
// install.sh 默认 venv，最后裸名走 PATH。
export function resolveArgoBin(env = {}, platform = process.platform) {
  const explicit = env.ARGO_BIN || process.env.ARGO_BIN
  if (explicit) return explicit
  if (existsSync(MONOREPO_ARGO_BIN)) return MONOREPO_ARGO_BIN
  if (platform === 'win32' && existsSync(INSTALL_VENV_ARGO_BIN_WIN)) return INSTALL_VENV_ARGO_BIN_WIN
  if (existsSync(INSTALL_VENV_ARGO_BIN)) return INSTALL_VENV_ARGO_BIN
  return 'argo'
}

// Windows CreateProcess 不识别 shebang：若解析到无后缀 bin/argo 或 .py 脚本，自动用 Python 解释器包装
export function resolveSpawnCommand(bin, args = [], env = {}, platform = process.platform) {
  if (platform === 'win32' && (/[\\/]bin[\\/]argo$/i.test(bin) || /\.py$/i.test(bin))) {
    const py = env.ARGO_PYTHON || process.env.ARGO_PYTHON || 'python'
    return { cmd: py, argv: [bin, ...args] }
  }
  return { cmd: bin, argv: args }
}

export function runArgoCli(args, { env = {}, stdin = null, timeoutMs = 30000, signal = null } = {}) {
  const mergedPath = mergedLaunchdPath(env)
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new Error('aborted')); return }
    const bin = resolveArgoBin({ ...env, PATH: mergedPath })
    const { cmd, argv } = resolveSpawnCommand(bin, args, env)
    const child = spawn(cmd, argv, {
      env: { ...process.env, ...env, PATH: mergedPath },
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    const out = []
    const err = []
    const timer = setTimeout(() => {
      // 超时路径也要摘掉 abort 监听器：长生命周期 AbortSignal 下残留会累积
      signal?.removeEventListener('abort', onAbort)
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
    child.stdin.on('error', () => {})
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

export function loadWatches(file = WATCH_STORE_FILE) {
  try {
    const raw = readFileSync(file, 'utf8')
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

export function saveWatches(items, file = WATCH_STORE_FILE) {
  try {
    mkdirSync(dirname(file), { recursive: true })
    const uniq = `${process.pid}.${Date.now()}.${Math.random().toString(36).slice(2, 6)}`
    const tmp = `${file}.tmp.${uniq}`
    writeFileSync(tmp, JSON.stringify(items, null, 2), 'utf8')
    renameSync(tmp, file)
  } catch { /* ignore */ }
}

// 跨进程状态文件互斥（wx 独占创建，语义对齐 scripts/pacing.py 的非阻塞文件锁）：
// 抢不到返回 null 让调用方让路；崩溃残留超 staleMs 强取自愈。
// 热榜与 watches 共用同一实现，消灭各自手抄（2026-11-21 审计：锁方案四家并存）。
export function acquireStateFileLock(file, staleMs = 5 * 60 * 1000) {
  try {
    mkdirSync(dirname(file), { recursive: true })
    return { fd: openSync(file, 'wx'), lockFile: file }
  } catch (err) {
    if (err?.code !== 'EEXIST') return null
    try {
      if (Date.now() - statSync(file).mtimeMs > staleMs) {
        unlinkSync(file)
        return { fd: openSync(file, 'wx'), lockFile: file }
      }
    } catch { /* 对方刚释放或刚重建，本轮回让路 */ }
    return null
  }
}

export function releaseStateFileLock(lock) {
  if (!lock) return
  try { closeSync(lock.fd) } catch { /* ignore */ }
  try { unlinkSync(lock.lockFile) } catch { /* ignore */ }
}

// watches 唯一真源是磁盘文件：读-改-写全程持锁，内存副本只作缓存。
// 旧「内存快照 + 全量覆盖写」在多宿主下互相抹改（2026-11-21 审计 P1）。
// 返回 null 表示锁被占（另一宿主写盘中），调用方按忙处理。
export function mutateWatches(mutator, file = WATCH_STORE_FILE) {
  const lock = acquireStateFileLock(`${file}.lock`)
  if (!lock) return null
  try {
    const next = mutator(loadWatches(file))
    if (Array.isArray(next)) saveWatches(next, file)
    return Array.isArray(next) ? next : loadWatches(file)
  } finally {
    releaseStateFileLock(lock)
  }
}

export function breakerFilePath() {
  return join(resolveStateDir(), 'circuit_breaker.json')
}

/*
 * diagnostics.js — argo-dsh 运行时监控与连接池只读诊断
 *
 * 为设置面板、外部插件或排查脚本提供只读状态自省能力，
 * 避免状态探查逻辑与核心检索逻辑耦合。
 */

import { existsSync, promises as fs } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const MAX_BYTES = 1024 * 1024 // 显式读取上限 1MB，防超大日志文件撑爆内存
const BYTE_LIMIT_GUARD = 'maxBytes guard: 1024 * 1024'

/**
 * 解析 argo 运行时状态目录，遵循 argo_paths.py 跨平台约定：
 * 显式参数 > ARGO_STATE_DIR > 已存在的 ~/.cache/unified-search >
 * Windows LOCALAPPDATA / POSIX XDG_CACHE_HOME > ~/.cache/unified-search。
 *
 * @param {string} [stateDir]
 */
export function resolveStateDir(stateDir) {
  if (stateDir) return stateDir
  if (process.env.ARGO_STATE_DIR) return process.env.ARGO_STATE_DIR
  const legacyDir = join(homedir(), '.cache', 'unified-search')
  if (existsSync(legacyDir)) return legacyDir
  if (process.platform === 'win32' && process.env.LOCALAPPDATA) {
    return join(process.env.LOCALAPPDATA, 'unified-search')
  }
  if (process.env.XDG_CACHE_HOME) {
    return join(process.env.XDG_CACHE_HOME, 'unified-search')
  }
  return legacyDir
}

/**
 * 读 argo 的本地使用日志，汇总成「路由观测」卡片数据。
 *
 * 数据来源与 stats_cli.py / usage_log.py 同一处（<state>/usage_log/*.jsonl），
 * 只读、不写、不出本机。不回传 query 原文；仅解析 route.jsonl 真实字段并汇总
 * 语言分布与兜底率。
 *
 * @param {string} [stateDir] - argo 状态目录；缺省走 resolveStateDir()
 * @param {number} [limit] - 最近几条样本
 */
export async function getRouteHealth(stateDir, limit = 10) {
  const dir = resolveStateDir(stateDir)
  const parsed = []
  const langCounts = {}
  const domainCounts = {}
  let fallbackCount = 0
  try {
    const routePath = join(dir, 'usage_log', 'route.jsonl')
    if (existsSync(routePath)) {
      const text = await fs.readFile(routePath, 'utf8')
      const truncated = text.length > MAX_BYTES ? text.slice(-MAX_BYTES) : text
      const lines = truncated.split('\n').filter(Boolean)
      for (const line of lines) {
        try {
          const row = JSON.parse(line)
          if (!row || typeof row !== 'object') continue
          const engine = row.engine || (row.decision && row.decision.engine) || ''
          const engines = Array.isArray(row.engines)
            ? row.engines
            : (engine ? [engine] : [])
          const domain = row.domain || (row.decision && row.decision.domain) || ''
          const lang = row.effective_lang || row.lang_override || row.primary_lang || ''
          const intents = Array.isArray(row.intents) ? row.intents : []
          const confidence = row.confidence ?? (row.decision && row.decision.confidence) ?? ''
          const mode = row.mode || ''
          if (!domain) fallbackCount += 1
          const langKey = lang || 'unknown'
          langCounts[langKey] = (langCounts[langKey] || 0) + 1
          const domKey = domain || 'fallback'
          domainCounts[domKey] = (domainCounts[domKey] || 0) + 1
          parsed.push({
            domain,
            engine,
            engines,
            primary_lang: row.primary_lang || '',
            effective_lang: lang,
            lang_override: row.lang_override || null,
            intents,
            confidence,
            mode,
            ts: row.ts || row.timestamp || '',
          })
        } catch { /* 单行坏数据不影响其余 */ }
      }
    }
  } catch { /* 读不到就空 */ }
  const total = parsed.length
  const samples = parsed.slice(-Math.max(1, limit))
  const summary = {
    total,
    fallbackCount,
    fallbackRate: total > 0 ? Number((fallbackCount / total).toFixed(3)) : 0,
    langCounts,
    topDomains: Object.entries(domainCounts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([name, count]) => ({ name, count })),
  }
  return { samples, summary, source: 'usage_log/route.jsonl', stateDir: dir }
}

/**
 * 获取当前常驻 MCP 连接池的运行时指标。
 *
 * @param {Array} pool - MCP 连接池实例数组
 * @returns {Object} 包含连接数、当前负载以及各连接健康状态的快照
 */
export function getMcpPoolStats(pool = []) {
  const connections = pool.map((conn, index) => ({
    index,
    load: typeof conn.load === 'function' ? conn.load() : 0,
    hasProcess: Boolean(conn.proc ?? true),
  }))

  const totalLoad = connections.reduce((sum, item) => sum + item.load, 0)

  return {
    poolSize: pool.length,
    activeConnections: pool.filter(conn => typeof conn.load === 'function' && conn.load() > 0).length,
    totalLoad,
    connections,
    timestamp: Date.now(),
  }
}

/**
 * 诊断并重置 MCP 连接池。
 *
 * @param {Array} pool - MCP 连接池实例数组
 * @param {Function} disposeFn - 释放函数
 * @returns {Object} 重置结果摘要
 */
export function resetMcpPool(pool = [], disposeFn) {
  const beforeCount = pool.length
  if (typeof disposeFn === 'function') {
    disposeFn()
  } else {
    for (const conn of [...pool]) {
      try { conn.close?.() } catch { /* ignore */ }
    }
    pool.length = 0
  }
  return {
    evictedCount: beforeCount,
    poolSizeNow: pool.length,
    success: true,
  }
}

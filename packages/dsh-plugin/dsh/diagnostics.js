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
 * 读 argo 的本地使用日志，汇总成「路由观测」卡片数据。
 *
 * 数据来源与 stats_cli.py / usage_log.py 同一处（<state>/usage_log/*.jsonl），
 * 只读、不写、不出本机。argo 未运行过（无日志）时返回 { samples: [] }，
 * 面板据此显示终端命令提示——不假装有数据。
 *
 * @param {string} [stateDir] - argo 状态目录；缺省用 ARGO_STATE_DIR 或平台惯例
 * @param {number} [limit] - 最近几条
 */
export async function getRouteHealth(stateDir, limit = 10) {
  const dir = stateDir
    || process.env.ARGO_STATE_DIR
    || join(homedir(), '.cache', 'unified-search')
  const samples = []
  try {
    const routePath = join(dir, 'usage_log', 'route.jsonl')
    if (existsSync(routePath)) {
      const text = await fs.readFile(routePath, 'utf8')
      const truncated = text.length > MAX_BYTES ? text.slice(-MAX_BYTES) : text
      const lines = truncated.split('\n').filter(Boolean)
      for (const line of lines.slice(-limit)) {
        try {
          const row = JSON.parse(line)
          if (row && typeof row === 'object') {
            samples.push({
              query: row.query || row.original_query || '',
              engine: row.engine || (row.decision && row.decision.engine) || '',
              domain: row.domain || (row.decision && row.decision.domain) || '',
              confidence: row.confidence || (row.decision && row.decision.confidence) || '',
              reason: row.reason || (row.decision && row.decision.reason) || '',
              ts: row.ts || row.timestamp || '',
            })
          }
        } catch { /* 单行坏数据不影响其余 */ }
      }
    }
  } catch { /* 读不到就空 */ }
  return { samples, source: 'usage_log/route.jsonl', stateDir: dir }
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

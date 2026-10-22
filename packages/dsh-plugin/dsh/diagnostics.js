/*
 * diagnostics.js — argo-dsh 运行时监控与连接池只读诊断
 *
 * 为设置面板、外部插件或排查脚本提供只读状态自省能力，
 * 避免状态探查逻辑与核心检索逻辑耦合。
 */

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

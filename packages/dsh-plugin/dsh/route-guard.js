/*
 * @taxueseek/argo-dsh — 控制面路由安全围栏（TCP Peer 主防线 + Host/Origin 纵深）
 *
 * 仅允许本机回环对端访问控制面接口：
 *  1. 当存在底层 TCP socket 时，以 req.socket.remoteAddress 为唯一主判据
 *     （无法被客户端伪造，绝不信任 X-Forwarded-For）；
 *  2. 叠加 Host 与 Origin 的回环/同源校验，防御本机浏览器的 DNS Rebinding。
 */

const IPV4_LOOPBACK_RE = /^127\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)$/

export function isLoopbackAddress(addr) {
  if (typeof addr !== 'string') return false
  const raw = addr.trim().toLowerCase()
  if (!raw) return false
  if (raw === '::1' || raw === 'localhost') return true
  if (IPV4_LOOPBACK_RE.test(raw)) return true
  if (raw.startsWith('::ffff:')) {
    return IPV4_LOOPBACK_RE.test(raw.slice(7))
  }
  return false
}

export function extractHostname(hostHeader) {
  if (typeof hostHeader !== 'string' || !hostHeader.trim()) return ''
  try {
    return new URL(`http://${hostHeader.trim()}`).hostname.toLowerCase()
  } catch {
    return ''
  }
}

export function isLoopbackHost(hostHeader) {
  const host = extractHostname(hostHeader)
  if (!host) return false
  if (host === 'localhost' || host === '[::1]' || host === '::1') return true
  return IPV4_LOOPBACK_RE.test(host)
}

export function checkLoopbackAndOrigin(ctx, req) {
  const peer = req?.socket?.remoteAddress ?? req?.connection?.remoteAddress ?? req?.remoteAddress
  if (peer !== undefined && peer !== null && peer !== '') {
    if (!isLoopbackAddress(String(peer))) return false
  } else if (!isLoopbackHost(req?.headers?.host)) {
    return false
  }

  const hostHeader = req?.headers?.host
  if (hostHeader && !isLoopbackHost(hostHeader)) {
    const expected = ctx?.webServer?.getOrigin?.()
    if (!expected) return false
    try {
      const expectedHost = new URL(expected).hostname.toLowerCase()
      if (extractHostname(hostHeader) !== expectedHost) return false
    } catch {
      return false
    }
  }

  const origin = req?.headers?.origin
  if (!origin) return true
  try {
    const expected = ctx?.webServer?.getOrigin?.()
    if (expected && origin === expected) return true
    const parsed = new URL(origin)
    return isLoopbackAddress(parsed.hostname)
  } catch {
    return false
  }
}

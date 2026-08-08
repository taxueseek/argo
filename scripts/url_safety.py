#!/usr/bin/env python3
"""url_safety.py — URL 请求安全校验（SSRF 防护）

对 fetch / crawl 等会发起网络请求的入口做统一拦截：
  1. scheme 白名单（仅 http/https，拒绝 file:// / ftp:// 等）
  2. 主机名黑名单（localhost、裸单标签主机、.local/.internal/.lan 等）
  3. DNS 解析后 IP 段检查（私有 / 环回 / 链路本地 / 保留 / CGNAT 等）
  4. 重定向目标同样逐跳校验（配合 http_client 的安全跟随）

SSRF 判定语义：按「域名在本机 DNS 下的可路由语义」自适应，而非机械按
IP 段一刀切。若域名解析出的全部地址均为 fake-ip 占位段（代理工具
Clash/Surge TUN 的 fake-ip 模式，如 198.18.0.0/15、fdfe:dcba:9876::/64），
说明该域名由代理接管、流量经 TUN 直达公网目标，到不了内网 —— 不构成
SSRF 风险，放行；解析含任一真实私有 IP 则仍拦截。

默认阻止私有地址；显式设置 ARGO_ALLOW_PRIVATE_URLS=1 可放行
（本地调试 / 自建内网服务场景）。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


# 私有 / 特殊用途 IPv4 段
_PRIVATE_NETWORKS_V4 = [
    "0.0.0.0/8",        # 本网络
    "10.0.0.0/8",       # 私有
    "100.64.0.0/10",    # CGNAT
    "127.0.0.0/8",      # 环回
    "169.254.0.0/16",   # 链路本地
    "172.16.0.0/12",    # 私有
    "192.168.0.0/16",   # 私有
    "192.0.0.0/24",     # IETF 协议分配
    "192.0.2.0/24",     # TEST-NET-1
    "198.18.0.0/15",    # 基准测试
    "198.51.100.0/24",  # TEST-NET-2
    "203.0.113.0/24",   # TEST-NET-3
    "224.0.0.0/4",      # 组播
    "240.0.0.0/4",      # 保留
]

# 私有 / 特殊用途 IPv6 段
_PRIVATE_NETWORKS_V6 = [
    "::1/128",          # 环回
    "::/128",           # 未指定
    "fc00::/7",         # 唯一本地地址 ULA
    "fe80::/10",        # 链路本地
    "ff00::/8",         # 组播
    "2001:db8::/32",    # 文档
    "64:ff9b::/96",     # NAT64 前缀
]

_PRIVATE_NETWORKS = [
    ipaddress.ip_network(n) for n in _PRIVATE_NETWORKS_V4 + _PRIVATE_NETWORKS_V6
]

# fake-ip 代理占位段：Clash/Surge 等 TUN 工具的 fake-ip 模式把域名解析到
# 这些段，流量由 TUN 设备接管转发至真实公网目标。占位段不可直达内网，
# 因此命中占位段不算 SSRF 风险（见 host_is_private）。
_FAKE_IP_NETWORKS = [
    ipaddress.ip_network("198.18.0.0/15"),      # Clash/Surge fake-ip 默认 v4 段
    ipaddress.ip_network("fdfe:dcba:9876::/64"),  # Clash fake-ip 默认 v6 段
]

# 裸主机名（无点）与内网域名后缀：默认视为不可路由目标
_PRIVATE_HOST_SUFFIXES = (
    ".local", ".internal", ".lan", ".home.arpa", ".corp", ".intranet",
)
_LOCALHOST_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def allow_private() -> bool:
    """是否显式放行私有地址（ARGO_ALLOW_PRIVATE_URLS=1）。"""
    return os.environ.get("ARGO_ALLOW_PRIVATE_URLS", "").strip() in {
        "1", "true", "yes",
    }


def is_private_ip(ip_str: str) -> bool:
    """判断 IP 是否属于私有 / 特殊用途段。"""
    try:
        ip = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE_NETWORKS)


def is_fake_ip_address(ip_str: str) -> bool:
    """判断 IP 是否属于 fake-ip 代理占位段。

    fake-ip 模式（Clash/Surge TUN）下代理工具把域名解析到占位段，
    流量由 TUN 设备接管转发到真实公网目标。占位段本身不可直达内网，
    因此不构成 SSRF 风险。仅当域名解析出的全部地址均为占位段时，
    才判定为「代理接管域名」并放行（见 host_is_private）。
    """
    try:
        ip = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return False
    return any(ip in net for net in _FAKE_IP_NETWORKS)


def host_is_private_name(host: str) -> bool:
    """主机名级判断：localhost / 裸主机名 / 内网域名后缀。"""
    h = (host or "").strip().rstrip(".").lower()
    if not h:
        return True
    if h in _LOCALHOST_NAMES:
        return True
    if "." not in h:
        return True  # 裸主机名（如 intranet、router）默认视为内网
    return h.endswith(_PRIVATE_HOST_SUFFIXES)


def host_is_private(host: str) -> bool:
    """综合判断主机是否私有：先主机名，再 DNS 解析后的所有 IP。

    fake-ip 占位段例外：若解析出的地址全部是占位段（域名被代理接管，
    流量走 TUN 直达公网），则不视为私网；只要含任一真实私有 IP 即拦截。
    """
    if host_is_private_name(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        # 解析失败无法确认公网性：保守按私有处理（宁可误伤不可漏防）
        return True
    seen = set()
    for info in infos:
        ip = info[4][0]
        if ip in seen:
            continue
        seen.add(ip)
        if is_fake_ip_address(ip):
            # 代理接管域名（fake-ip 占位），不视为内网
            continue
        if is_private_ip(ip):
            return True
    return False


def check_url(url: str) -> tuple[bool, str]:
    """校验 URL 是否可安全请求。返回 (ok, reason)。"""
    if not url or not isinstance(url, str):
        return False, "空 URL"
    try:
        parsed = urlparse(url)
    except ValueError as e:
        return False, f"URL 解析失败: {e}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"仅允许 http/https，收到 {scheme or '空'} scheme"
    host = parsed.hostname or ""
    if not host:
        return False, "URL 缺少主机名"
    if allow_private():
        return True, ""
    if host_is_private_name(host):
        return False, f"主机名指向本机/内网: {host}"
    if host_is_private(host):
        return False, f"目标 IP 属于私有/保留段: {host}"
    return True, ""


def is_safe_fetch_url(url: str) -> bool:
    """便捷布尔封装：仅 http/https 且目标非私有地址。"""
    ok, _ = check_url(url)
    return ok

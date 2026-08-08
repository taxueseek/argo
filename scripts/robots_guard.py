#!/usr/bin/env python3
"""
robots_guard.py — robots.txt 尊重层（合规抓取门禁）

RFC 9309 定义的 robots.txt 是站点所有者声明抓取意愿的公开协议。
本层在发起任何抓取前查询目标域的 robots.txt，尊重其 Disallow 规则：

  - 允许抓取 → 放行
  - 明确禁止 → robots_blocked=True（不抓取，交由上层返回合规拒绝）
  - 无法获取 robots.txt（404/5xx/超时/SSRF 拦截/缺依赖）→ 放行（容错）
  - 开关：ARGO_RESPECT_ROBOTS=0 关闭，默认开启

实现：stdlib urllib.robotparser 解析 + HttpClient 自抓取（统一走
SSRF 防护、UA 轮换、Retry-After 尊重），进程内按域缓存（TTL 1h）。

用法：
    from robots_guard import robots_blocked
    if robots_blocked("https://example.com/private/page"):
        # 目标站 robots.txt 禁止该路径，跳过抓取
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

try:
    from url_safety import check_url
except ImportError:  # pragma: no cover
    def check_url(url: str) -> tuple[bool, str]:
        return True, ""


# robots.txt 进程内缓存：key=(scheme, host)，TTL 1 小时
_CACHE_TTL = 3600
_cache: dict[tuple[str, str], tuple[float, "RobotFileParser | None"]] = {}
_lock = threading.Lock()

# 抓 robots.txt 时自报身份（RFC 9309 建议爬虫标识自己，不用随机 UA）
_ROBOTS_UA = "argo-fetch (+respect-robots; local research agent)"


def _robots_enabled() -> bool:
    """开关：ARGO_RESPECT_ROBOTS=0 关闭，默认开启。"""
    return os.environ.get("ARGO_RESPECT_ROBOTS", "1").strip() not in (
        "0", "false", "False", "no")


def _domain_key(url: str) -> tuple[str, str]:
    """返回 (scheme, host) 作为 robots 缓存键（http/https 内容可能不同）。"""
    p = urlparse(url)
    return (p.scheme or "https", p.hostname or "")


def _fetch_robots_txt(host: str, timeout: float) -> str | None:
    """抓取 https://host/robots.txt，返回正文；失败返回 None（调用方放行）。"""
    url = f"https://{host}/robots.txt"
    try:
        ok, _reason = check_url(url)
        if not ok:
            return None  # 本机 fake-ip/TUN 下解析到保留段被拦 → 容错放行
    except Exception:
        pass
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=timeout, max_retries=0, jitter=False)
        resp = client.get(url, extra_headers={"User-Agent": _ROBOTS_UA})
    except Exception:
        return None
    if resp.get("status", 0) >= 400:
        return None  # 404/5xx 视为无 robots 声明 → 放行
    text = resp.get("text") or ""
    return text if text.strip() else None


def _get_parser(scheme: str, host: str, timeout: float) -> RobotFileParser | None:
    """返回带缓存的域解析器；拿不到 robots 返回 None。"""
    now = time.time()
    key = (scheme, host)
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

    text = _fetch_robots_txt(host, timeout)
    rp: RobotFileParser | None = None
    if text is not None:
        rp = RobotFileParser()
        rp.parse(text.splitlines())

    with _lock:
        _cache[key] = (now, rp)
    return rp


def robots_blocked(url: str, timeout: float = 5.0) -> bool:
    """URL 是否被目标站 robots.txt 禁止抓取。

    返回 True = 明确禁止；False = 允许 / 无法获取 robots / 开关关闭。
    规则使用通配 UA（`*`）：本技能 UA 轮换，用具体 UA 匹配会漏掉
    `User-agent: *` 的通用规则，通配匹配最保守合规。
    """
    if not _robots_enabled():
        return False
    scheme, host = _domain_key(url)
    if not host:
        return False
    rp = _get_parser(scheme, host, timeout)
    if rp is None:
        return False  # 拿不到 robots.txt → 容错放行
    try:
        return not rp.can_fetch("*", url)
    except Exception:
        return False  # 解析异常 → 放行


# ─── 测试辅助 ────────────────────────────────────────────────────────────────

def clear_cache() -> None:
    """清空 robots.txt 缓存（测试隔离用）。"""
    with _lock:
        _cache.clear()


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com/"
    print(f"robots_blocked({url}) = {robots_blocked(url)}")

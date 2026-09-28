#!/usr/bin/env python3
"""
robots_guard.py — robots.txt 尊重层（合规抓取检查）

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
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:  # 仅类型检查用；运行时导入会连带 urllib.request → ssl → email
    from urllib.robotparser import RobotFileParser

# RobotFileParser 不在模块级导入：urllib.robotparser 会连带拉起
# urllib.request → http.client → ssl → email（实测 ≈14 ms）。搜索路径的
# known_blocked 只在「本机恰好有该主机 robots 存档」时才需要它，而那在
# CLI 每次新进程里都是少数分支——把导入挪进两个真正构造解析器的函数，
# 其余调用方（cache.local_status 每次搜索都来问一次）就不再为用不到的
# 网络栈买单。

try:
    from url_safety import check_url
except ImportError:  # pragma: no cover
    def check_url(url: str) -> tuple[bool, str]:
        return True, ""

from engine_env import env_flag  # 布尔开关统一判断（见 env_flag 的说明）


# robots.txt 进程内缓存：key=(scheme, host)，TTL 1 小时
_CACHE_TTL = 3600
_CACHE_MAX = 512  # 有界：与 _parser_cache 同纪律（长驻进程防无限增长）
_cache: dict[tuple[str, str], tuple[float, "RobotFileParser | None"]] = {}
_lock = threading.Lock()

# 抓 robots.txt 时自报身份（RFC 9309 建议爬虫标识自己，不用随机 UA）
_ROBOTS_UA = "argo-fetch (+respect-robots; local research agent)"


def _robots_enabled() -> bool:
    """开关：ARGO_RESPECT_ROBOTS=0 关闭，默认开启。"""
    return env_flag("ARGO_RESPECT_ROBOTS")


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
    rp = None
    if text is not None:
        from urllib.robotparser import RobotFileParser
        rp = RobotFileParser()
        rp.parse(text.splitlines())

    with _lock:
        if len(_cache) >= _CACHE_MAX:  # 有界：防长驻进程无限增长
            _cache.clear()
        _cache[key] = (now, rp)
    if text is not None:
        # 顺带落盘，供搜索路径只读复用（不影响本次判定）
        _persist_write(host, text)
    return rp


# ─── 跨进程 robots 判定（搜索侧只读）────────────────────────────────────────
#
# 进程内缓存对命令式 CLI 等于没有：每次调用都是新进程，每次都要重新抓
# robots.txt。而搜索结果里「这条建议抓取」的 URL，有相当一部分早就被判过
# 禁止抓取——实测 49 条建议里 7 条（14%）指向 robots 明令禁止的地址，
# 调用方照着建议去抓只会白跑一趟。
#
# 这里把已抓到的 robots.txt 原文按主机落盘，让**搜索路径能只读地判一次**，
# 全程不联网、不改抓取行为。存原文而不是存结论：robots 规则是按路径匹配的，
# 主机级「禁止」会把同一主机下允许的路径一起误判，存原文才能按 URL 精确判。
_PERSIST_TTL = 24 * 3600   # 只读判定用的有效期（仅作建议，抓取侧仍以实时为准）
_PERSIST_MAX = 500         # 落盘主机数上限，超出按 mtime 淘汰

# known_blocked 的解析记忆化：host → (mtime_ns, RobotFileParser)。
# 搜索路径对同一主机的 N 条结果会问 N 次（cache.local_status → known_blocked），
# 旧实现每次都 read_text + RobotFileParser().parse()——同一 host 的 robots.txt
# 被读 N 遍、解析 N 遍（20 条结果 / ~12 个主机 ≈ 1.5–4 ms/次搜索，每次搜索
# 都付）。按 (host, mtime_ns) 记忆：文件被改写时 mtime 变，缓存自然失效；
# 与上面抓取侧的 _cache（TTL 1h）互不共享——那条服务「抓不抓」，这条服务
# 「建议不建议核验」，两者的失效判据不同（一个跟时间，一个跟文件变化）。
_parser_cache: dict[str, tuple[int, "RobotFileParser"]] = {}
_PARSER_CACHE_MAX = 512


def _parser_for(host: str, f) -> "RobotFileParser | None":
    """按 (host, mtime_ns) 复用 RobotFileParser；读盘/解析失败返回 None。"""
    try:
        mtime_ns = f.stat().st_mtime_ns
    except OSError:
        return None
    hit = _parser_cache.get(host)
    if hit is not None and hit[0] == mtime_ns:
        return hit[1]
    try:
        from urllib.robotparser import RobotFileParser
        text = f.read_text(encoding="utf-8", errors="replace")
        rp = RobotFileParser()
        rp.parse(text.splitlines())
    except Exception:
        return None
    if len(_parser_cache) >= _PARSER_CACHE_MAX:  # 有界：防长驻进程无限增长
        _parser_cache.clear()
    _parser_cache[host] = (mtime_ns, rp)
    return rp


def _persist_dir():
    try:
        import argo_paths
        return argo_paths.ensure_state_dir("robots")
    except Exception:
        return None


def _persist_write(host: str, text: str) -> None:
    """把 robots.txt 原文落盘。失败静默——这只是加速层。"""
    d = _persist_dir()
    if d is None or not host:
        return
    try:
        import argo_paths
        argo_paths.atomic_write_text(d / f"{host}.txt", text)
        _persist_evict(d)
    except Exception:
        pass


def _persist_evict(d) -> None:
    try:
        files = sorted((f for f in d.iterdir() if f.suffix == ".txt"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[_PERSIST_MAX:]:
            f.unlink()
    except Exception:
        pass


def known_blocked(url: str, max_age: float = _PERSIST_TTL) -> bool | None:
    """只读判定：本地已存有该主机的 robots.txt 时返回其结论。

    True=禁止抓取 / False=允许 / **None=本地没有存档，不做判断**。
    None 是首要语义：搜索路径不该为了给一个答案去联网，也不该把「不知道」
    说成「可以抓」。
    """
    scheme, host = _domain_key(url)
    if not host:
        return None
    d = _persist_dir()
    if d is None:
        return None
    try:
        f = d / f"{host}.txt"
        if not f.is_file():
            return None
        if max_age and (time.time() - f.stat().st_mtime) > max_age:
            return None
        rp = _parser_for(host, f)
        if rp is None:
            return None
        return not rp.can_fetch("*", url)
    except Exception:
        return None


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
    # known_blocked 的解析记忆化一并清：它按 mtime_ns 自失效，但测试可能在
    # 同一纳秒粒度内改写文件，显式清掉更稳（clear_cache 的契约就是「回到
    # 什么都没读过的状态」）。
    _parser_cache.clear()


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com/"
    print(f"robots_blocked({url}) = {robots_blocked(url)}")

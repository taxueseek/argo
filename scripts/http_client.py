#!/usr/bin/env python3
"""
http_client.py — 零依赖增强 HTTP 请求层

替代 urllib 的轻量封装，提供 Hound 级别的搜索弹性：
  1. User-Agent 轮换池（模拟 Chrome/Safari/Firefox/Edge）
  2. Cookie Jar 积累（跨请求保持会话）
  3. 指数退避重试 + 429/503 Retry-After 尊重
  4. 请求间随机抖动延迟（避免被识别为 bot）
  5. curl subprocess fallback（需要更强反检测时）

纯 stdlib 实现，零 pip 依赖。

用法：
    from http_client import HttpClient
    client = HttpClient()
    resp = client.get("https://example.com")
    print(resp["status"], resp["text"][:200])
"""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import os
import random
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
from contextlib import contextmanager
from typing import Any

try:
    from url_safety import check_url
except ImportError:  # pragma: no cover
    def check_url(url: str) -> tuple[bool, str]:
        return True, ""


# ─── User-Agent 轮换池 ───────────────────────────────────────────────────────

# 模拟主流浏览器的完整请求头（不只是 UA 字符串，而是完整 header 集）
_UA_PROFILES = [
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:142.0) Gecko/20100101 Firefox/142.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="150", "Microsoft Edge";v="150"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    },
]


def _random_headers(extra: dict | None = None) -> dict:
    """生成一组随机浏览器请求头。"""
    profile = random.choice(_UA_PROFILES).copy()
    if extra:
        profile.update(extra)
    return profile


def _charset_of(content_type: str) -> str:
    """从 Content-Type 解析 charset；缺失或无法识别时回落 utf-8。

    未知编码必须先判定再解码：`bytes.decode("x-unknown-8bit")` 会抛 LookupError，
    而 LookupError 不是 OSError 子类，会被 HttpClient.get 的 `except Exception`
    吞掉并返回 status=0——一个正常的 200 响应被静默降级成「连接失败」，
    还被归因为 network。运维现实里 `charset=x-unknown-8bit`、
    `charset=gb2312;` 这类脏头并不罕见。
    """
    import codecs
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].strip().split(";")[0].strip().strip('"\'')
        try:
            codecs.lookup(charset)
            return charset
        except (LookupError, ValueError):
            pass
    return "utf-8"


def _decode_body(raw_body: bytes, content_type: str) -> str:
    """按 Content-Type 的 charset 解码响应体，任何失败都不丢 body。"""
    return raw_body.decode(_charset_of(content_type), errors="replace")


# ─── Retry-After 尊重（合规限速信号）────────────────────────────────────────

_RETRY_AFTER_MAX_WAIT = 10.0  # 服务器要求等待超过此秒数 → 放弃不重试


def retry_after_seconds(status: int, headers: dict | None,
                        max_wait: float = _RETRY_AFTER_MAX_WAIT) -> float | None:
    """解析响应的 Retry-After 头，返回应等待的秒数。

    仅 429（速率限制）与 503（服务过载）携带的 Retry-After 是明确的
    「请等待后再请求」信号，其他状态码忽略该头。返回 None 表示不等待：
      - 状态码非 429/503
      - 无 Retry-After 头
      - 头为 HTTP-date 形式或非数字（无法量化等待时间，保守放弃）
      - 等待时间超过 max_wait（超出可接受阈值，直接放弃重试）
    """
    if status not in (429, 503) or not headers:
        return None
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra is None:
        return None
    try:
        wait = float(str(ra).strip())
    except ValueError:
        return None
    if wait > max_wait:
        return None
    return max(0.0, wait)


# ─── 主机域族节流（并发桶 + 最小间隔）──────────────────────────────────────
#
# 背景：fan-out 并行查询时，同一引擎可能被多个工作线程同时命中，聚合起来
# 对源站形成密集连击，换来 429 与封锁。配额表管的是「一天能用多少次」，
# 管不了「这一秒打了多少次」——这里补上后者。
#
# 两条原则：
#   1. 同一家源站的多个域名（主站 / API 域 / CDN 域）共享同一个桶。站点
#      感受到的压力来自「这一家」，不是一个个孤立域名；按域名分桶会让
#      主站限流而 CDN 域继续冲锋，压力算总账时必然超。
#   2. 并发桶与最小间隔缺一不可：只有并发上限、间隔为 0，请求会在瞬间
#      齐发；只有间隔、没有并发上限，前序请求变慢时在途请求照样堆积。
#
# 只对声明的域族生效，未声明的域名不受影响——免费 API 有明确配额契约，
# 不需要进程内节流；节流是给「按网页语义访问、对突发敏感」的源用的。

# (组名, 域名后缀元组)，按声明顺序匹配，host 以任一后缀结尾即归入该组
_HOST_GROUP_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("zhihu", ("zhihu.com",)),
    ("weibo", ("weibo.com", "weibo.cn", "sinaimg.cn", "sina.com.cn")),
    ("xhs", ("xiaohongshu.com", "xhscdn.com")),
    ("bilibili", ("bilibili.com", "bilivideo.com", "hdslb.com", "b23.tv")),
    ("x", ("x.com", "twitter.com", "twimg.com", "t.co")),
    ("douyin", ("douyin.com", "iesdouyin.com", "douyinpic.com", "douyinvod.com")),
    ("baidu", ("baidu.com", "bdstatic.com", "bcebos.com")),
    ("byted", ("bytedance.com", "toutiao.com", "ibyteimg.com")),
    ("sogou", ("sogou.com",)),
    ("bing", ("bing.com",)),
    ("google", ("google.com", "gstatic.com")),
    ("yandex", ("yandex.com", "yandex.ru")),
    ("startpage", ("startpage.com",)),
    ("mojeek", ("mojeek.com",)),
    ("juejin", ("juejin.cn", "juejin.im")),
    ("v2ex", ("v2ex.com",)),
    ("linuxdo", ("linux.do",)),
)

# 每组默认节流参数：并发桶 / 相邻请求最小间隔。
# 经验起点：中文社交与内容平台风控最紧（2 并发 / 1s+）；搜索引擎页
# 次之（2 并发 / 800ms）；其余声明组放宽（3 并发 / 500ms）。
_HOST_GROUP_DEFAULTS: dict[str, tuple[int, int]] = {
    "zhihu": (2, 1200),
    "weibo": (2, 1200),
    "xhs": (2, 1500),
    "bilibili": (2, 1000),
    "x": (2, 1500),
    "douyin": (2, 1500),
    "baidu": (3, 800),
    "byted": (3, 800),
    "sogou": (2, 1000),
    "bing": (2, 800),
    "google": (2, 1000),
    "yandex": (2, 1000),
    "startpage": (2, 1000),
    "mojeek": (2, 800),
    "juejin": (3, 500),
    "v2ex": (3, 500),
    "linuxdo": (3, 500),
}

_SPEC_OVERRIDE_CACHE: dict[str, tuple[int, int] | None] = {}
_SPEC_OVERRIDE_LOCK = threading.Lock()


def host_group_for(url: str) -> str | None:
    """URL 属于哪个域族节流组；不在任何声明组返回 None（不限流）。"""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for group, suffixes in _HOST_GROUP_RULES:
        for suffix in suffixes:
            if host == suffix or host.endswith("." + suffix):
                return group
    return None


def register_spec_limit(engine: str, max_concurrency: Any, min_interval_ms: Any) -> None:
    """引擎 spec 显式声明覆盖（max_concurrency / min_interval_ms）。

    spec 值优先于域族默认；传非法值等于显式声明「不限流」（None），
    而非回落到域族默认——声明即契约。
    """
    def _norm(v: Any, default: int) -> int | None:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None
    conc = _norm(max_concurrency, 0) if max_concurrency is not None else None
    interval = _norm(min_interval_ms, 0) if min_interval_ms is not None else None
    with _SPEC_OVERRIDE_LOCK:
        _SPEC_OVERRIDE_CACHE[engine] = (
            (conc, interval) if conc or interval else None
        )


def _limits_for(url: str, engine: str | None) -> tuple[int, int] | None:
    """解析该请求应使用的 (并发上限, 最小间隔 ms)；None = 不限流。"""
    if engine:
        with _SPEC_OVERRIDE_LOCK:
            override = _SPEC_OVERRIDE_CACHE.get(engine)
        if override:
            return override
        if override is None and engine in _SPEC_OVERRIDE_CACHE:
            return None  # 显式注册过且声明不限流
    group = host_group_for(url)
    if group is None:
        return None
    return _HOST_GROUP_DEFAULTS.get(group, (2, 800))


class _HostBucket:
    """单个域族的节流桶：信号量（并发）+ 时间戳（最小间隔）。"""

    def __init__(self, max_concurrency: int, min_interval_ms: int):
        self._sem = threading.BoundedSemaphore(max_concurrency)
        self._interval = max(0.0, min_interval_ms / 1000.0)
        self._lock = threading.Lock()
        self._last_dispatch = 0.0
        self._active = 0  # 在途 lease() 数：LRU 淘汰只踢零活跃的桶（见 host_throttle）

    def _wait_slot(self) -> None:
        """等到本线程拿到「发起权」，并保证相邻发起间隔 >= interval。

        实现是取号-复核循环：在锁内检查「现在是否已到允许时刻」，
        到了就把下一允许时刻设为「**真实** now + interval」并返回；
        没到就按剩余时间睡一觉后**重新复核**。

        为什么必须复核，而不是一次算出 wait 就睡：
        `time.sleep` 只保证「至少睡够」，实测过冲可达数十毫秒（GIL/
        调度）。若按「进入函数时的 now + wait」预约下一位，过冲会让
        **实际**发起时刻晚于预约值，下一位仍按旧预约值唤醒——相邻两
        次真实发起就被过冲吃掉一段间隔。实测旧实现第 2 次间隔仅
        0.0001~0.044s（声明 60ms），节流退化为「只对首次生效」；测试
        test_min_interval_spread 在 HEAD 即因此间歇失败，是既有缺陷。
        复核循环用真实时刻推进预约位，过冲只让等待变短、不让间隔变短。
        """
        if self._interval <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._last_dispatch:
                    # 拿到发起权：下一允许时刻按真实 now 推进，
                    # 保证与本次真实发起严格相隔一个 interval
                    self._last_dispatch = now + self._interval
                    return
                wait = self._last_dispatch - now
            # 锁外睡眠；醒来后重新复核（过冲/并发都会在这里被纠正）
            time.sleep(wait)

    @contextmanager
    def lease(self):
        """进入即拿并发名额并按最小间隔排期，退出释放。"""
        self._sem.acquire()
        with self._lock:
            self._active += 1
        try:
            self._wait_slot()
            yield
        finally:
            with self._lock:
                self._active -= 1
            self._sem.release()


_BUCKETS: dict[str, _HostBucket] = {}
_BUCKETS_LOCK = threading.Lock()
_MAX_BUCKETS = 100
_bucket_last_used: dict[str, float] = {}


@contextmanager
def host_throttle(url: str, engine: str | None = None):
    """按 URL 的域族节流。无组 / 显式不限流 → 直接放行。

    桶键 = (组名, 并发上限, 间隔)：同组同参的引擎共享一个桶；个别引擎
    用 spec 覆盖了更紧的参数时自成桶，与组内其余引擎互不干扰。
    """
    limits = _limits_for(url, engine)
    if not limits:
        yield None
        return
    max_conc, interval = limits
    # spec 单值声明（只给并发或只给间隔）时另一个分量为 None——BoundedSemaphore(None)
    # 会在内部 value<0 处抛 TypeError，补默认值（与域族默认 2 并发一致；间隔 0=不限）
    max_conc = max_conc if max_conc else 2
    interval = interval if interval is not None else 0
    group = host_group_for(url)
    bucket_key = f"{group}|{max_conc}|{interval}"
    with _BUCKETS_LOCK:
        bucket = _BUCKETS.get(bucket_key)
        if bucket is None:
            if len(_BUCKETS) >= _MAX_BUCKETS:
                # LRU 淘汰，但只踢**零活跃**的桶：踢掉正被 lease() 持有的桶，
                # 同 key 新桶与旧桶租户并行，该主机的并发上限与最小间隔瞬间
                # 双份（限速击穿）。全部活跃时宁可靠桶数短暂越上限（桶键空间
                # 受配置约束，有界），也不打破约束。
                idle = [k for k in _bucket_last_used if _BUCKETS[k]._active == 0]
                if idle:
                    evict_key = min(idle, key=_bucket_last_used.get)
                    del _BUCKETS[evict_key]
                    del _bucket_last_used[evict_key]
            bucket = _HostBucket(max_conc, interval)
            _BUCKETS[bucket_key] = bucket
        _bucket_last_used[bucket_key] = time.monotonic()
    with bucket.lease():
        yield group


# ─── Cookie 管理 ─────────────────────────────────────────────────────────────

class _CookieManager:
    """跨请求保持 Cookie 积累（Hound 暖会话机制）。"""

    def __init__(self, persist_path: str | None = None):
        self._jar = http.cookiejar.CookieJar()
        self._persist_path = persist_path

    def get_cookie_header(self, url: str) -> str:
        """获取适用于指定 URL 的 Cookie 头。"""
        parsed = urllib.parse.urlparse(url)
        # 构建一个虚拟 request 对象让 cookiejar 提取
        import urllib.request as _urllib_request
        req = _urllib_request.Request(url)
        self._jar.add_cookie_header(req)
        return req.get_header("Cookie") or ""

    def extract_from_response(self, url: str, response_headers: list[tuple[str, str]]) -> None:
        """从响应头提取 Set-Cookie 并存入 jar。"""
        parsed = urllib.parse.urlparse(url)
        # 构建 mock request 让 cookiejar 能提取
        import urllib.request as _urllib_request
        req = _urllib_request.Request(url)
        # 使用 http.cookiejar.extract_cookies 需要 response 对象
        # 简化：手动解析 Set-Cookie
        for name, value in response_headers:
            if name.lower() == "set-cookie":
                self._parse_and_store(url, value)

    def _parse_and_store(self, url: str, set_cookie: str) -> None:
        """解析 Set-Cookie 头并存入 jar。"""
        try:
            parsed = urllib.parse.urlparse(url)
            # 用 MozillaCookieJar 的方式存储
            cookie = http.cookiejar.Cookie(
                version=0,
                name="",
                value="",
                port=None,
                port_specified=False,
                domain=parsed.hostname or "",
                domain_specified=True,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=parsed.scheme == "https",
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
            # 解析 name=value 部分
            parts = set_cookie.split(";")
            if parts:
                nv = parts[0].strip()
                if "=" in nv:
                    cookie.name, cookie.value = nv.split("=", 1)
                    cookie.domain = parsed.hostname or ""
                    self._jar.set_cookie(cookie)
        except Exception as _exc:
            # 每响应都跑：坏 Set-Cookie 留 debug 痕迹（此前是全仓最高频静默点）
            import logging
            logging.getLogger("unified_search").debug(
                f"Set-Cookie 解析失败: {_exc!r}")

    @property
    def jar(self) -> http.cookiejar.CookieJar:
        return self._jar


# ─── 主 HTTP 客户端 ──────────────────────────────────────────────────────────

class HttpClient:
    """增强 HTTP 客户端：UA 轮换 + Cookie 积累 + 重试弹性。

    设计原则：
    - 每次请求随机选择 UA profile（模拟不同浏览器）
    - 自动积累 Cookie（跨请求保持会话状态）
    - 429/503 尊重 Retry-After 头
    - 指数退避重试（最多 3 次）
    - 请求间随机抖动延迟（0.1-0.5s）
    """

    def __init__(self, timeout: float = 10.0, max_retries: int = 2,
                 jitter: bool = True, use_curl_fallback: bool = False):
        self.timeout = timeout
        self.max_retries = max_retries
        self.jitter = jitter
        self.use_curl_fallback = use_curl_fallback
        self._cookies = _CookieManager()
        self._last_request_time = 0.0

    def get(self, url: str, extra_headers: dict | None = None,
            follow_redirects: bool = True,
            engine: str | None = None,
            impersonate_profiles: list[str] | None = None) -> dict:
        """发送 GET 请求，返回统一响应格式。

        SSRF 防护：默认拒绝内网 / 私有地址目标（ARGO_ALLOW_PRIVATE_URLS=1
        显式放行）。校验失败返回 status=0 + error，不发起请求。
        engine：调用方引擎 id，用于按引擎 spec 覆盖域族节流参数。

        返回：{
            "status": int,
            "headers": dict,
            "text": str,
            "url": str,       # 最终 URL（跟随重定向后）
            "elapsed_ms": int,
            "from_cache": bool,
        }
        """
        ok, reason = check_url(url)
        if not ok:
            return {"status": 0, "headers": {}, "text": "", "url": url,
                    "elapsed_ms": 0, "error": f"URL 被 SSRF 防护拦截: {reason}"}

        self._apply_jitter()

        last_error = None
        # 域族节流包住整个重试循环：重试同样占用该源站的并发名额
        with host_throttle(url, engine):
            for attempt in range(self.max_retries + 1):
                try:
                    if impersonate_profiles:
                        # TLS 指纹仿冒档：反爬按 ClientHello 判机器人时启用
                        # （2026-09-26 Baidu 实测：Python 原生指纹整 IP 被降级
                        # 为壳页，chrome/safari 仿冒同刻 17/12 条）。
                        resp = self.get_impersonated(
                            url, extra_headers=extra_headers,
                            timeout=self.timeout, profiles=impersonate_profiles)
                    else:
                        resp = self._do_get(url, extra_headers, follow_redirects)
                    # 429/503 + Retry-After：服务器明确要求等待 → 按其指示等待后重试
                    # （等待服务器说的时间，而非盲退避；无头/超阈值则直接返回不重试）
                    wait = retry_after_seconds(resp.get("status", 0),
                                               resp.get("headers", {}))
                    if wait is not None and attempt < self.max_retries:
                        time.sleep(wait)
                        continue
                    return resp
                except (socket.timeout, ConnectionError, OSError) as e:
                    last_error = e
                    if attempt < self.max_retries:
                        wait = self._backoff_delay(attempt)
                        time.sleep(wait)
                except Exception as e:
                    last_error = e
                    break

        return {"status": 0, "headers": {}, "text": "", "url": url,
                "elapsed_ms": 0, "error": str(last_error)[:200]}

    def _do_get(self, url: str, extra_headers: dict | None,
                follow_redirects: bool) -> dict:
        """实际执行 GET 请求（使用 http.client，不自动解压）。"""
        start = time.time()
        current_url = url
        redirects_left = 5 if follow_redirects else 0

        while True:
            # 构建请求头
            headers = _random_headers(extra_headers)
            cookie_str = self._cookies.get_cookie_header(current_url)
            if cookie_str:
                headers["Cookie"] = cookie_str

            # 解析 URL
            parsed = urllib.parse.urlparse(current_url)
            if not parsed.scheme:
                current_url = "https://" + current_url
                parsed = urllib.parse.urlparse(current_url)

            # 使用 http.client（不自动解压，我们可以手动处理）；出口经 net_proxy 调度
            from net_proxy import request_selector, resolve_proxy
            from conn_pool import discard as _pool_discard, give_back as _pool_give, borrow_via_net_proxy as _pool_borrow
            proxy_url = resolve_proxy(current_url)
            # 连接池：优先取同主机空闲连接，没有才经 net_proxy 新建（出口单点不变）
            conn, _key, _reused = _pool_borrow(parsed, self.timeout, proxy_url)

            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query

            for _try in (0, 1):
                try:
                    conn.request("GET", request_selector(parsed, path, bool(proxy_url)),
                                 headers=headers)
                    resp = conn.getresponse()
                    break
                except (http.client.HTTPException, ConnectionError, OSError):
                    # 复用连接被服务端悄悄关闭（keep-alive 空闲超时）是最高频失效形态：丢弃换新重试一次，不占调用方重试预算
                    _pool_discard(_key, conn)
                    if not _reused or _try:
                        raise
                    conn, _key, _reused = _pool_borrow(parsed, self.timeout, proxy_url)
            status = resp.status
            resp_headers = dict(resp.getheaders())

            # 提取 Cookie
            self._cookies.extract_from_response(current_url, resp.getheaders())

            # 跟随重定向（301/302/303/307/308）：Location 相对/绝对都解析
            if status in (301, 302, 303, 307, 308) and redirects_left > 0:
                loc = resp.getheader("Location")
                # 响应体读干才能还池；读失败（连接已坏）就地丢弃。
                try:
                    resp.read()
                    _pool_give(_key, conn)
                except Exception:
                    _pool_discard(_key, conn)
                if not loc:
                    break
                current_url = urllib.parse.urljoin(current_url, loc)
                # 部分站点的 Location 是未编码的（zdic /hans/道）：原始 UTF-8
                # 字节被 http.client 按 latin-1 解码成乱码——先按 latin-1 还原
                # UTF-8，再补编码残余非 ASCII 字符（%XX 序列原样保留）
                if any(ord(ch) > 127 for ch in current_url):
                    try:
                        current_url = current_url.encode("latin-1").decode("utf-8")
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        pass
                    current_url = "".join(
                        ch if ord(ch) < 128 else urllib.parse.quote(ch)
                        for ch in current_url
                    )
                redirects_left -= 1
                continue

            # 读取 body
            raw_body = resp.read()
            # 还池 vs 关闭：服务端明示 Connection: close（will_close）时读完即废。
            if getattr(resp, "will_close", False):
                _pool_discard(_key, conn)
            else:
                _pool_give(_key, conn)

            # 手动解压
            encoding = resp.getheader("Content-Encoding", "")
            if "gzip" in encoding:
                import gzip
                import io
                raw_body = gzip.GzipFile(fileobj=io.BytesIO(raw_body)).read()
            elif "br" in encoding:
                try:
                    import brotli
                    raw_body = brotli.decompress(raw_body)
                except ImportError:
                    # brotli 不可用 → curl fallback
                    return self.get_with_curl(url, extra_headers)

            # 解码
            content_type = resp.getheader("Content-Type", "")
            text = _decode_body(raw_body, content_type)

            elapsed = int((time.time() - start) * 1000)

            return {
                "status": status,
                "headers": resp_headers,
                "text": text,
                "url": current_url,
                "elapsed_ms": elapsed,
                "from_cache": False,
            }

    def _apply_jitter(self) -> None:
        """请求间随机抖动延迟。"""
        if not self.jitter:
            return
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < 0.1:
            delay = random.uniform(0.05, 0.3)
            time.sleep(delay)
        self._last_request_time = time.time()

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """指数退避 + 随机抖动。"""
        base = min(2 ** attempt, 8)  # 1s, 2s, 4s, 8s cap
        return base + random.uniform(0, 0.5)

    def get_with_curl(self, url: str, extra_headers: dict | None = None) -> dict:
        """curl subprocess fallback（更强的反检测能力）。

        curl 的 TLS 指纹与 Python urllib 不同，某些网站对 curl 更友好。
        重定向由 Python 侧安全跟随（每跳校验），curl 自身不跟随。
        """
        ok, reason = check_url(url)
        if not ok:
            return {"status": 0, "headers": {}, "text": "", "url": url,
                    "elapsed_ms": 0, "error": f"URL 被 SSRF 防护拦截: {reason}"}

        start = time.time()
        headers = _random_headers(extra_headers)

        final_url = self._safe_follow_redirects(url, headers)
        if final_url is None:
            return {"status": 0, "headers": {}, "text": "", "url": url,
                    "elapsed_ms": int((time.time() - start) * 1000),
                    "error": "重定向目标被 SSRF 防护拦截"}

        cmd = ["curl", "-s", "--max-redirs", "0", "--max-time",
               str(int(self.timeout)),
               "-w", "\\n%{http_code}\\n%{url_effective}"]
        # 出口调度（issue #13）：curl 原生只认标准小写 env，argo 级配置
        # （ARGO_PROXY/rules）须显式 -x 下发
        try:
            from net_proxy import resolve_proxy
            _px = resolve_proxy(final_url)
            if _px:
                cmd.extend(["-x", _px])
        except Exception:
            pass
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
        cmd.append(final_url)

        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=self.timeout + 5)
            output = r.stdout.strip()
            lines = output.rsplit("\n", 2)
            if len(lines) >= 2:
                text = lines[0] if len(lines) == 2 else "\n".join(lines[:-2])
                status = int(lines[-2]) if lines[-2].isdigit() else 0
                final_url = lines[-1]
            else:
                text = output
                status = 0
                final_url = url

            elapsed = int((time.time() - start) * 1000)
            return {"status": status, "headers": {}, "text": text, "url": final_url,
                    "elapsed_ms": elapsed, "from_cache": False, "via_curl": True}
        except Exception as e:
            return {"status": 0, "headers": {}, "text": "", "url": url,
                    "elapsed_ms": 0, "error": str(e)[:200]}

    def _curl_proxies(self, url: str) -> dict | None:
        """curl_cffi 的 proxies 参数；出口解析唯一来源在 net_proxy。"""
        try:
            from net_proxy import resolve_proxy
            p = resolve_proxy(url)
            if not p:
                return None
            scheme = urllib.parse.urlparse(url).scheme or "https"
            return {scheme: p}
        except Exception:
            return None

    def get_impersonated(self, url: str, extra_headers: dict | None = None,
                         timeout: float | None = None,
                         profiles: list[str] | None = None) -> dict:
        """TLS 指纹伪造请求（curl_cffi impersonate）。

        原理：urllib/curl 的 TLS ClientHello 指纹与真实浏览器不同，
        反爬站点（Cloudflare 等）凭指纹即可判定机器人并直接 403。
        curl_cffi 可逐字节模拟 Chrome/Safari/Firefox 的 TLS 指纹，
        在不启动浏览器的情况下通过指纹检测。

        - 指纹轮换：按 profiles 顺序尝试，直到成功
        - SSRF 防护：重定向逐跳校验（与 get_with_curl 一致）
        - 失败返回 status=0 + error，不抛异常

        返回格式与 get() 一致，另附 `impersonate`（成功所用指纹）。
        """
        ok, reason = check_url(url)
        if not ok:
            return {"status": 0, "headers": {}, "text": "", "url": url,
                    "elapsed_ms": 0, "error": f"URL 被 SSRF 防护拦截: {reason}"}

        try:
            from curl_cffi import requests as cr
        except ImportError:
            return {"status": 0, "headers": {}, "text": "", "url": url,
                    "elapsed_ms": 0, "error": "curl_cffi not installed"}

        start = time.time()
        _timeout = timeout if timeout is not None else self.timeout
        headers = _random_headers(extra_headers)
        profiles = profiles or ["chrome", "safari", "firefox"]
        current = url

        for _ in range(5):  # 最多 5 跳重定向
            ok, _reason = check_url(current)
            if not ok:
                return {"status": 0, "headers": {}, "text": "", "url": current,
                        "elapsed_ms": int((time.time() - start) * 1000),
                        "error": f"重定向目标被 SSRF 防护拦截: {_reason}"}

            last_err = None
            last_resp = None  # 最后一次非重定向响应（用于全指纹被拒时上报）
            for fp in profiles:
                try:
                    resp = cr.get(current, impersonate=fp, headers=headers,
                                  timeout=_timeout, allow_redirects=False,
                                  proxies=self._curl_proxies(current))
                    status = resp.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if not location:
                            return {"status": status, "headers": dict(resp.headers),
                                    "text": resp.text, "url": current,
                                    "elapsed_ms": int((time.time() - start) * 1000),
                                    "from_cache": False, "impersonate": fp}
                        current = urllib.parse.urljoin(current, location)
                        break  # 进入下一跳
                    last_resp = (status, dict(resp.headers), resp.text)
                    if status in (429, 503):
                        # 明确停止信号（速率限制/服务过载）→ 与 TLS 指纹无关，
                        # 轮换指纹是徒劳的 bot 行为。立即返回并标记 stop_signal，
                        # 交由上层（fetch_v3 主链）停止升级重链。
                        return {"status": status, "headers": last_resp[1],
                                "text": last_resp[2], "url": current,
                                "elapsed_ms": int((time.time() - start) * 1000),
                                "from_cache": False, "impersonate": fp,
                                "stop_signal": True}
                    if status >= 400:
                        # 该指纹被反爬拒绝（403 等），轮换下一个指纹
                        continue
                    return {"status": status, "headers": last_resp[1],
                            "text": last_resp[2], "url": current,
                            "elapsed_ms": int((time.time() - start) * 1000),
                            "from_cache": False, "impersonate": fp}
                except Exception as e:
                    last_err = e
                    continue
            else:
                if last_resp is not None:
                    # 全部指纹均被拒 → 返回最后一次响应，交由上层降级
                    status, hdrs, text = last_resp
                    return {"status": status, "headers": hdrs, "text": text,
                            "url": current,
                            "elapsed_ms": int((time.time() - start) * 1000),
                            "from_cache": False,
                            "impersonate": profiles[-1]}
                # 全部指纹请求异常
                return {"status": 0, "headers": {}, "text": "", "url": current,
                        "elapsed_ms": int((time.time() - start) * 1000),
                        "error": f"TLS 指纹请求失败: {str(last_err)[:150]}"}

        return {"status": 0, "headers": {}, "text": "", "url": current,
                "elapsed_ms": int((time.time() - start) * 1000),
                "error": "重定向超过 5 跳"}

    def post(self, url: str, body: dict | None = None,
             extra_headers: dict | None = None,
             follow_redirects: bool = True,
             json_body: bool = True,
             engine: str | None = None) -> dict:
        """发送 POST 请求（anysearch 等 JSON-RPC/JSON API 用）。

        复用 get() 的 UA 轮换、Cookie 积累、重试退避、Retry-After 尊重、
        SSRF 防护与域族节流。返回格式与 get() 一致。body：dict → json.dumps
        （json_body=True）或 urlencode（json_body=False）；None 则不发送 body。
        """
        ok, reason = check_url(url)
        if not ok:
            return {"status": 0, "headers": {}, "text": "", "url": url,
                    "elapsed_ms": 0, "error": f"URL 被 SSRF 防护拦截: {reason}"}
        self._apply_jitter()
        last_error = None
        payload = None
        if body is not None:
            if json_body:
                payload = json.dumps(body).encode("utf-8")
            else:
                payload = urllib.parse.urlencode(body).encode("utf-8")
        with host_throttle(url, engine):
            for attempt in range(self.max_retries + 1):
                try:
                    resp = self._do_post(url, payload, extra_headers, follow_redirects)
                    wait = retry_after_seconds(resp.get("status", 0),
                                               resp.get("headers", {}))
                    if wait is not None and attempt < self.max_retries:
                        time.sleep(wait)
                        continue
                    return resp
                except (socket.timeout, ConnectionError, OSError) as e:
                    last_error = e
                    if attempt < self.max_retries:
                        time.sleep(self._backoff_delay(attempt))
                except Exception as e:
                    last_error = e
                    break
        return {"status": 0, "headers": {}, "text": "", "url": url,
                "elapsed_ms": 0, "error": str(last_error)[:200]}

    def _do_post(self, url: str, payload: bytes | None,
                 extra_headers: dict | None, follow_redirects: bool) -> dict:
        """实际执行 POST（使用 http.client，逻辑与 _do_get 保持一致但不改 GET 路径）。"""
        start = time.time()
        current_url = url
        redirects_left = 5 if follow_redirects else 0
        while True:
            headers = _random_headers(extra_headers)
            cookie_str = self._cookies.get_cookie_header(current_url)
            if cookie_str:
                headers["Cookie"] = cookie_str
            parsed = urllib.parse.urlparse(current_url)
            if not parsed.scheme:
                current_url = "https://" + current_url
                parsed = urllib.parse.urlparse(current_url)
            from net_proxy import open_connection, request_selector, resolve_proxy
            proxy_url = resolve_proxy(current_url)
            conn, via_proxy = open_connection(parsed, self.timeout, proxy_url)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            conn.request("POST", request_selector(parsed, path, via_proxy),
                         body=payload, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = dict(resp.getheaders())
            self._cookies.extract_from_response(current_url, resp.getheaders())
            if status in (301, 302, 303, 307, 308) and redirects_left > 0:
                loc = resp.getheader("Location")
                conn.close()
                if not loc:
                    break
                current_url = urllib.parse.urljoin(current_url, loc)
                redirects_left -= 1
                continue
            raw_body = resp.read()
            conn.close()
            encoding = resp.getheader("Content-Encoding", "")
            if "gzip" in encoding:
                import gzip, io
                raw_body = gzip.GzipFile(fileobj=io.BytesIO(raw_body)).read()
            elif "br" in encoding:
                try:
                    import brotli
                    raw_body = brotli.decompress(raw_body)
                except ImportError:
                    return self._do_post_fallback(url, payload, extra_headers)
            content_type = resp.getheader("Content-Type", "")
            text = _decode_body(raw_body, content_type)
            return {"status": status, "headers": resp_headers, "text": text,
                    "url": current_url,
                    "elapsed_ms": int((time.time() - start) * 1000),
                    "from_cache": False}

    def _do_post_fallback(self, url: str, payload: bytes | None,
                          extra_headers: dict | None) -> dict:
        """POST 的 curl fallback（brotli 不可用时）。"""
        headers = _random_headers(extra_headers)
        cmd = ["curl", "-s", "--max-time", str(int(self.timeout)), "-X", "POST",
               "-w", "\\n%{http_code}\\n%{url_effective}"]
        # 出口调度（issue #13）：与 GET 路径同一判据——此前这条分支漏了
        # resolve_proxy，「须代理出网」环境里 POST 必然直连失败（2026-09-28 复核）
        try:
            from net_proxy import resolve_proxy
            _px = resolve_proxy(url)
            if _px:
                cmd.extend(["-x", _px])
        except Exception:
            pass
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
        if payload is not None:
            cmd.extend(["--data-binary", "@-"])
        cmd.append(url)
        try:
            import subprocess
            # payload 恒为 bytes（post() 已 encode），故不用 text 模式：
            # text=True 收 bytes input 会 TypeError；stdout 手动按 UTF-8 解
            # （网页字节流本就 UTF-8，显式解也修掉 Windows GBK 乱码）
            r = subprocess.run(cmd, input=payload, capture_output=True,
                               timeout=self.timeout + 5)
            output = (r.stdout or b"").decode("utf-8", errors="replace").strip()
            lines = output.rsplit("\n", 2)
            if len(lines) >= 2:
                text = lines[0] if len(lines) == 2 else "\n".join(lines[:-2])
                status = int(lines[-2]) if lines[-2].isdigit() else 0
            else:
                text, status = output, 0
            return {"status": status, "headers": {}, "text": text, "url": url,
                    "elapsed_ms": 0, "from_cache": False}
        except Exception as e:
            return {"status": 0, "headers": {}, "text": "", "url": url,
                    "elapsed_ms": 0, "error": str(e)[:200]}

    def _safe_follow_redirects(self, url: str, headers: dict,
                               max_redirects: int = 5) -> str | None:
        """逐跳安全跟随重定向：每跳目标都过 SSRF 校验。

        返回最终 URL；任一跳被拦截或超过跳数返回 None。
        """
        current = url
        for _ in range(max_redirects):
            ok, _reason = check_url(current)
            if not ok:
                return None
            parsed = urllib.parse.urlparse(current)
            if parsed.scheme not in ("http", "https"):
                return None
            try:
                from net_proxy import open_connection, request_selector, resolve_proxy
                proxy_url = resolve_proxy(current)
                conn, via_proxy = open_connection(parsed, self.timeout, proxy_url)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                conn.request("HEAD", request_selector(parsed, path, via_proxy),
                             headers=headers)
                resp = conn.getresponse()
                status = resp.status
                location = resp.getheader("Location")
                conn.close()
            except Exception:
                return None
            if status not in (301, 302, 303, 307, 308) or not location:
                return current
            current = urllib.parse.urljoin(current, location)
        return None


# ─── 便捷函数 ────────────────────────────────────────────────────────────────

def fetch_url(url: str, timeout: float = 10.0, max_retries: int = 2) -> dict:
    """一次性 GET 请求的便捷函数。"""
    client = HttpClient(timeout=timeout, max_retries=max_retries)
    return client.get(url)


# ─── CLI 测试 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://httpbin.org/get"

    print(f"=== Testing HttpClient: {url} ===")
    client = HttpClient(timeout=10, max_retries=1)
    resp = client.get(url)

    print(f"Status: {resp['status']}")
    print(f"Elapsed: {resp['elapsed_ms']}ms")
    print(f"Text length: {len(resp['text'])}")
    if resp.get("text"):
        try:
            data = json.loads(resp["text"])
            print(f"Server saw UA: {data.get('headers', {}).get('User-Agent', 'N/A')[:60]}")
        except json.JSONDecodeError:
            print(f"First 200 chars: {resp['text'][:200]}")

    # 测试 curl fallback
    print(f"\n=== Testing curl fallback ===")
    resp2 = client.get_with_curl(url)
    print(f"Status: {resp2['status']}, via_curl: {resp2.get('via_curl')}")
    if resp2.get("text"):
        try:
            data = json.loads(resp2["text"])
            print(f"Server saw UA: {data.get('headers', {}).get('User-Agent', 'N/A')[:60]}")
        except json.JSONDecodeError:
            pass

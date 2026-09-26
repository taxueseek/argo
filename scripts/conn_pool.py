#!/usr/bin/env python3
"""conn_pool.py — http.client 连接池：keep-alive 复用的进程级实现。

问题（第一性原理）：argo 的 HTTP 出口此前是「一次请求一条连接」——每次请求
重付 TCP + TLS 握手（2 RTT：国内源 ≈20–60 ms，海外源 ≈300–600 ms）。一次
典型搜索的握手次数：2–3 个引擎各 1 次 + wave-1 对冲补发 1 次 + fetch_v3 单次
抓取 4–6 跳（md 协商探测 / mobile UA / 主链 / TLS 伪造 / jina / archive CDX）
+ `--verify` 的 3 线程 × fetch 全链 ≈ 12–18 次。即普通搜索 150–500 ms、
research/verify 路径 0.5–3 s 是纯握手开销——而同一主机连打多次时，这些握手
完全相同，本可一次完成。

设计约束（每条都对应一类事故）：

  1. **键 = (scheme, host, port, proxy_url)**：只有同主机、同出口（直连或
     同一个代理）才可复用。HTTPS 经代理时连接上已建立 CONNECT 隧道（主机
     固定在内），换主机必须换连接。重定向跨主机时，连接归还到 **borrow
     时记下的原键**——调用方负责，本模块按传入的 key 归还。
  2. **线程安全**：MCP server 常驻 + verify 的 ThreadPoolExecutor 并发借还。
     每键一把 LifoQueue（后进先出：刚还回的热连接最可能活着）+ 一把全局锁
     护计数。
  3. **陈旧连接**：服务端 keep-alive 空闲超时（常见 5–15 s）后，复用连接的
     首个请求必失败（RemoteDisconnected / BadStatusLine）。本模块不重试
     （GET 幂等，重试策略归调用方），但做三件事降低概率：借出时打时间戳，
     超过 IDLE_TTL 的连接直接关闭不借出；借出前检查 socket 未关闭；调用方
     对「复用连接的首个请求」失败应直接丢弃重借，不计入退避预算。
  4. **有界**：每键 idle 上限 + 全局 idle 上限 + idle TTL。超限还回即关闭。
     达到上限时 borrow 仍**新建无记账连接**——池是优化不是限流器，不能把
     并发卡死（早停弃置的 stray 线程也可能持连接，限流会让它们互相等）。
  5. **响应体读干才能还池**：否则下一个请求会读到上一个响应体。调用方守约
     （http_client._do_get 在读干 body 后才 give_back）；重定向响应未读干，
     走 discard。

计数口径：_live[key] = 「已创建且未关闭」的连接数（idle + 借出都算），
创建 +1、关闭 -1；idle 与借出之间流转不改计数。

开关：ARGO_HTTP_POOL=0 时 borrow 恒新建、give_back 恒关闭——退回「一次请求
一条连接」的旧行为，用于对拍与应急。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any
from urllib.parse import urlparse

# 每键 idle 连接上限：覆盖同主机的少量并发（verify 3 线程 + fetch 链 4–6 跳
# 很少同主机并发超过 4）
_MAX_IDLE_PER_KEY = 4
# 全局 idle 上限：长驻进程的硬 bound，防主机数多时无界囤积
_MAX_IDLE_TOTAL = 32
# idle 连接的最长存活：超过即视为已被服务端关闭，借出前关掉
_IDLE_TTL_S = 30.0

_lock = threading.Lock()
_pools: dict[tuple, "queue.LifoQueue"] = {}
_live: dict[tuple, int] = {}
_total_idle = 0


def _pool_enabled() -> bool:
    import os
    return os.environ.get("ARGO_HTTP_POOL", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def key_for(parsed: Any, proxy_url: str | None) -> tuple:
    """连接的复用键。parsed 为 urlparse 结果。"""
    scheme = (getattr(parsed, "scheme", "") or "https").lower()
    host = (getattr(parsed, "hostname", "") or "").lower()
    port = getattr(parsed, "port", None) or _default_port(scheme)
    return (scheme, host, port, proxy_url or "")


def key_of_url(url: str, proxy_url: str | None) -> tuple:
    """便捷入口：从 URL 字符串算键（测试与调试用）。"""
    from urllib.parse import urlparse as _up
    return key_for(_up(url), proxy_url)


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _sock_alive(conn: Any) -> bool:
    sock = getattr(conn, "sock", None)
    if sock is None:
        return False
    try:
        return sock.fileno() != -1
    except Exception:
        return False


def borrow(parsed: Any, timeout: float, proxy_url: str | None,
           factory: Any) -> tuple[Any, tuple, bool]:
    """借一条到目标主机的连接。返回 (conn, key, reused)。

    factory(parsed, timeout, proxy_url) -> conn：net_proxy.open_connection
    的签名适配（新建路径走它，保持「出口经 net_proxy 调度」这一单点）。
    """
    global _total_idle
    key = key_for(parsed, proxy_url)
    reused = None
    if _pool_enabled():
        with _lock:
            q = _pools.get(key)
            if q is not None:
                while True:
                    try:
                        conn, t = q.get_nowait()
                    except queue.Empty:
                        break
                    _total_idle -= 1
                    stale = (not _sock_alive(conn)
                             or (time.monotonic() - t) > _IDLE_TTL_S)
                    if stale:
                        _close(conn)
                        _live[key] = _live.get(key, 1) - 1
                        continue
                    # idle → 借出：_live 不变（连接未关闭，只是换了状态）
                    reused = conn
                    break
    if reused is not None:
        # 复用连接按本次请求的 timeout 重设 socket 超时（见模块头约束 3）
        sock = getattr(reused, "sock", None)
        if sock is not None:
            try:
                sock.settimeout(timeout)
            except Exception:
                pass
        return reused, key, True
    conn = factory(parsed, timeout, proxy_url)
    with _lock:
        _live[key] = _live.get(key, 0) + 1
    return conn, key, False


def borrow_via_net_proxy(parsed: Any, timeout: float,
                         proxy_url: str | None) -> tuple[Any, tuple, bool]:
    """便捷入口：工厂固定为 net_proxy.open_connection。

    open_connection 返回 `(conn, via_proxy)` 二元组，不能直接当工厂——
    这里包掉 via_proxy，池只拿 conn。保持「出口经 net_proxy 调度」这一
    单点：池只决定「新建还是复用」，不自己造连接。
    """
    def _factory(p: Any, t: float, px: str | None) -> Any:
        from net_proxy import open_connection
        conn, _via = open_connection(p, t, px)
        return conn

    return borrow(parsed, timeout, proxy_url, _factory)


def give_back(key: tuple, conn: Any) -> None:
    """还回一条健康连接（响应体已读干）。超限或开关关闭时直接关闭。"""
    global _total_idle
    if not _pool_enabled() or not _sock_alive(conn):
        discard(key, conn)
        return
    with _lock:
        q = _pools.setdefault(key, queue.LifoQueue())
        if q.qsize() >= _MAX_IDLE_PER_KEY or _total_idle >= _MAX_IDLE_TOTAL:
            over = True
        else:
            over = False
            q.put((conn, time.monotonic()))
            _total_idle += 1
    if over:
        _close(conn)
        with _lock:
            if key in _live:
                _live[key] -= 1
                if _live[key] <= 0:
                    _live.pop(key, None)


def discard(key: tuple, conn: Any) -> None:
    """丢弃一条连接（陈旧 / 响应体未读干 / 异常路径）。不计 idle。"""
    _close(conn)
    with _lock:
        if key in _live:
            _live[key] -= 1
            if _live[key] <= 0:
                _live.pop(key, None)


def stats() -> dict:
    with _lock:
        return {
            "keys": len(_pools),
            "idle": _total_idle,
            "live": dict(_live),
            "enabled": _pool_enabled(),
        }


def clear() -> None:
    """清空所有 idle 连接（测试隔离 / 进程退出前用）。"""
    global _total_idle
    with _lock:
        for q in _pools.values():
            while True:
                try:
                    conn, _t = q.get_nowait()
                except queue.Empty:
                    break
                _close(conn)
        _pools.clear()
        _total_idle = 0
        _live.clear()

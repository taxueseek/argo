#!/usr/bin/env python3
"""crawl_policy.py — 按主机限速、并发上限与 429 退避。

Argo 的既有 robots 合规在 fetch_v3 内部（robots_guard），这里补的是「节奏」：
同一主机两次请求开始之间至少隔 interval 秒，单主机并发受限，全局并发有上限；
被 429 时把该主机的额外间隔加倍（乘性增），成功一次减半（除性减），
让速度停在站点限额之下，而不是冷却结束再全速冲一遍。这是 TCP 拥塞控制的 AIMD
思路，PageSift 的 limiter 用同一套；Argo 的爬取时长是秒级，不需要更复杂的模型。

时间与睡眠可注入，测试用虚拟时钟验证间隔与退避，不真的等。
"""

from __future__ import annotations

import threading
import time
from contextlib import AbstractContextManager
from urllib.parse import urlparse

_PENALTY_START = 0.25
_PENALTY_MAX = 30.0
_PENALTY_FLOOR = 0.01


class _HostState:
    __slots__ = ("interval", "last_start", "penalty", "slots")

    def __init__(self, per_host_concurrency: int, interval: float):
        self.slots = threading.BoundedSemaphore(max(1, per_host_concurrency))
        self.last_start = float("-inf")
        self.penalty = 0.0
        self.interval = interval


class Lease(AbstractContextManager):
    """一次抓取许可。`note()` 可在 with 内记录响应状态。"""

    def __init__(self, gate: HostGate, host: str, state: _HostState):
        self._gate = gate
        self._host = host
        self._state = state
        self._status = 0
        self._retry_after: float | None = None

    def note(self, status: int = 0, retry_after: float | None = None) -> None:
        self._status = int(status or 0)
        self._retry_after = retry_after

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._gate._release(self._host, self._state, self._status, self._retry_after)
        return False


class HostGate:
    """同主机串行间隔 + 并发上限；线程安全。"""

    def __init__(self, interval: float = 1.0, per_host_concurrency: int = 2,
                 global_concurrency: int = 8, *, clock=None, sleep=None,
                 delay_resolver=None):
        self._interval = max(0.0, float(interval))
        self._per_host = max(1, int(per_host_concurrency))
        self._global = threading.BoundedSemaphore(max(1, global_concurrency))
        self._lock = threading.Lock()
        self._hosts: dict[str, _HostState] = {}
        self._delays: dict[str, float] = {}
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        if delay_resolver is None:
            try:
                from robots_guard import crawl_delay as delay_resolver  # type: ignore
            except ImportError:  # pragma: no cover - 骨架环境
                delay_resolver = lambda _url: None
        self._delay_resolver = delay_resolver

    @property
    def interval(self) -> float:
        """默认同主机最小间隔（秒）；调用方排等待预算时用。"""
        return self._interval

    def _state(self, host: str) -> _HostState:
        with self._lock:
            state = self._hosts.get(host)
            if state is None:
                state = _HostState(self._per_host, self._interval)
                self._hosts[host] = state
            return state

    def _delay_for(self, host: str, url: str) -> float:
        with self._lock:
            if host in self._delays:
                return self._delays[host]
        try:
            value = self._delay_resolver(url)
        except Exception:
            value = None
        delay = max(self._interval, float(value)) if value else self._interval
        with self._lock:
            self._delays[host] = delay
        return delay

    def slot(self, url: str) -> Lease:
        """阻塞到允许发出请求，返回租约；`with` 退出时释放并发位。"""
        host = (urlparse(url).netloc or "").lower()
        state = self._state(host)
        interval = self._delay_for(host, url)
        state.slots.acquire()
        acquired_global = False
        try:
            self._global.acquire()
            acquired_global = True
            while True:
                with self._lock:
                    now = self._clock()
                    # 正常间隔与 429 惩罚同时生效：惩罚是对站点压力的额外等待，
                    # 不是替代间隔；两者取大值，避免「惩罚刚衰减完就立刻连发」。
                    gate_at = state.last_start + max(interval, state.penalty)
                    if now >= gate_at:
                        state.last_start = now
                        return Lease(self, host, state)
                    wait = gate_at - now
                self._sleep(wait)
        except BaseException:
            if acquired_global:
                self._global.release()
            state.slots.release()
            raise

    def _release(self, host: str, state: _HostState, status: int,
                 retry_after: float | None) -> None:
        if status == 429:
            with self._lock:
                if state.penalty <= 0:
                    state.penalty = _PENALTY_START
                else:
                    state.penalty = min(_PENALTY_MAX, state.penalty * 2)
                if retry_after:
                    state.penalty = min(_PENALTY_MAX, max(state.penalty,
                                                          float(retry_after)))
        elif 200 <= status < 400:
            with self._lock:
                state.penalty = 0.0 if state.penalty < _PENALTY_FLOOR else state.penalty / 2
        self._global.release()
        state.slots.release()


def retry_after_seconds(result: dict) -> float | None:
    """从 fetch_v3 结果里取 Retry-After（秒）；拿不到返回 None。"""
    value = result.get("retry_after")
    if isinstance(value, (int, float)):
        return float(value)
    headers = result.get("headers") or {}
    if isinstance(headers, dict):
        for key, raw in headers.items():
            if str(key).lower() == "retry-after":
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None
    return None


def status_of(result: dict) -> int:
    """fetch_v3 多数路径带 status；没有时从错误串兜底识别 429/503。"""
    try:
        status = int(result.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    if status:
        return status
    error = str(result.get("error") or "")
    for code in (429, 503, 500, 404, 403):
        if str(code) in error:
            return code
    return 0

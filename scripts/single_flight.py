#!/usr/bin/env python3
"""single_flight.py — 进程内单飞合并：并发相同引擎调用只打一次上游。

设计：
- 线程并发（argo 执行模型是 ThreadPoolExecutor，非 asyncio）
- key = 引擎 + 规范化查询 + 有效参数指纹；同 key 并发调用合并为一次
  leader 执行，followers 等待 leader 结果
- leader 失败/超时 → followers 降级为独立调用（避免一次瞬时故障
  变成 N 相关故障）
- 执行完成后不存储：下一个调用者是新 leader（命中缓存层）
- ARGO_TOOL_CALL_COALESCE=0 可关闭；leaders/coalesced 计数供观测
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# follower 等待上限：leader 卡死时避免全体阻塞等待
_DEFAULT_WAIT_TIMEOUT_S = 60.0


def _enabled() -> bool:
    raw = (os.environ.get("ARGO_TOOL_CALL_COALESCE") or "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


class SingleFlightCoalescer:
    """线程版单飞合并器（供引擎执行层复用）。"""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._cond = threading.Condition()
        self._inflight: dict[str, tuple[threading.Event, list, float]] = {}
        self._lock = threading.Lock()
        self.leaders = 0
        self.coalesced = 0

    def _register_leader(self, key: str) -> None:
        with self._lock:
            self._inflight[key] = (threading.Event(), [], time.time())
            self.leaders += 1

    def _deregister(self, key: str) -> None:
        with self._lock:
            self._inflight.pop(key, None)

    def run(
        self,
        key: str,
        fn: Callable[[], T],
        *,
        wait_timeout: float = _DEFAULT_WAIT_TIMEOUT_S,
    ) -> T:
        if not key or not _enabled():
            return fn()
        with self._cond:
            entry = self._inflight.get(key)
            leader = entry is None
            if leader:
                self._register_leader(key)
            else:
                self.coalesced += 1
        if not leader:
            event, slots, started = entry  # type: ignore[assignment]
            waited = 0.0
            while not event.is_set():
                if waited >= wait_timeout:
                    break  # 超时：降级独立调用
                step = min(1.0, wait_timeout - waited)
                event.wait(timeout=step)
                waited += step
            if event.is_set() and slots and not isinstance(slots[0], Exception):
                return slots[0]  # type: ignore[return-value]
            # leader 失败或超时 → 独立执行（韧性）
            return fn()
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001
            self._publish(key, exc)
            self._deregister(key)
            raise
        self._publish(key, result)
        self._deregister(key)
        return result

    def _publish(self, key: str, value: Any) -> None:
        with self._cond:
            entry = self._inflight.get(key)
            if entry is None:
                return
            event, slots, _ = entry
            with self._lock:
                if slots:
                    slots[0] = value
                else:
                    slots.append(value)
            event.set()
            self._cond.notify_all()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "leaders": self.leaders,
                "coalesced": self.coalesced,
                "inflight": len(self._inflight),
            }


# 单例：引擎执行层共用
_engine_coalescer = SingleFlightCoalescer("engines")


def engine_coalescer() -> SingleFlightCoalescer:
    return _engine_coalescer

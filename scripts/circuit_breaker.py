#!/usr/bin/env python3
"""circuit_breaker.py — 引擎熔断 + 查询级负缓存

吸收 Hound 的 circuit-breaker 思路：
  - 连续失败 / 空结果 → 打开熔断，冷却期内跳过该引擎
  - 查询级负缓存：同一 query+engine 短 TTL 内不再打网络

状态持久化：~/.cache/unified-search/circuit_breaker.json
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Optional

STATE_PATH = os.path.expanduser("~/.cache/unified-search/circuit_breaker.json")

# 熔断参数
FAILURE_THRESHOLD = 2          # 连续失败次数
OPEN_SECONDS = 60              # 熔断冷却
EMPTY_NEGATIVE_TTL = 45        # 空结果负缓存（秒）
ERROR_NEGATIVE_TTL = 30        # 错误负缓存（秒）
HALF_OPEN_PROBE = True         # 冷却后允许一次探测

# 自适应禁用（v2.7）
DISABLE_AFTER_OPENS = 3        # 连续 open 达此次数 → 自动禁用（持久跳过）
DISABLE_COOLDOWN_SECONDS = 3600  # 禁用后 1h 内不自动恢复（避免频繁探测）


class CircuitBreaker:
    """进程内 + 磁盘共享的引擎熔断器。"""

    def __init__(self, state_path: str = STATE_PATH):
        self._path = state_path
        self._lock = threading.RLock()
        self._engines: dict[str, dict[str, Any]] = {}
        self._neg: dict[str, dict[str, Any]] = {}  # key → {expires, status}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self._path):
                data = json.loads(open(self._path, encoding="utf-8").read())
                self._engines = data.get("engines") or {}
                # 负缓存仅进程内有效，不从磁盘恢复（避免长期脏状态）
        except Exception:
            self._engines = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            # 只持久化引擎熔断态
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"engines": self._engines, "updated": time.time()}, f)
            os.replace(tmp, self._path)
        except Exception:
            pass

    # ── 引擎熔断 ────────────────────────────────────────────────────────────

    def status(self, engine: str) -> dict[str, Any]:
        """只读查询引擎熔断状态（不推进 half-open 探测、不落盘）。

        供路由层做「配额/熔断感知沉底」：主引擎熔断打开时自动切换到
        相近备选，正常路径组合集合不变，缓存键不变，速度零影响。
        """
        with self._lock:
            st = self._engines.get(engine) or {}
            state = st.get("state", "closed")
            opened_at = float(st.get("opened_at") or 0)
            return {
                "state": state,
                "failures": int(st.get("failures") or 0),
                "opened_at": opened_at,
                "cooldown_remain": max(0, int(OPEN_SECONDS - (time.time() - opened_at)))
                if state == "open" else 0,
            }

    def allow(self, engine: str) -> tuple[bool, str]:
        """是否允许调用该引擎。返回 (allowed, reason)。"""
        with self._lock:
            st = self._engines.get(engine) or {}
            state = st.get("state", "closed")
            opened_at = float(st.get("opened_at") or 0)

            # 自适应禁用：disabled 引擎直接拒绝（不再 half-open 探测，省超时）
            if state == "disabled":
                return False, "auto_disabled"

            if state == "open":
                # 冷却期已过 → half-open 探测（除非已连续多次 open 触发自动禁用）
                if time.time() - opened_at >= OPEN_SECONDS:
                    opens = int(st.get("opens") or 0)
                    disabled_at = float(st.get("disabled_at") or 0)
                    # 禁用条件：连续 open 达阈值，且距离上次禁用已超冷却（首次 disabled_at=0 视为可禁用）
                    can_disable = opens >= DISABLE_AFTER_OPENS and \
                        (disabled_at == 0 or
                         (time.time() - disabled_at) >= DISABLE_COOLDOWN_SECONDS)
                    if can_disable:
                        st["state"] = "disabled"
                        st["disabled_at"] = time.time()
                        self._engines[engine] = st
                        self._save()
                        return False, "auto_disabled"
                    # half-open：允许一次探测
                    st["state"] = "half_open"
                    self._engines[engine] = st
                    self._save()
                    return True, "half_open_probe"
                remain = int(OPEN_SECONDS - (time.time() - opened_at))
                return False, f"circuit_open:{remain}s"
            return True, "closed"

    def record_success(self, engine: str) -> None:
        with self._lock:
            self._engines[engine] = {
                "state": "closed",
                "failures": 0,
                "opens": 0,          # 重置连续 open 计数
                "last_ok": time.time(),
            }
            self._save()

    def record_failure(self, engine: str, kind: str = "error") -> None:
        """kind: error | timeout | empty"""
        with self._lock:
            st = self._engines.get(engine) or {"failures": 0, "state": "closed"}
            # empty 权重低：两次 empty 才算一次 failure 贡献
            if kind == "empty":
                st["empty_streak"] = int(st.get("empty_streak") or 0) + 1
                if st["empty_streak"] < 2:
                    self._engines[engine] = st
                    self._save()
                    return
                st["empty_streak"] = 0
            st["failures"] = int(st.get("failures") or 0) + 1
            st["last_fail"] = time.time()
            st["last_kind"] = kind
            if st["failures"] >= FAILURE_THRESHOLD or st.get("state") == "half_open":
                st["state"] = "open"
                st["opened_at"] = time.time()
                # 连续 open 计数：每次进入 open 视为一次「失败循环」
                st["opens"] = int(st.get("opens") or 0) + 1
            self._engines[engine] = st
            self._save()

    def reenable(self, engine: str) -> None:
        """外部主动恢复（用户测试通过 / 新环境确认）。"""
        with self._lock:
            self._engines[engine] = {
                "state": "closed", "failures": 0, "opens": 0,
                "last_ok": time.time(), "reenabled_at": time.time(),
            }
            self._save()

    def auto_disabled(self) -> list[str]:
        """返回所有处于自动禁用状态的引擎。"""
        with self._lock:
            return [e for e, st in self._engines.items()
                    if st.get("state") == "disabled"]

    # ── 查询级负缓存 ────────────────────────────────────────────────────────

    @staticmethod
    def _neg_key(query: str, engine: str) -> str:
        raw = f"neg|{query}|{engine}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def set_negative(self, query: str, engine: str, status: str = "no-results",
                     ttl: int | None = None) -> None:
        ttl = ttl if ttl is not None else (
            EMPTY_NEGATIVE_TTL if status == "no-results" else ERROR_NEGATIVE_TTL
        )
        key = self._neg_key(query, engine)
        with self._lock:
            self._neg[key] = {
                "expires": time.time() + ttl,
                "status": status,
                "engine": engine,
            }

    def get_negative(self, query: str, engine: str) -> Optional[dict[str, Any]]:
        key = self._neg_key(query, engine)
        with self._lock:
            hit = self._neg.get(key)
            if not hit:
                return None
            if time.time() >= float(hit.get("expires") or 0):
                self._neg.pop(key, None)
                return None
            return hit

    def clear_negative(self, query: str, engine: str) -> None:
        key = self._neg_key(query, engine)
        with self._lock:
            self._neg.pop(key, None)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            open_engines = [e for e, s in self._engines.items() if s.get("state") == "open"]
            return {
                "open_engines": open_engines,
                "tracked": len(self._engines),
                "neg_entries": len(self._neg),
            }


_breaker: CircuitBreaker | None = None


def get_breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker()
    return _breaker

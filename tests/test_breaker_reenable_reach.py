#!/usr/bin/env python3
"""test_breaker_reenable_reach — 熔断恢复环打通（2026-11-21）。

守的是「disabled 引擎冷却期满后能被探测翻转，不会事实性永久禁用」。

背景：allow() 对 disabled 引擎在 DISABLE_COOLDOWN_SECONDS 后会翻转
half_open（B4 设计意图「避免引擎恢复后永久无入口」），env_fp/key_fp
指纹自愈同样挂在 allow() 里；但路由层对 disabled 硬跳过，allow() 永不
被调用——生产实测 3/5 disabled 引擎 2-12 天无探测字段（biorxiv/
local_duckduckgo/courtlistener），恢复机制整体死代码化。

红/绿由本文件自己保证：修复前 _breaker_blocked 对 disabled 只读不翻，
status 恒 disabled → 恒 blocked；修复后首次调用触发 allow() 状态翻转，
第二次读到 half_open（cooldown_remain=0）自然放行。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

ENG = "reenable_probe_engine"


class _FakeBreaker:
    """内存态断路器桩：复用真 CircuitBreaker 的状态语义，不落盘。"""

    def __init__(self) -> None:
        self._engines: dict[str, dict] = {}
        from circuit_breaker import CircuitBreaker
        self._real = CircuitBreaker.__new__(CircuitBreaker)  # 只借用常量

    def status(self, engine: str) -> dict:
        import circuit_breaker as cb
        import engine_states
        st = self._engines.get(engine) or {}
        state = st.get("state", engine_states.BREAKER_CLOSED)
        return {
            "state": state,
            "failures": int(st.get("failures") or 0),
            "opens": int(st.get("opens") or 0),
            "opened_at": float(st.get("opened_at") or 0),
            "cooldown_remain": max(
                0,
                int(cb.OPEN_SECONDS - (time.time() - st.get("opened_at", 0)))
                if state == engine_states.BREAKER_OPEN else 0,
            ),
        }

    def allow(self, engine: str) -> tuple[bool, str]:
        # 复刻真 allow() 的 disabled 分支：超冷却翻转 half_open，未超冷却拒绝
        import circuit_breaker as cb
        import engine_states
        st = self._engines.get(engine) or {}
        if st.get("state") != engine_states.BREAKER_DISABLED:
            return True, "not_disabled"
        disabled_at = float(st.get("disabled_at") or 0)
        last_probe = float(st.get("reenable_probed_at") or 0)
        if max(disabled_at, last_probe) == 0 or \
                (time.time() - max(disabled_at, last_probe)) >= cb.DISABLE_COOLDOWN_SECONDS:
            st["state"] = engine_states.BREAKER_HALF_OPEN
            st["reenable_probed_at"] = time.time()
            self._engines[engine] = st
            return True, "half_open_reenable"
        return False, "auto_disabled"


class TestBreakerReenableReach(unittest.TestCase):
    def test_disabled_engine_flips_to_half_open_after_cooldown(self) -> None:
        import circuit_breaker as cb
        import engine_states
        from route_combo import _breaker_blocked

        b = _FakeBreaker()
        # 冷却已过 2h 的 disabled 引擎（生产实况：biorxiv 等 3 条 2-12 天无探测）
        b._engines[ENG] = {
            "state": engine_states.BREAKER_DISABLED,
            "disabled_at": time.time() - 7200,
            "failures": 10, "opens": 3,
        }
        with patch("circuit_breaker.get_breaker", return_value=b):
            # 首次路由过滤：仍按 blocked 剔除（本 combo 行为不变），但翻转已发生
            self.assertTrue(_breaker_blocked(ENG))
            self.assertEqual(b.status(ENG)["state"], engine_states.BREAKER_HALF_OPEN,
                             "allow() 必须被触发并翻转 half_open")
            # 第二次过滤：half_open（cooldown_remain=0）保留探测资格，放行
            self.assertFalse(_breaker_blocked(ENG),
                             "翻转后的 half_open 引擎不得继续被硬跳过")

    def test_disabled_engine_within_cooldown_stays_blocked(self) -> None:
        import engine_states
        from route_combo import _breaker_blocked

        b = _FakeBreaker()
        b._engines[ENG] = {
            "state": engine_states.BREAKER_DISABLED,
            "disabled_at": time.time() - 60,  # 冷却未满
            "failures": 10, "opens": 3,
        }
        with patch("circuit_breaker.get_breaker", return_value=b):
            self.assertTrue(_breaker_blocked(ENG))
            self.assertEqual(b.status(ENG)["state"], engine_states.BREAKER_DISABLED,
                             "冷却未满不得翻转")


if __name__ == "__main__":
    unittest.main()

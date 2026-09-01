#!/usr/bin/env python3
"""配额自愈闭环回归（F7，2026-08-29）。

设计目标（用户场景）：引擎远端配额耗尽（如 byted 10406 Free quota exhausted）
→ 执行层自动标记 → 路由组合层全模式排除、备用源自然接管 → 周期边界惰性
自愈自动恢复；提前恢复（充值）走 `python3 scripts/quota.py reset <engine>`。
全程零人工改配置——配额像 AI 模型订阅额度一样「恢复了就能访问」。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from quota import QuotaManager  # noqa: E402
import route as route_mod  # noqa: E402


def _fresh_manager() -> QuotaManager:
    """不落盘的 QuotaManager（隔离真实 quota.json 状态与热读监视器）。"""
    mgr = QuotaManager()
    mgr._state = {}
    mgr._save_state = lambda: None
    mgr._profiles_hot = None  # 屏蔽热读：真实 quota.json 不得污染测试态
    mgr._state_hot = None
    return mgr


class TestRemoteQuotaStateMachine(unittest.TestCase):
    """标记 / 排除 / 周期自愈 / 提前恢复。"""

    def test_mark_exclude_heal_cycle(self):
        mgr = _fresh_manager()
        mgr.mark_remote_exhausted("byted", reason="10406: Free quota exhausted")
        self.assertTrue(mgr.is_remote_exhausted("byted"))
        self.assertTrue(mgr.is_hard_down("byted"))
        self.assertFalse(mgr.is_available("byted", mode="auto"))
        self.assertFalse(mgr.is_available("byted", mode="fast"))
        mark = mgr._state["byted"]["remote_exhausted"]
        self.assertIn("Free quota", mark["reason"])
        self.assertIsNotNone(mgr.get_stats().get("byted", {}).get(
            "remote_exhausted_until"))
        # 周期边界已过 → 惰性自愈，引擎自动回归
        mgr._state["byted"]["remote_exhausted"]["until"] = time.time() - 1
        self.assertFalse(mgr.is_remote_exhausted("byted"))
        self.assertFalse(mgr.is_hard_down("byted"))
        self.assertTrue(mgr.is_available("byted", mode="auto"))

    def test_unknown_engine_defaults_to_day(self):
        mgr = _fresh_manager()
        mgr.mark_remote_exhausted("some_new_engine")
        until = mgr._state["some_new_engine"]["remote_exhausted"]["until"]
        self.assertTrue(86300 <= until - time.time() <= 86500,
                        "未知周期按 24h 保守处理")

    def test_clear_for_early_recovery(self):
        mgr = _fresh_manager()
        mgr.mark_remote_exhausted("byted", reason="quota")
        self.assertTrue(mgr.clear_remote_exhausted("byted"))
        self.assertFalse(mgr.is_remote_exhausted("byted"))
        self.assertFalse(mgr.clear_remote_exhausted("byted"), "重复清除返回 False")

    def test_healthy_engine_not_flagged(self):
        mgr = _fresh_manager()
        self.assertFalse(mgr.is_remote_exhausted("byted"))
        self.assertFalse(mgr.is_hard_down("byted"))


class TestRouteExclusion(unittest.TestCase):
    """路由组合层：配额死引擎全模式排除，备用源接管。"""

    DOMAIN = {
        "name": "news_realtime",
        "engines_combo": ["byted", "anysearch"],
        "primary": "byted",
        "fallback": "anysearch",
        "parallel": True,
    }

    def test_hard_down_engine_replaced_by_fallback(self):
        mgr = _fresh_manager()
        mgr.mark_remote_exhausted("byted", reason="10406")
        with patch.object(route_mod, "get_quota_manager", lambda: mgr):
            combo = route_mod._get_engines_combo(
                self.DOMAIN, {"byted", "anysearch"}, mode="auto", features=None)
        self.assertNotIn("byted", combo, "配额死引擎不得进组合")
        self.assertIn("anysearch", combo, "备用源应接管")

    def test_recovered_engine_returns_as_primary(self):
        mgr = _fresh_manager()
        with patch.object(route_mod, "get_quota_manager", lambda: mgr):
            combo = route_mod._get_engines_combo(
                self.DOMAIN, {"byted", "anysearch"}, mode="auto", features=None)
        self.assertEqual(combo[0], "byted", "恢复后 primary 自动回归首位")


class TestExecutionFeedback(unittest.TestCase):
    """执行层：quota-exhausted 结果自动反馈进状态机。"""

    def test_note_marks_manager(self):
        from search import _note_remote_quota_exhausted
        mgr = _fresh_manager()
        with patch("quota.get_quota_manager", lambda: mgr):
            _note_remote_quota_exhausted(
                "byted", "byted 10406: Free quota has been exhausted.")
        self.assertTrue(mgr.is_remote_exhausted("byted"))
        self.assertIn("10406", mgr._state["byted"]["remote_exhausted"]["reason"])

    def test_note_survives_manager_failure(self):
        from search import _note_remote_quota_exhausted
        with patch("quota.get_quota_manager",
                   side_effect=RuntimeError("boom")):
            # 反馈失败不得影响搜索主流程
            _note_remote_quota_exhausted("byted", "10406")


if __name__ == "__main__":
    unittest.main()

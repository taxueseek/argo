#!/usr/bin/env python3
"""回归测试 — 路由健康检查一致性 + 深度研究 local_first 效率。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


class TestRouteHealthFallbackConsistency(unittest.TestCase):
    """route 的健康检查 fallback（health_probe）必须与主路径同语义：
    只对 local_* 引擎做健康判定，非 local 引擎无条件保留。"""

    def test_non_local_engine_survives_health_probe_unavailable(self):
        from route import _get_engines_combo

        domain = {
            "name": "fact_check",
            "primary": "wikipedia",
            "engines_combo": ["wikipedia", "byted"],
        }
        enabled = {"wikipedia", "byted"}

        # 模拟 health_check 模块缺失 → route 落到 health_probe fallback
        with patch.dict(sys.modules, {"health_check": None}), \
             patch("health_probe.get_engine_status", side_effect=lambda e: {
                 "available": False,
             } if e == "wikipedia" else {"available": True}):
            combo = _get_engines_combo(domain, enabled, mode="auto")

        # wikipedia 是非 local 引擎，health_probe 说 unavailable 也不得被过滤
        self.assertIn("wikipedia", combo)
        self.assertEqual(combo[0], "wikipedia")


class TestLocalFirstEfficiency(unittest.TestCase):
    """local_first 决策树：先本地聚合，结果不足才升级，绝不先跑全量。"""

    def _sparse_local(self, calls: list) -> dict:
        def fake_super_search(query: str, **kw: object) -> dict:
            calls.append(kw.get("engine", "auto"))
            if kw.get("engine") == "local_search":
                # 本地结果稀疏（1 条 < 3）→ 应触发升级
                return {
                    "results": [{
                        "title": "local-hit", "url": "https://e.com/1",
                        "snippet": "x", "source": "local_bing",
                    }],
                    "engines_used": ["local_bing"],
                    "elapsed_ms": 1, "cached": False,
                }
            return {
                "results": [
                    {"title": f"r{i}", "url": f"https://e.com/{i}",
                     "snippet": "y", "source": "anysearch"}
                    for i in range(5)
                ],
                "engines_used": ["anysearch"],
                "elapsed_ms": 2, "cached": False,
            }
        return fake_super_search

    def test_local_first_sparse_upgrades_after_local(self):
        from research import collect_sources
        calls: list = []
        fake = self._sparse_local(calls)
        with patch("research.super_search", side_effect=fake):
            out = collect_sources(
                [{"query": "q1", "intent": "x", "strategy": "direct"}],
                max_results=5, timeout=5, depth="balanced", mode="auto",
                route_strategy="local_first",
            )
        # 先本地 → 不足升级全量；绝不允许「先全量再本地」的浪费顺序
        self.assertEqual(calls, ["local_search", "auto"])
        self.assertTrue(out["sub_results"][0]["upgraded_to_full"])
        self.assertEqual(len(out["merged_results"]), 5)

    def test_local_first_sufficient_no_upgrade(self):
        from research import collect_sources
        calls: list = []

        def fake_super_search(query: str, **kw: object) -> dict:
            calls.append(kw.get("engine", "auto"))
            return {
                "results": [
                    {"title": f"l{i}", "url": f"https://e.com/{i}",
                     "snippet": "y", "source": "local_bing"}
                    for i in range(5)
                ],
                "engines_used": ["local_bing"],
                "elapsed_ms": 1, "cached": False,
            }

        with patch("research.super_search", side_effect=fake_super_search):
            out = collect_sources(
                [{"query": "q1", "intent": "x", "strategy": "direct"}],
                max_results=5, timeout=5, depth="balanced", mode="auto",
                route_strategy="local_first",
            )
        # 本地结果充足 → 只跑本地一次，不升级
        self.assertEqual(calls, ["local_search"])
        self.assertFalse(out["sub_results"][0]["upgraded_to_full"])

    def test_non_local_first_runs_full_once(self):
        from research import collect_sources
        calls: list = []

        def fake_super_search(query: str, **kw: object) -> dict:
            calls.append(kw.get("engine", "auto"))
            return {
                "results": [
                    {"title": "a", "url": "https://e.com/a",
                     "snippet": "y", "source": "anysearch"}
                ],
                "engines_used": ["anysearch"],
                "elapsed_ms": 1, "cached": False,
            }

        with patch("research.super_search", side_effect=fake_super_search):
            collect_sources(
                [{"query": "q1", "intent": "x", "strategy": "direct"}],
                max_results=5, timeout=5, depth="balanced", mode="auto",
                route_strategy="cost_aware",
            )
        self.assertEqual(calls, ["auto"])


class TestMacroDataWorldbankPriority(unittest.TestCase):
    """macro_data 非美国查询：worldbank 前置不被 primary 扶正覆盖。"""

    def test_foreign_macro_query_prefers_worldbank(self):
        from route import route_query
        for q in ("中国GDP", "日本通胀", "欧元区失业率"):
            d = route_query(q)
            self.assertEqual(d["domain"], "macro_data")
            self.assertEqual(
                d["engines_combo"][0], "worldbank",
                f"{q} 应 worldbank 优先（FRED 无该国数据）",
            )

    def test_us_macro_query_keeps_fred_primary(self):
        from route import route_query
        d = route_query("美国CPI")
        self.assertEqual(d["domain"], "macro_data")
        self.assertEqual(d["engines_combo"][0], "fred")


if __name__ == "__main__":
    unittest.main()

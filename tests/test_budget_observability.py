#!/usr/bin/env python3
"""test_budget_observability — 预算记账与单调钟（2026-09-16）。

守的两件事：
  1. budget_used_ms/budget_total_ms 进 timing.budget（timing 默认开且 agent
     档保留，预算可见性随答案到达）；deep 等无预算模式键缺席；
  2. 预算钟换成 time.monotonic（t0_mono）后预算语义不变——deadline 照样
     止损。墙钟会被 NTP 跳变拉扯，预算窗随之失真，故整套换单调钟；
     t0_mono 缺省 None 时回落 wall（既有调用方/测试逐位兼容）。
"""
from __future__ import annotations

import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import search  # noqa: E402
from search import execute_search, _strip_for_agent  # noqa: E402
from cache import SearchCache  # noqa: E402
from stage_timing import StageTiming  # noqa: E402

QUERY = "budget observability probe query"


class _AllowAllBreaker:
    def allow(self, eng):
        return True, "closed"

    def get_negative(self, *a, **k):
        return None

    def status(self, eng):
        return {"state": "closed"}

    def record_success(self, *a, **k):
        pass

    def record_failure(self, *a, **k):
        pass

    def set_negative(self, *a, **k):
        pass

    def clear_negative(self, *a, **k):
        pass


def _good(tag):
    return [{"title": f"{QUERY} result {tag} {i}",
             "snippet": f"relevant snippet about {QUERY} variant {i}",
             "url": f"https://example.com/{tag}/{i}"}
            for i in range(3)]


class TestBudgetObservability(unittest.TestCase):
    def _execute(self, decision, fake, mode="fast", patch_budget=None):
        calls = []

        def _spy(query_, eng, **kwargs):
            calls.append(eng)
            return fake(query_, eng)

        cache = SearchCache(db_path=":memory:")
        t0 = time.perf_counter()
        with ExitStack() as stack:
            for p in (
                patch("search.engine_search", side_effect=_spy),
                patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
                patch("quota.get_quota_manager", return_value=MagicMock()),
                *([patch.object(search, "_FAST_TOTAL_BUDGET_S", patch_budget)]
                  if patch_budget is not None else []),
            ):
                stack.enter_context(p)
            out = execute_search(
                QUERY, decision, max_results=5, timeout=10, depth="fast",
                cache=cache, skip_cache=True, mode=mode,
                timing=StageTiming())
        return out, calls, time.perf_counter() - t0

    def _decision(self, combo, parallel=True):
        return {"engines_combo": combo, "engines": combo, "parallel": parallel,
                "domain": "general_search", "engine": combo[0]}

    def test_budget_fields_present_and_sane(self):
        """fast 模式：timing.budget 记账齐全，消耗不超过总额太多。"""
        out, _, wall = self._execute(
            self._decision(["quick_primary", "slow_second"]),
            lambda _q, eng: (_good(eng) if eng == "quick_primary"
                             else (time.sleep(2.0) or [])),
            patch_budget=6.0)
        budget = out["timing"].get("budget")
        self.assertIsNotNone(budget, "fast 模式必须报出 budget")
        self.assertEqual(budget["total_ms"], 6000)
        self.assertGreaterEqual(budget["used_ms"], 0)
        # 早停成功时消耗远小于预算（慢引擎被弃置不计入墙钟消耗）
        self.assertLess(budget["used_ms"], 6000, "早停后预算消耗应远小于总额")
        self.assertLess(wall, 2.0, "慢次引擎不应拖住进程")

    def test_deep_mode_has_no_budget_key(self):
        """deep 预算语义（2026-09-30 起 deep 也有 15s 总预算）：
        timing.budget 必须如实报 {used, total=15000}——「无预算」曾是
        deep 的契约，但它被一个 timeout=20 的慢源架空（实测单查询 12s+
        全在等 firecrawl），改成有界但只截病态拖尾；测试改为锁「键存在
        且 total 正确、不早停语义不变」。"""
        def fake(_q, eng):
            time.sleep(0.02)
            return _good(eng)

        out, _, _ = self._execute(
            self._decision(["a", "b"]), fake, mode="deep")
        budget = out["timing"].get("budget")
        self.assertIsNotNone(budget)
        self.assertEqual(budget["total_ms"], 15000)
        self.assertLess(budget["used_ms"], 15000)

    def test_deep_budget_escape_hatch(self):
        """execution.deep_budget_s=0 恢复无界（回滚无侵入）：budget 键缺席。

        _deep_budget_s 是按值传参链（search → hooks → dispatch），这里 patch
        search.get_execution_config——它是 execute_search 实际读的名字。
        """
        def fake(_q, eng):
            time.sleep(0.02)
            return _good(eng)

        real = search.get_execution_config
        with patch.object(search, "get_execution_config",
                          side_effect=lambda *a, **k:
                              {**real(), "deep_budget_s": 0}):
            out, _, _ = self._execute(
                self._decision(["a", "b"]), fake, mode="deep")
        self.assertIsNone(out["timing"].get("budget"),
                          "deep_budget_s=0 时 deep 必须恢复无界（budget 键缺席）")

    def test_agent_strip_keeps_budget(self):
        """--fields agent 剥遥测但留 timing：预算可见性必须活下来。"""
        payload = {"timing": {"budget": {"used_ms": 120, "total_ms": 6000}},
                   "results": [{"title": "t", "url": "https://e.com/1"}]}
        slim = _strip_for_agent(payload)
        self.assertEqual(slim["timing"]["budget"]["total_ms"], 6000)

    def test_monotonic_clock_enforces_budget(self):
        """单调钟下预算止损照常生效：deadline 后不再起新引擎。"""
        def fake(_q, eng):
            time.sleep(0.8)
            return _good(eng)

        out, calls, wall = self._execute(
            self._decision(["a", "b", "c"], parallel=False),
            fake, patch_budget=1.0)
        self.assertLess(len(calls), 3, f"预算 1s 内不应跑满 3 个串行引擎：{calls}")
        self.assertLess(wall, 2.5, "预算止损失效（被慢引擎拖住）")


if __name__ == "__main__":
    unittest.main()

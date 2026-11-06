#!/usr/bin/env python3
"""test_adaptive_tail.py — 尾宽按 backup 引擎 P75 分层（2026-10-16）。

## 这条门锁的是什么

固定 1.5s 尾宽对一切 backup 一视同仁，而 backup 的历史 P75 差异极大：
redskill 236ms 等 1.5s 几乎必到（耐心有回报），anysearch P75 2.6s 等 1.5s
只有约五成概率——等不到就是纯墙钟。消融实测（ARGO_STRAGGLER_GRACE_S
1.5 → 0.5 → 0）样本墙钟 2581 → 2260 → 1873ms（-12% / -27%）。

分层规则（straggler_policy.straggler_tail）：
  P75 ≤ ½·base  → base       快源：尾窗内几乎必到，等满不损质量
  ½·base < P75 ≤ 1.5·base → 0.7·base  中速略收
  P75 > 1.5·base → 0.4·base（下限 0.3s）慢源：大概率白等，早收工
fail-open 契约：开关关 / 无数据 / 学习器异常 → 原样返回 base（逐位旧行为）。

默认关（ARGO_ADAPTIVE_TAIL=1 开启）：理由是**本开关自己的** A/B 未证实收益，
调度改动的收益必须由交错 A/B 证明后才转默认开。别与 ARGO_ADAPTIVE_GRACE 的
现状混为一谈——那个 2026-10-19（c52b7d4）已转默认开，两者 A/B 结论不同。

## 集成侧锁的东西

engine_dispatch 的收尾等待真的按「仍在等待的引擎」的 P75 收窄：慢 backup
在分层尾宽到期时被弃置（墙钟 ≈ 首个交付 + 分层尾宽），而不是拖到它自己的
超时；且弃置不毒化结果（交付来自已完成的引擎）。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

# 直接 `python3 tests/test_adaptive_tail.py` 跑时不会加载 conftest.py，本用例
# 会真的写生产 usage_log（2026-10-16 实测留下 17 条 quick_primary 记录）。
# 模块级兜底：只在本文件被单独执行时生效，pytest 路径由 conftest 统一管。
os.environ.setdefault("ARGO_USAGE_LOG", "0")

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import search  # noqa: E402
from cache import SearchCache  # noqa: E402
from search import execute_search  # noqa: E402
from straggler_policy import straggler_tail  # noqa: E402

QUERY = "尾宽分层集成用例"


def _good(tag: str, n: int = 3) -> list:
    return [
        {"title": f"{QUERY} 数据 {tag} {i}",
         "snippet": f"关于{QUERY}的说明 {i}",
         "url": f"https://example.com/{tag}/{i}"}
        for i in range(n)
    ]


class _FakeLearner:
    def __init__(self, p75_ms):
        self._p75 = p75_ms

    def latency_percentile(self, engine, p=0.75, min_samples=5):
        return self._p75


class TestStragglerTailTiers(unittest.TestCase):
    """分层取值表的单测：边界值与 fail-open。"""

    BASE = 1.5

    def _tail(self, p75_ms, base=None):
        with patch("adaptive.get_learner", return_value=_FakeLearner(p75_ms)):
            return straggler_tail("anysearch", base or self.BASE)

    def test_flag_off_returns_base_unchanged(self):
        """默认关：即便学习器里有慢源数据，也逐位返回 base。"""
        import os
        old = os.environ.pop("ARGO_ADAPTIVE_TAIL", None)
        try:
            with patch("adaptive.get_learner",
                       return_value=_FakeLearner(99000)):
                self.assertEqual(straggler_tail("anysearch", self.BASE),
                                 self.BASE)
        finally:
            if old is not None:
                os.environ["ARGO_ADAPTIVE_TAIL"] = old

    def test_fast_engine_gets_full_base(self):
        """P75 ≤ ½·base（0.75s）：尾窗内几乎必到，等满不损质量。"""
        import os
        os.environ["ARGO_ADAPTIVE_TAIL"] = "1"
        try:
            self.assertEqual(self._tail(200), self.BASE)
            self.assertEqual(self._tail(750), self.BASE)  # 边界：≤ 取等
        finally:
            del os.environ["ARGO_ADAPTIVE_TAIL"]

    def test_mid_engine_gets_softened_tail(self):
        """½·base < P75 ≤ 1.5·base：收到 0.7×base。"""
        import os
        os.environ["ARGO_ADAPTIVE_TAIL"] = "1"
        try:
            self.assertAlmostEqual(self._tail(1000), self.BASE * 0.7)
            self.assertAlmostEqual(self._tail(2250), self.BASE * 0.7)  # 边界
        finally:
            del os.environ["ARGO_ADAPTIVE_TAIL"]

    def test_slow_engine_gets_short_tail_with_floor(self):
        """P75 > 1.5·base：收到 0.4×base，且不低于 0.3s 地板。"""
        import os
        os.environ["ARGO_ADAPTIVE_TAIL"] = "1"
        try:
            self.assertAlmostEqual(self._tail(2600), 0.6)
            self.assertAlmostEqual(self._tail(99000), 0.6)
            # 地板：base 很小（0.5）时 0.4×=0.2 → 抬回 0.3
            self.assertEqual(self._tail(99000, base=0.5), 0.3)
        finally:
            del os.environ["ARGO_ADAPTIVE_TAIL"]

    def test_no_data_fails_open_to_base(self):
        """样本不足/过时（None）：不收窄。"""
        import os
        os.environ["ARGO_ADAPTIVE_TAIL"] = "1"
        try:
            self.assertEqual(self._tail(None), self.BASE)
            self.assertEqual(self._tail(0), self.BASE)
        finally:
            del os.environ["ARGO_ADAPTIVE_TAIL"]

    def test_learner_error_fails_open_to_base(self):
        """学习器抛错：fail-open，不阻断调度主路径。"""
        import os
        os.environ["ARGO_ADAPTIVE_TAIL"] = "1"
        try:
            with patch("adaptive.get_learner", side_effect=RuntimeError("db locked")):
                self.assertEqual(straggler_tail("anysearch", self.BASE),
                                 self.BASE)
        finally:
            del os.environ["ARGO_ADAPTIVE_TAIL"]

    def test_nonpositive_base_short_circuits(self):
        """base<=0 或空引擎名：原样返回（尾宽整体关闭的消融态）。"""
        self.assertEqual(straggler_tail("anysearch", 0), 0)
        self.assertEqual(straggler_tail("", self.BASE), self.BASE)

    def test_pb_hos_low_reliability_straggler_tightened(self):
        """PB-HOS 期望价值调制：中慢速且低健康分（<0.35）的尾源进一步收紧尾宽。"""
        class _ScoredLearner(_FakeLearner):
            def __init__(self, p75_ms, score):
                super().__init__(p75_ms)
                self._score = score

            def get_score(self, _engine):
                return self._score

        os.environ["ARGO_ADAPTIVE_TAIL"] = "1"
        try:
            with patch("adaptive.get_learner", return_value=_ScoredLearner(1000, 0.20)):
                t_low = straggler_tail("shaky_backup", self.BASE)
            with patch("adaptive.get_learner", return_value=_ScoredLearner(1000, 0.70)):
                t_ok = straggler_tail("healthy_backup", self.BASE)
            self.assertLess(t_low, t_ok)
        finally:
            del os.environ["ARGO_ADAPTIVE_TAIL"]


class _AllowAllBreaker:
    def allow(self, *_a, **_k):
        return True, ""

    def get_negative(self, *_a, **_k):
        return None

    def status(self, *_a, **_k):
        return {"state": "closed"}

    def record_success(self, *_a, **_k):
        pass

    def record_failure(self, *_a, **_k):
        pass

    def record_note(self, *_a, **_k):
        pass

    def set_negative(self, *_a, **_k):
        pass

    def clear_negative(self, *_a, **_k):
        pass


class TestDispatchHonorsTieredTail(unittest.TestCase):
    """集成：收尾等待按分层尾宽到期，慢 backup 被弃置而不是拖墙钟。"""

    def _run(self, tail_fn, primary_sleep=0.05, backup_sleep=2.5):
        calls: list[str] = []

        def fake(_q, eng, **_kw):
            calls.append(eng)
            if eng == "slow_backup":
                time.sleep(backup_sleep)
                return _good("bk")
            # 首引擎交付但**不够用**（1 条 < min_results 3）：早停不放行，
            # 收尾等待由尾宽决定——正是要测的那段。
            time.sleep(primary_sleep)
            return _good("pm", n=1)

        cache = SearchCache(db_path=":memory:")
        decision = {"engines_combo": ["quick_primary", "slow_backup"],
                    "engines": ["quick_primary", "slow_backup"],
                    "parallel": True, "domain": "general",
                    "engine": "quick_primary", "early_stop_min_results": 3,
                    "no_early_stop": False}
        t0 = time.perf_counter()
        with ExitStack() as stack:
            for p in (
                patch("search.engine_search", side_effect=fake),
                patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
                patch("quota.get_quota_manager", return_value=MagicMock()),
                # 对冲窗压到 0.05：backup 几乎与 primary 同时起跑，
                # 保证尾宽生效时它确实在 pending 里。
                patch.object(search, "_PRIMARY_GRACE_S", 0.05),
                patch.object(search, "straggler_tail", tail_fn),
            ):
                stack.enter_context(p)
            out = execute_search(
                QUERY, decision, max_results=5, timeout=10, depth="fast",
                cache=cache, skip_cache=True, mode="fast")
        return out, calls, time.perf_counter() - t0

    def test_slow_backup_cut_at_tiered_tail(self):
        """慢 backup（P75 2.6s → 分层 0.6s）：墙钟 ≈ primary + 0.6s，不等它 2.5s。"""
        def tail_fn(eng, base):
            return 0.6 if eng == "slow_backup" else base

        out, calls, wall = self._run(tail_fn)
        self.assertIn("quick_primary", calls)
        self.assertLess(wall, 1.2,
                        f"慢 backup 未被分层尾宽截断，墙钟 {wall:.2f}s")
        self.assertTrue(out["results"], "首引擎的结果应照常交付")

    def test_flat_tail_ablation_waits_longer(self):
        """消融对照：分层函数原样返回 base（1.5s）时墙钟明显更长。

        没有这条对照，上一条可能只是碰巧快（例如 backup 恰好也早完成）。
        """
        out, calls, wall = self._run(lambda eng, base: base)
        self.assertGreaterEqual(wall, 1.4,
                                f"固定尾宽臂应等满 1.5s，实测 {wall:.2f}s")


if __name__ == "__main__":
    unittest.main()

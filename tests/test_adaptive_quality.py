#!/usr/bin/env python3
"""test_adaptive_quality — 自适应学习的质量维度（2026-09-27 接通）。

守的是「只学快慢、不学好坏」这个盲区。`engine_perf.quality` 这列自建库起
就存在，升级前**全仓无人写入、无人读取**：实测 1978 行数据 quality 全部
为 0.0，于是评分公式里
`success × latency × cost` 里「成功返回一堆无关结果」与「成功返回精准
结果」完全等价——而这正是多数免密钥源的常态。

三类契约：
  1. 写：成功的结果带质量回写，空/失败不写（None 而非 0.0）；
  2. 读：质量因子进评分，且**无质量数据时严格中性**（存量库不降权）；
  3. 界：quality_factor 的 ±25% 封顶不被单因子放大。

第 2 条的红线来自一个具体事故：升级前库里 1978 行 quality 全 0.0，若用
`avg_quality > 0` 判定「有无数据」，这批行会被读成「质量极差」而把全仓
引擎一次性降权。现口径一律看 `SUM(quality)`。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import adaptive  # noqa: E402
import query_signals  # noqa: E402

# 常量与 quality_factor 在升级前不存在。这里刻意**不直接 import**：
# 直接 import 会让未修复代码在 collection 阶段 ImportError——那是「模块没有
# 这个名字」，不是「行为不对」，测试钉不住行为。改为软取并给出占位，使未修复
# 代码走到断言处失败（红在行为上，而非红在导入上）。
QUALITY_FACTOR_MIN = getattr(adaptive, "QUALITY_FACTOR_MIN", 0.0)
QUALITY_FACTOR_MAX = getattr(adaptive, "QUALITY_FACTOR_MAX", 0.0)
quality_factor = getattr(adaptive, "quality_factor", None)

Q = "大语言模型Agent框架对比"
RELEVANT = [
    {"title": "AI Agent 框架深度对比", "snippet": "对比 LangGraph CrewAI 主流框架"},
    {"title": "Agent 框架评测", "snippet": "大语言模型 框架选型指南"},
]
IRRELEVANT = [
    {"title": "CheckThat 2025 Competition", "snippet": "CLEF 2025 lab task participation"},
    {"title": "eRisk Depression Detection", "snippet": "depression classification strategies"},
]


def _quality_factor(avg, total):
    """薄封装：升级前 quality_factor 不存在时直接判失败（而非跳过）。"""
    if quality_factor is None:
        raise AssertionError(
            "adaptive.quality_factor 不存在：质量维度未接通，"
            "评分仍是 success×latency×cost（只学快慢不学好坏）")
    return quality_factor(avg, total)


def _relevance(q, docs, top_k=3):
    """同上：result_relevance 升级前不存在，判失败而非跳过。"""
    fn = getattr(query_signals, "result_relevance", None)
    if fn is None:
        raise AssertionError(
            "query_signals.result_relevance 不存在：没有质量信号源，"
            "搜索层无处回写质量")
    return fn(q, docs, top_k)


class _TmpLearner:
    """把 AdaptiveLearner 钉到临时 DB，避免污染真实 ~/.cache。"""

    def __enter__(self):
        self._orig = adaptive.DB_PATH
        adaptive.DB_PATH = Path(tempfile.mkdtemp()) / "adaptive.db"
        self.learner = adaptive.AdaptiveLearner()
        return self.learner

    def __exit__(self, *exc):
        adaptive.DB_PATH = self._orig
        return False


class TestQualityFactor(unittest.TestCase):
    def test_no_data_is_neutral(self):
        """无质量数据 → 1.0。中性是硬要求：存量库不得被降权。"""
        self.assertEqual(_quality_factor(None, 0), 1.0)
        self.assertEqual(_quality_factor(0.0, 0), 1.0)
        self.assertEqual(_quality_factor(0.0, None), 1.0)

    def test_all_zero_avg_with_data_still_demoted(self):
        """sum>0 但 avg 极低（测过且很差）→ 应降权，不能被误当中性。"""
        # avg=0.1, sum>0：有数据且质量差 → 0.80
        self.assertAlmostEqual(_quality_factor(0.1, 1.0), 0.80, places=3)

    def test_bounds_are_clamped(self):
        self.assertEqual(_quality_factor(0.0, 1.0), QUALITY_FACTOR_MIN)
        self.assertEqual(_quality_factor(1.0, 1.0), QUALITY_FACTOR_MAX)
        # 越界输入不穿透
        self.assertEqual(_quality_factor(5.0, 1.0), QUALITY_FACTOR_MAX)
        self.assertEqual(_quality_factor(-3.0, 1.0), QUALITY_FACTOR_MIN)

    def test_monotonic_in_quality(self):
        """质量越好因子越大（不能反向）。"""
        vals = [_quality_factor(a, 1.0) for a in (0.0, 0.25, 0.5, 0.75, 1.0)]
        self.assertEqual(vals, sorted(vals))


class TestResultRelevance(unittest.TestCase):
    def test_relevant_scores_high_irrelevant_low(self):
        hi = _relevance(Q, RELEVANT)
        lo = _relevance(Q, IRRELEVANT)
        self.assertIsNotNone(hi)
        self.assertIsNotNone(lo)
        self.assertGreater(hi, lo)

    def test_no_signal_returns_none(self):
        """无法判定必须返回 None（= 没测），不是 0.0（= 很差）。"""
        self.assertIsNone(_relevance("", RELEVANT))
        self.assertIsNone(_relevance("   ", RELEVANT))
        self.assertIsNone(_relevance("!!! ???", RELEVANT))   # 纯符号
        self.assertIsNone(_relevance(Q, []))
        self.assertIsNone(_relevance(Q, [{"error": "timeout"}]))

    def test_error_rows_ignored(self):
        mixed = [{"error": "timeout"}] + RELEVANT
        self.assertEqual(_relevance(Q, mixed), _relevance(Q, RELEVANT))

    def test_in_unit_range(self):
        for docs in (RELEVANT, IRRELEVANT, mixed_docs := RELEVANT + IRRELEVANT):
            v = _relevance(Q, docs)
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)
        del mixed_docs


class TestQualityRecordingAndScoring(unittest.TestCase):
    def test_record_persists_quality(self):
        with _TmpLearner() as L:
            L.record("e1", success=True, latency_ms=100, quality=0.8)
            L.record("e1", success=True, latency_ms=100, quality=0.4)
            stats = L.get_stats()
            self.assertTrue(stats["e1"]["quality_tracked"])
            self.assertAlmostEqual(stats["e1"]["avg_quality"], 0.6, places=2)

    def test_none_quality_is_not_measured(self):
        """quality=None → 不参与判定（quality_tracked=False）。"""
        with _TmpLearner() as L:
            L.record("e2", success=True, latency_ms=100, quality=None)
            stats = L.get_stats()
            self.assertFalse(stats["e2"]["quality_tracked"])
            self.assertEqual(stats["e2"]["quality_factor"], 1.0)

    def test_empty_rows_do_not_create_quality_signal(self):
        """空结果不该被当成「质量为零」拖低引擎。"""
        with _TmpLearner() as L:
            L.record("e3", success=False, latency_ms=100, empty=True, quality=None)
            stats = L.get_stats()
            self.assertFalse(stats["e3"]["quality_tracked"])

    def test_quality_demotes_bad_engine_in_score(self):
        """同样成功同样快，坏结果的引擎分数必须更低。"""
        with _TmpLearner() as L:
            for _ in range(3):
                L.record("good", success=True, latency_ms=100, quality=0.9)
                L.record("bad", success=True, latency_ms=100, quality=0.0)
            L._score_cache.clear()
            self.assertGreater(L.get_score("good"), L.get_score("bad"))

    def test_legacy_db_without_quality_column_migrates(self):
        """老库没有 quality 列 → 自动补列，历史行不降权。"""
        import sqlite3
        with _TmpLearner() as L:
            pass
        tmp = Path(tempfile.mkdtemp()) / "adaptive.db"
        conn = sqlite3.connect(str(tmp))
        conn.execute(
            "CREATE TABLE engine_perf (engine TEXT NOT NULL, success INTEGER NOT NULL,"
            " latency_ms REAL NOT NULL, cost REAL NOT NULL DEFAULT 0.0,"
            " created_at REAL NOT NULL, empty INTEGER NOT NULL DEFAULT 0)"
        )
        conn.commit()
        conn.close()
        orig = adaptive.DB_PATH
        adaptive.DB_PATH = tmp
        try:
            L2 = adaptive.AdaptiveLearner()
            L2.record("old", success=True, latency_ms=100)   # 不传 quality
            stats = L2.get_stats()
            self.assertFalse(stats["old"]["quality_tracked"])
            self.assertEqual(stats["old"]["quality_factor"], 1.0)
        finally:
            adaptive.DB_PATH = orig

    def test_get_ranking_matches_get_score(self):
        """两个视图必须同一套公式，否则排名与逐引擎查询自相矛盾。"""
        with _TmpLearner() as L:
            L.record("a", success=True, latency_ms=100, quality=0.9)
            L.record("b", success=True, latency_ms=100, quality=0.1)
            L._score_cache.clear()
            ranking = dict(L.get_ranking())
            self.assertAlmostEqual(ranking["a"], L.get_score("a"), places=4)
            self.assertAlmostEqual(ranking["b"], L.get_score("b"), places=4)


if __name__ == "__main__":
    unittest.main()

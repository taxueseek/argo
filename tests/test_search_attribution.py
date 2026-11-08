#!/usr/bin/env python3
"""tests/test_search_attribution.py — 引擎级耗时归因（写入侧提取 + 读侧聚合）

背景（2026-10-15）：usage_log 的 query 流此前只记 engines_used，不记各引擎
耗时，导致「哪个源吃掉这次查询」只能靠人离线重算 1059 条日志才发现
local_search 独跑 109 次 / 占 369s。本模块是那笔账的写入与读出口，两条
路径共用同一份口径（search_attribution），故两侧一起测。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import search_attribution as sa  # noqa: E402


class TestTopEngineLatency:
    """写入侧：{引擎: 耗时} → 最慢 Top-N 二元组。"""

    def test_empty_and_none_are_empty(self):
        assert sa.top_engine_latency(None) == []
        assert sa.top_engine_latency({}) == []

    def test_descending_by_latency(self):
        got = sa.top_engine_latency(
            {"fast_src": 120, "slow_src": 3500, "mid_src": 900}, top_n=3)
        assert got == [["slow_src", 3500], ["mid_src", 900], ["fast_src", 120]]

    def test_default_top_n_is_three(self):
        got = sa.top_engine_latency(
            {f"e{i}": (i + 1) * 100 for i in range(6)})
        assert len(got) == 3
        assert [e for e, _ in got] == ["e5", "e4", "e3"]

    def test_skips_non_numeric(self):
        """畸形耗时不参与，但不得把整条记录写崩。"""
        got = sa.top_engine_latency(
            {"ok": 500, "none": None, "str": "300", "nan_flag": True})
        assert got == [["ok", 500]]

    def test_rounds_floats(self):
        assert sa.top_engine_latency({"e": 1234.6}) == [["e", 1235]]

    def test_ties_are_deterministic(self):
        """同耗时按引擎名定序：重复运行输出一致，便于 diff 与断言。"""
        a = sa.top_engine_latency({"b": 100, "a": 100})
        b = sa.top_engine_latency({"a": 100, "b": 100})
        assert a == b == [["a", 100], ["b", 100]]


class TestAggregateSlowEngines:
    """读侧：多行日志聚合 → 长期慢源排行。"""

    def test_accumulates_and_ranks_by_total(self):
        rows = [
            {"engine_latency_top": [["local_search", 3000], ["anysearch", 1500]]},
            {"engine_latency_top": [["local_search", 2500]]},
        ]
        got = sa.aggregate_slow_engines(rows)
        assert got[0] == {"engine": "local_search", "total_ms": 5500,
                          "max_ms": 3000, "queries": 2}
        assert got[1]["engine"] == "anysearch"
        assert got[1]["total_ms"] == 1500

    def test_empty_rows(self):
        assert sa.aggregate_slow_engines([]) == []
        assert sa.aggregate_slow_engines([{"count": 3}]) == []

    def test_malformed_entries_skipped_not_fatal(self):
        """历史日志可能有旧格式/坏行，读侧必须 fail-soft。"""
        rows = [
            {"engine_latency_top": [["good", 900], ["bador", "x"], ["short"],
                                    ["three", 1, 2]]},
            {"engine_latency_top": None},
            {"engine_latency_top": 42},
        ]
        assert sa.aggregate_slow_engines(rows) == [
            {"engine": "good", "total_ms": 900, "max_ms": 900, "queries": 1}]

    def test_top_n_caps_result(self):
        rows = [{"engine_latency_top": [[f"e{i}", (i + 1) * 10]
                                        for i in range(8)]}]
        assert len(sa.aggregate_slow_engines(rows, top_n=5)) == 5
        assert sa.aggregate_slow_engines(rows, top_n=0) == []


class TestUsageLogFieldContract:
    """写入侧字段名是读侧与用户可见出口的契约，改名必须两边一起动。"""

    def test_pipeline_emits_attribution_field(self):
        src = (SKILL_DIR / "scripts" / "search_pipeline.py").read_text(
            encoding="utf-8")
        assert "usage_log_attribution" in src, \
            "finalize 未接入归因字段，日志将退回「只有谁参与」"

    def test_stats_reads_attribution_field(self):
        src = (SKILL_DIR / "scripts" / "stats_cli.py").read_text(
            encoding="utf-8")
        assert "aggregate_slow_engines" in src, \
            "argo stats 未展示慢源归因，写了没人看"


class TestEngineResultCounts:
    """各引擎交付条数：区分「慢源在拖尾」与「慢源是唯一出货的那个」。"""

    def test_counts_per_engine_including_zero(self):
        got = sa.engine_result_counts([
            {"engine": "wechat_sogou", "status": "no-results", "results_count": 0},
            {"engine": "sogou_weixin_browser", "status": "ok", "results_count": 10},
            {"engine": "anysearch", "status": "cancelled", "results_count": 0},
        ])
        assert got == {"wechat_sogou": 0, "sogou_weixin_browser": 10,
                       "anysearch": 0}

    def test_empty_and_none(self):
        assert sa.engine_result_counts(None) == {}
        assert sa.engine_result_counts([]) == {}

    def test_malformed_entries_skipped_or_zeroed(self):
        got = sa.engine_result_counts([
            "junk", None, {"status": "ok", "results_count": 3},
            {"engine": "a", "results_count": "5"},
            {"engine": "b", "results_count": True},
            {"engine": "c", "results_count": -2},
            {"engine": "d"},
        ])
        assert got == {"a": 0, "b": 0, "c": 0, "d": 0}

    def test_repeated_engine_keeps_max(self):
        got = sa.engine_result_counts([
            {"engine": "octen", "results_count": 0},
            {"engine": "octen", "results_count": 4},
            {"engine": "octen", "results_count": 2},
        ])
        assert got == {"octen": 4}

    def test_limit_caps_distinct_engines(self):
        rows = [{"engine": f"e{i}", "results_count": i} for i in range(12)]
        got = sa.engine_result_counts(rows)
        assert list(got) == [f"e{i}" for i in range(8)]

    def test_attribution_includes_field_only_when_outcomes_exist(self):
        with_outcomes = sa.usage_log_attribution(
            {"x": 900}, engine_outcomes=[{"engine": "x", "results_count": 5}])
        assert with_outcomes["engine_results"] == {"x": 5}
        assert "engine_results" not in sa.usage_log_attribution({"x": 900})

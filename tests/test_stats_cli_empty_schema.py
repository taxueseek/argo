#!/usr/bin/env python3
"""`argo stats` 空日志也必须有稳定字段（issue #19 / nightly KeyError）。

CI 全新 runner 没有 usage_log，stats 曾只回 {"samples": 0}；夜间账本直接读
d["avg_elapsed_ms"]，于是每次首次运行必炸。字段形状要稳定，指标用 None
表示「没有样本」，不能用 0 伪造一条 0ms 曲线。
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import stats_cli  # noqa: E402


def test_empty_query_summary_has_stable_schema(monkeypatch):
    monkeypatch.setattr(stats_cli.usage_log, "tail", lambda name, n: [])
    summary = stats_cli._query_summary(5)
    assert summary["samples"] == 0
    assert summary["avg_elapsed_ms"] is None
    assert summary["result_rate"] is None
    assert summary["recovered"] == 0
    assert summary["top_engines"] == []
    assert summary["slowest_engines"] == []
    assert summary["recent"] == []


def test_build_report_keeps_query_shape(monkeypatch):
    monkeypatch.setattr(stats_cli.usage_log, "tail", lambda name, n: [])
    report = stats_cli.build_report(5)
    assert set(report) >= {"data_dir", "privacy", "query", "hints"}
    assert "avg_elapsed_ms" in report["query"]

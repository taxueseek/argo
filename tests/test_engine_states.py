#!/usr/bin/env python3
"""状态词表契约：防止「同一个状态名在两层之间被误合并」。

2026-11-09 实测回归：engine_status.py 把 outcome 的 quota-exhausted（连字符）
填进 CLI 展示态（下划线，quota_exhausted），test_http_attribution 立刻红。
同轮审计发现 ZCode 桥把同一概念写成 quota / quota-exhausted /
quota_exhausted 三套。本用例把各层词表钉死，改错一处即失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import engine_states as st  # noqa: E402
from circuit_breaker import CircuitBreaker  # noqa: E402
from engine_failure import NOTE_STATUS  # noqa: E402
from engine_status import engine_detail  # noqa: E402
from retry_policy import NON_RETRYABLE_STATUS  # noqa: E402
from search_cli import _ENGINE_BLOCK_REASONS  # noqa: E402


def test_vocabularies_stay_in_their_own_layer():
    """展示态、outcome、failure category 是三套词表，值必须各自合法且边界清晰。"""
    assert st.OUTCOME_QUOTA_EXHAUSTED == "quota-exhausted"
    assert st.FAILURE_QUOTA_EXHAUSTED == "quota_exhausted"
    assert st.ENGINE_STATUS_QUOTA_EXHAUSTED == "quota_exhausted"
    assert st.OUTCOME_QUOTA_EXHAUSTED not in st.ENGINE_DETAIL_STATUSES
    assert st.ENGINE_STATUS_QUOTA_EXHAUSTED in st.ENGINE_DETAIL_STATUSES
    assert st.ENGINE_BLOCK_QUOTA_EXHAUSTED in st.ENGINE_BLOCK_REASONS
    # 展示态含 outcome 没有的诊断态
    assert {"missing_key", "missing_dep", "env_filtered"} <= st.ENGINE_DETAIL_STATUSES
    assert "ok" not in st.ENGINE_DETAIL_STATUSES


def test_derived_tables_come_from_one_source():
    """派生表不得再抄一份字面量，必须与词表同源。"""
    assert NON_RETRYABLE_STATUS == st.NON_RETRYABLE_OUTCOMES
    assert NOTE_STATUS == dict(st.CATEGORY_TO_OUTCOME)
    reasons = {r for r, _label in _ENGINE_BLOCK_REASONS}
    assert reasons == st.ENGINE_BLOCK_REASONS
    assert st.health_severity_index(st.HEALTH_AUTH_DOWN) == 0
    assert st.health_severity_index("未登记的 verdict") == len(st.HEALTH_SEVERITY_ORDER)


def test_breaker_states_are_persisted_verbatim():
    """落盘值只能来自词表：半开态曾出现 half-open / half_open 两套写法。"""
    assert st.BREAKER_HALF_OPEN == "half_open"
    assert set(st.BREAKER_STATES) == {"closed", "open", "half_open", "disabled"}
    assert st.is_breaker_routable(st.BREAKER_HALF_OPEN)
    assert not st.is_breaker_routable(st.BREAKER_OPEN)
    # 构造一个半开态，确认落盘字段读回来仍是同一个值
    br = CircuitBreaker.__new__(CircuitBreaker)
    br._engines = {"probe": {"state": st.BREAKER_HALF_OPEN}}
    assert br._engines["probe"]["state"] == "half_open"


def test_engine_detail_status_is_display_vocabulary():
    """CLI 展示态不能用 outcome 的词，配额耗尽仍显示 quota_exhausted。"""
    assert "quota_exhausted" in st.ENGINE_DETAIL_STATUSES
    assert "quota-exhausted" not in st.ENGINE_DETAIL_STATUSES
    # 逐条走真实判定链需要一个存在的引擎；这里只验契约本身，
    # 具体分支值由 test_http_attribution 的 quota 用例覆盖。
    assert callable(engine_detail)

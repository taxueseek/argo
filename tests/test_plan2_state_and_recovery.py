#!/usr/bin/env python3
"""回归门禁：方案二状态机并发、恢复门控与 spec 延迟/推荐接线。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import engine_states
from circuit_breaker import CircuitBreaker, DISABLE_COOLDOWN_SECONDS, OPEN_SECONDS
from quota import QuotaManager
from recovery import build_recovery_plan
from recovery_runner import run_pipeline_recovery
from single_flight import SingleFlightCoalescer


def test_half_open_single_probe_and_state_name(tmp_path):
    cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
    cb._engines["eng_a"] = {
        "state": engine_states.BREAKER_HALF_OPEN,
        "failures": 2,
        "opens": 1,
        "opened_at": time.time() - OPEN_SECONDS - 1,
    }
    ok1, reason1 = cb.allow("eng_a")
    assert ok1 is True
    assert reason1 == engine_states.BREAKER_HALF_OPEN

    ok2, reason2 = cb.allow("eng_a")
    assert ok2 is False
    assert reason2 == "half_open_probe_busy"


def test_reenable_probe_preserves_disabled_at(tmp_path):
    cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
    old_disabled = time.time() - DISABLE_COOLDOWN_SECONDS - 10
    cb._engines["eng_b"] = {
        "state": engine_states.BREAKER_DISABLED,
        "failures": 3,
        "opens": 3,
        "disabled_at": old_disabled,
    }
    ok, reason = cb.allow("eng_b")
    assert ok is True
    assert reason == "half_open_reenable"
    assert cb._engines["eng_b"]["disabled_at"] == old_disabled


def test_quota_get_stats_no_deadlock_with_rlock():
    qm = QuotaManager()
    stats = qm.get_stats()
    assert isinstance(stats, dict)
    assert len(stats) > 0


def test_recovery_l2_gate_keeps_single_l3_when_no_l1_l2_steps():
    plan = build_recovery_plan(
        "rust async",
        ["anysearch"],
        engines_fallback=["duckduckgo", "wikipedia"],
        enabled={"anysearch", "duckduckgo", "wikipedia"},
        mode="auto",
        max_level=engine_states.RECOVERY_LEVEL_L2,
    )
    assert len(plan) == 1
    assert plan[0].level == engine_states.RECOVERY_LEVEL_L3
    assert len(plan[0].engines) == 1


def test_recovery_l0_still_fail_closed():
    plan = build_recovery_plan(
        "rust async",
        ["anysearch"],
        engines_fallback=["duckduckgo"],
        enabled={"anysearch", "duckduckgo"},
        mode="auto",
        max_level=engine_states.RECOVERY_LEVEL_L0,
    )
    assert plan == []


def test_recovery_runner_skips_usage_emit_when_plan_empty():
    emitted = []
    merged, info = run_pipeline_recovery(
        query="rust async",
        merged=[],
        raw_results={},
        decision={"domain": "general", "engines_fallback": []},
        engines=["anysearch"],
        available_engines_fn=lambda routable_only=True: ["anysearch"],
        max_results=5,
        max_rec_level=engine_states.RECOVERY_LEVEL_L0,
        echo_triggered=False,
        primary_grace_s=0.0,
        timeout=1.0,
        depth="fast",
        mode="fast",
        since_iso=None,
        until_iso=None,
        skip_cache=True,
        quota_batch=type("QB", (), {"add": lambda *a: None})(),
        engine_search_fn=lambda *a, **k: [],
        emit_usage_fn=lambda s, p: emitted.append((s, p)),
    )
    assert merged == []
    assert info is not None
    assert emitted == []


def test_single_flight_does_not_return_base_exception():
    sf = SingleFlightCoalescer()
    with sf._cond:
        sf._register_leader("k2")
        ev, slots, _ = sf._inflight["k2"]
        slots.append(KeyboardInterrupt("abort"))
        ev.set()
    # follower 遇到 BaseException 应走独立执行而不是返回异常对象
    res = sf.run("k2", lambda: "ok", wait_timeout=0.1)
    assert res == "ok"

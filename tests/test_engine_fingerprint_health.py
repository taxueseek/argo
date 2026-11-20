#!/usr/bin/env python3
"""test_engine_fingerprint_health.py — 环境指纹自愈与健康回归差分单测。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import circuit_breaker  # noqa: E402
import engine_fingerprint  # noqa: E402
import engine_health  # noqa: E402
import engine_states  # noqa: E402


def test_fingerprint_changes_on_proxy_and_spec(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    spec_a = {"type": "http", "url": "https://example.com/a"}
    spec_b = {"type": "http", "url": "https://example.com/b"}

    fp1 = engine_fingerprint.compute_engine_fingerprint("demo_src", spec=spec_a)
    fp2 = engine_fingerprint.compute_engine_fingerprint("demo_src", spec=spec_a)
    fp3 = engine_fingerprint.compute_engine_fingerprint("demo_src", spec=spec_b)
    assert len(fp1) == 16
    assert fp1 == fp2
    assert fp1 != fp3

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    fp4 = engine_fingerprint.compute_engine_fingerprint("demo_src", spec=spec_a)
    assert fp4 != fp1


def test_circuit_breaker_heals_on_env_fingerprint_change(tmp_path, monkeypatch):
    state_file = tmp_path / "cb.json"
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    br = circuit_breaker.CircuitBreaker(state_path=str(state_file))

    # 连续触发错误进入 OPEN 状态
    br.record_failure("demo_fp_eng", kind=engine_states.BREAKER_KIND_ERROR)
    br.record_failure("demo_fp_eng", kind=engine_states.BREAKER_KIND_ERROR)
    allowed, reason = br.allow("demo_fp_eng")
    assert not allowed
    assert reason.startswith("circuit_open:")

    # 环境指纹改变（例如配置了代理）→ 立即复位为 half_open 并放行探测
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    allowed2, reason2 = br.allow("demo_fp_eng")
    assert allowed2
    assert reason2 == "env_fingerprint_changed"
    st = br.status("demo_fp_eng")
    assert st["state"] == engine_states.BREAKER_HALF_OPEN
    assert st["failures"] == 0


def test_circuit_breaker_heals_rate_limit_cooldown_on_env_change(tmp_path, monkeypatch):
    state_file = tmp_path / "cb_rl.json"
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    br = circuit_breaker.CircuitBreaker(state_path=str(state_file))

    br.record_failure("demo_rl_eng", kind=engine_states.BREAKER_KIND_RATE_LIMITED)
    allowed, reason = br.allow("demo_rl_eng")
    assert not allowed
    assert reason.startswith("rate_limit_cooldown:")

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    allowed2, reason2 = br.allow("demo_rl_eng")
    assert allowed2
    assert reason2 == "env_fingerprint_changed"


def test_engine_health_regressions_diff(tmp_path, monkeypatch):
    state_file = tmp_path / "cb_health.json"
    baseline_file = tmp_path / "baseline.json"
    br = circuit_breaker.CircuitBreaker(state_path=str(state_file))
    monkeypatch.setattr(circuit_breaker, "get_breaker", lambda: br)

    # 首次基线：eng_a 已 flapping
    br.record_failure("eng_a", kind=engine_states.BREAKER_KIND_ERROR)
    br.record_failure("eng_a", kind=engine_states.BREAKER_KIND_ERROR)
    rep1 = engine_health.diff_regressions(baseline_path=baseline_file, save_baseline=True)
    assert rep1["regression_count"] == 1
    assert rep1["regressions"][0]["engine"] == "eng_a"
    assert rep1["baseline_saved"] is True

    # 无变化再次对比：不应重复报回归
    rep2 = engine_health.diff_regressions(baseline_path=baseline_file, save_baseline=False)
    assert rep2["regression_count"] == 0
    assert rep2["recovered_count"] == 0

    # eng_a 恢复，eng_b 新挂 → 精准报 eng_b 回归 + eng_a 恢复
    br.record_success("eng_a")
    br.record_failure("eng_b", kind=engine_states.BREAKER_KIND_ERROR)
    br.record_failure("eng_b", kind=engine_states.BREAKER_KIND_ERROR)
    rep3 = engine_health.diff_regressions(baseline_path=baseline_file, save_baseline=True)
    assert [r["engine"] for r in rep3["regressions"]] == ["eng_b"]
    assert [r["engine"] for r in rep3["recovered"]] == ["eng_a"]
    assert rep3["regressions"][0]["current_verdict"] == engine_states.HEALTH_FLAPPING
    assert rep3["recovered"][0]["current_verdict"] == engine_states.HEALTH_UNKNOWN

#!/usr/bin/env python3
"""失败态缓存守卫的回归测试（2026-09-29 新增）。

实锤事故（2026-09-28 实测）：`--engine parallel` 未配 key → 状态层
env_ready=True、执行层取不到值， outcome 落
{"status": "error", "detail": "PARALLEL_API_KEY 未设置"}，连同空结果
一起写进 L2（{"results": [], "ttl": 45}）。用户按文档配好
PARALLEL_API_KEY 后，同一查询仍 cached=true 并回放同一条「未设置」——
与 issue #12 报告人的体验逐字同构。同类形态还有 circuit_open。

本文件锁定的性质：
  1. 全部引擎配置/状态类失败 + 无有效结果 → 拒绝写入（核心）；
  2. 有有效结果 → 照常写入（个别引擎瞬时失败不毒化整批）；
  3. 网络类失败（timeout/no-results/blocked）→ 照常写入负缓存
     （其中混有「真的没有」，EMPTY_RESULT_TTL 是既有设计）；
  4. builder 路径漏到 status="error" 的缺密钥 detail → 拒绝；
  5. SearchCache.set 集成：拒绝后读不到，配好 key 重搜不被旧失败挡住；
  6. attempt_cache_write 把拒绝类别名 surfaced 给调用方（不抛穿主路径）。
"""
from __future__ import annotations

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(SCRIPT_DIR, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

os.environ.setdefault("ARGO_STATE_DIR", "/tmp/argo-failed-state-guard-test")

from cache_guard import (  # noqa: E402
    FailedStateCacheRejected,
    assert_not_failed_state,
    attempt_cache_write,
    cache_write_rejections,
    failed_state_reason,
    is_state_failure_outcome,
)
from cache import SearchCache  # noqa: E402


def _payload(outcomes, results=None):
    return {
        "results": results if results is not None else [],
        "engine_outcomes": outcomes,
    }


def _hit(engine="parallel", status="error", detail="PARALLEL_API_KEY 未设置"):
    return {"engine": engine, "status": status, "results_count": 0,
            "latency_ms": 12, "detail": detail}


class TestFailedStateReason:
    def test_all_missing_env_blocked(self):
        p = _payload([_hit("parallel"), _hit("seltz", detail="SELTZ_API_KEY 未设置")])
        reason = failed_state_reason(p)
        assert reason and "配置/状态类失败" in reason
    def test_valid_results_always_allowed(self):
        # 有有效结果：个别引擎的配置失败不毒化整批（照常缓存）
        p = _payload(
            [_hit("parallel"), {"engine": "anysearch", "status": "ok",
                                "results_count": 5, "latency_ms": 300}],
            results=[{"title": "t", "url": "https://x.com", "relevance": 0.8}],
        )
        assert failed_state_reason(p) is None

    def test_network_failures_still_cached(self):
        # 网络类失败不拦：负缓存是既有设计（EMPTY_RESULT_TTL），其中混有
        # 「真的没有」；等一会重试由 TTL 自然兜底
        for status in ("timeout", "no-results", "blocked", "rate-limited"):
            p = _payload([{"engine": "e", "status": status,
                           "results_count": 0, "latency_ms": 8000}])
            assert failed_state_reason(p) is None, status

    def test_builder_leak_error_with_missing_env_detail(self):
        # 路由层 env 拦截覆盖不到的自定义 required_env 引擎：status="error"
        # 但 detail 是缺密钥文本（issue #12 的 parallel/seltz/you 形态）
        p = _payload([_hit("you", status="error", detail="YDC_API_KEY 未设置")])
        assert failed_state_reason(p) is not None

    def test_plain_error_without_env_keyword_allowed(self):
        # 普通 error（上游 500 等）不是配置类：不拦，走既有负缓存
        p = _payload([_hit("e", status="error", detail="upstream 500")])
        assert failed_state_reason(p) is None

    def test_circuit_open_and_auth_and_quota_blocked(self):
        for status, detail in (
                ("skipped-circuit-open", "auto_disabled"),
                ("auth-failed", "HTTP 401"),
                ("quota-exhausted", "远端配额耗尽")):
            p = _payload([{"engine": "e", "status": status,
                           "results_count": 0, "latency_ms": 1, "detail": detail}])
            assert failed_state_reason(p) is not None, status

    def test_mixed_state_and_network_failure_blocked(self):
        # 一个缺密钥 + 一个超时、无有效结果：这次检索整体没成，拦
        p = _payload([
            _hit("parallel"),
            {"engine": "anysearch", "status": "timeout",
             "results_count": 0, "latency_ms": 8000},
        ])
        assert failed_state_reason(p) is not None

    def test_no_outcomes_allowed(self):
        # 旧载荷/直写路径没有 engine_outcomes：不猜，不拦
        assert failed_state_reason({"results": []}) is None
        assert failed_state_reason({"results": [], "engine_outcomes": []}) is None
        assert failed_state_reason("not a dict") is None

    def test_single_engine_dict_form(self):
        # CLI --engine 路径的直写形态：outcomes 可能是单个 dict
        p = {"results": [], "engine_outcomes": _hit()}
        assert failed_state_reason(p) is not None


class TestAssertNotFailedState:
    def test_raises_with_context(self):
        with pytest.raises(FailedStateCacheRejected) as ei:
            assert_not_failed_state(_payload([_hit()]), context="SearchCache.set")
        assert "SearchCache.set" in str(ei.value)

    def test_registered_in_cache_write_rejections(self):
        assert FailedStateCacheRejected in cache_write_rejections()

    def test_attempt_cache_write_surfaces_class_name(self):
        # 守卫拒绝不抛穿主路径：返回类别名，调用方写进 cache_write_skip
        def _write():
            assert_not_failed_state(_payload([_hit()]), context="t")
        assert attempt_cache_write(_write, context="t") == "FailedStateCacheRejected"
        assert attempt_cache_write(lambda: None, context="t") is None


class TestSearchCacheIntegration:
    def test_failed_state_not_written_then_readable_after_fix(self):
        cache = SearchCache(db_path=":memory:")
        payload = _payload([_hit("parallel")])
        # 直接走守卫（模拟 finalize 的 attempt_cache_write 被拒后不落库）
        with pytest.raises(FailedStateCacheRejected):
            cache.set("q", "parallel", 5, payload)
        assert cache.get("q", "parallel", 5) is None
        # 配好 key 后的成功载荷照常写入、可读回
        ok = _payload(
            [{"engine": "parallel", "status": "ok", "results_count": 1,
              "latency_ms": 200}],
            results=[{"title": "t", "url": "https://x.com", "relevance": 0.8}],
        )
        cache.set("q", "parallel", 5, ok)
        got = cache.get("q", "parallel", 5)
        assert got is not None and len(got["results"]) == 1

    def test_engine_level_state_failure_skips_negative_write(self):
        # per-engine 层：dispatch 调用点已跳过写入；这里锁定 set_engine 对
        # 正常空结果的负缓存行为不受影响（守卫只在 combo 载荷层）
        cache = SearchCache(db_path=":memory:")
        cache.set_engine("q", "e", 5, [])
        got = cache.get_engine("q", "e", 5)
        assert got == []


class TestIsStateFailureOutcome:
    def test_status_vocabulary(self):
        assert is_state_failure_outcome({"status": "skipped-missing-env"})
        assert is_state_failure_outcome({"status": "skipped-circuit-open"})
        assert is_state_failure_outcome({"status": "auth-failed"})
        assert is_state_failure_outcome({"status": "quota-exhausted"})
        assert is_state_failure_outcome(
            {"status": "error", "detail": "YDC_API_KEY 未设置"})
        assert not is_state_failure_outcome({"status": "error", "detail": "boom"})
        assert not is_state_failure_outcome({"status": "ok"})
        assert not is_state_failure_outcome({"status": "no-results"})
        assert not is_state_failure_outcome("not a dict")

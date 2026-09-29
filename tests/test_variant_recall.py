#!/usr/bin/env python3
"""Step 1 —— multi-query 变体召回波回归门。

锁四条契约（对应 search_pipeline._variant_recall_wave / _should_variant_recall）：
  1. gate：快档/预算档（mode=fast|budget 或 depth=fast）不触发；仅主 query
     召回不足才触发；结果充足则零成本不动。
  2. 触发时用**变体**（非主 query 本身）补充召回，结果作为独立 ranked list 返回。
  3. 变体结果必带 _engine 标记（rrf_merge 的加权融合依赖它）。
  4. 失败安全：无衍生变体、或 engine_search 抛异常 → 返回空，绝不外抛。

为何绕开 _run_one：变体召回不碰熔断/负缓存/per-engine 记账（变体失败不该污染
主引擎健康），故此处直接以假 engine_search 打桩验证，无需拉起真实 dispatch。
"""
from __future__ import annotations

import os
import sys
import tempfile

# 状态隔离必须早于 import 任何 argo 模块（与 ranking_eval 同范式）
os.environ.setdefault("ARGO_STATE_DIR", tempfile.mkdtemp(prefix="argo-variant-test-"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

from search_pipeline import _SearchRequest  # noqa: E402
from variant_recall import (  # noqa: E402
    should_variant_recall,
    variant_recall_wave,
)


def _mk_req(**over) -> _SearchRequest:
    """最小可用 _SearchRequest；默认 auto/balanced、召回不足场景。"""
    base = dict(
        query="GPT-5 capabilities", decision={"domain": "general", "engine": "auto"},
        engines=["anysearch", "local_bing"], engines_combo=["anysearch", "local_bing"],
        domain="general", mode="auto", depth="balanced", timeout=10, max_results=5,
        retrieval_query="GPT-5 capabilities", parallel=True, eff_timeout=8.0,
        exclude_terms=[], qu=None, since_iso=None, until_iso=None, since_ts=None,
        until_ts=None, time_aware=False, skip_cache=False, timing=None,
        on_progress=None, sort="relevance", cache=None, engine_label="auto",
        cache_engine_key="auto", emit_usage_log=lambda *a, **k: None, breaker=None,
    )
    base.update(over)
    return _SearchRequest(**base)


# ── 1. gate 纯函数 ────────────────────────────────────────────────────────────

def test_gate_skips_fast_and_budget_tiers():
    # 最轻量预算档不触发（时延可预期优先）
    assert should_variant_recall(_mk_req(mode="fast"), [[{}]], 5) is False
    assert should_variant_recall(_mk_req(mode="budget"), [[{}]], 5) is False
    # 默认路径 mode=auto + depth=fast：召回不足应触发（depth=fast 不拦截）
    assert should_variant_recall(_mk_req(mode="auto", depth="fast"), [[{}]], 5) is True


def test_gate_fires_only_when_under_recalled():
    req = _mk_req()
    assert should_variant_recall(req, [[{}], [{}]], 5) is True       # 2 < 5 不足
    assert should_variant_recall(req, [[{}] * 6], 5) is False        # 6 ≥ 5 充足
    assert should_variant_recall(req, [], 5) is True                 # 空更应补


def test_gate_env_killswitch(monkeypatch):
    # ARGO_MULTI_QUERY=0 一键关闭（运维回滚 / 无侵入 A/B 对照）
    monkeypatch.setenv("ARGO_MULTI_QUERY", "0")
    assert should_variant_recall(_mk_req(), [[{}]], 5) is False
    monkeypatch.setenv("ARGO_MULTI_QUERY", "1")
    assert should_variant_recall(_mk_req(), [[{}]], 5) is True


# ── 2. 变体召回行为 ───────────────────────────────────────────────────────────

def test_variant_recall_uses_variants_not_main_query():
    calls: list[tuple[str, str, object]] = []

    def fake_engine_search(q, eng, **kw):
        calls.append((q, eng, kw.get("skip_cache")))
        return [{"title": f"r-{q}-{eng}", "url": f"https://x/{q}/{eng}", "snippet": "s"}]

    extra = variant_recall_wave(_mk_req(), fake_engine_search)
    assert extra, "GPT-5 应拆出变体并召回"
    # 每条变体结果都带 _engine（rrf_merge 加权依赖）
    for lst in extra:
        for r in lst:
            assert r.get("_engine")
    # 调用的 query 是变体，绝非主 query 本身；且 skip_cache=True（隔离缓存）
    assert calls
    assert all(q != "GPT-5 capabilities" for q, _, _ in calls)
    assert all(sc is True for _, _, sc in calls)


def test_variant_recall_no_variants_returns_empty():
    def fake(q, eng, **kw):
        return [{"url": "u"}]
    # 纯字母、无连字符数字、无 concept/acronym 命中 → 无衍生变体
    assert variant_recall_wave(_mk_req(retrieval_query="hello world"), fake) == []


def test_variant_recall_engine_error_is_safe():
    def boom(q, eng, **kw):
        raise RuntimeError("network down")
    # 失败安全：任何引擎异常都吞掉，返回空而非外抛
    assert variant_recall_wave(_mk_req(), boom) == []


def test_variant_recall_error_entries_are_dropped():
    def mixed(q, eng, **kw):
        return [{"error": "boom", "source": eng},
                {"title": "ok", "url": f"https://x/{eng}", "snippet": "s"}]
    extra = variant_recall_wave(_mk_req(), mixed)
    for lst in extra:
        assert all("error" not in r for r in lst)  # error 条目被剔除

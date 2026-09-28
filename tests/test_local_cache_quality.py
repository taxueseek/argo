#!/usr/bin/env python3
"""tests/test_local_cache_quality.py — local 缓存质量收口（2026-09-29 方案C）。

守四个修复，对应 2026-09-29 审查结论：
  1. 缓存写侧补 relevance=score：cache_guard._entry_relevance 只认
     relevance/rerank_dims.relevance，local 结果只有 score——退化守卫对
     local 缓存失明，「退化回声固化」原事故类不设防。relevance 只进
     缓存载荷，命中返回时剥掉，对外 schema 不变
  2. cache.set 整体 try 包裹：守卫（DegradedCacheRejected 等）与 IO 异常
     只剥夺本次缓存资格，不应炸掉已经拿到的搜索结果
  3. 显式引擎时从分类推断缓存域（此前恒 local_general，新闻结果被按
     general 1h TTL 缓存）
  4. fallback 失败不再静默（吞错类残留：引擎报的只有 CLI 错误）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "sub-skills" / "local-search", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import search_v3  # noqa: E402
from engine_registry import get_registry  # noqa: E402


class FakeCache:
    """确定性假缓存：记录 set 调用，可注入 get 命中与 set 异常。"""

    def __init__(self, hit=None, set_behavior=None):
        self._hit = hit
        self._set_behavior = set_behavior
        self.set_calls = []

    def get(self, *a, **k):
        return self._hit

    def set(self, *a, **k):
        self.set_calls.append(k)
        if self._set_behavior is not None:
            self._set_behavior(*a, **k)


def _patch_search(monkeypatch, results=None):
    results = results if results is not None else [
        {"title": "r1", "url": "https://a.example.com/1", "snippet": "x",
         "score": 0.8, "source": "local_bing"},
        {"title": "r2", "url": "https://a.example.com/2", "snippet": "y",
         "score": 0.6, "source": "local_bing"},
    ]

    def fake_search_one(name, query, n=5, timeout=None, since=None, until=None):
        return [dict(r) for r in results], ""

    monkeypatch.setattr(search_v3, "_search_one", fake_search_one)


# ── 1. 缓存载荷带 relevance ──────────────────────────────────────────────────

def test_cache_payload_carries_relevance(monkeypatch):
    _patch_search(monkeypatch)
    fake = FakeCache()
    monkeypatch.setattr(search_v3, "SearchCache", lambda: fake)
    out = search_v3.search_engines("q", engines=["local_bing"], n=3,
                                   skip_cache=False, mode="deep")
    assert len(fake.set_calls) == 1
    stored = fake.set_calls[0]["results"]
    for r in stored["results"]:
        assert r["relevance"] == pytest.approx(r["score"])
    # 对外返回不携带 relevance（schema 不变）
    assert all("relevance" not in r for r in out["results"])


def test_cache_hit_strips_internal_relevance(monkeypatch):
    _patch_search(monkeypatch)
    hit = {"_cache_level": "L2", "results": [
        {"title": "cached", "url": "https://c.example.com/1", "score": 0.7,
         "relevance": 0.7}]}
    fake = FakeCache(hit=hit)
    monkeypatch.setattr(search_v3, "SearchCache", lambda: fake)
    out = search_v3.search_engines("q", engines=["local_bing"], n=3,
                                   skip_cache=False, mode="deep")
    assert out["cached"] is True
    assert all("relevance" not in r for r in out["results"])
    assert out["results"][0]["title"] == "cached"


# ── 2. 守卫拒绝不炸搜索 ───────────────────────────────────────────────────────

def test_guard_rejection_degrades_to_skip_cache(monkeypatch):
    _patch_search(monkeypatch)

    def _reject(*a, **k):
        raise RuntimeError("DegradedCacheRejected: 判定为上游退化")

    fake = FakeCache(set_behavior=_reject)
    monkeypatch.setattr(search_v3, "SearchCache", lambda: fake)
    out = search_v3.search_engines("q", engines=["local_bing"], n=3,
                                   skip_cache=False, mode="deep")
    # 结果照常返回（此前 cache.set 未包裹，异常会杀死整个 search_engines）
    assert len(out["results"]) == 2
    assert out["engines_used"] == ["local_bing"]


# ── 3. 显式引擎的缓存域推断 ───────────────────────────────────────────────────

def test_infer_cache_domain_from_categories():
    reg = get_registry()
    assert search_v3._infer_cache_domain(["local_bing_news"], reg) == "local_news"
    assert search_v3._infer_cache_domain(["local_github"], reg) == "local_code"
    assert search_v3._infer_cache_domain(["local_arxiv"], reg) == "local_academic"
    assert search_v3._infer_cache_domain(["local_wikipedia"], reg) == "local_reference"
    # web_general / 无映射分类回 local_general
    assert search_v3._infer_cache_domain(["local_bing"], reg) == "local_general"


def test_explicit_engines_get_domain_cache(monkeypatch):
    """显式新闻引擎时 cache_domain 应为 local_news（此前恒 local_general）。"""
    _patch_search(monkeypatch)
    fake = FakeCache()
    monkeypatch.setattr(search_v3, "SearchCache", lambda: fake)
    search_v3.search_engines("q", engines=["local_bing_news"], n=3,
                             skip_cache=False, mode="deep")
    assert fake.set_calls[0]["domain"] == "local_news"


# ── 4. fallback 失败上报 ──────────────────────────────────────────────────────

def test_fallback_failure_surfaced_in_error(monkeypatch):
    def fake_cli(spec, query, n, timeout, since=None, until=None):
        return [], "ddgs: DecodeError"

    def boom(*a, **k):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(search_v3, "_run_cli_engine", fake_cli)
    monkeypatch.setattr(search_v3, "_fetch", boom)
    res, err = search_v3._search_one("local_bing", "q", n=3)
    assert res == []
    assert "ddgs: DecodeError" in err          # CLI 错误保留
    assert "fallback 抓取失败" in err           # fallback 失败不再静默
    assert "network unreachable" in err


def test_dead_config_removed():
    src = (ROOT / "sub-skills" / "local-search" / "config.yaml").read_text(encoding="utf-8")
    assert "request_interval_ms" not in src  # 全仓无消费者的死配置

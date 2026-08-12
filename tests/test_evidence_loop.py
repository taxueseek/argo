#!/usr/bin/env python3
"""
test_evidence_loop.py — 证据闭环 P0 回归测试

覆盖：fetch 证据提取 / URL 证据缓存 / 回填 / 高后果门控 / verify 核验模式。
全部 mock fetch，不打真实网络。缓存用 tmp_path 隔离，不碰生产 SQLite。
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from cache import SearchCache  # noqa: E402
import evidence_loop  # noqa: E402


@pytest.fixture()
def cache(tmp_path):
    """临时隔离缓存（L1+L2 独立，不污染生产库）。"""
    return SearchCache(db_path=str(tmp_path / "evidence_test.db"))


# ── A. fetch 证据提取 ──────────────────────────────────────────────────────────

def test_extract_fetch_evidence_success():
    """成功 fetch 提取正文级证据分：数字/定义信号、word_count、content_ok。"""
    fr = {
        "success": True,
        "url": "https://example.com/price",
        "title": "Gold price 2026",
        "content": (
            "The gold price reached $2,950 per ounce in August 2026, up 8% from last year. "
            "Central banks increased reserves by 400 tonnes. Market analysts define this as "
            "a structural shift compared to 2024 levels."
            * 3
        ),
        "quality_score": 0.72,
        "content_ok": True,
        "page_type": "article",
        "source_type": "news",
        "fetch_method": "http",
        "cached": False,
    }
    ev = evidence_loop.extract_fetch_evidence(fr)
    assert ev is not None
    assert ev["url"] == "https://example.com/price"
    assert ev["absorption"] is not None and 0 <= ev["absorption"] <= 1
    assert ev["word_count"] > 50
    assert ev["content_ok"] is True
    assert ev["page_type"] == "article"
    assert ev["fetch_method"] == "http"


def test_extract_fetch_evidence_failure_none():
    """抓取失败 / 空正文 / 无 URL → 返回 None（不产生缓存污染）。"""
    assert evidence_loop.extract_fetch_evidence({}) is None
    assert evidence_loop.extract_fetch_evidence({"success": False, "url": "u"}) is None
    assert evidence_loop.extract_fetch_evidence(
        {"success": True, "url": "u", "content": "   "}) is None
    assert evidence_loop.extract_fetch_evidence(
        {"success": True, "url": "", "content": "body"}) is None


def test_ttl_for_fetch_result():
    """TTL 按页面类型：news 短、docs 长、默认。"""
    assert evidence_loop.ttl_for_fetch_result({"source_type": "news"}) == 600
    assert evidence_loop.ttl_for_fetch_result({"source_type": "docs"}) == 86400
    assert evidence_loop.ttl_for_fetch_result({"page_type": "reference"}) == 86400
    assert evidence_loop.ttl_for_fetch_result({}) == 3600


# ── B. URL 证据缓存 ────────────────────────────────────────────────────────────

def test_evidence_cache_roundtrip(cache):
    """证据分写读命中。"""
    ev = {
        "url": "https://a.com/p",
        "absorption": 0.61,
        "quality_score": 0.8,
        "word_count": 120,
        "content_ok": True,
        "evidence_flags": {"has_numbers": True, "has_definition": False},
        "page_type": "article",
        "fetch_method": "http",
    }
    evidence_loop.store_fetch_evidence("https://a.com/p", ev, cache)
    hit = evidence_loop.lookup_fetch_evidence("https://a.com/p", cache)
    assert hit is not None
    assert hit["absorption"] == 0.61
    assert hit["url"] == "https://a.com/p"
    # 未命中
    assert evidence_loop.lookup_fetch_evidence("https://a.com/other", cache) is None


def test_evidence_cache_isolated_from_fetch_cache(cache):
    """证据分缓存与 fetch 正文缓存隔离：写 evidence 不影响 fetch 读取，反之亦然。"""
    cache.set_evidence("https://a.com/p", {"absorption": 0.5, "url": "https://a.com/p"})
    assert cache.get_fetch("https://a.com/p") is None  # evidence 不串进 fetch

    cache.set_fetch("https://a.com/p", {"content": "x" * 100, "success": True}, ttl=600)
    assert cache.get_evidence("https://a.com/p")["absorption"] == 0.5  # fetch 不覆盖 evidence


# ── C. 回填 ────────────────────────────────────────────────────────────────────

def test_backfill_results_with_and_without_cache(cache):
    """有证据缓存的结果回填；无缓存的不回填；不改排序。"""
    cache.set_evidence("https://v.com/known", {
        "url": "https://v.com/known", "absorption": 0.72,
        "quality_score": 0.9, "word_count": 200, "content_ok": True,
        "evidence_flags": {"has_numbers": True}, "fetch_method": "http",
    })
    results = [
        {"url": "https://v.com/known", "title": "A", "score": 0.9},
        {"url": "https://v.com/unknown", "title": "B", "score": 0.8},
    ]
    out = evidence_loop.backfill_results(results, cache)
    assert out[0]["has_fetched_evidence"] is True
    assert out[0]["post_fetch_absorption"] == 0.72
    assert out[0]["fetched_evidence"]["word_count"] == 200
    assert "has_fetched_evidence" not in out[1]
    # 排序不变
    assert [r["title"] for r in out] == ["A", "B"]


# ── D. 高后果门控 ──────────────────────────────────────────────────────────────

def test_is_high_consequence_domain():
    """finance/health/legal/事实安全域命中；通用/技术域不命中。"""
    for d in ("stock_query", "fund_query", "financial_news", "macro_data",
              "medical", "legal", "us_legal", "wenshu_query", "fact_check",
              "aviation_weather", "crypto_search"):
        assert evidence_loop.is_high_consequence_domain(d), d
    for d in ("general", "tech_deep", "film_search", "sports_search",
              "academic", "code_search", None, ""):
        assert not evidence_loop.is_high_consequence_domain(d), d


def test_gate_results_high_consequence(cache):
    """高后果域：fetch_required=True；未核验结果标记 fetch_suggested。"""
    results = [
        {"url": "https://v.com/a", "title": "A", "score": 0.9},
        {"url": "https://v.com/b", "title": "B", "score": 0.8},
    ]
    gate = evidence_loop.gate_results(results, "stock_query", cache)
    assert gate["fetch_required"] is True
    assert gate["high_consequence_domain"] == "stock_query"
    assert gate["suggested"] == ["https://v.com/a", "https://v.com/b"]
    assert gate["verified_count"] == 0
    assert gate["pending_count"] == 2
    assert results[0]["fetch_suggested"] is True


def test_gate_results_verified_skips_suggest(cache):
    """已核验结果不再建议核验，verified_count 计数。"""
    cache.set_evidence("https://v.com/known", {
        "url": "https://v.com/known", "absorption": 0.7,
    })
    results = [
        {"url": "https://v.com/known", "title": "Known"},
        {"url": "https://v.com/new", "title": "New"},
    ]
    gate = evidence_loop.gate_results(results, "medical", cache)
    assert gate["verified_count"] == 1
    assert gate["pending_count"] == 1
    assert gate["suggested"] == ["https://v.com/new"]
    assert results[0]["fetch_suggested"] is False
    assert results[0]["has_fetched_evidence"] is True
    assert results[1]["fetch_suggested"] is True


def test_gate_results_serp_not_suggested(cache):
    """SERP/跳转链接不标记 fetch_suggested。"""
    results = [
        {"url": "https://www.bing.com/search?q=argo", "title": "SERP",
         "evidence_flags": {"is_serp": True}},
        {"url": "https://v.com/real", "title": "Real"},
    ]
    gate = evidence_loop.gate_results(results, "general", cache)
    assert gate["fetch_required"] is False
    assert gate["suggested"] == ["https://v.com/real"]
    assert results[0]["fetch_suggested"] is False
    assert results[1]["fetch_suggested"] is True


# ── E. verify 核验模式 ─────────────────────────────────────────────────────────

def _fake_fetch_factory(url, absorption=0.7, success=True, content_len=300):
    """构造 mock fetch_fn：返回带正文的成功/失败结果。"""
    def _fetch(u, max_chars=8000, timeout=8.0):
        if not success:
            return {"url": u, "success": False, "error": "mock fail",
                    "content": "", "fetch_method": "mock"}
        content = (
            "Gold price reached $2,950 per ounce in August 2026, up 8% year-over-year. "
            "Central banks added 400 tonnes to reserves, compared to 320 tonnes in 2024. "
            "Market analysts define this as a structural shift in demand."
            * max(1, content_len // 160)
        )
        return {
            "url": u, "success": True, "title": "Test", "content": content,
            "quality_score": 0.7, "content_ok": True,
            "page_type": "article", "source_type": "news",
            "fetch_method": "mock", "cached": False,
        }
    return _fetch


def test_verify_results_fetch_and_backfill(cache):
    """verify 模式：fetch 未核验结果 → 回填 → evidence_revision 分布。"""
    results = [
        {"url": "https://v.com/gold", "title": "Gold", "absorption": 0.2},
        {"url": "https://v.com/silver", "title": "Silver", "absorption": 0.3},
    ]
    v = evidence_loop.verify_results(
        results, "gold price", cache=cache,
        fetch_fn=_fake_fetch_factory("https://v.com/gold"), top_k=2,
    )
    assert len(v["verified"]) == 2
    rs = v["revision_summary"]
    assert rs["n"] == 2
    # 正文吸收分应高于 snippet 级（0.2/0.3 → 正文含数字 ≈0.42）
    assert rs["improved"] >= 1
    assert all(x["delta"] > 0 for x in v["verified"])
    assert all(x["post_absorption"] > 0.3 for x in v["verified"])
    # 结果已回填
    assert results[0]["has_fetched_evidence"] is True
    assert results[0]["post_fetch_absorption"] > 0.3
    assert results[0]["fetch_suggested"] is False
    # 证据已入缓存 → 二次 verify 跳过
    v2 = evidence_loop.verify_results(
        results, "gold price", cache=cache,
        fetch_fn=_fake_fetch_factory("https://v.com/gold"), top_k=2,
    )
    assert v2["skipped_cached"] == 2
    assert v2["revision_summary"]["n"] == 0


def test_verify_results_fetch_failure_pending(cache):
    """fetch 失败 → 进入 pending，不产生 revision 记录。"""
    results = [
        {"url": "https://v.com/down", "title": "Down", "absorption": 0.3},
        {"url": "https://v.com/ok", "title": "Ok", "absorption": 0.3},
    ]

    def _fetch_mixed(u, max_chars=8000, timeout=8.0):
        if "down" in u:
            return {"url": u, "success": False, "error": "timeout", "content": ""}
        return _fake_fetch_factory(u)("u", max_chars, timeout)

    v = evidence_loop.verify_results(
        results, "q", cache=cache, fetch_fn=_fetch_mixed, top_k=2,
    )
    assert "https://v.com/down" in v["pending"]
    assert len(v["verified"]) == 1
    assert v["revision_summary"]["n"] == 1


def test_verify_results_respects_top_k(cache):
    """top_k 限制核验条数。"""
    results = [{"url": f"https://v.com/r{i}", "title": f"R{i}", "absorption": 0.2}
               for i in range(5)]
    v = evidence_loop.verify_results(
        results, "q", cache=cache,
        fetch_fn=_fake_fetch_factory("https://v.com/r0"), top_k=2,
    )
    assert len(v["verified"]) == 2
    assert v["revision_summary"]["n"] == 2


# ── F. fetch_v3 集成：成功抓取自动写证据缓存 ───────────────────────────────────

def test_fetch_v3_writes_evidence_cache(monkeypatch, tmp_path):
    """fetch_v3 成功路径自动写 URL 证据分缓存（隔离 db，不真打网）。"""
    import cache as cache_mod
    import fetch_v3

    db = str(tmp_path / "fetch_v3_test.db")
    # fetch_v3 函数内 `from cache import SearchCache` → 打 cache 模块类，隔离 db
    _orig = cache_mod.SearchCache

    def _factory(*a, **kw):
        kw.pop("db_path", None)
        return _orig(db_path=db, **kw)

    monkeypatch.setattr(cache_mod, "SearchCache", _factory)

    def _fake_http(url, max_chars=8000, timeout=8.0):
        return {
            "url": url, "success": True, "title": "Fake",
            "content": (
                "Gold reached $2,950 per ounce in August 2026, up 8% year-over-year. "
                "Central banks added 400 tonnes, compared to 320 tonnes in 2024."
                * 4
            ),
            "html": "<html><body>" + "x" * 100 + "</body></html>",
            "fetch_method": "http",
        }

    monkeypatch.setattr(fetch_v3, "_http_fetch", _fake_http)
    monkeypatch.setattr(fetch_v3, "_assess_quality", lambda r: {
        **r, "content_ok": True, "page_type": "article",
        "source_type": "news", "quality_score": 0.6,
    })

    fr = fetch_v3.fetch_v3("https://v.com/fake", skip_cache=False)
    assert fr["success"] is True

    c = cache_mod.SearchCache(db_path=db)
    ev = c.get_evidence("https://v.com/fake")
    assert ev is not None
    assert ev["absorption"] is not None
    assert ev["url"] == "https://v.com/fake"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

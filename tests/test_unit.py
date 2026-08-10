#!/usr/bin/env python3
"""Unified Search v2 单元测试 — config / route / engines / cache / tfidf / quota / adaptive"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
LOCAL_SEARCH_DIR = SKILL_DIR / "sub-skills" / "local-search"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(LOCAL_SEARCH_DIR))

from config import load_config, get_engines, get_domains, get_cost_factor, get_cost_tiers
from route import extract_features, match_domain, route_query
from cache import SearchCache, DOMAIN_TIER_MAP, CACHE_TIERS  # noqa: F401
from engines import get_registry, available_engines, search as engine_search
from tfidf_router import semantic_route, SemanticRouter
from quota import QuotaManager
from adaptive import AdaptiveLearner

from engine_registry import EngineRegistry, get_engine, list_engines, list_categories, update_availability
from local_health_check import apply_threshold, _detect_anti_bot
from smart_router import extract_features as local_extract_features, route_query as local_route_query


class TestConfig(unittest.TestCase):
    def test_load_config(self):
        cfg = load_config(force=True)
        self.assertIn("engines", cfg)
        self.assertIn("domains", cfg)

    def test_engines_enabled(self):
        engines = get_engines()
        self.assertTrue(len(engines) >= 3)
        self.assertIn("anysearch", engines)

    def test_cost_factor(self):
        # anysearch 是 free
        self.assertEqual(get_cost_factor("anysearch"), 1.0)
        # 未知引擎默认 1.0
        self.assertEqual(get_cost_factor("nonexistent"), 1.0)

    def test_cost_tiers(self):
        tiers = get_cost_tiers()
        self.assertIn("free", tiers)
        self.assertIn("anysearch", tiers.get("free", []))

    def test_tilde_expanded(self):
        cfg = load_config(force=True)
        for spec in cfg.get("engines", {}).values():
            if "cmd" in spec and isinstance(spec["cmd"], list):
                for item in spec["cmd"]:
                    self.assertFalse(item.startswith("~"))


class TestRoute(unittest.TestCase):
    def test_user_override(self):
        d = route_query("anything", engine_override="duckduckgo")
        self.assertEqual(d["engine"], "duckduckgo")
        self.assertEqual(d["confidence"], 1.0)

    def test_stock_domain(self):
        d = route_query("英伟达股价")
        self.assertEqual(d["engine"], "sina_quote")
        self.assertEqual(d["domain"], "stock_query")
        # daily depth=fast 预算 2：答案主源 + 备选；eastmoney 在 depth=deep 全量 combo
        self.assertLessEqual(len(d.get("engines") or []), 2)
        d_deep = route_query("英伟达股价", depth="deep")
        self.assertIn("eastmoney", d_deep.get("engines") or [])

    def test_fund_domain(self):
        d = route_query("基金净值")
        self.assertEqual(d["domain"], "fund_query")
        # 主源 eastmoney；熔断/配额时可能沉底到 anysearch，combo 仍应含基金域源
        combo = d.get("engines") or d.get("engines_combo") or []
        self.assertTrue(
            set(combo) & {"eastmoney", "anysearch"},
            f"fund combo unexpected: {combo}",
        )
        self.assertIn(d["engine"], ("eastmoney", "anysearch", *combo[:1]))

    def test_technical_english(self):
        d = route_query("Python asyncio internals")
        # 技术英文问句可落到 english_tech / code_search 等；引擎含 octen 等 T 系
        tech_engines = {
            "anysearch", "byted", "duckduckgo", "github", "octen", "tavily",
            "local_stackoverflow", "local_github", "local_bing", "bocha",
        }
        self.assertTrue(
            d["engine"] in tech_engines or any(e in tech_engines for e in d.get("engines", [])),
            f"unexpected tech route: {d.get('engine')} / {d.get('engines')}",
        )
        self.assertIn(
            d.get("domain"),
            (
                "english_tech",
                "code_search",
                "package_search",
                "general_search",
                None,
                "tech_deep",
                "local_code",
            ),
        )

    def test_mode_budget_filters_paid(self):
        d = route_query("latest AI news", mode="budget")
        # budget 模式不应包含付费引擎
        for eng in d.get("engines", []):
            self.assertNotEqual(get_cost_factor(eng), 0.3)

    def test_features_extracted(self):
        f = extract_features("React vs Vue 哪个好")
        self.assertTrue(f["has_compare"])
        self.assertGreater(f["chinese_ratio"], 0)

    def test_has_reason(self):
        d = route_query("贵州茅台股价")
        self.assertIn("reason", d)
        self.assertTrue(len(d["reason"]) > 0)

    def test_zero_tfidf_not_eastmoney(self):
        """P0：TF-IDF 全 0 时禁止塌缩到 eastmoney 等垂直引擎。"""
        d = route_query("pytest fixtures")
        self.assertNotEqual(d["engine"], "eastmoney")
        forbidden = {"eastmoney", "ths_hot", "cls_telegraph", "em_global_news"}
        self.assertNotIn(d["engine"], forbidden)
        for eng in d.get("engines", []):
            self.assertNotIn(eng, forbidden)

    def test_finance_still_eastmoney(self):
        d = route_query("贵州茅台股价")
        self.assertEqual(d["engine"], "sina_quote")
        self.assertEqual(d.get("domain"), "stock_query")
        # 深度路径仍保留东财（日常 budget 只保留前 2 答案源）
        d_deep = route_query("贵州茅台股价", depth="deep")
        self.assertIn("eastmoney", d_deep.get("engines") or [])


class TestTfidfRouter(unittest.TestCase):
    def test_route_returns_scores(self):
        scores = semantic_route("英伟达财报", top_k=3)
        self.assertTrue(len(scores) >= 1)
        # 确认返回 (engine, score, reason) 三元组
        for item in scores:
            self.assertEqual(len(item), 3)
            self.assertIsInstance(item[0], str)
            self.assertIsInstance(item[1], float)
            self.assertIsInstance(item[2], str)

    def test_academic_route(self):
        scores = semantic_route("transformer attention mechanism paper", top_k=3)
        engines = [s[0] for s in scores]
        self.assertTrue(any(e in engines for e in ["arxiv", "semantic_scholar", "openalex"]))

    def test_finance_route(self):
        scores = semantic_query = semantic_route("基金净值 股票行情", top_k=3)
        self.assertTrue(scores[0][1] > 0)

    def test_should_parallel(self):
        router = SemanticRouter()
        # 深度研究型查询应触发并行
        self.assertTrue(router.should_parallel("深度分析 AI 格局", []))
        # 简单查询不触发
        self.assertFalse(router.should_parallel("天气", [("duckduckgo", 0.5, "")]))


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_cache.db")
        self.cache = SearchCache(db_path=self.db_path)
        self.cache.clear(older_than_hours=0)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_l1_hit(self):
        self.cache.set("q1", "anysearch", 5, {"results": [{"title": "x"}]}, domain="general")
        hit = self.cache.get("q1", "anysearch", 5, domain="general")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("_cache_level"), "L1")

    def test_l2_persist(self):
        self.cache.set("q2", "duckduckgo", 3, {"results": [{"title": "y"}]}, domain="general")
        cache2 = SearchCache(db_path=self.db_path)
        hit = cache2.get("q2", "duckduckgo", 3, domain="general")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("_cache_level"), "L2")

    def test_domain_ttl(self):
        self.assertEqual(SearchCache.resolve_ttl("stock_query"), 300)
        self.assertEqual(SearchCache.resolve_ttl("fund_query"), 300)
        self.assertEqual(SearchCache.resolve_ttl("tech_deep"), 7200)
        self.assertEqual(SearchCache.resolve_ttl("general_search"), 3600)
        self.assertEqual(SearchCache.resolve_ttl("unknown"), 3600)
        # v2.4.1：缺失域补全
        self.assertIn("chinese_general", DOMAIN_TIER_MAP)
        self.assertIn("fact_check", DOMAIN_TIER_MAP)
        self.assertEqual(SearchCache.resolve_ttl("chinese_general"), 3600)
        self.assertEqual(SearchCache.resolve_ttl("fact_check"), 7200)

    def test_freshness_ttl_cap(self):
        """时效敏感查询强制 TTL ≤ 900，即使 domain 是 general。"""
        from cache import is_freshness_sensitive_query, REALTIME_TTL_CAP
        self.assertTrue(is_freshness_sensitive_query("今日热点新闻"))
        self.assertTrue(is_freshness_sensitive_query("A股 实时 行情"))
        self.assertFalse(is_freshness_sensitive_query("Python asyncio 教程"))
        ttl = SearchCache.resolve_ttl("chinese_general", query="今日热点新闻")
        self.assertLessEqual(ttl, REALTIME_TTL_CAP)
        # set 后写入的 _ttl 也不得超过 cap
        self.cache.set(
            "今日热点新闻", "anysearch", 5,
            {"results": [{"title": "n", "url": "http://n"}]},
            domain="chinese_general", mode="auto", depth="fast",
        )
        hit = self.cache.get("今日热点新闻", "anysearch", 5,
                             domain="chinese_general", mode="auto", depth="fast")
        self.assertIsNotNone(hit)
        self.assertLessEqual(hit.get("_ttl", 99999), REALTIME_TTL_CAP)

    def test_query_normalize_cache_hit(self):
        """空白/大小写差异应命中同一缓存键。"""
        self.cache.set(
            "React  Hooks", "anysearch", 5,
            {"results": [{"title": "r", "url": "http://r"}]},
            domain="general", mode="auto", depth="fast",
        )
        hit = self.cache.get("react  hooks", "anysearch", 5,
                             domain="general", mode="auto", depth="fast")
        self.assertIsNotNone(hit)
        hit2 = self.cache.get("  React   Hooks  ", "anysearch", 5,
                              domain="general", mode="auto", depth="fast")
        self.assertIsNotNone(hit2)

    def test_general_no_eod_extension(self):
        """general 域不再日末延长；research 仍可。"""
        from cache import SAME_DAY_ELIGIBLE_TIERS
        self.assertNotIn("general", SAME_DAY_ELIGIBLE_TIERS)
        self.assertIn("research", SAME_DAY_ELIGIBLE_TIERS)
        self.assertIn("evergreen", SAME_DAY_ELIGIBLE_TIERS)
        ttl_g = self.cache._resolve_effective_ttl("general_search")
        self.assertEqual(ttl_g, 3600)
        ttl_r = self.cache._resolve_effective_ttl("tech_deep")
        self.assertGreaterEqual(ttl_r, 7200)

    def test_ttl_expiry(self):
        self.cache.set("q3", "anysearch", 5, {"results": [{"title": "z"}]},
                       domain="general", ttl=0)
        time.sleep(0.05)
        self.assertIsNone(self.cache.get("q3", "anysearch", 5, domain="general"))

    def test_depth_isolation(self):
        """P0：fast/deep 缓存互不污染。"""
        self.cache.set(
            "qd", "anysearch", 5,
            {"results": [{"title": "fast", "url": "http://a"}]},
            domain="general", mode="auto", depth="fast",
        )
        hit_fast = self.cache.get("qd", "anysearch", 5, domain="general",
                                  mode="auto", depth="fast")
        hit_deep = self.cache.get("qd", "anysearch", 5, domain="general",
                                  mode="auto", depth="deep")
        self.assertIsNotNone(hit_fast)
        self.assertIsNone(hit_deep)

    def test_soft_max_results(self):
        """P1：cached_n >= requested_n 可柔性命中。"""
        self.cache.set(
            "qn", "anysearch", 5,
            {"results": [{"title": str(i), "url": f"http://x/{i}"} for i in range(5)]},
            domain="general", mode="auto", depth="fast",
        )
        hit3 = self.cache.get("qn", "anysearch", 3, domain="general",
                              mode="auto", depth="fast")
        self.assertIsNotNone(hit3)
        self.assertEqual(len(hit3["results"]), 3)
        hit10 = self.cache.get("qn", "anysearch", 10, domain="general",
                               mode="auto", depth="fast")
        self.assertIsNone(hit10)  # 缓存不足 → miss

    def test_empty_result_short_ttl(self):
        self.cache.set(
            "qe", "anysearch", 5, {"results": []},
            domain="general", mode="auto", depth="fast",
        )
        hit = self.cache.get("qe", "anysearch", 5, domain="general",
                             mode="auto", depth="fast")
        self.assertIsNotNone(hit)
        self.assertLessEqual(hit.get("_ttl", 999), 60)

    def test_per_engine_and_fetch(self):
        self.cache.set_engine(
            "qe2", "local_bing", 3,
            [{"title": "a", "url": "http://a"}],
            domain="general", mode="auto", depth="fast",
        )
        eng = self.cache.get_engine(
            "qe2", "local_bing", 3, domain="general", mode="auto", depth="fast",
        )
        self.assertEqual(len(eng), 1)
        self.cache.set_fetch("https://example.com/x", {"success": True, "content": "hi"})
        fh = self.cache.get_fetch("https://example.com/x")
        self.assertTrue(fh.get("success"))


class TestCircuitBreaker(unittest.TestCase):
    def test_negative_and_open(self):
        from circuit_breaker import CircuitBreaker
        path = os.path.join(tempfile.mkdtemp(), "cb.json")
        cb = CircuitBreaker(state_path=path)
        eng = "test_dead_engine_xyz"
        self.assertTrue(cb.allow(eng)[0])
        cb.record_failure(eng, kind="timeout")
        cb.record_failure(eng, kind="timeout")
        allowed, reason = cb.allow(eng)
        self.assertFalse(allowed)
        self.assertIn("circuit_open", reason)
        cb.set_negative("q", eng, status="no-results", ttl=30)
        self.assertIsNotNone(cb.get_negative("q", eng))

    def test_empty_never_auto_disables(self):
        """empty（查询无结果）是查询级信号，不累计 opens → 永不 auto-disable。

        回归：local_search 聚合器曾因子源瞬态错误被扁平化为 empty，opens 累计
        到阈值后被静默禁用，导致零成本本地路径被 anysearch 兜底顶替。
        """
        from circuit_breaker import CircuitBreaker, OPEN_SECONDS, DISABLE_AFTER_OPENS
        cb = CircuitBreaker(state_path=os.path.join(tempfile.mkdtemp(), "cb.json"))
        eng = "test_empty_eng"
        for _ in range(DISABLE_AFTER_OPENS * 3):
            # 4 次 empty = 2 failure → open（empty 不累计 opens）
            for _ in range(4):
                cb.record_failure(eng, kind="empty")
            cb._engines[eng]["opened_at"] = time.time() - OPEN_SECONDS - 1
            cb.allow(eng)  # half-open 探测
            for _ in range(4):
                cb.record_failure(eng, kind="empty")
        st = cb._engines[eng]
        self.assertLess(st.get("opens", 0), DISABLE_AFTER_OPENS)
        self.assertNotIn(eng, cb.auto_disabled())
        # 冷却期过后仍可探测，未被永久禁用
        cb._engines[eng]["opened_at"] = time.time() - OPEN_SECONDS - 1
        allowed, _ = cb.allow(eng)
        self.assertTrue(allowed)  # 仍可探测，未被永久禁用


class TestRrfConsensus(unittest.TestCase):
    def test_consensus_engines(self):
        from search import rrf_merge
        a = [{"url": "http://u1", "title": "t", "source": "anysearch", "_engine": "anysearch"}]
        b = [{"url": "http://u1", "title": "t", "source": "duckduckgo", "_engine": "duckduckgo"}]
        merged = rrf_merge([a, b])
        self.assertEqual(len(merged), 1)
        cons = merged[0].get("consensus_engines") or []
        self.assertIn("anysearch", cons)
        self.assertIn("duckduckgo", cons)


class TestEngines(unittest.TestCase):
    def test_registry_has_anysearch(self):
        registry = get_registry()
        self.assertIn("anysearch", registry)

    def test_available_engines(self):
        engines = available_engines()
        self.assertIn("anysearch", engines)

    def test_search_returns_list(self):
        # 模拟调用（不实际发请求，测试接口）
        result = engine_search("test nonexistent engine xyz", "nonexistent", n=1)
        self.assertIsInstance(result, list)


class TestQuota(unittest.TestCase):
    def test_quota_manager_init(self):
        mgr = QuotaManager()
        self.assertIsNotNone(mgr)

    def test_record_and_remaining(self):
        mgr = QuotaManager()
        mgr.record("test_engine", success=True)
        # 无限配额引擎返回 1.0
        self.assertEqual(mgr.get_remaining_ratio("test_engine"), 1.0)


class TestAdaptive(unittest.TestCase):
    def test_learner_init(self):
        learner = AdaptiveLearner()
        self.assertIsNotNone(learner)

    def test_record_and_score(self):
        learner = AdaptiveLearner()
        learner.record("test_engine", success=True, latency_ms=500, cost=0.0)
        score = learner.get_score("test_engine")
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


class TestLocalSearchRegistry(unittest.TestCase):
    def test_registry_loads_engines(self):
        reg = EngineRegistry()
        engines = reg.list_engines()
        self.assertIn("local_arxiv", engines)
        self.assertIn("local_wikipedia", engines)

    def test_list_by_category(self):
        reg = EngineRegistry()
        self.assertIn("local_arxiv", reg.list_engines(category="academic"))
        self.assertIn("local_github", reg.list_engines(category="code"))
        self.assertIn("local_baidu", reg.list_engines(category="chinese"))

    def test_get_engine_has_fields(self):
        reg = EngineRegistry()
        eng = reg.get_engine("local_arxiv")
        self.assertIsNotNone(eng)
        self.assertIn("type", eng)
        self.assertIn("available", eng)

    def test_update_availability(self):
        reg = EngineRegistry()
        reg.update_availability("local_test_engine", False, fail_reason="unit_test")
        self.assertFalse(reg.is_available("local_test_engine"))
        reg.update_availability("local_test_engine", True)
        self.assertTrue(reg.is_available("local_test_engine"))


class TestLocalSearchHealthCheck(unittest.TestCase):
    def test_detect_anti_bot(self):
        self.assertIsNotNone(_detect_anti_bot("please complete the captcha", 200))
        self.assertEqual(_detect_anti_bot("", 429), "rate_limited")
        self.assertIsNone(_detect_anti_bot("normal result page", 200))

    def test_apply_threshold_success_recover(self):
        prev = {"available": False, "consecutive_failures": 2}
        report = {"available": True, "status": 200, "latency_ms": 500}
        self.assertTrue(apply_threshold(report, prev))

    def test_apply_threshold_two_failures(self):
        prev = {"available": True, "consecutive_failures": 1}
        report = {"available": False, "status": 503, "latency_ms": 500}
        self.assertFalse(apply_threshold(report, prev))

    def test_apply_threshold_slow_marks_unavailable(self):
        prev = {"available": True, "consecutive_failures": 0}
        report = {"available": False, "status": 200, "latency_ms": 9000}
        self.assertFalse(apply_threshold(report, prev))


class TestLocalSearchSmartRouter(unittest.TestCase):
    def test_route_academic(self):
        decision = local_route_query("transformer attention paper")
        self.assertIn("local_arxiv", decision["engines"])
        self.assertEqual(decision["domain"], "academic")

    def test_route_code(self):
        decision = local_route_query("python list comprehension stackoverflow")
        self.assertIn("local_stackoverflow", decision["engines"])

    def test_route_reference(self):
        decision = local_route_query("what is the capital of France wikipedia")
        self.assertIn("local_wikipedia", decision["engines"])

    def test_preferred_engines_override(self):
        decision = local_route_query("anything", preferred_engines=["local_github"], require_available=False)
        self.assertEqual(decision["engines"], ["local_github"])


class TestLocalSearchConfigIntegration(unittest.TestCase):
    def test_local_search_in_cost_tiers(self):
        tiers = get_cost_tiers()
        self.assertIn("local_search", tiers.get("free", []))
        self.assertIn("local_arxiv", tiers.get("free", []))

    def test_local_search_engine_config(self):
        engines = get_engines()
        self.assertIn("local_search", engines)
        self.assertEqual(engines["local_search"].get("type"), "cli")


class TestAdaptiveTTLModeMatch(unittest.TestCase):
    """自适应 TTL 必须按实际 mode/depth 查旧键，否则 fast/budget 下永不生效。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache = SearchCache(db_path=os.path.join(self.tmpdir.name, "ttl.db"))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_fast_mode_stable_content_extends_ttl(self):
        results = {"results": [{"url": "http://a", "title": "title-a"}] * 4}
        self.cache.set("稳定查询", "byted", 5, dict(results),
                       domain="general", mode="fast", depth="fast")
        self.cache.set("稳定查询", "byted", 5, dict(results),
                       domain="general", mode="fast", depth="fast")
        key = self.cache._key("稳定查询", "byted", 5, "general", "fast", "fast", kind="combo")
        hit = self.cache._l2.get(key)
        self.assertIsNotNone(hit)
        # 内容稳定 → TTL 延长到 base*2（7200）；修复前 fast 模式查 auto/fast 键恒 miss，恒 3600
        self.assertEqual(hit.get("_ttl"), 7200)

    def test_fast_mode_changed_content_keeps_base(self):
        self.cache.set("变化查询", "byted", 5,
                       {"results": [{"url": "http://a", "title": "v1"}] * 4},
                       domain="general", mode="fast", depth="fast")
        self.cache.set("变化查询", "byted", 5,
                       {"results": [{"url": "http://b", "title": "v2-different"}] * 4},
                       domain="general", mode="fast", depth="fast")
        key = self.cache._key("变化查询", "byted", 5, "general", "fast", "fast", kind="combo")
        hit = self.cache._l2.get(key)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("_ttl"), 3600)

    def test_balanced_depth_isolated_key(self):
        """balanced 深度与 fast 深度键隔离，互不串 TTL。"""
        self.cache.set("深度隔离", "byted", 5,
                       {"results": [{"url": "http://a", "title": "t"}] * 4},
                       domain="general", mode="auto", depth="balanced")
        key_b = self.cache._key("深度隔离", "byted", 5, "general", "auto", "balanced", kind="combo")
        key_f = self.cache._key("深度隔离", "byted", 5, "general", "auto", "fast", kind="combo")
        self.assertNotEqual(key_b, key_f)
        self.assertIsNotNone(self.cache._l2.get(key_b))
        self.assertIsNone(self.cache._l2.get(key_f))


class TestCumulativeSufficient(unittest.TestCase):
    """wave-2 提前终止：跨引擎累计结果判定（修复 search.py 并行拖尾）。"""

    @staticmethod
    def _mod():
        import importlib
        import search
        return importlib.reload(search)

    def test_single_engine_insufficient_multi_engine_sufficient(self):
        mod = self._mod()
        raw = {
            "eng_a": [{"title": "a1", "snippet": "s"}],  # 1 条 < 2
            "eng_b": [{"title": "b1", "snippet": "s"}],  # 累计 2 条够 fast
        }
        self.assertTrue(mod._cumulative_sufficient(raw, mode="fast", min_results=None))
        self.assertFalse(mod._cumulative_sufficient(
            {"eng_a": raw["eng_a"]}, mode="fast", min_results=None))

    def test_min_results_override(self):
        mod = self._mod()
        raw = {"eng": [{"title": "a", "snippet": "s"}]}
        # 答案域 min_results=1：1 条即够
        self.assertTrue(mod._cumulative_sufficient(raw, mode="auto", min_results=1))
        # 普通域 auto：3 条才够
        self.assertFalse(mod._cumulative_sufficient(raw, mode="auto", min_results=None))

    def test_errors_excluded(self):
        mod = self._mod()
        raw = {
            "eng_a": [{"error": "boom"}],
            "eng_b": [{"error": "timeout"}],
        }
        self.assertFalse(mod._cumulative_sufficient(raw, mode="fast"))


class TestHttpClientEngineIntegration(unittest.TestCase):
    """引擎层 HttpClient 接入：GET 走渐进增强层，env=0 回退 urllib。"""

    def test_http_get_raw_uses_http_client_by_default(self):
        """ARGO_ENGINE_HTTP_CLIENT 默认开启 → GET 走 HttpClient。"""
        from engines_base import _http_get_raw
        calls = []

        class _FakeClient:
            def __init__(self, *a, **kw):
                calls.append(("init", kw.get("jitter"), kw.get("max_retries")))

            def get(self, url, extra_headers=None, follow_redirects=False):
                calls.append(("get", url, bool(follow_redirects)))
                return {"status": 200, "text": '{"ok": 1}', "error": ""}

        with patch("engines_base.os.environ.get", return_value="1"), \
             patch("http_client.HttpClient", _FakeClient):
            raw = _http_get_raw("https://x.example/q", {"Accept": "*/*"}, 8)
        self.assertEqual(raw, '{"ok": 1}')
        self.assertEqual(calls[0], ("init", False, 1))  # 搜索热路径禁 jitter
        self.assertTrue(calls[1][2])  # follow_redirects 开启

    def test_http_get_raw_env_off_falls_back_urllib(self):
        """ARGO_ENGINE_HTTP_CLIENT=0 → 回退 urllib（存量 mock 兼容）。"""
        from engines_base import _http_get_raw
        captured = []

        class _FakeResp:
            def read(self):
                return b'{"ok": 1}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=0):
            captured.append(req.full_url)
            return _FakeResp()

        with patch("engines_base.os.environ.get", return_value="0"), \
             patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            raw = _http_get_raw("https://y.example/q", {}, 8)
        self.assertEqual(raw, '{"ok": 1}')
        self.assertEqual(captured, ["https://y.example/q"])

    def test_http_get_raw_non_200_returns_none(self):
        """HttpClient 非 200/空 body → 返回 None（调用方按无结果处理）。"""
        from engines_base import _http_get_raw

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            def get(self, url, extra_headers=None, follow_redirects=False):
                return {"status": 403, "text": "forbidden", "error": ""}

        with patch("engines_base.os.environ.get", return_value="1"), \
             patch("http_client.HttpClient", _FakeClient):
            self.assertIsNone(_http_get_raw("https://x.example/q", {}, 8))

    def test_redirect_following_in_http_client(self):
        """follow_redirects 死参数修复：301/302 跟随到最终页。"""
        from http_client import HttpClient
        import http.client as hc
        seen = []

        class _FakeResp:
            def __init__(self, status, headers):
                self.status = status
                self._headers = list(headers.items())

            def getheader(self, name, default=None):
                for k, v in self._headers:
                    if k.lower() == name.lower():
                        return v
                return default

            def getheaders(self):
                return list(self._headers)

            def read(self):
                return b""

        class _FakeConn:
            def __init__(self, host, port, timeout=None):
                seen.append(host)

            def request(self, method, path, headers=None):
                pass

            def getresponse(self):
                if len(seen) == 1:
                    return _FakeResp(302, {"Location": "https://final.example/page"})
                return _FakeResp(200, {})

            def close(self):
                pass

        with patch("http.client.HTTPSConnection", _FakeConn), \
             patch("http.client.HTTPConnection", _FakeConn), \
             patch.object(HttpClient, "_apply_jitter"):
            c = HttpClient(timeout=5, max_retries=0)
            c._cookies = type("NoopCookies", (), {
                "get_cookie_header": lambda self, u: None,
                "extract_from_response": lambda self, u, h: None,
            })()
            r = c.get("https://first.example/", follow_redirects=True)
        # 跟随 302 后请求了最终 host
        self.assertIn("final.example", seen)
        self.assertEqual(r.get("status"), 200)


class TestRouteSamplingGuard(unittest.TestCase):
    """engine_override 直通分支无有效 features，不得消耗遥测采样槽。"""

    def test_override_branch_does_not_sample(self):
        from route import _sample_route
        calls = []
        orig_emit = None
        try:
            import telemetry
            orig_emit = telemetry.emit
            telemetry.emit = lambda *a, **kw: calls.append(a)
        except ImportError:
            pass
        try:
            _sample_route({}, {"engine": "x", "features": {}})
            _sample_route({}, {"engine": "x"})
        finally:
            if orig_emit is not None:
                import telemetry
                telemetry.emit = orig_emit
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

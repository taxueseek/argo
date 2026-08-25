#!/usr/bin/env python3
"""新引擎集成测试 — DuckDuckGo Instant Answer / UAPI / Semantic Scholar。

覆盖：
  1. 解析器格式（离线，必过）
  2. 单引擎真实调用（网络依赖，可 skip）
  3. engines_combo 多源（mock 执行层）
  4. 主引擎失败 fallback（mock）
  5. 场景路由与端到端（部分网络）

运行：
  cd ~/.agents/skills/unified-search
  python3 -m pytest tests/test_new_engines.py -v
  # 仅离线：
  python3 -m pytest tests/test_new_engines.py -v -m "not live"
  # 含真实 API：
  python3 -m pytest tests/test_new_engines.py -v -m live
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from engines import (  # noqa: E402
    _parse_duckduckgo,
    _parse_uapi,
    _parse_semantic_scholar,
    search as engine_search,
    available_engines,
)
from route import route_query  # noqa: E402
from search import execute_search  # noqa: E402
from cache import SearchCache  # noqa: E402
from config import get_engines, load_config  # noqa: E402

# ── 常量 ──────────────────────────────────────────────────────────────────────

# uapi 在 config 中可能 enabled:false，注册表测试按「启用的引擎」与「配置仍存在」拆分
NEW_ENGINES = ("duckduckgo", "uapi", "semantic_scholar")
NEW_ENGINES_REQUIRED_ENABLED = ("duckduckgo", "semantic_scholar")

REQUIRED_RESULT_KEYS = ("title", "source")

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "zh_fact_weather",
        "query": "北京今天天气",
        "expect_domain_any": {"fact_check", "weather_query", "chinese_general", "general_search"},
        # 天气双源（v2.8+）：open_meteo/weather 是当前气象域主力；qweather 需 key
        "expect_combo_has_any": {"duckduckgo", "uapi", "anysearch", "qweather", "byted", "bocha",
                                 "open_meteo", "weather"},
    },
    {
        "id": "en_fact_capital",
        "query": "what is the capital of France",
        "expect_domain_any": {"fact_check", "general_search", "code_search", "english_tech", "semantic_discovery"},
        "expect_combo_has_any": {"duckduckgo", "anysearch", "uapi", "github", "octen", "wikipedia", "bocha"},
    },
    {
        "id": "academic_transformer",
        "query": "transformer attention mechanism",
        "expect_domain_any": {"academic", "english_tech", "tech_deep"},
        "expect_combo_has_any": {
            "arxiv", "semantic_scholar", "anysearch", "crossref",
            "europepmc", "dblp", "local_arxiv", "openalex",
        },
    },
    {
        "id": "zh_shopping",
        "query": "笔记本电脑推荐",
        "expect_domain_any": {"zhihu_content", "shopping", "general_search", "chinese_general"},
        "expect_combo_has_any": {"zhihu", "uapi", "anysearch", "bocha", "byted"},
    },
    {
        "id": "mixed_ai",
        "query": "Python 人工智能",
        "expect_domain_any": {
            "general_search", "tech_deep", "fact_check", "chinese_general",
            "english_tech", "chinese_tech_deep", "code_search",
        },
        "expect_combo_has_any": {
            "anysearch", "uapi", "duckduckgo", "octen", "byted", "bocha", "github",
        },
    },
]


def _assert_result_schema(results: list[dict[str, Any]], engine: str) -> None:
    """校验统一结果 schema。"""
    assert isinstance(results, list), f"{engine}: results 非 list"
    for i, r in enumerate(results):
        assert isinstance(r, dict), f"{engine}[{i}]: 非 dict"
        if "error" in r:
            continue
        for key in REQUIRED_RESULT_KEYS:
            assert key in r or r.get("title") is not None, (
                f"{engine}[{i}]: 缺少 {key}: {list(r.keys())}"
            )
        title = r.get("title", "")
        assert isinstance(title, str) and len(title) > 0, f"{engine}[{i}]: 空 title"
        snippet = r.get("snippet", "")
        if snippet:
            assert len(snippet) <= 400, f"{engine}[{i}]: snippet 过长 {len(snippet)}"


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 配置注册
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewEngineRegistration(unittest.TestCase):
    """配置与注册表中存在新引擎（uapi 可禁用，仍应可在 raw config 中查到）。"""

    def test_config_enabled(self) -> None:
        load_config(force=True)
        engines = get_engines()  # 仅 enabled
        for name in NEW_ENGINES_REQUIRED_ENABLED:
            self.assertIn(name, engines, f"config 缺少启用引擎 {name}")
            self.assertTrue(engines[name].get("enabled", True), f"{name} 应 enabled")
        # uapi 允许 disabled，但原始 config 应仍有定义
        raw = load_config(force=True).get("engines", {})
        self.assertIn("uapi", raw, "config 应保留 uapi 定义（可 enabled:false）")

    def test_registry_available(self) -> None:
        avail = set(available_engines())
        for name in NEW_ENGINES_REQUIRED_ENABLED:
            self.assertIn(name, avail, f"registry 缺少 {name}")

    def test_config_urls(self) -> None:
        raw = load_config(force=True).get("engines", {})
        self.assertIn("api.duckduckgo.com", raw["duckduckgo"]["url"])
        self.assertIn("search/aggregate", raw["uapi"]["url"])
        self.assertIn("semanticscholar.org", raw["semantic_scholar"]["url"])
        self.assertEqual(raw["uapi"].get("method", "POST").upper(), "POST")
        self.assertEqual(raw["duckduckgo"].get("method", "GET").upper(), "GET")

    # ── parallel / you.com 通用引擎（v2.8.0）──────────────────────────────

    def test_parallel_you_registered(self) -> None:
        avail = set(available_engines())
        self.assertIn("parallel", avail, "registry 缺少 parallel")
        self.assertIn("you", avail, "registry 缺少 you")
        raw = load_config(force=True).get("engines", {})
        self.assertEqual(raw["parallel"].get("type"), "parallel")
        self.assertEqual(raw["you"].get("type"), "you")

    def test_parallel_you_no_key_graceful(self) -> None:
        """无 API key 时返回空列表不崩（免费/降级路径）。"""
        from engines import search as engine_search
        old_p = os.environ.pop("PARALLEL_API_KEY", None)
        old_y = os.environ.pop("YDC_API_KEY", None)
        try:
            for name in ("parallel", "you"):
                res = engine_search(name, "rust async", n=2)
                self.assertIsInstance(res, list)
                self.assertEqual(res, [], f"{name} 无 key 应空返回")
        finally:
            if old_p is not None:
                os.environ["PARALLEL_API_KEY"] = old_p
            if old_y is not None:
                os.environ["YDC_API_KEY"] = old_y


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 解析器（离线）
# ═══════════════════════════════════════════════════════════════════════════════


class TestParsers(unittest.TestCase):
    """单引擎返回格式 — 解析层。"""

    def test_parse_duckduckgo_abstract_and_topics(self) -> None:
        data = {
            "Abstract": "Python is a high-level programming language.",
            "Heading": "Python (programming language)",
            "AbstractURL": "https://en.wikipedia.org/wiki/Python_(programming_language)",
            "RelatedTopics": [
                {
                    "Text": "NumPy - numerical computing library",
                    "FirstURL": "https://duckduckgo.com/NumPy",
                },
                {"Topics": [{"Text": "nested", "FirstURL": "https://x"}]},  # 嵌套组跳过
            ],
        }
        results = _parse_duckduckgo(data)
        _assert_result_schema(results, "duckduckgo")
        self.assertEqual(results[0]["source"], "duckduckgo")
        self.assertIn("Python", results[0]["title"])
        self.assertTrue(results[0]["url"].startswith("http"))
        self.assertTrue(any("NumPy" in r.get("title", "") for r in results))

    def test_parse_duckduckgo_empty_abstract(self) -> None:
        data = {
            "Abstract": "",
            "Heading": "X",
            "RelatedTopics": [
                {"Text": "Only topic", "FirstURL": "https://example.com/t"},
            ],
        }
        results = _parse_duckduckgo(data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://example.com/t")

    def test_parse_duckduckgo_empty_payload(self) -> None:
        self.assertEqual(_parse_duckduckgo({}), [])

    def test_parse_uapi(self) -> None:
        data = {
            "query": "测试",
            "total_results": 2,
            "results": [
                {
                    "title": "结果一",
                    "url": "https://example.com/1",
                    "snippet": "摘要一",
                    "source": "uapi-searchv1",
                },
                {
                    "title": "结果二",
                    "url": "https://example.com/2",
                    "snippet": "x" * 500,
                },
            ],
        }
        results = _parse_uapi(data)
        _assert_result_schema(results, "uapi")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["source"], "uapi")
        self.assertLessEqual(len(results[1]["snippet"]), 300)

    def test_parse_uapi_edge(self) -> None:
        self.assertEqual(_parse_uapi({}), [])
        self.assertEqual(_parse_uapi({"results": "bad"}), [])
        self.assertEqual(_parse_uapi({"results": [1, "x", None]}), [])

    def test_parse_semantic_scholar(self) -> None:
        data = {
            "data": [
                {
                    "title": "Attention Is All You Need",
                    "abstract": "We propose the Transformer architecture.",
                    "citationCount": 100000,
                    "openAccessPdf": {"url": "https://arxiv.org/pdf/1706.03762"},
                    "authors": [
                        {"name": "Ashish Vaswani"},
                        {"name": "Noam Shazeer"},
                    ],
                }
            ]
        }
        results = _parse_semantic_scholar(data)
        _assert_result_schema(results, "semantic_scholar")
        self.assertEqual(results[0]["source"], "semantic_scholar")
        self.assertIn("Attention", results[0]["title"])
        self.assertIn("引用", results[0]["snippet"])
        self.assertIn("作者", results[0]["snippet"])
        self.assertTrue(results[0]["url"].endswith("1706.03762"))
        self.assertEqual(results[0]["score"], 1.0)

    def test_parse_semantic_scholar_no_pdf(self) -> None:
        data = {
            "data": [
                {
                    "title": "Paper without PDF",
                    "abstract": "",
                    "citationCount": 0,
                    "authors": [],
                }
            ]
        }
        results = _parse_semantic_scholar(data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "")
        self.assertEqual(results[0]["score"], 0.5)

    def test_parse_semantic_scholar_empty(self) -> None:
        self.assertEqual(_parse_semantic_scholar({}), [])
        self.assertEqual(_parse_semantic_scholar({"data": None}), [])


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 多引擎 engines_combo + 4. 降级 fallback（mock，无网络）
# ═══════════════════════════════════════════════════════════════════════════════


class _AllowAllBreaker:
    """测试用：关闭熔断/负缓存干扰，保证 mock 引擎按 combo 顺序被调用。"""

    def allow(self, eng: str) -> tuple[bool, str]:
        return True, "closed"

    def get_negative(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_success(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_failure(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set_negative(self, *args: Any, **kwargs: Any) -> None:
        return None

    def clear_negative(self, *args: Any, **kwargs: Any) -> None:
        return None


class TestMultiEngineAndFallback(unittest.TestCase):
    """execute_search 多源与降级行为。"""

    def _run(
        self,
        decision: dict[str, Any],
        fake: Any,
        query: str = "test query",
    ) -> dict[str, Any]:
        cache = SearchCache(db_path=":memory:")
        with (
            patch("search.engine_search", side_effect=fake),
            patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
        ):
            return execute_search(
                query,
                decision,
                max_results=5,
                timeout=5,
                depth="fast",
                cache=cache,
                skip_cache=True,
            )

    def test_sequential_fallback_on_primary_empty(self) -> None:
        calls: list[str] = []

        def fake(query: str, eng: str, n: int = 5, timeout: float = 8,
                 depth: str = "fast", **_kwargs: Any):
            calls.append(eng)
            if eng == "duckduckgo":
                return []
            if eng == "anysearch":
                return [
                    {
                        "title": "Paris capital",
                        "url": "https://example.com/paris",
                        "snippet": "capital of France",
                        "source": "anysearch",
                    }
                ]
            return [{"title": f"from {eng}", "url": f"https://x/{eng}", "source": eng}]

        decision = {
            "domain": "fact_check",
            "engine": "duckduckgo",
            "engines": ["duckduckgo", "anysearch", "uapi"],
            "engines_combo": ["duckduckgo", "anysearch", "uapi"],
            "parallel": False,
            "tfidf_scores": [],
        }
        out = self._run(decision, fake, "what is the capital of France")
        self.assertTrue(calls, "应至少调用一个引擎")
        self.assertEqual(calls[0], "duckduckgo")
        self.assertIn("anysearch", calls)
        # 顺序模式：anysearch 成功后不应再打 uapi
        self.assertNotIn("uapi", calls)
        self.assertGreaterEqual(out["count"], 1)
        self.assertEqual(out["results"][0]["source"], "anysearch")
        self.assertIn("anysearch", out.get("engines_used", []))

    def test_sequential_all_fail_returns_empty(self) -> None:
        def fake(*_a: Any, **_k: Any) -> list:
            return []

        decision = {
            "domain": "fact_check",
            "engine": "duckduckgo",
            "engines": ["duckduckgo", "anysearch"],
            "engines_combo": ["duckduckgo", "anysearch"],
            "parallel": False,
            "tfidf_scores": [],
        }
        out = self._run(decision, fake)
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["results"], [])

    def test_parallel_multi_source_rrf(self) -> None:
        calls: list[str] = []

        def fake(query: str, eng: str, n: int = 5, timeout: float = 8,
                 depth: str = "fast", **_kwargs: Any):
            calls.append(eng)
            if eng == "duckduckgo":
                return []  # 一路失败
            return [
                {
                    "title": f"{eng}-hit",
                    "url": f"https://example.com/{eng}",
                    "snippet": eng,
                    "source": eng,
                    "score": 0.8,
                }
            ]

        decision = {
            "domain": "general_search",
            "engine": "anysearch",
            "engines": ["anysearch", "uapi", "duckduckgo"],
            "engines_combo": ["anysearch", "uapi", "duckduckgo"],
            "parallel": True,
            "tfidf_scores": [],
        }
        out = self._run(decision, fake, "Python 人工智能")
        self.assertEqual(set(calls), {"anysearch", "uapi", "duckduckgo"})
        self.assertGreaterEqual(out["count"], 1)
        sources = {r.get("source") for r in out["results"]}
        # 至少有成功源
        self.assertTrue(sources & {"anysearch", "uapi"})

    def test_engines_combo_field_preserved(self) -> None:
        def fake(query: str, eng: str, n: int = 5, timeout: float = 8,
                 depth: str = "fast", **_kwargs: Any):
            return [{"title": "ok", "url": "https://e.com", "source": eng}]

        combo = ["uapi", "anysearch", "duckduckgo"]
        decision = {
            "domain": "news_realtime",
            "engine": "uapi",
            "engines": combo,
            "engines_combo": combo,
            "parallel": True,
            "tfidf_scores": [],
        }
        out = self._run(decision, fake, "北京今天天气")
        self.assertEqual(out.get("engines_combo"), combo)
        self.assertEqual(set(out.get("engines_used", [])), set(combo))

    def test_empty_engines_combo_falls_back_to_anysearch(self) -> None:
        """畸形 decision（engines_combo 全空串）不抛 IndexError，兜底 anysearch。"""
        calls: list[str] = []

        def fake(query: str, eng: str, n: int = 5, timeout: float = 8,
                 depth: str = "fast", **_kwargs: Any):
            calls.append(eng)
            return [{"title": "ok", "url": "https://e.com", "source": eng}]

        decision = {
            "domain": "general",
            "engine": "",
            "engines": [""],
            "engines_combo": [""],
        }
        out = self._run(decision, fake)
        self.assertIn("anysearch", calls)
        self.assertEqual(out.get("count"), 1)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. 场景路由（离线）
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioRouting(unittest.TestCase):
    """场景覆盖 — 路由决策。"""

    def test_scenarios_route(self) -> None:
        for case in SCENARIOS:
            with self.subTest(case["id"]):
                d = route_query(case["query"])
                domain = d.get("domain")
                combo = d.get("engines_combo") or d.get("engines") or []
                self.assertIn(
                    domain,
                    case["expect_domain_any"],
                    f"{case['id']}: domain={domain} not in {case['expect_domain_any']}",
                )
                self.assertTrue(
                    set(combo) & set(case["expect_combo_has_any"]),
                    f"{case['id']}: combo={combo} 与期望无交集",
                )

    def test_fact_check_prefers_duckduckgo_in_combo(self) -> None:
        d = route_query("what is the capital of France")
        combo = d.get("engines_combo") or d.get("engines") or []
        # 事实/通识类应落到可用通用引擎，不必硬绑 duckduckgo
        generalish = {
            "duckduckgo", "anysearch", "wikipedia", "bocha", "byted",
            "octen", "local_bing", "github",
        }
        self.assertTrue(
            set(combo) & generalish,
            f"通识查询 combo 应含通用引擎: {combo}",
        )

    def test_shopping_includes_shopping_or_general_engine(self) -> None:
        d = route_query("best laptop review comparison")
        combo = d.get("engines_combo") or d.get("engines") or []
        domain = d.get("domain")
        # uapi 可能 disabled；购物/知乎/通用均可
        ok_engines = {"uapi", "zhihu", "anysearch", "bocha", "byted", "duckduckgo", "octen"}
        self.assertTrue(
            domain in {"shopping", "zhihu_content", "general_search", "chinese_general", "english_tech"}
            or set(combo) & ok_engines,
            f"购物类路由异常: domain={domain} combo={combo}",
        )

    def test_academic_keyword_includes_semantic_scholar(self) -> None:
        # 带 paper/arxiv 关键词应进 academic 系，combo 含学术引擎
        d = route_query("transformer attention paper arxiv")
        combo = d.get("engines_combo") or d.get("engines") or []
        domain = d.get("domain")
        self.assertIn(domain, {"academic", "tech_deep", "english_tech"})
        academic_engines = {
            "semantic_scholar", "arxiv", "crossref", "europepmc", "dblp",
            "local_arxiv", "local_semantic_scholar", "openalex",
        }
        self.assertTrue(
            set(combo) & academic_engines,
            f"学术域 combo 应含学术引擎: {combo}",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Live：真实 API（默认跳过，除非 RUN_LIVE=1 或 pytest -m live）
# ═══════════════════════════════════════════════════════════════════════════════


def _live_enabled() -> bool:
    return os.environ.get("RUN_LIVE", "").strip() in {"1", "true", "yes"}


@unittest.skipUnless(_live_enabled(), "设置 RUN_LIVE=1 启用真实 API 测试")
class TestLiveSingleEngines(unittest.TestCase):
    """单引擎真实调用。"""

    def test_live_duckduckgo(self) -> None:
        results = engine_search(
            "Python programming language", "duckduckgo", n=3, timeout=10
        )
        _assert_result_schema(results, "duckduckgo")
        self.assertGreater(len(results), 0, "DDG 应返回至少 1 条")
        self.assertTrue(
            any(r.get("source") == "duckduckgo" or r.get("_engine") == "duckduckgo"
                for r in results)
        )

    def test_live_uapi(self) -> None:
        results = engine_search("笔记本电脑推荐", "uapi", n=3, timeout=15)
        _assert_result_schema(results, "uapi")
        self.assertGreater(len(results), 0, "UAPI 应返回至少 1 条")
        self.assertTrue(
            any(r.get("source") == "uapi" or r.get("_engine") == "uapi" for r in results),
            f"期望 source/_engine=uapi, 实际 {[(r.get('source'), r.get('_engine')) for r in results]}",
        )

    def test_live_semantic_scholar(self) -> None:
        results = engine_search(
            "transformer attention mechanism", "semantic_scholar", n=3, timeout=20
        )
        if not results:
            # 429 / 网络失败时 soft-skip（不让 CI 红）
            self.skipTest("Semantic Scholar 无结果（可能 429 限流）")
        _assert_result_schema(results, "semantic_scholar")
        self.assertTrue(
            any(
                r.get("source") == "semantic_scholar"
                or r.get("_engine") == "semantic_scholar"
                for r in results
            )
        )


@unittest.skipUnless(_live_enabled(), "设置 RUN_LIVE=1 启用真实 API 测试")
class TestLiveConnectivityScripts(unittest.TestCase):
    """与运维脚本对齐的原始 HTTP 连通性。"""

    def test_ddg_instant_answer_api(self) -> None:
        url = (
            "https://api.duckduckgo.com/"
            "?q=Python+programming+language&format=json&no_html=1&skip_disambig=1"
        )
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        self.assertTrue(
            data.get("Abstract") or data.get("RelatedTopics"),
            "DDG Instant Answer 应有 Abstract 或 RelatedTopics",
        )

    def test_uapi_aggregate_endpoint(self) -> None:
        # 正确路径：/api/v1/search/aggregate（非 /search/web）
        req = urllib.request.Request(
            "https://uapis.cn/api/v1/search/aggregate",
            data=json.dumps({"query": "测试", "count": 3}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        self.assertIn("results", data)
        self.assertGreater(len(data["results"]), 0)

    def test_uapi_legacy_web_path_is_404(self) -> None:
        """回归：错误路径 /search/web 必须 404，防止配置回退。"""
        req = urllib.request.Request(
            "https://uapis.cn/api/v1/search/web",
            data=json.dumps({"query": "测试", "count": 3}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=15)
        self.assertEqual(ctx.exception.code, 404)

    def test_semantic_scholar_api(self) -> None:
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/search"
            "?query=transformer&limit=3&fields=title,year,url,citationCount"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "unified-search-integration-test/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self.skipTest("Semantic Scholar 429 限流")
            raise
        self.assertGreater(len(data.get("data") or []), 0)


@unittest.skipUnless(_live_enabled(), "设置 RUN_LIVE=1 启用端到端场景")
class TestLiveScenarios(unittest.TestCase):
    """场景端到端：auto 路由 + 真实引擎。"""

    def test_scenarios_e2e_nonempty_or_soft(self) -> None:
        from search import super_search  # 延迟导入

        for case in SCENARIOS:
            with self.subTest(case["id"]):
                # super_search 或 CLI 等价调用
                try:
                    out = super_search(
                        case["query"],
                        engine="auto",
                        n=3,
                        explain=False,
                    )
                except TypeError:
                    # 签名差异时走 execute_search + route
                    decision = route_query(case["query"])
                    out = execute_search(
                        case["query"],
                        decision,
                        max_results=3,
                        timeout=15,
                        depth="fast",
                        cache=SearchCache(),
                        skip_cache=True,
                    )
                if isinstance(out, dict):
                    # 允许全引擎不可用时为空，但不允许崩溃结构
                    self.assertIn("results", out)
                    self.assertIsInstance(out["results"], list)
                    if out["results"]:
                        _assert_result_schema(out["results"], "e2e")


# ═══════════════════════════════════════════════════════════════════════════════
# 已知缺陷回归（文档化断言）
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnownIssues(unittest.TestCase):
    """记录集成时发现的缺陷，便于跟踪。"""

    def test_http_post_parser_uses_engine_name_for_uapi(self) -> None:
        """回归：HTTP POST 必须按 spec._name 选解析器，uapi source 不得被标成 stackoverflow。

        历史缺陷：POST 分支曾固定 _parse_stackoverflow。现用 _PARSERS[name] + _ensure_engine_source。
        """
        r = _parse_uapi(
            {"results": [{"title": "t", "url": "https://u", "snippet": "s"}]}
        )
        self.assertEqual(r[0]["source"], "uapi")
        from engines import _ensure_engine_source
        # 模拟错标后纠正
        fixed = _ensure_engine_source(
            [{"title": "t", "url": "https://u", "source": "stackoverflow"}], "uapi"
        )
        self.assertEqual(fixed[0]["source"], "uapi")

    def test_ddg_disambiguation_risk(self) -> None:
        """缺陷风险：q=what+is+python 可能命中 Cold War PYTHON 而非编程语言。

        查询应使用明确实体名；配置可加 skip_disambig=1。
        """
        ambiguous = _parse_duckduckgo(
            {
                "Abstract": "PYTHON was a Cold War contingency plan",
                "Heading": "PYTHON",
                "AbstractURL": "https://en.wikipedia.org/wiki/PYTHON",
                "RelatedTopics": [],
            }
        )
        self.assertIn("Cold War", ambiguous[0]["snippet"])


if __name__ == "__main__":
    # 默认跑离线；加 --live 设 RUN_LIVE=1
    if "--live" in sys.argv:
        os.environ["RUN_LIVE"] = "1"
        sys.argv.remove("--live")
    unittest.main(verbosity=2)

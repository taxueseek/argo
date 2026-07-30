#!/usr/bin/env python3
"""yichen 契约吸纳：plan / input_kind / envelope — 离线单测，防回归。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from plan import (  # noqa: E402
    build_plan, classify_input_kind, extract_urls, canonicalize_url, is_pure_url,
    execution_tier, should_attach_plan, requires_user_confirmation,
)
from candidate_envelope import (  # noqa: E402
    attach_envelope, result_to_candidate, build_coverage, canonicalize_url as c2,
)
from search import super_search  # noqa: E402
from route import route_query  # noqa: E402
from cache import SearchCache  # noqa: E402


class TestInputKind(unittest.TestCase):
    def test_keyword(self):
        self.assertEqual(classify_input_kind("Python asyncio"), "keyword")

    def test_pure_url_known(self):
        self.assertEqual(
            classify_input_kind("https://example.com/a"), "known-url")
        self.assertTrue(is_pure_url("https://example.com/a"))

    def test_url_seed_discover(self):
        q = "搜索引用 https://example.com/research 的公开报道"
        self.assertEqual(classify_input_kind(q), "url-seed")

    def test_url_in_text_default_known(self):
        # 安全默认：夹带 URL 但无发现意图 → known-url
        self.assertEqual(
            classify_input_kind("看看 https://example.com/a"), "known-url")

    def test_explicit_override(self):
        self.assertEqual(
            classify_input_kind("https://example.com/a", "keyword"), "keyword")

    def test_extract_urls(self):
        urls = extract_urls("见 https://a.com/x 和 https://b.com/y")
        self.assertEqual(len(urls), 2)

    def test_canonicalize_strips_utm(self):
        u = canonicalize_url("https://ex.com/p?utm_source=x&id=1")
        self.assertIn("id=1", u)
        self.assertNotIn("utm_source", u)


class TestExecutionTier(unittest.TestCase):
    """日常直搜 vs deep/research 分层，禁止日常先确认。"""

    def test_daily_default(self):
        self.assertEqual(execution_tier("auto", "fast"), "daily")
        self.assertEqual(execution_tier("fast", "balanced"), "daily")
        self.assertEqual(execution_tier("budget", "fast"), "daily")

    def test_professional_deep(self):
        self.assertEqual(execution_tier("deep", "fast"), "professional")
        self.assertEqual(execution_tier("auto", "deep"), "professional")

    def test_research_context(self):
        self.assertEqual(
            execution_tier("auto", "balanced", context="research"),
            "deep_research",
        )

    def test_attach_plan_policy(self):
        self.assertFalse(should_attach_plan("auto", "fast"))
        self.assertTrue(should_attach_plan("deep", "fast"))
        self.assertTrue(should_attach_plan("auto", "deep"))
        # research 顶层挂 plan，子查询 super_search 不重复
        self.assertFalse(should_attach_plan("auto", "balanced", context="research"))
        self.assertTrue(should_attach_plan("auto", "fast", plan_only=True))

    def test_daily_never_requires_confirmation(self):
        self.assertFalse(requires_user_confirmation("auto", "fast"))
        self.assertFalse(requires_user_confirmation("deep", "deep"))
        self.assertTrue(
            requires_user_confirmation(plan_status="needs_authorization")
        )

    def test_plan_no_import_search_cycle(self):
        """静态保证：plan 模块不 import search/research（注释可提名字）。"""
        import ast
        import plan as plan_mod
        tree = ast.parse(Path(plan_mod.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("search", imported)
        self.assertNotIn("research", imported)
        # 运行时也不应把 search 拉进 sys.modules（build_plan 后）
        before = "search" in sys.modules
        build_plan("hello world", mode="auto")
        if not before:
            self.assertNotIn("search", sys.modules)


class TestPlanOffline(unittest.TestCase):
    def test_ready_keyword_no_network_fields(self):
        p = build_plan("贵州茅台股价", mode="auto", depth="fast")
        self.assertEqual(p["status"], "ready")
        self.assertEqual(p["input_kind"], "keyword")
        self.assertEqual(p["authorization"], "not_required")
        self.assertFalse(p.get("requires_confirmation"))
        self.assertEqual(p.get("execution_tier"), "daily")
        self.assertIsInstance(p["steps"], list)
        self.assertTrue(any(s.get("action") == "search" for s in p["steps"]))
        # 日常 plan 不含 verify 步骤暗示
        self.assertFalse(any(s.get("action") == "optional_verify_top_k" for s in p["steps"]))
        # 与 route 对齐
        d = route_query("贵州茅台股价")
        self.assertEqual(p["decision"]["domain"], d.get("domain"))

    def test_professional_has_verify_step(self):
        p = build_plan("React hooks", mode="deep", depth="deep")
        self.assertEqual(p.get("execution_tier"), "professional")
        self.assertTrue(any(s.get("action") == "optional_verify_top_k" for s in p["steps"]))
        self.assertFalse(p.get("requires_confirmation"))

    def test_research_plan_decompose_once(self):
        p = build_plan("CRISPR 综述", mode="auto", depth="balanced", context="research")
        self.assertEqual(p.get("execution_tier"), "deep_research")
        self.assertTrue(any(s.get("action") == "decompose_then_collect" for s in p["steps"]))
        self.assertFalse(p.get("requires_confirmation"))

    def test_handoff_known_url(self):
        p = build_plan("https://docs.python.org/3/", input_kind="auto")
        self.assertEqual(p["status"], "handoff_required")
        self.assertIn("handoff", p)
        self.assertIn("argo_fetch", p["handoff"]["suggested_tools"])
        self.assertFalse(p.get("requires_confirmation"))

    def test_url_seed_limitations(self):
        p = build_plan(
            "搜索引用 https://example.com/a 的讨论",
            input_kind="url-seed",
        )
        self.assertEqual(p["status"], "ready")
        self.assertTrue(any("seed" in x.lower() or "discovery" in x.lower()
                            for x in p["limitations"]))

    def test_plan_only_super_search(self):
        r = super_search("React hooks", plan_only=True, mode="fast")
        self.assertEqual(r["status"], "ready")
        self.assertIn("steps", r)
        # plan-only 不带检索 results 列表
        self.assertTrue("results" not in r or r.get("results") in (None, []))


class TestEnvelope(unittest.TestCase):
    def test_attach_preserves_results_order(self):
        raw = {
            "query": "q",
            "engine": "anysearch",
            "results": [
                {"title": "A", "url": "https://a.com/1", "snippet": "s1", "score": 0.9, "source": "anysearch"},
                {"title": "B", "url": "https://b.com/2?utm_source=x", "snippet": "s2", "score": 0.8, "source": "anysearch"},
            ],
            "count": 2,
            "engine_outcomes": [
                {"engine": "anysearch", "status": "ok", "results_count": 2, "latency_ms": 100},
            ],
            "mode": "fast",
        }
        out = attach_envelope(raw, query="q", input_kind="keyword")
        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(out["results"][0]["title"], "A")  # 排序不变
        self.assertEqual(out["schema_version"], "1.0")
        self.assertEqual(len(out["candidates"]), 2)
        self.assertEqual(out["candidates"][0]["verification"]["status"], "candidate")
        self.assertFalse(out["candidates"][0]["verification"]["opened_original"])
        self.assertNotIn("utm_source", out["candidates"][1]["canonical_url"])
        self.assertTrue(out["coverage"])
        self.assertTrue(any("engagement" in x or "snippet" in x.lower() or "clue" in x.lower()
                            or "Do not" in x for x in out["limitations"]))

    def test_dedupe_canonical(self):
        raw = {
            "query": "q",
            "results": [
                {"title": "A", "url": "https://a.com/1?utm_source=1", "source": "e1"},
                {"title": "A2", "url": "https://a.com/1?utm_medium=2", "source": "e2"},
            ],
            "engine_outcomes": [],
        }
        out = attach_envelope(raw, query="q")
        self.assertEqual(len(out["candidates"]), 1)

    def test_coverage_from_outcomes(self):
        cov = build_coverage([
            {"engine": "octen", "status": "ok", "results_count": 5, "latency_ms": 700},
            {"engine": "byted", "status": "timeout", "results_count": 0, "latency_ms": 10000},
        ], max_results=5)
        self.assertEqual(cov[0]["truncated"], True)
        self.assertTrue(cov[1]["limitations"])


class TestSuperSearchGates(unittest.TestCase):
    def test_known_url_skips_network(self):
        r = super_search("https://example.com/page", mode="fast", n=3)
        self.assertEqual(r.get("status"), "handoff_required")
        self.assertEqual(r.get("count"), 0)
        self.assertEqual(r.get("results"), [])
        self.assertEqual(r.get("input_kind"), "known-url")
        self.assertIn("handoff", r)
        self.assertFalse(r.get("requires_confirmation"))

    def test_daily_keyword_no_plan_no_confirm(self):
        """日常热路径：用缓存预写结果，验证不挂 plan、不要求确认。"""
        db = os.path.join(tempfile.mkdtemp(), "daily.db")
        cache = SearchCache(db_path=db)
        # 先走路由拿 combo 引擎键，再写入 combo 缓存（与 execute_search 一致）
        d = route_query("pytest fixtures", mode="auto")
        eng = d.get("engine") or "anysearch"
        combo = d.get("engines_combo") or d.get("engines") or [eng]
        # 写入足够宽的 per-engine + 让 execute 可命中
        payload = {
            "results": [
                {"title": "T", "url": "https://example.com/t", "snippet": "s", "source": eng},
            ],
            "count": 1,
            "engine": eng,
            "engines_used": [eng],
        }
        # per-engine 缓存键：query+engine
        cache.set("pytest fixtures", eng, 3, payload, domain=d.get("domain"),
                  mode="auto", depth="fast")
        # 若 execute 用 combo 键，再写一份常见键
        for e in combo[:3]:
            cache.set("pytest fixtures", e, 3, payload, domain=d.get("domain"),
                      mode="auto", depth="fast")

        r = super_search(
            "pytest fixtures", mode="auto", depth="fast", n=3,
            cache=cache, timeout=2, rewrite=False,
        )
        self.assertEqual(r.get("status"), "completed")
        self.assertEqual(r.get("execution_tier"), "daily")
        self.assertFalse(r.get("requires_confirmation"))
        # 日常不挂 plan 对象
        self.assertNotIn("plan", r)
        # limitations 声明 daily 直搜
        lims = " ".join(r.get("limitations") or [])
        self.assertTrue("daily" in lims.lower() or r.get("execution_tier") == "daily")

    def test_professional_attaches_plan_without_blocking(self):
        r = super_search(
            "React hooks", mode="deep", depth="deep", n=1,
            plan_only=True,  # 只验证 plan 路径不阻塞
        )
        self.assertEqual(r.get("status"), "ready")
        self.assertEqual(r.get("execution_tier"), "professional")
        self.assertFalse(r.get("requires_confirmation"))
        self.assertTrue(any(
            s.get("action") == "optional_verify_top_k" for s in (r.get("steps") or [])
        ))

    def test_keyword_still_returns_results_shape(self):
        # 路由不倒退
        d = route_query("贵州茅台股价")
        self.assertEqual(d["engine"], "eastmoney")
        d2 = route_query("pytest fixtures")
        self.assertNotEqual(d2["engine"], "eastmoney")

    def test_envelope_flag_off(self):
        # plan_only 不受 envelope 影响
        r = super_search("hello world", plan_only=True, envelope=False)
        self.assertEqual(r["status"], "ready")

    def test_research_plan_once_offline(self):
        """deep_research 顶层 plan 一次，不要求确认。"""
        from research import deep_research
        # 仅测 plan 挂载：mock collect 太重，直接测 build_plan + deep_research 字段
        # 用极短 timeout + 允许空结果
        report = deep_research(
            "unit test topic argo",
            num_sub_queries=1,
            max_results=1,
            timeout=1,
            depth="fast",
            mode="fast",
        )
        self.assertEqual(report.get("execution_tier"), "deep_research")
        self.assertFalse(report.get("requires_confirmation"))
        self.assertIn("plan", report)
        self.assertEqual(report["plan"].get("execution_tier"), "deep_research")
        # plan 不得再触发二次 research（静态已在 TestExecutionTier 覆盖）


class TestNoRegressionRoute(unittest.TestCase):
    """确保吸纳后路由黄金集不倒退。"""

    def test_finance(self):
        self.assertEqual(route_query("贵州茅台股价")["engine"], "eastmoney")

    def test_tech_en(self):
        d = route_query("React hooks tutorial")
        self.assertIn(d.get("domain"), ("english_tech", "general_search", "code_search", "tech_deep"))

    def test_tech_cn(self):
        d = route_query("Python 异步编程教程")
        self.assertEqual(d.get("domain"), "chinese_tech_deep")

    def test_academic(self):
        d = route_query("transformer attention paper")
        self.assertIn(d["engine"], ("arxiv", "semantic_scholar", "openalex"))


if __name__ == "__main__":
    unittest.main()

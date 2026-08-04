#!/usr/bin/env python3
"""P0 v2.5 单元测试 — query_understanding / recovery / 五维rerank / fact_align / 新引擎 / 新域。

离线为主（不依赖网络）。运行：
  cd ~/.claude/skills/argo
  python3 -m pytest tests/test_p0_v25.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from query_understanding import (  # noqa: E402
    understand, parse_negation, parse_geo, classify_intents, split_multi_intent,
    QueryUnderstanding,
)
from recovery import (  # noqa: E402
    relax_query, synonym_expand, translate_heuristic, build_recovery_plan,
    pick_alternative_engines, run_recovery, RecoveryResult,
    _result_has_query_signal,
)
from fact_align import align_facts, extract_facts  # noqa: E402
from search import local_five_dim_rerank  # noqa: E402
from route import route_query, extract_features  # noqa: E402
from engines import available_engines, get_registry  # noqa: E402
from config import get_engines, load_config, get_domains  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# P0-001 查询理解
# ═══════════════════════════════════════════════════════════════════════════════

class TestQueryUnderstanding(unittest.TestCase):
    def test_negation_chinese(self):
        excl, clean = parse_negation("除了百度的搜索引擎")
        self.assertIn("百度", excl)
        self.assertNotIn("百度", clean)

    def test_negation_variants(self):
        self.assertIn("广告", parse_negation("不想要广告的内容")[0])
        self.assertIn("广告", parse_negation("不要广告")[0])
        self.assertIn("baidu", [e.lower() for e in parse_negation("搜索引擎 排除baidu")[0]])
        self.assertIn("jquery", [e.lower() for e in parse_negation("React without jQuery")[0]])
        self.assertIn("ads", [e.lower() for e in parse_negation("news NOT ads")[0]])

    def test_negation_dash(self):
        excl, _ = parse_negation("小米 SU7 口碑 -广告")
        self.assertIn("广告", excl)

    def test_geo_trigger(self):
        geo = parse_geo("附近医院")
        self.assertIsNotNone(geo)
        self.assertTrue(geo["has_geo"])
        self.assertEqual(geo["trigger"], "附近")

    def test_geo_city(self):
        geo = parse_geo("北京周边亲子游")
        self.assertIsNotNone(geo)
        self.assertEqual(geo["city"], "北京")

    def test_geo_none(self):
        self.assertIsNone(parse_geo("Python 教程"))

    def test_intents(self):
        self.assertIn("compare", classify_intents("React vs Vue 哪个好"))
        self.assertIn("definition", classify_intents("什么是 Transformer"))
        self.assertIn("news", classify_intents("英伟达最新进展"))
        self.assertIn("social", classify_intents("小米SU7 车主口碑"))

    def test_multi_intent_split(self):
        splits = split_multi_intent("上海周边亲子游 以及 露营地推荐")
        self.assertEqual(len(splits), 2)

    def test_multi_intent_no_split_short(self):
        # 并列词但一段太短 → 不拆
        self.assertEqual(split_multi_intent("茶 和 咖啡"), [])

    def test_understand_returns_dataclass(self):
        qu = understand("除了百度的搜索引擎")
        self.assertIsInstance(qu, QueryUnderstanding)
        self.assertIn("百度", qu.exclude_terms)
        self.assertGreater(qu.confidence, 0)
        self.assertIn("exclude_terms", qu.to_dict())

    def test_understand_type_error(self):
        with self.assertRaises(TypeError):
            understand(None)  # type: ignore


# ═══════════════════════════════════════════════════════════════════════════════
# P0-005 route 特征 + 动态并行度
# ═══════════════════════════════════════════════════════════════════════════════

class TestRouteFeaturesAndParallel(unittest.TestCase):
    def test_features_have_geo_negation_intents(self):
        f = extract_features("附近医院 除了私立")
        self.assertTrue(f["has_geo"])
        self.assertTrue(f["has_negation"])
        self.assertIn("intents", f)

    def test_geo_adds_openstreetmap(self):
        d = route_query("附近医院")
        combo = d.get("engines_combo", [])
        self.assertIn("local_openstreetmap", combo)

    def test_definition_single_engine_serial(self):
        d = route_query("什么是 Transformer")
        self.assertFalse(d["parallel"])
        self.assertEqual(len(d["engines_combo"]), 1)

    def test_compare_multi_engine_parallel(self):
        d = route_query("React vs Vue 对比")
        self.assertTrue(d["parallel"])
        self.assertGreaterEqual(len(d["engines_combo"]), 2)


# ═══════════════════════════════════════════════════════════════════════════════
# P0-002 恢复决策树
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecovery(unittest.TestCase):
    def test_relax(self):
        out = relax_query('"贵州茅台" 2025 最新财报 详细')
        self.assertNotIn('"', out)
        self.assertIn("贵州茅台", out)
        self.assertNotIn("最新", out)

    def test_synonym(self):
        out = synonym_expand("英伟达 财报")
        self.assertIsNotNone(out)
        self.assertIn("earnings", out)

    def test_synonym_miss(self):
        self.assertIsNone(synonym_expand("量子纠缠现象"))

    def test_translate_heuristic(self):
        out = translate_heuristic("React 教程")
        self.assertIsNotNone(out)
        self.assertIn("react", out.lower())

    def test_pick_alternative_engines(self):
        alt = pick_alternative_engines(["anysearch"], ["duckduckgo"], None)
        self.assertIn("duckduckgo", alt)
        self.assertNotIn("anysearch", alt)

    def test_pick_alternative_blocks_vertical_pollution(self):
        """垂直域空结果恢复不得拉 pypi/npm/jin10/crates 等无关源。"""
        tried = ["thesportsdb", "wikipedia"]
        enabled = {
            "thesportsdb", "wikipedia", "pypi", "npm", "jin10", "crates",
            "github", "anysearch", "duckduckgo", "local_bing", "wikidata", "imdb",
        }
        fallback = [e for e in sorted(enabled) if e not in tried]
        alt = pick_alternative_engines(tried, fallback, enabled, max_n=4)
        blocked = {"pypi", "npm", "jin10", "crates", "github", "imdb"}
        self.assertTrue(set(alt).isdisjoint(blocked), f"polluted picks: {alt}")
        # 通用源优先；同族 knowledge 可补 wikidata
        self.assertTrue(any(e in alt for e in ("anysearch", "duckduckgo", "local_bing")))
        self.assertIn("wikidata", alt)

    def test_pick_alternative_allows_same_family_code(self):
        """代码域已试 pypi 时，同族 npm 可作为恢复候选。"""
        alt = pick_alternative_engines(
            ["pypi"], ["npm", "jin10", "anysearch"],
            {"pypi", "npm", "jin10", "anysearch"}, max_n=3,
        )
        self.assertIn("anysearch", alt)
        self.assertIn("npm", alt)
        self.assertNotIn("jin10", alt)

    def test_plan_fast_no_l4(self):
        plan = build_recovery_plan("英伟达 财报", ["anysearch"], ["duckduckgo"], mode="fast")
        levels = {s.level for s in plan}
        self.assertNotIn("L4", levels)
        self.assertTrue(levels <= {"L1", "L2", "L3"})
        # fast 只保留 L1 + L3
        self.assertTrue(levels <= {"L1", "L3"})

    def test_plan_auto_has_l4(self):
        plan = build_recovery_plan("英伟达 财报", ["anysearch"], ["duckduckgo"], mode="auto")
        levels = {s.level for s in plan}
        self.assertIn("L4", levels)

    def test_run_recovery_success(self):
        def executor(q, engines):
            if "earnings" in q or "英伟达" in q or q == "英伟达 财报":
                return [{"title": "英伟达 earnings 财报", "url": "http://x", "source": "e"}]
            return []
        results, rec = run_recovery("英伟达 财报 请问", ["anysearch"], executor,
                                    engines_fallback=["duckduckgo"], mode="auto")
        self.assertTrue(rec.recovered)
        self.assertGreater(len(results), 0)

    def test_run_recovery_executor_error_isolated(self):
        def bad_executor(q, engines):
            raise RuntimeError("boom")
        results, rec = run_recovery("test", ["anysearch"], bad_executor, mode="auto")
        self.assertFalse(rec.recovered)
        self.assertTrue(any("executor-error" in s["outcome"] for s in rec.steps_tried))

    def test_result_query_signal_gate(self):
        self.assertTrue(_result_has_query_signal(
            "WHO headquarters",
            {"title": "WHO Headquarters", "snippet": "Geneva", "url": "who.int"},
        ))
        self.assertFalse(_result_has_query_signal(
            "WHO headquarters",
            {"title": "Chegg homework", "snippet": "study support", "url": "chegg.com"},
        ))

    def test_run_recovery_rejects_unrelated_hits(self):
        def junk_executor(q, engines):
            return [{"title": "Chegg homework help", "url": "https://chegg.com", "source": "x"}]
        results, rec = run_recovery(
            "WHO headquarters", ["wikidata"], junk_executor,
            engines_fallback=["anysearch"], mode="auto",
        )
        self.assertFalse(rec.recovered)
        self.assertEqual(results, [])


# ═══════════════════════════════════════════════════════════════════════════════
# P0-003 本地五维 rerank
# ═══════════════════════════════════════════════════════════════════════════════

class TestFiveDimRerank(unittest.TestCase):
    def _sample(self):
        return [
            {"title": "Python asyncio 完整教程 2025", "snippet": "详解 async await 事件循环 20 例",
             "url": "https://docs.python.org/asyncio", "source": "docs"},
            {"title": "asyncio 简介", "snippet": "简短", "url": "https://blog.x/a", "source": "blog"},
            {"title": "asyncio 完整教程 2025", "snippet": "详解 async await 事件循环 20 例",
             "url": "https://mirror.y/a", "source": "mirror"},
        ]

    def test_returns_scored_and_dims(self):
        out = local_five_dim_rerank("Python asyncio 教程", self._sample())
        self.assertEqual(len(out), 3)
        for r in out:
            self.assertIn("rerank_dims", r)
            for k in ("relevance", "authority", "freshness", "completeness", "novelty"):
                self.assertIn(k, r["rerank_dims"])

    def test_sorted_desc(self):
        out = local_five_dim_rerank("Python asyncio 教程", self._sample())
        scores = [r["score"] for r in out]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_novelty_penalizes_duplicate(self):
        out = local_five_dim_rerank("Python asyncio 教程", self._sample())
        # 近重复的 mirror 标题在靠后位置 novelty 应 <1
        novelties = [r["rerank_dims"]["novelty"] for r in out]
        self.assertTrue(min(novelties) < 1.0)

    def test_tech_domain_weights(self):
        # tech 域不应崩溃且返回全量
        out = local_five_dim_rerank("asyncio", self._sample(), domain="tech_deep")
        self.assertEqual(len(out), 3)

    def test_empty(self):
        self.assertEqual(local_five_dim_rerank("q", []), [])


# ═══════════════════════════════════════════════════════════════════════════════
# P0-004 事实交叉标记
# ═══════════════════════════════════════════════════════════════════════════════

class TestFactAlign(unittest.TestCase):
    def test_extract_facts(self):
        facts = dict(extract_facts("Python 3.12 提速 5% 发布于 2023-10-02 营收120亿元"))
        self.assertEqual(facts.get("version"), "3.12")
        self.assertEqual(facts.get("percent"), "5%")
        self.assertEqual(facts.get("date"), "2023-10-02")

    def test_corroboration(self):
        results = [
            {"title": "A", "snippet": "提速 5%", "url": "https://a.com/x"},
            {"title": "B", "snippet": "提速 5%", "url": "https://b.com/y"},
            {"title": "C", "snippet": "无关内容", "url": "https://c.com/z"},
        ]
        out = align_facts(results, mode="auto", depth="deep")
        self.assertIsNotNone(out)
        corr = {(c["type"], c["value"]) for c in out["fact_corroborated"]}
        self.assertIn(("percent", "5%"), corr)

    def test_conflict(self):
        results = [
            {"title": "A", "snippet": "版本 v1.2", "url": "https://a.com/x"},
            {"title": "B", "snippet": "版本 v2.0", "url": "https://b.com/y"},
            {"title": "C", "snippet": "版本 v1.2", "url": "https://c.com/z"},
        ]
        out = align_facts(results, mode="auto", depth="deep")
        self.assertIsNotNone(out)
        conflict_types = {c["type"] for c in out["fact_conflicts"]}
        self.assertIn("version", conflict_types)

    def test_fast_mode_skips(self):
        results = [{"title": "A", "snippet": "5%", "url": "https://a.com"}] * 3
        self.assertIsNone(align_facts(results, mode="fast", depth="fast"))

    def test_below_threshold_skips(self):
        results = [{"title": "A", "snippet": "5%", "url": "https://a.com"}]
        self.assertIsNone(align_facts(results, min_results=3, mode="auto", depth="deep"))


# ═══════════════════════════════════════════════════════════════════════════════
# 新引擎注册 + 新域路由
# ═══════════════════════════════════════════════════════════════════════════════

class TestNewEngines(unittest.TestCase):
    NEW = ("finviz", "seeking_alpha", "qweather", "wenshu", "jin10")

    def test_config_enabled(self):
        load_config(force=True)
        engines = get_engines()
        for name in self.NEW:
            self.assertIn(name, engines, f"config 缺少 {name}")
            self.assertTrue(engines[name].get("enabled"))

    def test_registry_available(self):
        avail = set(available_engines())
        for name in self.NEW:
            self.assertIn(name, avail, f"registry 缺少 {name}")

    def test_engine_returns_list_on_missing_key(self):
        # qweather 无 key 时返回显式 error 项而非崩溃
        from engines import search as engine_search
        import os
        old = os.environ.pop("QWEATHER_KEY", None)
        try:
            res = engine_search("北京天气", "qweather", n=1, timeout=3)
            self.assertIsInstance(res, list)
        finally:
            if old is not None:
                os.environ["QWEATHER_KEY"] = old


class TestNewDomains(unittest.TestCase):
    def test_us_stock_domain(self):
        d = route_query("AAPL 美股 盘前")
        self.assertEqual(d["domain"], "us_stock")
        self.assertIn("finviz", d["engines_combo"])

    def test_weather_domain(self):
        d = route_query("北京天气预报")
        self.assertEqual(d["domain"], "weather_query")
        # qweather 需 API key 才进 routable；无 key 时回退 duckduckgo 等通用源
        from config import get_engines, load_config
        try:
            enabled = get_engines(load_config(), routable_only=True)
        except TypeError:
            enabled = get_engines(load_config())
        if "qweather" in enabled:
            self.assertEqual(d["engine"], "qweather")
        else:
            self.assertIn(d["engine"], ("duckduckgo", "anysearch", "byted", "local_bing"))

    def test_medical_domain(self):
        d = route_query("高血压 症状 用药 临床指南")
        self.assertEqual(d["domain"], "medical")

    def test_legal_domain(self):
        d = route_query("刑法 判例 司法解释")
        self.assertEqual(d["domain"], "legal")

    def test_domains_valid_config(self):
        # 所有新域在 config 中可解析
        names = {dom.get("name") for dom in get_domains()}
        for n in ("us_stock", "weather_query", "medical", "legal", "wenshu_query", "jin10_flash"):
            self.assertIn(n, names)


if __name__ == "__main__":
    unittest.main(verbosity=2)

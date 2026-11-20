#!/usr/bin/env python3
"""test_query_operators.py — 查询操作符契约层的回归锁（离线）。

借鉴 DeepSeek Search 的操作符透传语义（api-docs.deepseek.com/zh-cn/guides/
search），落地为 Argo 的解析层：Google 风格查询里的指令从路由与检索两侧
剥掉，结构化进 operators（v1 消费：langs→路由语言覆盖、after/before→时间
窗兜底、exclude_terms→融合后过滤；sites 记入可观测，按引擎下推是后续增量）。

本文件锁三层：
  1. 解析器：八类操作符、多站点 OR、词内连字符、裸 OR 不误伤、无操作符零改动；
  2. 路由隔离：site: 不再把路由锁死进错误域（实测前 `DeepSeek site:github.com`
     → code_search，应为 ai_model）；
  3. prepare 汇合：-词的排除项并入 query_understanding 的 exclude_terms。

运行：
    python3 -m pytest tests/test_query_operators.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from query_operators import parse_query_operators, filter_results_by_sites, operator_cache_suffix  # noqa: E402


class TestParser(unittest.TestCase):
    """八类操作符的解析契约。"""

    def test_site_positive(self):
        clean, ops = parse_query_operators("DeepSeek site:github.com")
        self.assertEqual(clean, "DeepSeek")
        self.assertEqual(ops["sites"], ["github.com"])

    def test_site_exclude(self):
        clean, ops = parse_query_operators("DeepSeek -site:example.com")
        self.assertEqual(clean, "DeepSeek")
        self.assertEqual(ops["exclude_sites"], ["example.com"])

    def test_multi_site_or_connector_consumed(self):
        clean, ops = parse_query_operators(
            "DeepSeek site:github.com OR site:huggingface.co lang:zh after:2025")
        self.assertEqual(clean, "DeepSeek")
        self.assertEqual(ops["sites"], ["github.com", "huggingface.co"])
        self.assertEqual(ops["langs"], ["zh"])
        self.assertEqual(ops["after"], "2025")

    def test_exclude_term_with_inner_hyphen(self):
        clean, ops = parse_query_operators("GPT-4o -gpt-3")
        self.assertEqual(clean, "GPT-4o")
        self.assertEqual(ops["exclude_terms"], ["gpt-3"])

    def test_exclude_phrase(self):
        clean, ops = parse_query_operators('DeepSeek -"release notes"')
        self.assertEqual(clean, "DeepSeek")
        self.assertEqual(ops["exclude_terms"], ["release notes"])

    def test_exact_phrase_stays_in_clean(self):
        """「"短语"」是意图本体不是指令——留在 clean_query 供 phrase_filter。"""
        clean, ops = parse_query_operators('DeepSeek "精确短语"')
        self.assertEqual(clean, 'DeepSeek "精确短语"')
        self.assertEqual(ops["exclude_terms"], [])

    def test_lang_multi(self):
        clean, ops = parse_query_operators("模型 lang:zh,en")
        self.assertEqual(clean, "模型")
        self.assertEqual(ops["langs"], ["zh", "en"])

    def test_date_window(self):
        clean, ops = parse_query_operators("茅台 after:2026-01-01 before:2026-06-01")
        self.assertEqual(clean, "茅台")
        self.assertEqual(ops["after"], "2026-01-01")
        self.assertEqual(ops["before"], "2026-06-01")

    def test_date_year_only(self):
        _, ops = parse_query_operators("模型 after:2025")
        self.assertEqual(ops["after"], "2025")

    def test_no_operator_untouched(self):
        q = "普通查询无操作符"
        clean, ops = parse_query_operators(q)
        self.assertEqual(clean, q)
        self.assertFalse(any([ops["sites"], ops["exclude_sites"],
                              ops["exclude_terms"], ops["langs"],
                              ops["after"], ops["before"], ops["dropped"]]))

    def test_bare_or_preserved(self):
        """查询自身的布尔 OR（非站点连接词）必须原样保留。"""
        clean, _ = parse_query_operators("Rust OR Go 后端选型")
        self.assertEqual(clean, "Rust OR Go 后端选型")

    def test_spaced_dash_not_exclusion(self):
        """空格-空格是分隔符不是排除号（Google 语义只认紧贴的 -）。"""
        clean, ops = parse_query_operators("python - django tutorial")
        self.assertEqual(clean, "python - django tutorial")
        self.assertEqual(ops["exclude_terms"], [])

    def test_empty_and_garbage(self):
        self.assertEqual(parse_query_operators(""), ("", parse_query_operators("")[1]))
        clean, ops = parse_query_operators("!!!")
        self.assertEqual(clean, "!!!")
        self.assertEqual(ops["dropped"], [])


class TestRoutingIsolation(unittest.TestCase):
    """site: 不再把路由锁死进错误域（解析层存在的理由）。"""

    def test_site_does_not_hijack_domain(self):
        from route import route_query
        hijacked = route_query("DeepSeek site:github.com", mode="auto").get("domain")
        clean_side = route_query("DeepSeek", mode="auto").get("domain")
        self.assertEqual(hijacked, "code_search",
                         "对照组：site: 形态今天以前确实锁进 code_search")
        self.assertEqual(clean_side, "ai_model",
                         "剥掉 site: 后应回自然域（ai_model）")
        self.assertNotEqual(hijacked, clean_side)

    def test_lang_override_changes_combo(self):
        from route import route_query
        d = route_query("茅台 股价 行情", mode="auto", lang_override=["ja"])
        feats = d.get("features") or {}
        self.assertEqual(feats.get("primary_lang"), "ja")
        # ja 覆盖应换到 ja 友好组合（hatena 等在列）
        self.assertIn("hatena_bookmark", d.get("engines_combo") or [])

    def test_lang_override_bypasses_route_cache(self):
        """lang_override 改变决策本身，不能与无覆盖查询共享缓存键。"""
        from route import route_query_cached
        d = route_query_cached("茅台 股价 行情", mode="auto", lang_override=["en"])
        self.assertNotEqual(d.get("route_cached"), True,
                            "带 lang_override 的决策不得来自磁盘缓存")


class TestPrepareMergesExcludeTerms(unittest.TestCase):
    """prepare 把 -词的排除项并入 query_understanding 的通道。"""

    def test_operator_exclude_merged(self):
        from search_entry import prepare
        from cache import SearchCache
        cache = SearchCache(db_path=":memory:")
        decision = {
            "domain": "general", "engine": "auto",
            "engines_combo": ["anysearch"], "parallel": False,
            "operators": {"exclude_terms": ["广告", "推广"]},
        }
        prepared = prepare("贵州茅台 股价 -广告", decision, 5, 8, "balanced",
                           cache, skip_cache=True,
                           hooks=__import__("search_entry")._SearchHooks(
                               engine_search=lambda *a, **k: [],
                               available_engines=lambda: ["anysearch"],
                               get_cost_factor=lambda e: 1.0,
                               get_engines=lambda **k: {"anysearch": {}},
                               get_execution_config=lambda: {},
                               missing_env_for=lambda e: [],
                               classify_outcome=lambda *a, **k: {},
                               note_quota_exhausted=lambda *a, **k: None,
                               per_engine_budget_s=8.0, fast_budget_s=6.0,
                               auto_budget_s=10.0, deep_budget_s=15.0,
                               primary_grace_s=1.0, straggler_grace_s=0.5,
                               straggler_tail_fn=lambda e: 0.5,
                               serial_stagger_s=0.25))
        from search_entry import _SearchRequest  # noqa: F401
        # prepare 的 exclude_terms 落在请求对象上（noise 过滤的输入）
        req = prepared.req if hasattr(prepared, "req") else prepared
        excl = list(getattr(req, "exclude_terms", None) or [])
        for t in ("广告", "推广"):
            self.assertIn(t, excl, f"操作符排除词 {t} 应并入 exclude_terms")


class TestSiteFilter(unittest.TestCase):
    """site:/-site: 后置过滤（2026-10-17 落地执行，不再只记录）。"""

    _RESULTS = [
        {"title": "gh", "url": "https://github.com/a"},
        {"title": "gist", "url": "https://gist.github.com/b"},
        {"title": "other", "url": "https://models.dev/c"},
        {"title": "card", "url": ""},
    ]

    def test_include_matches_domain_and_subdomain(self):
        kept, dropped = filter_results_by_sites(
            self._RESULTS, ["github.com"], [])
        self.assertEqual([r["title"] for r in kept], ["gh", "gist", "card"])
        self.assertEqual(dropped, 1)

    def test_exclude_removes_matching_host(self):
        kept, dropped = filter_results_by_sites(
            self._RESULTS, [], ["github.com"])
        self.assertEqual([r["title"] for r in kept], ["other", "card"])
        self.assertEqual(dropped, 2)

    def test_include_and_exclude_combined(self):
        kept, dropped = filter_results_by_sites(
            self._RESULTS, ["github.com"], ["gist.github.com"])
        self.assertEqual([r["title"] for r in kept], ["gh", "card"])
        self.assertEqual(dropped, 2)

    def test_no_operators_is_noop(self):
        kept, dropped = filter_results_by_sites(self._RESULTS, None, None)
        self.assertIs(kept, self._RESULTS)
        self.assertEqual(dropped, 0)


class TestOperatorCacheSuffix(unittest.TestCase):
    """操作符必须进缓存键，否则受限查询与无约束查询互串。"""

    def test_no_operators_is_empty(self):
        self.assertEqual(operator_cache_suffix(None), "")
        self.assertEqual(operator_cache_suffix({}), "")

    def test_site_and_exclusion_are_partitioned(self):
        _, ops = parse_query_operators("python -site:realpython.com")
        suffix = operator_cache_suffix(ops)
        self.assertIn("xsite=realpython.com", suffix)
        self.assertNotEqual(suffix, operator_cache_suffix({}))

    def test_exclude_terms_and_langs(self):
        _, ops = parse_query_operators("AI -广告 lang:zh,en")
        suffix = operator_cache_suffix(ops)
        self.assertIn("not=广告", suffix)
        self.assertIn("lang=zh,en", suffix)

    def test_after_before_not_duplicated(self):
        _, ops = parse_query_operators("AI after:2026")
        self.assertEqual(operator_cache_suffix(ops), "",
                         "时间窗由 since/until 维度负责，不重复进后缀")

    def test_country_is_partitioned(self):
        _, ops = parse_query_operators("solar policy country:de")
        self.assertIn("cc=DE", operator_cache_suffix(ops))


class TestCountryAndProviderPushdown(unittest.TestCase):
    """country:/region: 指令与四大高质引擎原生参数下推契约。"""

    def test_country_directive_parsing_and_invalid_stripped(self):
        clean, ops = parse_query_operators("solar subsidies country:de")
        self.assertEqual(clean, "solar subsidies")
        self.assertEqual(ops["country"], "DE")

        clean2, ops2 = parse_query_operators("fed rate region:united states")
        self.assertEqual(clean2, "fed rate")
        self.assertEqual(ops2["country"], "US")

        clean3, ops3 = parse_query_operators("test country:marsland")
        self.assertEqual(clean3, "test")
        self.assertIsNone(ops3["country"])
        self.assertTrue(any("country:marsland" in d for d in ops3["dropped"]))

    def test_compile_provider_params_all_four(self):
        from query_operators import (
            compile_provider_params,
            format_exa_notices,
            is_evergreen_tech_query,
        )
        ops = {
            "sites": ["arxiv.org"],
            "exclude_sites": ["medium.com"],
            "langs": ["en"],
            "country": "US",
        }
        exa = compile_provider_params(
            "exa", "transformer benchmark paper",
            operators=ops, since="2026-01-01", until="2026-06-01",
        )
        self.assertEqual(exa["category"], "publication")
        self.assertEqual(exa["includeDomains"], ["arxiv.org"])
        self.assertEqual(exa["excludeDomains"], ["medium.com"])
        self.assertEqual(exa["startPublishedDate"], "2026-01-01T00:00:00.000Z")
        self.assertEqual(exa["endPublishedDate"], "2026-06-01T23:59:59.999Z")
        self.assertEqual(exa["userLocation"], "US")

        tavily = compile_provider_params(
            "tavily", "NVDA earnings revenue",
            operators={"sites": ["sec.gov"], "country": "US"},
            since="2026-01-01",
        )
        self.assertEqual(tavily["topic"], "finance")
        self.assertEqual(tavily["include_domains"], ["sec.gov"])
        self.assertEqual(tavily["start_date"], "2026-01-01")
        self.assertEqual(tavily["country"], "united states")

        parallel = compile_provider_params(
            "parallel", "Rust async runtime",
            operators={"sites": ["github.com"], "exclude_sites": ["spam.io"]},
            since="2026-02-01", until="2026-05-01",
        )
        self.assertEqual(parallel["source_policy"], {
            "include_domains": ["github.com"],
            "exclude_domains": ["spam.io"],
            "after_date": "2026-02-01",
            "before_date": "2026-05-01",
        })

        you = compile_provider_params(
            "you", "React docs",
            operators={"sites": ["react.dev"], "country": "GB", "langs": ["en"]},
        )
        self.assertEqual(you["boost_domains"], ["react.dev"])
        self.assertEqual(you["country"], "GB")
        self.assertEqual(you["language"], "en")

        self.assertTrue(is_evergreen_tech_query("latest React docs typescript"))
        self.assertFalse(is_evergreen_tech_query("今日 突发 新闻"))
        self.assertEqual(
            format_exa_notices([{"message": "Relaxed site filter"}, "Relaxed site filter"]),
            "Exa: Relaxed site filter",
        )


if __name__ == "__main__":
    unittest.main()

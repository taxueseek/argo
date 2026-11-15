#!/usr/bin/env python3
"""通用层选源排序（source_selection）的契约测试。

钉住三件事：
  1. 无自适应样本时，实测先验决定顺序（tavily/yuanbao 在 local_bing 前）；
  2. 有 ≥3 条样本时，自适应分能翻转先验（坏源被降下去）；
  3. 研究档把 purpose="research" 传下去（延迟悬崖 2s→8s 的入口）。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import general_pool  # noqa: E402
import source_selection  # noqa: E402
from adaptive import AdaptiveLearner  # noqa: E402
from source_selection import rank_general_candidates  # noqa: E402

_POOL = ["local_bing", "anysearch", "tavily_free", "yuanbao", "parallel_free"]


class TestGeneralRanking(unittest.TestCase):
    def test_priors_decide_when_no_samples(self):
        def score_fn(_e, _d, _l, _p):
            return 0.5, 0

        out = rank_general_candidates(
            _POOL, enabled=set(_POOL), mode="auto", depth="fast",
            context="search", score_fn=score_fn)
        self.assertEqual(out[0], "yuanbao")
        self.assertLess(out.index("tavily_free"), out.index("local_bing"))
        self.assertEqual(out[-1], "local_bing")

    def test_adaptive_flips_prior_with_enough_samples(self):
        def score_fn(engine, _d, _l, _p):
            if engine == "local_bing":
                return 1.0, 5
            if engine == "tavily_free":
                return 0.2, 5
            return 0.5, 0

        out = rank_general_candidates(
            _POOL, enabled=set(_POOL), mode="auto", depth="fast",
            context="search", score_fn=score_fn)
        self.assertLess(out.index("local_bing"), out.index("tavily_free"),
                        "有足够样本时，自适应分必须能推翻先验")

    def test_research_purpose_is_passed(self):
        seen: list[str] = []

        def score_fn(_e, _d, _l, purpose):
            seen.append(purpose)
            return 0.5, 0

        rank_general_candidates(
            _POOL, enabled=set(_POOL), mode="deep", depth="deep",
            context="search", score_fn=score_fn)
        self.assertEqual(set(seen), {"research"})

    def test_non_latin_script_prioritises_language_coverage(self):
        """西里尔/阿拉伯等脚本下，语言覆盖源必须留在 top-2。"""
        out = rank_general_candidates(
            _POOL, enabled=set(_POOL), mode="auto", depth="fast",
            context="search", lang="cyrillic",
            score_fn=lambda *a: (0.5, 0))
        self.assertTrue(
            {"anysearch", "local_bing"} & set(out[:2]),
            f"非拉丁脚本 top-2 缺语言覆盖源：{out}")

    def test_dead_high_prior_loses_to_healthy_lower_prior(self):
        """高先验但已证实失败的源，必须让位给健康源（加法混合，不是乘法）。

        2026-10-27 起覆盖地板把 local_bing（实测交付 2 条）挡在领队外，
        这里改用同一层内的任何合格源（parallel_free）验证「自适应能翻转
        先验」，local_bing 的落败由 test_thin_coverage_engine_never_leads 锁。
        """
        def score_fn(engine, _d, _l, _p):
            return {"yuanbao": (0.0, 5), "parallel_free": (1.0, 5)}.get(
                engine, (0.5, 0))

        out = rank_general_candidates(
            _POOL, enabled=set(_POOL), mode="auto", depth="fast",
            context="search", score_fn=score_fn)
        self.assertLess(out.index("parallel_free"), out.index("yuanbao"))

    def test_empty_pool_returns_empty(self):
        self.assertEqual(rank_general_candidates(
            [], enabled=set(), score_fn=lambda *a: (0.5, 0)), [])


class TestResearchLatencyCliff(unittest.TestCase):
    """研究档把延迟悬崖从 2s 抬到 8s——这条判据必须有直接测试。"""

    def test_research_scores_slow_source_higher(self):
        learner = AdaptiveLearner.__new__(AdaptiveLearner)
        row = (10, 10, 10, 4000.0, 0.0, 0.8, 8.0, time.time())
        daily = learner._score_from_row(row, "daily")
        research = learner._score_from_row(row, "research")
        self.assertLess(daily, research,
                        "4s 的源在日常档应被 2s 悬崖压住，研究档不该")


class TestGeneralOrderPinning(unittest.TestCase):
    """逃生门的边界：拼错引擎名不得让动态排序静默失效。"""

    def test_unknown_lead_does_not_pin(self):
        with patch.object(
                general_pool, "_get_env",
                side_effect=lambda name, default="": (
                    "garbage" if name.endswith("LEAD") else "")), \
                patch.object(general_pool, "_env_flag", return_value=False):
            _order, pinned = general_pool.general_order()
        self.assertFalse(pinned)

    def test_known_lead_pins(self):
        with patch.object(
                general_pool, "_get_env",
                side_effect=lambda name, default="": (
                    "anysearch" if name.endswith("LEAD") else "")), \
                patch.object(general_pool, "_env_flag", return_value=False):
            order, pinned = general_pool.general_order()
        self.assertTrue(pinned)
        self.assertEqual(order[0], "anysearch")

    def test_excluded_engines_are_not_reintroduced(self):
        """配置池排除的 local_baidu/wikipedia 不得由兜底分支放回。"""
        with patch.dict(os.environ, {"ARGO_PAID_SEARCH": "off"}):
            out = general_pool.ranked_general(
                {"local_baidu", "wikipedia"}, score_fn=lambda *a: (0.5, 0))
        self.assertEqual(out, [])


class TestPaidGeneralCandidate(unittest.TestCase):
    """DeepSeek 作为预算内付费候选：开关/预算/模式三道门。"""

    def test_included_when_allowed(self):
        with patch.object(source_selection, "_paid_candidate_allowed",
                          return_value=True):
            out = rank_general_candidates(
                ["yuanbao", "deepseek_search"], enabled={"yuanbao"},
                score_fn=lambda *a: (0.5, 0))
        self.assertIn("deepseek_search", out)

    def test_excluded_when_paid_off(self):
        with patch.dict(os.environ, {"ARGO_PAID_SEARCH": "off"}):
            out = rank_general_candidates(
                ["yuanbao", "deepseek_search"], enabled={"yuanbao"},
                score_fn=lambda *a: (0.5, 0))
        self.assertNotIn("deepseek_search", out)

    def test_never_in_fast_mode(self):
        with patch.dict(os.environ, {"ARGO_PAID_SEARCH": "auto"}):
            out = rank_general_candidates(
                ["yuanbao", "deepseek_search"], enabled={"yuanbao"},
                mode="fast", score_fn=lambda *a: (0.5, 0))
        self.assertNotIn("deepseek_search", out)


if __name__ == "__main__":
    unittest.main()


class TestCoverageFloorAndLanguageGate(unittest.TestCase):
    """2026-10-27（issue #23）：交付覆盖地板 + 语言门。

    两个真实失败：无 key 环境里 tavily_free（常态 2 条）当上领队，nightly
    条数 5→2；有 key 环境里 yuanbao（只声明 chinese）给英文查询领跑，
    5 条全是中文长摘要，体积翻倍。排序必须自己避开这两种源。
    """

    def _rank(self, pool, lang, mode="auto", depth="fast"):
        return rank_general_candidates(
            pool, enabled=set(pool), mode=mode, depth=depth,
            context="search", lang=lang, score_fn=lambda *a: (0.5, 0))

    def test_english_query_demotes_chinese_only_engine(self):
        """en 查询不让「只声明 chinese」的源领跑（yuanbao → 层 1）。"""
        out = self._rank(_POOL, "en")
        self.assertNotEqual(out[0], "yuanbao",
                            f"英文查询仍由中文语料源领跑：{out}")
        self.assertEqual(out[0], "parallel_free", out)

    def test_chinese_query_keeps_yuanbao_lead(self):
        """zh 查询 yuanbao 仍领跑：10-26 的 489ms 收益不因语言门回退。"""
        out = self._rank(_POOL, "zh")
        self.assertEqual(out[0], "yuanbao", out)

    def test_thin_coverage_engine_never_leads(self):
        """tavily_free 常态 2 条（低于地板 3），任何语言都不当领队。"""
        for lang in ("en", "zh", ""):
            out = self._rank(_POOL, lang)
            self.assertNotEqual(out[0], "tavily_free", f"lang={lang}: {out}")

    def test_keyless_pool_keeps_coverage(self):
        """无 key 环境（模拟 CI）：yuanbao 不在 enabled，领队必须交够数。"""
        pool = [e for e in _POOL if e != "yuanbao"]
        out = rank_general_candidates(
            pool, enabled=set(pool), mode="auto", depth="fast",
            context="search", lang="en", score_fn=lambda *a: (0.5, 0))
        self.assertIn(out[0], {"parallel_free", "anysearch"}, out)

    def test_non_latin_script_skips_tiers(self):
        """非中英脚本不参与分层：语言覆盖源仍按既有 boost 排在 top-2。"""
        out = self._rank(_POOL, "cyrillic")
        self.assertTrue({"anysearch", "local_bing"} & set(out[:2]), out)

    def test_european_langs_demote_chinese_only_and_prefer_multilingual(self):
        """es/de 等明确非中文语种查询不应让仅声明 chinese 的 yuanbao 领跑。"""
        for lang in ("es", "de"):
            out = self._rank(["yuanbao", "parallel_free"], lang)
            self.assertEqual(out[0], "parallel_free", f"lang={lang}: {out}")

    def test_intent_differentiated_general_ranking(self):
        """同语言层下，news 意图应把 anysearch 推到 firecrawl 前面；compare 保持结构源在前。"""
        base = rank_general_candidates(
            ["firecrawl", "anysearch"],
            enabled={"firecrawl", "anysearch"},
            mode="auto", depth="fast", context="search", lang="en",
            score_fn=lambda *a: (0.5, 0),
        )
        news = rank_general_candidates(
            ["firecrawl", "anysearch"],
            enabled={"firecrawl", "anysearch"},
            mode="auto", depth="fast", context="search", lang="en",
            intents=["news"], score_fn=lambda *a: (0.5, 0),
        )
        self.assertEqual(base[0], "firecrawl", base)
        self.assertEqual(news[0], "anysearch", news)

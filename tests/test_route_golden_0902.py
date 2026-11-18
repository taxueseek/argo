#!/usr/bin/env python3
"""P1 金标回归门（2026-09-02）：fast 早停冗余守卫 + 路由结构金标钉。

背景（重定义证据链）：
  上午实测 fast 模式英文技术查询只执行 anysearch 单引擎且结果全不相关；
  逐层复现后定位真问题——串行路径「任一引擎有结果即 break」，首引擎上游
  波动返回高计数垃圾时早停吞掉次引擎，无第二意见。auto 因跑满 combo 有
  RRF 融合而幸存。

修复：
  search.py 新增 _query_coverage_ok（查询-结果词面覆盖守卫）接入
  _sufficient_internal（wave-1/wave-2）与串行 break；只影响「是否早停」，
  不丢弃结果，最坏代价多跑一个引擎。

本文件三层检查（全部离线，mock 引擎）：
  1. 守卫单元：当日事故样本（mdn 垃圾）必须被判「不充分」；好结果不误伤；
     CJK fail-open；min_results 答案型语义不动。
  2. 执行集成：真实 route_query 决策 + mock 引擎，垃圾首引擎 → 次引擎
     被补跑且融合结果包含救援来源；好首引擎 → 早停保持（零成本不回退）。
  3. 路由金标钉：无域 fast 保底 combo 必含 ≥2 免费通用源等结构契约。

运行：
  python3 -m pytest tests/test_route_golden_0902.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from search import (  # noqa: E402
    _query_coverage_ok,
    _results_sufficient,
    execute_search,
)
from cache import SearchCache  # noqa: E402
from route import route_query  # noqa: E402


class _AllowAllBreaker:
    """测试用：关闭熔断/负缓存干扰（与 test_new_engines 同款）。"""

    def allow(self, eng: str) -> tuple[bool, str]:
        return True, "closed"

    def status(self, eng: str) -> dict:
        return {"state": "closed"}

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


# ── 事故样本（2026-09-02 上午真实输出节选，mdn 噪声）─────────────────────────

QUERY = "Crawl4AI pruning content filter extraction"

MORNING_GARBAGE = [
    {"title": "The structured clone algorithm",
     "url": "https://developer.mozilla.org/en-US/docs/Web/API/Structured_clone_algorithm",
     "snippet": "The structured clone algorithm copies complex JavaScript objects.",
     "source": "anysearch"},
    {"title": "Link macros",
     "url": "https://developer.mozilla.org/en-US/docs/MDN/Writing_guidelines/Page_structures/Links",
     "snippet": "MDN provides numerous macros to create always up-to-date links.",
     "source": "anysearch"},
    {"title": "<link> HTML external resource link element",
     "url": "https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/link",
     "snippet": "The <link> HTML element specifies relationships.",
     "source": "anysearch"},
    {"title": "GitHub Discussions",
     "url": "https://developer.mozilla.org/en-US/docs/MDN/Community/Discussions",
     "snippet": "GitHub Discussions is a collaborative communication forum.",
     "source": "anysearch"},
    {"title": "Using filter effects",
     "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/filter-effects",
     "snippet": "You have just hovered over a black-and-white or sepia image.",
     "source": "anysearch"},
]

GOOD_RESULTS = [
    {"title": "Fit Markdown with Pruning & BM25 - Crawl4AI documentation",
     "url": "https://docs.crawl4ai.com/core/fit-markdown/",
     "snippet": "PruningContentFilter removes noise based on link density and text density.",
     "source": "anysearch"},
    {"title": "crawl4ai/docs/md_v2/blog/releases/0.4.0.md at main - GitHub",
     "url": "https://github.com/unclecode/crawl4ai/blob/main/docs/md_v2/blog/releases/0.4.0.md",
     "snippet": "Introducing PruningContentFilter and BM25ContentFilter for extraction.",
     "source": "anysearch"},
]


# ── 事故样本（2026-10-17 实锤：查询词回声）────────────────────────────────────
# local_bing 把整句当单词去查，返回的是查询里「keyboard」这一个词的词典释义/
# 同主题页。与 09-02 那次的差别：结果**与查询有词面交集**（都含 keyboard），
# 于是「零交集」判据放行，早停照常收工、anysearch 被 cancelled，回声即成答案。

ECHO_QUERY = "keyboard switch lube service 2026"

ECHO_GARBAGE = [
    {"title": "Online Virtual Keyboard - Qwertia",
     "url": "https://qwertia.com/online-keyboard",
     "snippet": "Type online with a virtual keyboard, no download needed.",
     "source": "local_bing"},
    {"title": "Amazon.com: Keyboard",
     "url": "https://www.amazon.com/s?k=keyboard",
     "snippet": "Shop keyboards on Amazon. Free shipping on eligible orders.",
     "source": "local_bing"},
    {"title": "Computer keyboard - Wikipedia",
     "url": "https://en.wikipedia.org/wiki/Computer_keyboard",
     "snippet": "A computer keyboard is an input device modeled after the typewriter.",
     "source": "local_bing"},
    {"title": "On-Screen Keyboard for Windows - Comfort Software Group",
     "url": "https://www.comfort-software.com/on-screen-keyboard.html",
     "snippet": "On-Screen Keyboard is a virtual keyboard for Windows.",
     "source": "local_bing"},
    {"title": "Keyboard Tester - Check Your Keyboard Keys",
     "url": "https://keyboardtester.com/",
     "snippet": "Test every key on your keyboard with this online tool.",
     "source": "local_bing"},
]

ECHO_GOOD = [
    {"title": "How to Lube Mechanical Keyboard Switches With Krytox",
     "url": "https://example.com/lube-guide",
     "snippet": "Lubricating switch springs and stems reduces scratchiness.",
     "source": "anysearch"},
    {"title": "Switch Lube Service",
     "url": "https://kbdfans.com/switch-lube-service",
     "snippet": "Professional switch lubing service for mechanical keyboards.",
     "source": "anysearch"},
    {"title": "Mechanical Keyboard Switch Lubing & Modification",
     "url": "https://example.com/lubing-mods",
     "snippet": "A full guide to switch lubing, films and springs.",
     "source": "anysearch"},
]


# ── 1. 守卫单元 ───────────────────────────────────────────────────────────────

class TestQueryCoverageGuard(unittest.TestCase):
    def test_morning_garbage_not_sufficient_fast(self):
        """事故样本：5 条高计数 mdn 垃圾必须被判「不充分」（修复前 True）。"""
        self.assertFalse(_results_sufficient(
            MORNING_GARBAGE, mode="fast", query=QUERY))

    def test_morning_garbage_not_sufficient_auto(self):
        self.assertFalse(_results_sufficient(
            MORNING_GARBAGE, mode="auto", query=QUERY))

    def test_good_results_still_sufficient_fast(self):
        """好结果不误伤：早停成本语义保持。"""
        self.assertTrue(_results_sufficient(
            GOOD_RESULTS, mode="fast", query=QUERY))

    def test_cjk_char_level_guard(self):
        """CJK 字符级覆盖：零字符交集的垃圾同样拒早停；正常中文结果放行。"""
        garbage = [{"title": "完全无关的页面", "snippet": "随便什么内容",
                    "source": "anysearch"},
                   {"title": "另一条", "snippet": "内容", "source": "anysearch"}]
        self.assertFalse(_results_sufficient(
            garbage, mode="fast", query="苹果 手机 推荐"))
        good = [{"title": "苹果手机推荐榜单", "snippet": "2026 年最值得买的机型",
                 "source": "anysearch"},
                {"title": "苹果手机选购攻略", "snippet": "各型号对比与推荐",
                 "source": "anysearch"}]
        self.assertTrue(_results_sufficient(
            good, mode="fast", query="苹果 手机 推荐"))

    def test_empty_query_legacy_behavior(self):
        """无查询词（理论边界）→ 守卫放行，保持历史行为。"""
        self.assertTrue(_results_sufficient(
            GOOD_RESULTS, mode="fast", query=""))

    def test_min_results_answer_type_untouched(self):
        """答案型域 min_results 语义不动：1 条快照即够用（计数判定）。"""
        self.assertTrue(_results_sufficient(
            [{"title": "irrelevant title", "snippet": "body text",
              "source": "worldbank"}],
            mode="auto", min_results=1, query="china gdp 2024"))

    def test_query_echo_not_sufficient(self):
        """查询词回声（2026-10-17）：每条最多命中 1 个查询词 → 拒早停。

        这批结果与查询**有**词面交集（都含 keyboard），旧的「零交集」判据
        会放行；实锤里正是这一点让早停照常收工、anysearch 被 cancelled。
        """
        self.assertFalse(_results_sufficient(
            ECHO_GARBAGE, mode="auto", query=ECHO_QUERY))
        self.assertFalse(_results_sufficient(
            ECHO_GARBAGE, mode="fast", query=ECHO_QUERY))

    def test_multi_word_hits_not_blocked(self):
        """正常结果同时命中多个查询词 → 放行（守卫不误伤好结果）。"""
        self.assertTrue(_results_sufficient(
            ECHO_GOOD, mode="auto", query=ECHO_QUERY))
        self.assertTrue(_results_sufficient(
            ECHO_GOOD, mode="fast", query=ECHO_QUERY))

    def test_echo_rule_skipped_for_short_query(self):
        """查询词 <3 时不启用回声判据：短查询本就难以多词命中，拦它即误伤。"""
        self.assertTrue(_results_sufficient(
            [{"title": "Keyboard", "snippet": "keyboard info",
              "source": "local_bing"},
             {"title": "Keyboard layout", "snippet": "keyboard layout info",
              "source": "local_bing"},
             {"title": "Keyboard tester", "snippet": "test your keyboard",
              "source": "local_bing"}],
            mode="auto", query="keyboard"))
        self.assertTrue(_results_sufficient(
            [{"title": "Keyboard switch", "snippet": "keyboard switch info",
              "source": "local_bing"},
             {"title": "Keyboard switches", "snippet": "switches for keyboard",
              "source": "local_bing"},
             {"title": "Mechanical keyboard", "snippet": "keyboard details",
              "source": "local_bing"}],
            mode="auto", query="keyboard switch"))


# ── 2. 执行集成（真实路由决策 + mock 引擎）────────────────────────────────────

class TestEarlyStopRescueIntegration(unittest.TestCase):
    def _execute(self, query: str, decision: dict[str, Any], fake: Any) -> tuple[dict, list[str]]:
        calls: list[str] = []
        cache = SearchCache(db_path=":memory:")

        def _spy(query_: str, eng: str, **kwargs: Any) -> list:
            calls.append(eng)
            return fake(query_, eng, **kwargs)

        with (
            patch("search.engine_search", side_effect=_spy),
            patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
            patch("quota.get_quota_manager", return_value=MagicMock(**{
                "is_hard_down.return_value": False,
                "remote_exhausted_marks.return_value": {},
            })),
        ):
            out = execute_search(
                query, decision, max_results=5, timeout=5, depth="fast",
                cache=cache, skip_cache=True)
        return out, calls

    def test_garbage_primary_rescued_by_second_engine(self):
        """事故场景回放：anysearch 垃圾 → 守卫拒绝早停 → duckduckgo 补跑。

        组合在此钉死：本用例测的是救援语义，不是「路由器今天选谁」。
        通用兜底已改为按实测先验排序（2026-10-26），断言具体引擎名会让
        这条用例随源健康度漂移。
        """
        decision = route_query(QUERY, mode="fast", depth="fast", context="search")
        decision = {**decision, "engines": ["anysearch", "local_bing"],
                    "engines_combo": ["anysearch", "local_bing"]}

        def fake(_q: str, eng: str, **_k: Any) -> list:
            if eng == "anysearch":
                return MORNING_GARBAGE
            if eng == "local_bing":
                return GOOD_RESULTS
            return []

        out, calls = self._execute(QUERY, decision, fake)
        self.assertIn("local_bing", calls, "守卫应触发次引擎补跑")
        merged_titles = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertIn("Crawl4AI", merged_titles, "融合结果应包含救援来源")
        self.assertIn("local_bing", out.get("engines_used", []))

    def test_good_primary_keeps_early_stop(self):
        """控制组：首引擎好结果 → 早停保持，次引擎零调用（成本不回退）。"""
        decision = route_query(QUERY, mode="fast", depth="fast", context="search")
        decision = {**decision, "engines": ["anysearch", "local_bing"],
                    "engines_combo": ["anysearch", "local_bing"]}

        def fake(_q: str, eng: str, **_k: Any) -> list:
            return GOOD_RESULTS if eng == "anysearch" else []

        _out, calls = self._execute(QUERY, decision, fake)
        self.assertNotIn("local_bing", calls, "好结果不应触发补跑")
        self.assertEqual(calls[0], "anysearch")

    def test_wave_path_parallel_rescue(self):
        """并行 wave 路径同语义：垃圾主引擎 → wave-2 补跑次引擎。"""
        decision = route_query(QUERY, mode="fast", depth="fast", context="search")
        decision = {**decision, "parallel": True,
                    "engines": ["anysearch", "local_bing"],
                    "engines_combo": ["anysearch", "local_bing"]}

        def fake(_q: str, eng: str, **_k: Any) -> list:
            if eng == "anysearch":
                return MORNING_GARBAGE
            if eng == "local_bing":
                return GOOD_RESULTS
            return []

        out, calls = self._execute(QUERY, decision, fake)
        self.assertIn("local_bing", calls)
        self.assertIn("local_bing", out.get("engines_used", []))

    def test_echo_primary_rescued_by_second_engine(self):
        """实锤回放（2026-10-17）：local_bing 回声 → 守卫拒早停 → anysearch 补跑。

        修复前：回声结果与查询有词面交集（都含 keyboard），守卫放行 →
        local_bing 先到即收工，anysearch 被 cancelled，8 条回声成最终答案。
        """
        decision = route_query(ECHO_QUERY, mode="fast", depth="fast", context="search")
        # 组合钉死：本用例测回声守卫是否触发补跑，不测路由选源。
        decision = {**decision, "engines": ["local_bing", "anysearch"],
                    "engines_combo": ["local_bing", "anysearch"]}

        def fake(_q: str, eng: str, **_k: Any) -> list:
            if eng == "local_bing":
                return ECHO_GARBAGE
            if eng == "anysearch":
                return ECHO_GOOD
            return []

        out, calls = self._execute(ECHO_QUERY, decision, fake)
        self.assertIn("anysearch", calls, "回声结果应触发次引擎补跑")
        self.assertIn("anysearch", out.get("engines_used", []))
        merged = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertIn("Lube", merged, "融合结果应包含救援来源")

    def test_echo_output_triggers_recovery(self):
        """回声不得成为最终输出：唯一可用引擎是回声源时，恢复链必须换引擎。

        这是 2026-10-17 实锤的另一半——早停守卫只能保证「不丢弃更好的结果」，
        当 anysearch 超时、只剩 local_bing 回声时，得由恢复链换引擎兜底。
        """
        decision = route_query(ECHO_QUERY, mode="auto", depth="fast", context="search")
        # 场景钉桩：auto 组合随兜底/对冲位与引擎健康态漂移——v2.9.6 方案B 把
        # octen 放进对冲位后，干净状态目录下 combo 是 ['anysearch','octen']，
        # fake 给 octen 交足即收工、恢复链不触发，本用例红（全量里靠早前测试
        # 把 octen 记死才蒙混过关，属会话状态耦合不是契约）。本用例要测的是
        # 「唯一可用引擎是回声源时恢复链必须换引擎」，与路由器挑中谁无关，
        # 故把主源/次源钉成 anysearch + local_bing，恢复链的换引擎池不动。
        decision = {**decision,
                    "engines": ["anysearch", "local_bing"],
                    "engines_combo": ["anysearch", "local_bing"]}

        def fake(_q: str, eng: str, **_k: Any) -> list:
            if eng == "local_bing":
                return ECHO_GARBAGE   # 主源：整批查询词回声
            if eng == "anysearch":
                return []             # 次源：模拟超时/无结果
            return ECHO_GOOD          # 救援源（uapi/firecrawl/parallel_free…）

        out, calls = self._execute(ECHO_QUERY, decision, fake)
        titles = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertNotIn("Virtual Keyboard", titles,
                         "回声结果不得成为最终输出")
        self.assertTrue(out.get("recovery"), "应记录恢复链触发")
        self.assertNotEqual(out["recovery"].get("level_used"), "L1",
                            "放宽查询救不了「引擎选错」，必须到 L3 换引擎")


# ── 3. 路由金标钉（结构契约，与 matrix_search_eval ROUTE_MATRIX 增补同源）──────

class TestRouteGoldenPins(unittest.TestCase):
    def test_nodomain_fast_fallback_has_two_free_engines(self):
        """无域查询 fast 保底 combo 必含 ≥2 免费通用源（单引擎无冗余即事故温床）。

        route_query 把无模式命中归一化为 catch-all 域 general_search。
        """
        d = route_query(QUERY, mode="fast", depth="fast", context="search")
        self.assertEqual(d.get("domain"), "general_search")
        combo = d.get("engines_combo") or []
        # 免 key 通用源（2026-10-26 实测池：tavily/parallel/firecrawl 也是
        # 免 key；anysearch/uapi/local_bing 保留，避免随池子增删误红）。
        free_general = {"anysearch", "local_bing", "uapi",
                        "tavily_free", "parallel_free", "firecrawl"}
        self.assertGreaterEqual(
            len(free_general & set(combo)), 2,
            f"fast 兜底 combo 应含 ≥2 免费通用源，实际 {combo}")

    def test_english_tech_domain_hit(self):
        d = route_query("python asyncio tutorial", mode="fast", depth="fast", context="search")
        self.assertEqual(d.get("domain"), "english_tech")
        self.assertIn(d.get("engine"), ("octen", "anysearch", "duckduckgo"))

    def test_zh_food_domain_hit(self):
        d = route_query("附近好吃的本帮菜馆", mode="fast", depth="fast", context="search")
        self.assertEqual(d.get("domain"), "chinese_general")
        self.assertEqual((d.get("features") or {}).get("primary_lang"), "zh")


if __name__ == "__main__":
    unittest.main()

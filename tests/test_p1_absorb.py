"""tests/test_p1_absorb.py — P1 精华吸纳测试

P1-3 通用免费源清单唯一来源（route 保底 / recovery L3 同源，消除重复）
P1-2 统一健康度视图（engine_detail.runtime = 熔断 + 学习分聚合）
P1-1 多意图路由（match_domains 多命中 + 预算内次域补充，主域优先）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


class TestGeneralFreeSingleSource(unittest.TestCase):
    """P1-3：通用免费源清单唯一来源。

    2026-10-26 起默认路径按实测先验排序；本类守的是**静态逃生门**
    （ARGO_GENERAL_FALLBACK_STATIC=1）仍能逐位回到旧顺序。默认策略
    另由 TestGeneralFallbackConfigPool 与 test_source_selection_1026 覆盖。
    """

    def setUp(self):
        import os
        self._old_static = os.environ.get("ARGO_GENERAL_FALLBACK_STATIC")
        os.environ["ARGO_GENERAL_FALLBACK_STATIC"] = "1"

    def tearDown(self):
        import os
        if self._old_static is None:
            os.environ.pop("ARGO_GENERAL_FALLBACK_STATIC", None)
        else:
            os.environ["ARGO_GENERAL_FALLBACK_STATIC"] = self._old_static

    def test_recovery_uses_shared_constant(self):
        from engine_policy import GENERAL_FREE_FALLBACK
        from recovery import _GENERAL_FREE_COMBO
        self.assertEqual(tuple(_GENERAL_FREE_COMBO), GENERAL_FREE_FALLBACK)

    def test_route_fallback_general_first(self):
        from engine_policy import GENERAL_FREE_FALLBACK
        from route import _general_fallback
        # 取样集合从唯一来源推导，不写死名单：名单增删时这条守的是
        # 「通用源领队 + local_search 殿后 + 顺序与来源一致」这个契约，
        # 而不是某天的某个快照。领队者是 local_search 会踩 combo_budget=2 的
        # 截断（见 route_combo._general_fallback docstring 的实测）。
        enabled = {"local_search", "github"} | set(GENERAL_FREE_FALLBACK)
        fb = _general_fallback(enabled)
        # 领队取自唯一来源，不写死引擎名（2026-10-21 领队从 anysearch 换到 octen）。
        self.assertEqual(fb[0], GENERAL_FREE_FALLBACK[0])
        self.assertEqual(fb[:len(GENERAL_FREE_FALLBACK)], list(GENERAL_FREE_FALLBACK))
        self.assertEqual(fb[-1], "local_search")
        self.assertNotIn("local_search", fb[:2],
                         "预算截断后 local_search 不得进入前 2 位")
        self.assertNotIn("github", fb)

    def test_route_fallback_filters_disabled(self):
        from route import _general_fallback
        # 禁用项剔除，启用项保持清单顺序且 local_search 殿后。
        fb = _general_fallback({"local_search", "anysearch"})
        self.assertEqual(fb, ["anysearch", "local_search"])

    def test_route_fallback_local_search_respects_enabled(self):
        """local_search 不在 enabled（未注册/被禁）时不得被兜底选中。"""
        from route import _general_fallback
        fb = _general_fallback({"anysearch", "local_bing"})
        self.assertEqual(fb, ["anysearch", "local_bing"])
        self.assertNotIn("local_search", fb)

    def test_route_fallback_local_survives_when_general_gone(self):
        """最坏面：通用源全部不可用时，本地检索仍是最后一道保底。"""
        from route import _general_fallback
        self.assertEqual(_general_fallback({"local_search"}), ["local_search"])


class TestGeneralFallbackConfigPool(unittest.TestCase):
    """默认路径：配置池 + 实测排序（不是静态元组）。"""

    def test_config_pool_is_used_and_local_search_is_last(self):
        import os
        from unittest.mock import patch
        from route import _general_fallback
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARGO_GENERAL_FALLBACK_STATIC", None)
            os.environ.pop("ARGO_GENERAL_FALLBACK_LEAD", None)
            os.environ.pop("ARGO_GENERAL_FALLBACK_LEGACY", None)
            enabled = {"yuanbao", "tavily_free", "parallel_free", "firecrawl",
                       "anysearch", "uapi", "local_bing", "local_search"}
            fb = _general_fallback(enabled)
        self.assertEqual(fb[-1], "local_search")
        self.assertNotIn("local_search", fb[:2])
        # 池子里不再有已证实零交付/非通用搜索的源（octen 保留在池尾：
        # 额度耗尽由配额层跳过，不因单机余额为空从公共配置删除）
        for dead in ("local_baidu", "wikipedia"):
            self.assertNotIn(dead, fb)


class TestRuntimeHealthView(unittest.TestCase):
    """P1-2：统一健康度视图（engine_detail.runtime）"""

    def test_engine_detail_has_runtime(self):
        from engine_status import engine_detail
        d = engine_detail("hackernews")
        self.assertIn("runtime", d)
        self.assertIn("breaker", d["runtime"])
        self.assertIn("adaptive_score", d["runtime"])
        self.assertIn("state", d["runtime"]["breaker"])

    def test_list_detail_batch_includes_runtime(self):
        from engine_status import list_engines_detail
        rows = list_engines_detail(routable_only=True)
        self.assertTrue(all("runtime" in r for r in rows))


class TestMultiIntentRoute(unittest.TestCase):
    """P1-1：多意图路由（match_domains + 预算内补充）"""

    def test_match_domains_multi_hit(self):
        from route import match_domains
        hits = match_domains("茅台 股价 财报 分红")
        names = [d.get("name") for d in hits]
        self.assertIn("stock_query", names)
        self.assertGreaterEqual(len(hits), 2)

    def test_match_domains_single_hit(self):
        from route import match_domains
        hits = match_domains("贵州茅台股价")
        self.assertEqual(hits[0].get("name"), "stock_query")

    def test_match_domain_backward_compat(self):
        from route import match_domain
        d = match_domain("贵州茅台股价")
        self.assertEqual(d.get("name"), "stock_query")

    def test_multi_intent_keeps_primary_first(self):
        """多意图查询：行情意图优先，primary 必须是行情族。

        原断言是 `len(combo) <= 3`（预算口径）。2026-11-02 改为准确率口径：
        用户问「股价」时，无论 combo 多长，第一个跑的必须是行情源——预算
        是成本问题，不是路由正确性问题，混在一起会让「路由变好但变贵」
        被误判成回归。
        """
        from route import route_query
        from engine_families import family_of
        r = route_query("茅台 股价 财报 分红", mode="auto", depth="balanced")
        self.assertEqual(r.get("domain"), "stock_query")
        self.assertIn("sina_quote", r.get("engines_combo") or [])
        self.assertEqual(family_of(r.get("engine") or ""),
                         "finance_market",
                         f"行情意图的 primary 必须是行情源，实际 {r.get('engine')}")

    def test_multi_intent_fast_budget(self):
        from route import route_query
        r = route_query("茅台 股价 财报 分红", mode="fast", depth="fast")
        combo = r.get("engines_combo") or []
        self.assertLessEqual(len(combo), 2)
        self.assertEqual(r.get("parallel"), False)


if __name__ == "__main__":
    unittest.main()

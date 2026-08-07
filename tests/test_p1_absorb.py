"""tests/test_p1_absorb.py — P1 精华吸纳测试

P1-3 通用免费源清单单一真源（route 兜底 / recovery L3 同源，消除重复）
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
    """P1-3：通用免费源清单单一真源"""

    def test_recovery_uses_shared_constant(self):
        from engine_policy import GENERAL_FREE_FALLBACK
        from recovery import _GENERAL_FREE_COMBO
        self.assertEqual(tuple(_GENERAL_FREE_COMBO), GENERAL_FREE_FALLBACK)

    def test_route_fallback_local_first(self):
        from engine_policy import GENERAL_FREE_FALLBACK
        from route import _general_fallback
        enabled = {"local_search", "anysearch", "duckduckgo",
                   "local_bing", "local_baidu", "wikipedia"}
        fb = _general_fallback(enabled)
        # 本地优先 + 通用免费源顺序与单一真源一致
        self.assertEqual(fb[0], "local_search")
        self.assertEqual(fb[1:], list(GENERAL_FREE_FALLBACK))

    def test_route_fallback_filters_disabled(self):
        from route import _general_fallback
        fb = _general_fallback({"local_search", "anysearch"})
        self.assertEqual(fb, ["local_search", "anysearch"])


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
        from route import route_query
        r = route_query("茅台 股价 财报 分红", mode="auto", depth="balanced")
        combo = r.get("engines_combo") or []
        self.assertEqual(r.get("domain"), "stock_query")
        self.assertLessEqual(len(combo), 3)
        self.assertIn("sina_quote", combo[:2])

    def test_multi_intent_fast_budget(self):
        from route import route_query
        r = route_query("茅台 股价 财报 分红", mode="fast", depth="fast")
        combo = r.get("engines_combo") or []
        self.assertLessEqual(len(combo), 2)
        self.assertEqual(r.get("parallel"), False)


if __name__ == "__main__":
    unittest.main()

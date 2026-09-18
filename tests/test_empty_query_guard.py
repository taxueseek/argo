#!/usr/bin/env python3
"""无词元查询短路守卫的回归测试（全 mock，不联网）。

背景：纯符号/纯标点/纯 emoji 查询（如 "!!!@#$%"）没有任何词元，文本引擎
必然空手而归，放行的实测代价是 3.1 s dispatch + anysearch 超时 + 一条垃圾
缓存。super_search 现在在改写与路由之前短路这类查询。

守卫只拦 auto 档：显式 --engine、local_first、plan_only 都是用户在点名
「就要这么搜」，必须原样放行——这三条不拦是本文件的主要回归面。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _sentinel_result(query: str) -> dict:
    """execute_search 的替身：被调用即记号，返回最小可用结果。"""
    return {"query": query, "results": [], "engine": "sentinel",
            "funnel": {"routed": 0, "called": 0, "returned": 0,
                       "deduped": 0, "filtered": 0, "kept": 0}}


class TestContentlessQueryGuard(unittest.TestCase):

    def test_pure_symbols_short_circuit(self):
        """纯符号查询：短路返回空结果，不进 execute_search。"""
        import search
        with patch.object(search, "execute_search",
                          side_effect=AssertionError("不应发起分发")):
            out = search.super_search("!!!@#$%", n=3, skip_cache=True)
        self.assertEqual(out["results"], [])
        self.assertEqual(out["engines_used"], [])
        self.assertEqual(out["funnel"]["routed"], 0)
        self.assertTrue(any("no word tokens" in s for s in out["limitations"]))
        self.assertEqual(out["query"], "!!!@#$%")

    def test_emoji_only_short_circuit(self):
        import search
        with patch.object(search, "execute_search",
                          side_effect=AssertionError("不应发起分发")):
            out = search.super_search("😀😀😀", n=3, skip_cache=True)
        self.assertEqual(out["results"], [])

    def test_single_letter_not_blocked(self):
        """单字母 "C" 有词元（\w 命中），必须照常进 execute_search。"""
        import search
        with patch.object(search, "execute_search",
                          side_effect=lambda **kw: _sentinel_result(kw.get("query", ""))):
            out = search.super_search("C", n=1, skip_cache=True)
        self.assertEqual(out.get("engine"), "sentinel")

    def test_explicit_engine_not_blocked(self):
        """用户显式点名引擎时，符号查询也放行（「=>」这类代码符号是真实搜索）。"""
        import search
        with patch.object(search, "execute_search",
                          side_effect=lambda **kw: _sentinel_result(kw.get("query", ""))):
            out = search.super_search("!!!", engine="crates", n=1, skip_cache=True)
        self.assertEqual(out.get("engine"), "sentinel")

    def test_local_first_not_blocked(self):
        """local_first 是用户点名本地路径，不拦。"""
        import search
        with patch.object(search, "execute_search",
                          side_effect=lambda **kw: _sentinel_result(kw.get("query", ""))):
            out = search.super_search("!!!", local_first=True, n=1, skip_cache=True)
        self.assertEqual(out.get("engine"), "sentinel")

    def test_plan_only_still_plans(self):
        """plan_only 是显式离线计划请求，守卫不得截胡（build_plan 的返回即计划本体）。"""
        import search
        out = search.super_search("!!!", plan_only=True, n=1, skip_cache=True)
        self.assertEqual(out.get("status"), "ready")
        self.assertIn("steps", out)
        self.assertNotIn("results", out)  # 守卫的返回形态必带 results，plan 不带


if __name__ == "__main__":
    unittest.main()

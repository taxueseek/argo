#!/usr/bin/env python3
"""verify_with 单元测试：规则选引擎、attach 段结构、fail-open、CLI 旗标存在。"""
from __future__ import annotations

import os
import sys
import unittest

SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPT_DIR))

from verify_with import attach_verify_with, pick_verify_engine  # noqa: E402


def _fake_runner(pages):
    def run(query, engine, n=5, timeout=10):
        return list(pages)
    return run


class TestPickEngine(unittest.TestCase):
    def test_price_query_picks_exa(self):
        self.assertEqual(pick_verify_engine("iPhone 17 Pro Max 官方价格"), "exa")
        self.assertEqual(pick_verify_engine("Switch2 多少钱"), "exa")
        self.assertEqual(pick_verify_engine("AWS pricing"), "exa")

    def test_spec_query_picks_exa(self):
        self.assertEqual(pick_verify_engine("iPhone 17 参数 对比"), "exa")

    def test_non_match_returns_none(self):
        self.assertIsNone(pick_verify_engine("python async 教程"))
        self.assertIsNone(pick_verify_engine(""))
        self.assertIsNone(pick_verify_engine("杭州到黄山坐高铁最快需要多久？"))


class TestAttach(unittest.TestCase):
    def test_auto_no_rule_skips(self):
        results = {"results": [], "domain": None}
        vw = attach_verify_with(results, "python async 教程",
                                runner=_fake_runner([]))
        self.assertIsNone(vw["engine"])
        self.assertEqual(vw["skipped"], "no_rule")
        self.assertNotIn("verify_with", results)  # 段由调用方落，不在本层

    def test_explicit_engine_runs_and_tags(self):
        pages = [
            {"url": "https://www.apple.com.cn/shop/buy-iphone/iphone-17-pro",
             "title": "购买 iPhone 17 Pro", "snippet": "RMB 11,999"},
            {"url": "https://www.apple.com/uk/shop/buy-iphone", "title": "Buy", "snippet": ""},
        ]
        results = {"results": [
            {"url": "https://www.apple.com.cn/shop/buy-iphone/iphone-17-pro/mg074ch/a",
             "title": "主查官方", "snippet": ""},
            {"url": "https://finance.example/news", "title": "转述", "snippet": ""},
        ], "domain": None}
        vw = attach_verify_with(results, "任意查询", engine="exa",
                                runner=_fake_runner(pages))
        self.assertEqual(vw["engine"], "exa")
        self.assertEqual(len(vw["results"]), 2)
        self.assertTrue(all(r.get("verify_provenance") for r in vw["results"]))
        # 域级重叠：apple.com.cn 两路都出现
        self.assertIn("www.apple.com.cn", vw["overlap"]["domains"])
        self.assertEqual(vw["overlap"]["count"], 1)

    def test_runner_error_fail_open(self):
        def boom(query, engine, n=5, timeout=10):
            raise RuntimeError("engine down")
        vw = attach_verify_with({"results": []}, "价格", engine="exa",
                                runner=boom)
        self.assertEqual(vw["engine"], "exa")
        self.assertTrue(vw["skipped"].startswith("error:"))
        self.assertNotIn("results", vw)

    def test_empty_verify_results(self):
        vw = attach_verify_with({"results": []}, "价格", engine="exa",
                                runner=_fake_runner([]))
        self.assertEqual(vw["results"], [])
        self.assertEqual(vw["overlap"], {"domains": [], "count": 0})


class TestCliFlags(unittest.TestCase):
    """CLI 是否支持已文档化的开关，对着解析器对象断言（search_cli 约定）。"""

    def test_flags_registered(self):
        from search_cli import build_parser
        parser = build_parser()
        self.assertIsNotNone(parser.parse_args(["q", "--numverify"]))
        self.assertIsNotNone(parser.parse_args(["q", "--numverify", "2"]))
        self.assertIsNotNone(parser.parse_args(["q", "--verify-with"]))
        self.assertIsNotNone(parser.parse_args(["q", "--verify-with", "exa"]))
        args = parser.parse_args(["q", "--numverify", "2", "--verify-with"])
        self.assertEqual(args.numverify, 2)
        self.assertEqual(args.verify_with, "auto")   # const
        self.assertIsNone(parser.parse_args(["q"]).numverify)
        self.assertIsNone(parser.parse_args(["q"]).verify_with)


if __name__ == "__main__":
    unittest.main()


class TestAgentSlim(unittest.TestCase):
    """agent 档瘦身：新段必须以精简形态保留（花了墙钟的产出不能静默丢）。"""

    def test_slim_numverify(self):
        from search_output import _slim_numverify
        slim = _slim_numverify({
            "query": "q",
            "summary": {"checked": 2, "confirmed": 1, "missing": 1},
            "verified": [
                {"url": "https://a", "verdict": "confirmed", "hits": ["5999"],
                 "misses": [], "fetch_method": "http", "title": "t"},
                {"url": "https://b", "verdict": "missing", "hits": [],
                 "misses": ["11999"], "fetch_method": "http"},
            ],
            "pending": []})
        self.assertEqual(slim["summary"]["missing"], 1)
        self.assertEqual(len(slim["verified"]), 2)
        self.assertNotIn("hits", slim["verified"][0])   # 细节剥掉
        self.assertNotIn("title", slim["verified"][0])
        self.assertNotIn("pending", slim)               # 空列表不落键

    def test_slim_verify_with(self):
        from search_output import _slim_verify_with
        slim = _slim_verify_with({
            "engine": "exa", "elapsed_ms": 1588.8,
            "results": [{"url": "https://www.apple.com.cn/x", "title": "t"}],
            "overlap": {"domains": ["www.apple.com.cn"], "count": 1}})
        self.assertEqual(slim["engine"], "exa")
        self.assertEqual(slim["result_count"], 1)
        self.assertNotIn("results", slim)               # 逐条剥掉
        self.assertEqual(slim["overlap"]["count"], 1)

    def test_slim_no_rule_kept(self):
        from search_output import _slim_verify_with
        slim = _slim_verify_with({"engine": None, "skipped": "no_rule"})
        self.assertEqual(slim["skipped"], "no_rule")

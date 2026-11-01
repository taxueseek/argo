#!/usr/bin/env python3
"""numeric_verify 单元测试：关键数字抽取、比对 verdict、冲突降权、选样守卫。"""
from __future__ import annotations

import os
import sys
import unittest

SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPT_DIR))

from numeric_verify import (  # noqa: E402
    extract_critical_numbers,
    maybe_verify_numbers,
    numeric_verify_results,
    reorder_by_numeric,
)


def _mk(url: str, title: str, snippet: str, score: float = 1.0,
        **extra) -> dict:
    r = {"url": url, "title": title, "snippet": snippet, "score": score}
    r.update(extra)
    return r


class TestExtractCriticalNumbers(unittest.TestCase):
    def test_price_prefix_and_suffix(self):
        nums = extract_critical_numbers("RMB 11,999 起售，另有 $1,299 和 ¥5999 的档，最低 5999元")
        norms = {n["norm"] for n in nums if n["kind"] == "price"}
        self.assertIn("11999", norms)
        self.assertIn("1299", norms)
        self.assertIn("5999", norms)

    def test_percent(self):
        nums = extract_critical_numbers("同比增长 37.5%，环比 12%")
        pcts = {n["norm"] for n in nums if n["kind"] == "pct"}
        self.assertEqual(pcts, {"37.5", "12"})

    def test_dates_cn_and_iso(self):
        nums = extract_critical_numbers("2025年9月10日发布，9月13日开售，见 2025-09-15 公告")
        dates = {n["norm"] for n in nums if n["kind"] == "date"}
        self.assertIn("9月10日2025年", dates)
        self.assertIn("9月13日", dates)
        self.assertIn("2025-09-15", dates)

    def test_wan_notation_excluded(self):
        # 「2.5万」vs「25000」换算噪音：v1 不抽万/亿记数法
        nums = extract_critical_numbers("销量 2.5万 台，营收 5999万")
        self.assertEqual([n for n in nums if n["kind"] == "price"], [])

    def test_number_quantifier_not_date(self):
        # 「12 组 3 通道」不是 12月3日
        nums = extract_critical_numbers("配置 12 组 3 通道阵列")
        self.assertEqual([n for n in nums if n["kind"] == "date"], [])

    def test_dedup(self):
        nums = extract_critical_numbers("5999元起，5999元封顶")
        prices = [n["norm"] for n in nums if n["kind"] == "price"]
        self.assertEqual(prices.count("5999"), 1)


class TestVerdictAndFetch(unittest.TestCase):
    def _run(self, results, fetch_fn, top_k=3):
        return numeric_verify_results(results, "q", top_k=top_k,
                                      fetch_fn=fetch_fn)

    def test_confirmed(self):
        page = ("苹果公布了 iPhone 17 系列国行版售价：5999、8999、17999 元。"
                "其中 Pro Max 顶配版本 Storage 1TB 售价最高，全系 9 月 12 日晚开售，"
                "首发当日官网与直营店同步现货。")
        res = [_mk("https://x.example/a", "售价公布",
                   "最贵17999！起价5999元 Pro 8999元")]
        out = self._run(res, lambda url, max_chars, timeout: {
            "success": True, "content": page, "fetch_method": "http"})
        self.assertEqual(out["summary"]["confirmed"], 1)
        self.assertEqual(res[0]["numeric_verification"]["verdict"], "confirmed")

    def test_missing_on_spam_page(self):
        # 摘要全是 iPhone 价格，页面是博彩内容：一个数字都没有 → missing
        page = "欢迎来到完美电竞平台，注册即送彩金，最新赛事直播与下注通道。" * 5
        res = [_mk("https://hospital.example/x", "5999元起！iPhone 17 Pro Max 发售",
                   "全新 iPhone 17 Pro Max 售价 5999 元起")]
        out = self._run(res, lambda url, max_chars, timeout: {
            "success": True, "content": page, "fetch_method": "http"})
        self.assertEqual(out["summary"]["missing"], 1)

    def test_partial(self):
        page = ("iPhone 17 起售价 5999 元，其他版本价格与配置请咨询客服或到店了解。"
                "本文仅覆盖基础款，Pro 与 Pro Max 的售价信息另行成文，"
                "请以苹果官方商城页面实时标价为准，促销活动价格以结算页为准。"
                "以上信息更新于发布当日，后续调整恕不另行通知。")
        res = [_mk("https://x.example/p", "售价", "5999元 与 17999元 两档")]
        out = self._run(res, lambda url, max_chars, timeout: {
            "success": True, "content": page, "fetch_method": "http"})
        self.assertEqual(out["summary"]["partial"], 1)

    def test_unfetchable_not_penalized_later(self):
        res = [_mk("https://x.example/u", "售价 5999元", "5999元")]
        out = self._run(res, lambda url, max_chars, timeout: {
            "success": False, "content": "", "fetch_method": "http"})
        self.assertEqual(out["summary"]["unfetchable"], 1)
        # unfetchable 不参与降权
        ro = reorder_by_numeric(res)
        self.assertFalse(ro["reordered"])

    def test_short_page_counts_unfetchable(self):
        res = [_mk("https://x.example/s", "售价 5999元", "5999元")]
        out = self._run(res, lambda url, max_chars, timeout: {
            "success": True, "content": "ok", "fetch_method": "http"})
        self.assertEqual(out["summary"]["unfetchable"], 1)

    def test_serp_and_non_number_skipped(self):
        calls = []

        def fetch(url, max_chars, timeout):
            calls.append(url)
            return {"success": True, "content": "5999 元在这里", "fetch_method": "http"}

        res = [
            _mk("https://www.baidu.com/s?wd=iphone", "SERP", "5999元"),
            _mk("https://x.example/1", "纯文本无数字", "没有关键数字的摘要"),
            _mk("https://x.example/2", "有数字", "售价 11999 元"),
        ]
        out = self._run(res, fetch, top_k=3)
        # SERP 与无数字条目都不付 RTT；只有第 3 条被抓
        self.assertEqual(calls, ["https://x.example/2"])
        self.assertEqual(out["summary"]["checked"], 1)

    def test_parallel_multi_target(self):
        # 2026-11-01 实测踩中：>1 条目标走 run_bounded 并行路径，worker 返回
        # 载荷与 item 拼接曾发生双层嵌套（单条串行路径测不出来）——本用例
        # 固定该回归。
        res = [
            _mk("https://a.example/1", "价一", "售价 5999 元"),
            _mk("https://b.example/2", "价二", "售价 11999 元"),
        ]
        out = self._run(res, lambda url, max_chars, timeout: {
            "success": True,
            "content": f"页面正文 {url} 官方标价 5999 元与 11999 元，" + "说明文字。" * 20,
            "fetch_method": "http"})
        self.assertEqual(out["summary"]["checked"], 2)
        self.assertEqual(out["summary"]["confirmed"], 2)

    def test_noop_when_no_numbers(self):
        res = [_mk("https://x.example/1", "t", "没有数字")]
        out = maybe_verify_numbers(res, "q", fetch_fn=lambda *a, **k: {})
        self.assertIsNone(out)


class TestReorder(unittest.TestCase):
    def test_missing_drops_score_and_resorts(self):
        res = [
            _mk("https://a.example", "毒", "5999元", score=0.90),
            _mk("https://b.example", "好", "11999元", score=0.80),
        ]
        res[0]["numeric_verification"] = {"verdict": "missing"}
        res[1]["numeric_verification"] = {"verdict": "confirmed"}
        ro = reorder_by_numeric(res, weight=0.35)
        self.assertTrue(ro["reordered"])
        by_url = {r["url"]: r["score"] for r in res}
        self.assertAlmostEqual(by_url["https://a.example"], round(0.90 * 0.65, 4))
        self.assertEqual(by_url["https://b.example"], 0.80)   # confirmed 不动
        # 降权后 b（0.80）反超到第一位
        self.assertEqual([r["url"] for r in res],
                         ["https://b.example", "https://a.example"])

    def test_partial_half_weight(self):
        res = [_mk("https://a.example", "t", "5999元", score=1.0)]
        res[0]["numeric_verification"] = {"verdict": "partial"}
        reorder_by_numeric(res, weight=0.35)
        self.assertAlmostEqual(res[0]["score"], round(1.0 * (1 - 0.175), 4))

    def test_no_verification_no_change(self):
        res = [_mk("https://a.example", "t", "s", score=0.5)]
        ro = reorder_by_numeric(res)
        self.assertFalse(ro["reordered"])
        self.assertEqual(res[0]["score"], 0.5)

    def test_weight_clamped(self):
        res = [_mk("https://a.example", "t", "s", score=1.0)]
        res[0]["numeric_verification"] = {"verdict": "missing"}
        reorder_by_numeric(res, weight=99)
        self.assertAlmostEqual(res[0]["score"], 0.0)


if __name__ == "__main__":
    unittest.main()

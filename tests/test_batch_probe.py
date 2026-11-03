#!/usr/bin/env python3
"""test_batch_probe.py — 批量 URL 预检回归测试。

覆盖：
  1. 本地规则分类：非 http(s) → unsupported、登录墙域 → needs_auth
  2. verdict 三档：go / go_with_skips / stop
  3. actionable 语义：只有 needs_auth 可行动
  4. 报告格式：问题分节 + 可执行清单
  5. 不探测不臆测：未开 --probe 时合法 URL 不被判死
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import batch_probe as bp  # noqa: E402


class TestClassifyUrl:
    def test_invalid_scheme(self):
        assert bp.classify_url("ftp://a.com/x") == bp.UNSUPPORTED
        assert bp.classify_url("not a url") == bp.UNSUPPORTED
        assert bp.classify_url("") == bp.UNSUPPORTED

    def test_login_wall(self):
        assert bp.classify_url("https://x.com/home") == bp.NEEDS_AUTH
        assert bp.classify_url("https://www.instagram.com/p/abc/") == bp.NEEDS_AUTH
        assert bp.classify_url("https://medium.com/@a/post") == bp.NEEDS_AUTH

    def test_subdomain_matches(self):
        assert bp.classify_url("https://api.x.com/status/1") == bp.NEEDS_AUTH

    def test_open_site_unknown(self):
        assert bp.classify_url("https://example.com/a") == bp.UNKNOWN
        assert bp.classify_url("https://arxiv.org/abs/1234") == bp.UNKNOWN

    def test_already_have(self):
        known = {"https://example.com/known"}
        assert bp.classify_url("https://example.com/known", known) == bp.ALREADY_HAVE
        assert bp.classify_url("https://example.com/other", known) == bp.UNKNOWN


class TestProbeBatch:
    def test_mixed_verdict_go_with_skips(self):
        report = bp.probe_batch([
            "https://example.com/a",
            "https://x.com/home",
            "not a url",
        ])
        assert report["total"] == 3
        assert report["ready"] == 1
        assert report["verdict"] == "go_with_skips"
        assert report["by_problem"]["needs_auth"] == ["https://x.com/home"]
        assert report["by_problem"]["unsupported"] == ["not a url"]

    def test_all_clean_go(self):
        report = bp.probe_batch(["https://example.com/a", "https://example.com/b"])
        assert report["verdict"] == "go"
        assert report["ready"] == 2

    def test_all_bad_stop(self):
        report = bp.probe_batch(["not a url", "ftp://x"])
        assert report["verdict"] == "stop"
        assert report["ready"] == 0

    def test_no_probe_no_death_verdict(self):
        # 纪律：不探测不臆测。未开 --probe 时合法 URL 绝不被判 not_found
        report = bp.probe_batch(["https://probably-dead.example.com/x"])
        assert report["verdict"] == "go"
        assert "not_found" not in report["by_problem"]

    def test_actionable_only_needs_auth(self):
        report = bp.probe_batch([
            "https://x.com/home",
            "not a url",
            "https://example.com/ok",
        ])
        actionable = [it for it in report["items"] if it["actionable"]]
        assert len(actionable) == 1
        assert actionable[0]["problem"] == bp.NEEDS_AUTH

    def test_ready_urls_lists_executable(self):
        report = bp.probe_batch([
            "https://example.com/a",
            "https://x.com/home",
        ])
        assert report["ready_urls"] == ["https://example.com/a"]


class TestReportFormat:
    def test_text_report_sections(self):
        report = bp.probe_batch([
            "https://example.com/a",
            "https://x.com/home",
            "not a url",
        ])
        text = bp._format_report(report)
        assert "需要登录态" in text
        assert "无法处理" in text
        assert "可执行清单" in text
        assert "https://x.com/home" in text
        assert "not a url" in text
        assert "https://example.com/a" in text

    def test_json_shape(self):
        report = bp.probe_batch(["https://example.com/a"])
        assert set(report) >= {"total", "ready", "verdict", "items",
                               "ready_urls", "by_problem"}

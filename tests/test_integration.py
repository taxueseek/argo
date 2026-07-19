#!/usr/bin/env python3
"""Unified Search v2 集成测试 — 端到端路由 + 缓存 + JSON schema"""

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"


class TestEndToEnd(unittest.TestCase):
    """端到端测试：通过 CLI 调用验证完整流程。"""

    def run_search(self, query, engine="auto", mode="auto", no_cache=True, timeout=15):
        cmd = [sys.executable, str(SCRIPT_DIR / "search.py"), query,
               "--engine", engine, "--mode", mode, "--json"]
        if no_cache:
            cmd.append("--no-cache")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        return {}

    def test_routes_stock_to_eastmoney(self):
        data = self.run_search("贵州茅台股价")
        self.assertEqual(data.get("engine"), "eastmoney")
        self.assertIn("elapsed_ms", data)

    def test_routes_fund_to_eastmoney(self):
        data = self.run_search("基金净值")
        self.assertEqual(data.get("engine"), "eastmoney")

    def test_routes_academic_to_arxiv(self):
        data = self.run_query = self.run_search("transformer attention paper")
        self.assertEqual(data.get("engine"), "arxiv")

    def test_routes_zhihu_content(self):
        data = self.run_search("笔记本电脑推荐")
        self.assertEqual(data.get("engine"), "zhihu")

    def test_json_schema(self):
        data = self.run_search(f"schema-test-{time.time()}")
        required_fields = ["engine", "engines", "results", "count", "elapsed_ms"]
        for field in required_fields:
            self.assertIn(field, data, f"缺少字段: {field}")

    def test_engine_override(self):
        data = self.run_search("Python asyncio", engine="arxiv")
        self.assertEqual(data.get("engine"), "arxiv")

    def test_mode_fast(self):
        data = self.run_search("最新新闻", mode="fast")
        self.assertEqual(data.get("mode"), "fast")

    def test_mode_budget(self):
        data = self.run_search("科技资讯", mode="budget")
        self.assertEqual(data.get("mode"), "budget")
        # budget 模式不应使用付费引擎
        for eng in data.get("engines", []):
            self.assertNotEqual(eng, "tavily")

    def test_explain_flag(self):
        cmd = [sys.executable, str(SCRIPT_DIR / "search.py"), "测试",
               "--explain", "--no-cache", "--json", "--engine", "anysearch"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertIn("results", data)


class TestTfidfRouterCLI(unittest.TestCase):
    """TF-IDF 路由 CLI 测试。"""

    def test_router_cli_output(self):
        cmd = [sys.executable, str(SCRIPT_DIR / "tfidf_router.py"), "英伟达财报"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        # 输出应包含至少一个已知引擎名（新增 local_* 档案后可能不是 anysearch）
        known_engines = ["anysearch", "eastmoney", "local_search", "local_sogou", "local_stackoverflow"]
        self.assertTrue(any(e in r.stdout.lower() for e in known_engines),
                        f"输出未包含已知引擎名: {r.stdout}")


class TestCacheCLI(unittest.TestCase):
    """缓存 CLI 测试。"""

    def test_cache_stats(self):
        cmd = [sys.executable, str(SCRIPT_DIR / "cache.py"), "stats"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        stats = json.loads(r.stdout)
        self.assertIn("l1", stats)
        self.assertIn("l2", stats)


class TestQuotaCLI(unittest.TestCase):
    """配额 CLI 测试。"""

    def test_quota_stats(self):
        cmd = [sys.executable, str(SCRIPT_DIR / "quota.py"), "stats"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        stats = json.loads(r.stdout)
        self.assertIsInstance(stats, dict)


class TestAdaptiveCLI(unittest.TestCase):
    """自适应学习 CLI 测试。"""

    def test_adaptive_rank(self):
        cmd = [sys.executable, str(SCRIPT_DIR / "adaptive.py"), "rank"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

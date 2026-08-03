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

    def run_search(self, query, engine="auto", mode="auto", no_cache=True, timeout=60):
        """调用 search.py CLI。学术/多引擎 miss 常需 20–40s，默认超时 60s。"""
        cmd = [sys.executable, str(SCRIPT_DIR / "search.py"), query,
               "--engine", engine, "--mode", mode, "--json", "-n", "2",
               "--timeout", "8"]
        if no_cache:
            cmd.append("--no-cache")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and r.stdout.strip():
                # 部分环境 stderr 混入时取最后一个 JSON 对象
                text = r.stdout.strip()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    start = text.find("{")
                    end = text.rfind("}")
                    if start >= 0 and end > start:
                        return json.loads(text[start : end + 1])
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        return {}

    def test_routes_stock_to_sina(self):
        # v2.5.1 起 stock 域 combo 有意缩短（sina_quote + tencent_quote），
        # eastmoney 不再必然出现在日常 combo 中；断言主源与域即可。
        data = self.run_search("贵州茅台股价")
        self.assertEqual(data.get("engine"), "sina_quote")
        self.assertEqual(data.get("domain"), "stock_query")
        self.assertIn("elapsed_ms", data)

    def test_routes_fund_to_eastmoney(self):
        data = self.run_search("基金净值")
        self.assertEqual(data.get("engine"), "eastmoney")

    def test_routes_academic_to_arxiv(self):
        data = self.run_search("transformer attention paper")
        self.assertTrue(data, "CLI 应返回 JSON")
        # 学术主引擎可为 arxiv / crossref / semantic_scholar 等
        academic = {
            "arxiv", "crossref", "semantic_scholar", "europepmc", "dblp",
            "openalex", "local_arxiv", "local_semantic_scholar",
        }
        eng = data.get("engine")
        engines = set(data.get("engines") or [])
        self.assertTrue(
            eng in academic or engines & academic or data.get("domain") == "academic",
            f"学术路由异常: engine={eng} engines={engines} domain={data.get('domain')}",
        )

    def test_routes_zhihu_content(self):
        data = self.run_search("笔记本电脑推荐")
        self.assertTrue(data, "CLI 应返回 JSON")
        # 购物/知乎/中文通用均可
        ok = {"zhihu", "bocha", "byted", "anysearch", "uapi"}
        eng = data.get("engine")
        self.assertTrue(
            eng in ok or data.get("domain") in {"zhihu_content", "shopping", "chinese_general", "general_search"},
            f"中文导购路由异常: engine={eng} domain={data.get('domain')}",
        )

    def test_json_schema(self):
        data = self.run_search(f"schema-test-{time.time()}")
        required_fields = ["engine", "engines", "results", "count", "elapsed_ms"]
        for field in required_fields:
            self.assertIn(field, data, f"缺少字段: {field}")

    def test_engine_override(self):
        data = self.run_search("Python asyncio", engine="arxiv")
        self.assertTrue(data, "CLI 应返回 JSON")
        # override 时 decision.engine 应为 arxiv；部分路径可能只体现在 engines 列表
        eng = data.get("engine")
        engines = data.get("engines") or []
        self.assertTrue(
            eng == "arxiv" or "arxiv" in engines,
            f"engine override 未生效: engine={eng} engines={engines}",
        )

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

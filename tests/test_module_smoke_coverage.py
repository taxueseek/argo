#!/usr/bin/env python3
"""盲区模块最小回归（2026-09-29，批3 覆盖缺口补齐）。

2026-09-28 覆盖盘点发现四个「改了没人守」的模块：stats_cli（argo stats
用户面入口）、calibrate_lowquality（低质信号标定）、search_entry（编排
准备段）、mcp_handlers_surface（五工具 handler）。本文件给每个模块锁
最小契约——不求全覆盖，求「核心路径坏掉时这里有红」。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class TestStatsCli(unittest.TestCase):
    """argo stats：空状态目录不崩、JSON 出口可用、退出码诚实。"""

    def test_build_report_on_empty_state(self):
        import stats_cli

        report = stats_cli.build_report(5)
        self.assertIsInstance(report, dict)
        # 空目录：各流计数为 0 而不是 KeyError/None
        self.assertEqual(report.get("queries", 0) or 0, 0)

    def test_main_json_exit_zero(self):
        import io
        from contextlib import redirect_stdout

        import stats_cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = stats_cli.main(["--json"])
        self.assertEqual(rc, 0)
        import json

        payload = json.loads(buf.getvalue())
        self.assertIsInstance(payload, dict)


class TestCalibrateLowQuality(unittest.TestCase):
    """低质信号标定：读金标样本、输出每个信号的判别力（AUC/F1/FP/FN）。"""

    def test_run_reports_all_signals(self):
        import calibrate_lowquality as cal

        report = cal.run()
        self.assertGreaterEqual(report.get("n", 0), 20,
                                "标定集应 ≥20 条（2026-09-27 扩充后）")
        signals = report.get("signals") or {}
        for name in ("clickbait", "template", "title_body_gap",
                     "keyword_density"):
            self.assertIn(name, signals, f"标定报告缺信号 {name}")
            row = signals[name]
            self.assertIn("auc", row)
            self.assertGreaterEqual(row["auc"], 0.0)
            self.assertLessEqual(row["auc"], 1.0)


class TestSurfaceTools(unittest.TestCase):
    """五工具 handler：必填校验走 -32602（不是 KeyError 变种的 -32000）。"""

    def test_required_params_from_schema(self):
        from mcp_handlers_surface import _check_required

        self.assertEqual(_check_required("argo_answer", {}), ["query"])
        self.assertEqual(_check_required("argo_answer", {"query": "x"}), [])
        self.assertEqual(_check_required("argo_extract", {}), ["url"])

    def test_missing_required_is_invalid_params(self):
        from mcp_handlers_surface import handle_surface_tool

        out = handle_surface_tool("argo_answer", {})
        self.assertTrue(out and out.get("isError"))
        self.assertIn("-32602", out["content"][0]["text"])
        self.assertIn("query", out["content"][0]["text"])

    def test_non_surface_tool_returns_none(self):
        from mcp_handlers_surface import handle_surface_tool

        self.assertIsNone(handle_surface_tool("argo_search", {"query": "x"}))


class TestSearchEntryPrepare(unittest.TestCase):
    """prepare 直测：miss 交出 req/run，二次同请求命中 cached。"""

    def _hooks(self):
        import search as S
        from search_entry import _SearchHooks

        return _SearchHooks(
            engine_search=S.engine_search,
            available_engines=S.available_engines,
            get_cost_factor=S.get_cost_factor,
            get_engines=S.get_engines,
            get_execution_config=S.get_execution_config,
            missing_env_for=S._missing_env_for,
            classify_outcome=S._classify_engine_outcome,
            note_quota_exhausted=S._note_remote_quota_exhausted,
            per_engine_budget_s=S._PER_ENGINE_BUDGET_S,
            fast_budget_s=S._FAST_TOTAL_BUDGET_S,
            auto_budget_s=S._AUTO_TOTAL_BUDGET_S,
            primary_grace_s=S._PRIMARY_GRACE_S,
            straggler_grace_s=S._straggler_grace(),
            serial_stagger_s=S._serial_stagger(),
        )

    def test_miss_then_hit(self):
        import tempfile

        from cache import SearchCache
        from search_entry import prepare

        decision = {"domain": "general", "engine": "auto",
                    "engines_combo": ["anysearch"],
                    "engines": ["anysearch"], "engine_request": "auto",
                    "parallel": False, "mode": "auto", "depth": "fast",
                    "reason": "test", "features": {}}
        cache = SearchCache(db_path=os.path.join(
            tempfile.mkdtemp(prefix="argo-entry-test-"), "c.db"))
        hooks = self._hooks()
        kw = dict(max_results=5, timeout=8, depth="fast",
                  cache=cache, skip_cache=False, mode="fast",
                  since=None, until=None, sort="relevance",
                  engine_domain=None, engine_sub_domain=None,
                  timing=None, hooks=hooks)

        p1 = prepare("argo prepare smoke test", dict(decision), **kw)
        self.assertIsNone(p1.cached, "首次应为 miss")
        self.assertIsNotNone(p1.req)
        self.assertIsNotNone(p1.run)

        # 写入一份与缓存键匹配的载荷，模拟 finalize 的落库
        payload = {"results": [{"title": "t", "url": "https://x.example/1",
                                "snippet": "s", "relevance": 0.9}],
                   "engines_used": ["anysearch"]}
        cache.set("argo prepare smoke test", "auto", 5, payload,
                  domain="general", mode="fast", depth="fast")

        p2 = prepare("argo prepare smoke test", dict(decision), **kw)
        self.assertIsNotNone(p2.cached, "同请求二次应命中（请求身份键）")
        self.assertTrue(p2.cached.get("results"))


if __name__ == "__main__":
    unittest.main()

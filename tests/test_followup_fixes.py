#!/usr/bin/env python3
"""test_followup_fixes.py — 11-11 九笔改动复核所暴露缺陷的回归门禁。

复核结论是「提交信息里的效果数字与生产路径差一层」，共六处：

1. JEV 的 `with ThreadPoolExecutor` 退出时 shutdown(wait=True)，硬截止失效；
2. `sanitize_schema` 是死代码，tools/list 出口不做运行时净化；
3. `secret_guard` 只挂在 search_cli 出口，MCP tools/call 未接；
4. 阶段2 语义加权只改 `verified` 副本，没写回 results 原始行；
5. 新增测试没进快门禁 pre-commit；
6. initialize.instructions 仍描述旧的 19 工具面与已被收敛的 argo_local_search。

每条都配一条「删掉修复就挂」的反例，不测「函数存在」。
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evidence_loop  # noqa: E402
import mcp_transport  # noqa: E402
import mcp_tools  # noqa: E402
import semantic_evidence as se  # noqa: E402


class TestJevDeadlineActuallyReturns(unittest.TestCase):
    """硬截止必须真的在 deadline 附近返回，不能 join 卡住的线程。"""

    def test_deadline_returns_without_joining_slow_futures(self):
        def slow(text, labels, instructions, timeout, criteria=None):
            time.sleep(2.0)
            return {"label": labels[0], "confidence": 0.9, "scores": None}

        with patch.object(se, "_should_bypass_cache", return_value=True), \
                patch.object(se, "DEFAULT_JEV_DEADLINE_S", 0.3), \
                patch.object(se, "_jev_choice", slow):
            started = time.monotonic()
            result = se._classify_jev(
                ["a", "b"], ("x", "y"), instructions=None,
                keep=[(0, "a"), (1, "b")], timeout=5.0)
            elapsed = time.monotonic() - started

        # 旧实现 with + shutdown(wait=True) 会等满 2.0s；修复后不 join。
        self.assertLess(elapsed, 1.0, f"硬截止失效：实际耗时 {elapsed:.2f}s")
        self.assertIsNone(result, "超时的两条都未完成，应整批 fail-open 返回 None")

    def test_partial_results_survive_when_siblings_time_out(self):
        """一条秒回、一条卡死：必须保留已完成的那条，且整体不拖到慢请求结束。"""
        def mixed(text, labels, instructions, timeout, criteria=None):
            if text == "fast":
                return {"label": "x", "confidence": 0.9, "scores": None}
            time.sleep(2.0)
            return {"label": "y", "confidence": 0.9, "scores": None}

        with patch.object(se, "_should_bypass_cache", return_value=True), \
                patch.object(se, "DEFAULT_JEV_DEADLINE_S", 0.3), \
                patch.object(se, "_jev_choice", mixed):
            started = time.monotonic()
            result = se._classify_jev(
                ["fast", "slow"], ("x", "y"), instructions=None,
                keep=[(0, "fast"), (1, "slow")], timeout=5.0)
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0, f"硬截止失效：实际耗时 {elapsed:.2f}s")
        self.assertEqual(result[0]["label"], "x")
        self.assertIsNone(result[1], "未完成的那条必须留 None 而不是编造判定")


class TestToolsListSanitizesAtRuntime(unittest.TestCase):
    """sanitize_schema 必须真接进 tools/list 出口，不能只靠人肉删键。"""

    def test_tools_list_strips_injected_forbidden_keys(self):
        dirty = {"type": "object", "properties": {"q": {"type": "string", "default": "x", "minimum": 1}},
                 "required": ["q"]}
        with patch.object(mcp_tools, "TOOLS",
                          [{"name": "argo_probe", "description": "probe", "inputSchema": dirty}]), \
                patch.object(mcp_tools, "CORE_TOOL_NAMES", ("argo_probe",)):
            tools = mcp_transport.handle_rpc("tools/list", {})["tools"]
        spec = tools[0]["inputSchema"]["properties"]["q"]
        self.assertNotIn("default", spec)
        self.assertNotIn("minimum", spec)
        self.assertEqual(tools[0]["inputSchema"]["required"], ["q"])

    def test_dsh_native_tools_generator_sanitizes(self):
        """DSH 派生规格同样走净化，避免生成物把非标注解带回网关。"""
        import gen_native_tools
        dirty = {"type": "object", "properties": {"q": {"type": "string", "default": "x"}},
                 "required": ["q"]}
        # gen_native_tools 用 `from mcp_tools import TOOLS` 直接绑定了列表对象，
        # patch mcp_tools.TOOLS 不会改到它手里的引用，故 patch 模块内的符号。
        with patch.object(gen_native_tools, "TOOLS",
                          [{"name": "argo_probe", "description": "probe", "inputSchema": dirty}]):
            built = gen_native_tools.build_native_tools()
        self.assertNotIn("default", built["argo_probe"]["parameters"]["properties"]["q"])


class TestMcpToolsCallRedaction(unittest.TestCase):
    """MCP tools/call 返回必须过脱敏，不能只挂 search_cli。"""

    def test_tools_call_redacts_embedded_secret(self):
        secret = "sk-proj" + ("a" * 32)
        fake_result = {"content": [{"type": "text", "text": f"leaked {secret}"}]}
        with patch.object(mcp_transport, "execute_tool", return_value=fake_result):
            resp = mcp_transport.handle_rpc("tools/call", {"name": "argo_search", "arguments": {"query": "q"}})
        text = json.dumps(resp, ensure_ascii=False)
        self.assertNotIn(secret, text)
        self.assertIn("[REDACTED_SECRET]", text)


class TestEvidenceSubstantiveBackfillsResults(unittest.TestCase):
    """阶段2 的 +0.15 / -0.10 与一手源 +0.20 必须落到 results 原始行的排序分。"""

    def test_adjustments_write_back_to_result_row(self):
        results = [{"url": "https://podcast.example.com/ep1", "title": "Deep Discussion", "score": 0.8}]

        def fake_fetch(url, max_chars=8000, timeout=10.0):
            return {"url": url, "success": True, "content": "深度探讨了主题细节内容，包含大量实质逻辑。"}

        def fake_post(state, questions, *, timeout):
            return {"answers": {"cls": {"type": "choice",
                                        "choice": "substantive in-depth discussion",
                                        "confidence": 0.95, "probabilities": {}}}}

        with patch.dict("os.environ", {"ARGO_SEMANTIC_EVIDENCE": "1"}), \
                patch.object(se, "enabled", return_value=True), \
                patch.object(se, "_jev_post", fake_post), \
                patch.object(se, "assess_support", return_value=None), \
                patch.object(se, "assess_source_types", return_value=None):
            out = evidence_loop.verify_results(
                results, query="测试主题", top_k=1, fetch_fn=fake_fetch)

        verified = {v["url"]: v for v in out["verified"]}
        row = results[0]
        self.assertTrue(row.get("substantive_discussion"), "标记必须写回原始结果行")
        self.assertAlmostEqual(
            row["post_fetch_absorption"], verified["https://podcast.example.com/ep1"]["post_absorption"],
            places=3, msg="排序分必须与 verified 里的加权后数值一致")
        self.assertGreater(
            row["post_fetch_absorption"], verified["https://podcast.example.com/ep1"]["pre_absorption"],
            msg="+0.15 加权必须真的抬高数值")

    def test_passing_mention_writes_back_negative_delta(self):
        results = [{"url": "https://podcast.example.com/ep2", "title": "Passing Chat", "score": 0.75}]

        def fake_fetch(url, max_chars=8000, timeout=10.0):
            return {"url": url, "success": True, "content": "闲聊顺带提了一句，主要聊别的。"}

        def fake_post(state, questions, *, timeout):
            return {"answers": {"cls": {"type": "choice",
                                        "choice": "passing mention or brief reference",
                                        "confidence": 0.92, "probabilities": {}}}}

        with patch.dict("os.environ", {"ARGO_SEMANTIC_EVIDENCE": "1"}), \
                patch.object(se, "enabled", return_value=True), \
                patch.object(se, "_jev_post", fake_post), \
                patch.object(se, "assess_support", return_value=None), \
                patch.object(se, "assess_source_types", return_value=None):
            out = evidence_loop.verify_results(
                results, query="测试主题", top_k=1, fetch_fn=fake_fetch)

        verified = {v["url"]: v for v in out["verified"]}
        row = results[0]
        self.assertTrue(row.get("passing_mention"))
        self.assertAlmostEqual(
            row["post_fetch_absorption"], verified["https://podcast.example.com/ep2"]["post_absorption"],
            places=3, msg="-0.10 降权必须写回原始结果行")


class TestPrecommitListsNewGates(unittest.TestCase):
    """新增的回归门禁必须进快门禁，否则只有 CI 全量才拦得住退步。"""

    _REQUIRED = (
        "test_semantic_evidence_jev.py", "test_mcp_schema_maas_compliance.py",
        "test_mcp_consolidated_tools.py", "test_redactor_guard.py",
        "test_schema_drift_diagnosis.py", "test_followup_fixes.py",
    )

    def test_precommit_runs_all_new_gates(self):
        text = (ROOT / "scripts" / "pre-commit").read_text(encoding="utf-8")
        for name in self._REQUIRED:
            self.assertIn(name, text, f"快门禁未包含 {name}")

    def test_gate_entries_point_to_real_test_files(self):
        """清单里的每份用例必须真实存在，不能写错名字让门禁静默漏跑。"""
        for name in self._REQUIRED:
            self.assertTrue((ROOT / "tests" / name).is_file(), f"{name} 不存在")

    # 不在这里 subprocess 跑 `bash scripts/pre-commit`：本文件已进快门禁清单，
    # 从门禁内部再拉起门禁会无限递归。门禁是否退出 0 由提交前手动跑一次
    # 验证，写成自动化用例只会把「检查」变成「自我调用」。


class TestInitializeInstructionsMatchCoreSurface(unittest.TestCase):
    """initialize 短指令不能继续描述已被收敛掉的工具面。"""

    def test_instructions_no_longer_name_retired_core_tools(self):
        text = mcp_transport.handle_rpc("initialize", {})["instructions"]
        self.assertNotIn("argo_local_search", text)
        self.assertIn("argo_screenshot", text)
        self.assertIn("argo_research", text)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""test_review_round3.py — 2026-09-01 审查修复回归。

覆盖：
  - route 语言感知 combo：日文查询（汉字占比高）不得误入中文分支
  - mcp_server --call 退出码契约（isError / 裸 error → 1；成功 → 0）
  - mcp_handlers 未知工具 isError 形态
  - mcp_transport 裸 {"error":...} 识别（协议错误通道）
  - 长尾工具边界夹取（social max_results / job num / crawl max_pages）
  - argo_fetch mode=extract 输出结构级上限
  - argo_search include_domains / exclude_domains 域过滤
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("ARGO_STATE_DIR", tempfile.mkdtemp(prefix="argo_test_r3_"))


class TestLangAwareComboOrder(unittest.TestCase):
    """ja/ko 查询带汉字（CJK 占比 >0.15）时不得判为中文走中文分支。"""

    def _order(self, features, combo):
        from route import _lang_aware_combo_order
        return _lang_aware_combo_order(combo, features, "general", set())

    def test_japanese_with_kanji_pushes_zh_only_to_tail(self):
        # 「東京タワーの営業時間」：汉字 東京塔营業時間 6/10 → ratio 0.6，但 primary_lang=ja
        features = {"primary_lang": "ja", "chinese_ratio": 0.6}
        combo = ["zhihu", "hackernews", "local_bing"]
        out = self._order(features, combo)
        self.assertLess(out.index("hackernews"), out.index("zhihu"))

    def test_korean_not_treated_as_zh(self):
        features = {"primary_lang": "ko", "chinese_ratio": 0.2}
        out = self._order(features, ["zhihu", "hackernews"])
        self.assertLess(out.index("hackernews"), out.index("zhihu"))

    def test_zh_branch_kept_for_chinese(self):
        features = {"primary_lang": "zh", "chinese_ratio": 0.9}
        out = self._order(features, ["zhihu", "hackernews"])
        self.assertLess(out.index("zhihu"), out.index("hackernews"))

    def test_ratio_fallback_still_works_without_primary_lang(self):
        # primary_lang 缺失（旧调用方）时 ratio 兜底仍生效
        features = {"chinese_ratio": 0.8}
        out = self._order(features, ["zhihu", "hackernews"])
        self.assertLess(out.index("zhihu"), out.index("hackernews"))


class TestCallModeExitCode(unittest.TestCase):
    """--call 退出码：isError / 裸 error → 1，成功 → 0。"""

    def test_unknown_tool_exits_1(self):
        import mcp_server
        rc = mcp_server.call_mode("argo_nope", "{}")
        self.assertEqual(rc, 1)

    def test_is_error_tool_exits_1(self):
        import mcp_server
        rc = mcp_server.call_mode("argo_article", '{"url": "https://example.com/x"}')
        self.assertEqual(rc, 1)

    def test_invalid_payload_exits_2(self):
        import mcp_server
        self.assertEqual(mcp_server.call_mode("argo_search", "{bad json"), 2)


class TestUnknownToolShape(unittest.TestCase):
    """未知工具走 isError content 形态，与工具级错误同契约。"""

    def test_unknown_tool_is_error(self):
        from mcp_handlers import execute_tool
        result = execute_tool("argo_nope", {})
        self.assertTrue(result.get("isError"))
        self.assertIn("content", result)
        body = json.loads(result["content"][0]["text"])
        self.assertEqual(body["error"]["code"], -32601)

    def test_transport_detects_bare_error(self):
        # handle_rpc 的方法级错误仍是裸形态，transport 层据此走 JSON-RPC error 通道
        from mcp_transport import handle_rpc
        resp = handle_rpc("bogus/method", {})
        self.assertIn("error", resp)
        self.assertNotIn("content", resp)


class TestHandlerClamps(unittest.TestCase):
    """长尾工具边界夹取与 schema 同口径。"""

    def test_clamp_int(self):
        from mcp_handlers import _clamp_int
        self.assertEqual(_clamp_int(99, 5, 1, 20), 20)
        self.assertEqual(_clamp_int(0, 5, 1, 20), 1)
        self.assertEqual(_clamp_int("7", 5, 1, 20), 7)
        self.assertEqual(_clamp_int("abc", 5, 1, 20), 5)
        self.assertEqual(_clamp_int(None, 5, 1, 20), 5)

    def test_extract_output_capped(self):
        from mcp_handlers import _cap_extract_output
        big_table = [[f"cell-{i}-{j}" * 30 for j in range(4)] for i in range(3)]
        long_cell = [["x" * 1000, "y" * 1000]]
        out = _cap_extract_output({
            "tables": [big_table, long_cell] * 10,
            "metadata": {"title": "t" * 2000},
            "jsonld": [{"data": "z" * 9000}, {"ok": 1}] * 6,
            "url": "https://x",
        })
        self.assertLessEqual(len(out["tables"]), 12)
        for table in out["tables"]:
            for row in table:
                for cell in row:
                    self.assertLessEqual(len(str(cell)), 300)
        self.assertLessEqual(len(out["metadata"]["title"]), 500)
        self.assertLessEqual(len(out["jsonld"]), 8)
        self.assertTrue(out.get("jsonld_truncated"))
        # JSON 仍可解析（结构级裁剪，不做字符串截断）
        json.dumps(out, ensure_ascii=False)


class TestDomainFilter(unittest.TestCase):
    """argo_search 域过滤（include/exclude，含子域匹配）。"""

    def test_include_keeps_subdomains(self):
        from search import filter_results_by_domains
        results = [
            {"url": "https://github.com/a/b"},
            {"url": "https://api.github.com/v3"},
            {"url": "https://example.com/x"},
        ]
        kept, note = filter_results_by_domains(results, include_domains=["github.com"])
        self.assertEqual(len(kept), 2)
        self.assertIn("kept 2", note)

    def test_exclude_drops_domain(self):
        from search import filter_results_by_domains
        results = [
            {"url": "https://www.pinterest.com/pin/1"},
            {"url": "https://example.com/y"},
        ]
        kept, _ = filter_results_by_domains(results, exclude_domains=["pinterest.com"])
        self.assertEqual([r["url"] for r in kept], ["https://example.com/y"])

    def test_no_filters_returns_as_is(self):
        from search import filter_results_by_domains
        results = [{"url": "https://a.com"}]
        kept, note = filter_results_by_domains(results)
        self.assertEqual(kept, results)
        self.assertIsNone(note)

    def test_super_search_signature_accepts_domain_params(self):
        # 契约存在性：super_search 接受域过滤参数（实现路径由单测覆盖）
        import inspect
        import search as search_mod
        params = inspect.signature(search_mod.super_search).parameters
        self.assertIn("include_domains", params)
        self.assertIn("exclude_domains", params)


class TestNativeToolsSyncGate(unittest.TestCase):
    """native-tools.mjs 由 gen_native_tools.py 生成，漂移门禁必须生效。"""

    def test_gen_check_passes(self):
        import subprocess
        gen = Path(__file__).resolve().parent.parent / "scripts" / "gen_native_tools.py"
        r = subprocess.run([sys.executable, str(gen), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)


if __name__ == "__main__":
    unittest.main()

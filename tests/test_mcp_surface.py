#!/usr/bin/env python3
"""test_mcp_surface.py — 新补 MCP 工具（extract/preflight/answer/watch/cite）行为冒烟。

全部离线：网络点打桩在读取处（citations.fetch_metadata / extract._extract_fetch /
answer.seltz_answer）；watch 只走本地状态文件的 list/remove；preflight 不开
probe 走纯本地规则。execute_tool 直接调用（不经 transport）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import mcp_handlers  # noqa: E402


def _payload(result):
    return json.loads(result["content"][0]["text"])


class TestArgoCite(unittest.TestCase):
    def test_doi_list_resolves_to_citations(self):
        import citations

        fake_meta = {"doi": "10.1000/x", "title": "Argo: unified search",
                     "authors": [{"family": "Wang", "given": "Taxue"}],
                     "year": "2026", "container": "Journal of Search", "type": "article-journal"}
        with patch.object(citations, "fetch_metadata", lambda doi: fake_meta):
            result = mcp_handlers.execute_tool(
                "argo_cite", {"dois": ["10.1000/aaa", "10.1000/bbb"], "style": "apa"})
        self.assertFalse(result.get("isError"))
        payload = _payload(result)
        self.assertEqual(payload["style"], "apa")
        self.assertEqual(len(payload["citations"]), 2)
        self.assertTrue(all("Argo" in c["citation"] for c in payload["citations"]))

    def test_single_doi_error_isolated(self):
        import citations

        meta = {"doi": "10.1000/good", "title": "T",
                "authors": [{"family": "Wang", "given": "T"}], "year": "2026"}

        def flaky(doi):
            if "bad" in doi:
                raise RuntimeError("upstream 500")
            return meta

        with patch.object(citations, "fetch_metadata", flaky):
            result = mcp_handlers.execute_tool(
                "argo_cite", {"dois": ["10.1000/bad", "10.1000/good"], "style": "gbt7714"})
        payload = _payload(result)
        self.assertIn("error", payload["citations"][0])
        self.assertIn("citation", payload["citations"][1])


class TestArgoPreflight(unittest.TestCase):
    def test_local_rules_offline(self):
        result = mcp_handlers.execute_tool(
            "argo_preflight", {"urls": ["https://example.com/a",
                                         "https://mp.weixin.qq.com/s/xyz"]})
        self.assertFalse(result.get("isError"))
        payload = _payload(result)
        self.assertGreaterEqual(len(payload.get("results", payload.get("items", []))), 1)

    def test_empty_urls_is_tool_error(self):
        result = mcp_handlers.execute_tool("argo_preflight", {"urls": []})
        self.assertTrue(result.get("isError"))


class TestArgoWatch(unittest.TestCase):
    def test_list_and_remove_roundtrip(self):
        result = mcp_handlers.execute_tool("argo_watch", {"action": "list"})
        self.assertFalse(result.get("isError"))
        known = _payload(result)
        target = None
        if isinstance(known, list) and known:
            target = known[0].get("url")
        if target:
            rm = mcp_handlers.execute_tool("argo_watch",
                                           {"action": "remove", "url": target})
            self.assertTrue(_payload(rm).get("removed") in (True, False))

    def test_add_requires_url(self):
        result = mcp_handlers.execute_tool("argo_watch", {"action": "add"})
        self.assertTrue(result.get("isError"))

    def test_unknown_action_rejected(self):
        result = mcp_handlers.execute_tool("argo_watch", {"action": "purge"})
        self.assertTrue(result.get("isError"))


class TestArgoExtract(unittest.TestCase):
    def test_tables_and_metadata(self):
        import extract

        fake_html = ("<html><head><title>T</title>"
                     "<meta name=\"description\" content=\"D\"></head><body>"
                     "<table><tr><th>h</th></tr><tr><td>v</td></tr></table></body></html>")
        with patch.object(extract, "_extract_fetch",
                          lambda url, mc, t: {"success": True, "html": fake_html}):
            result = mcp_handlers.execute_tool(
                "argo_extract", {"url": "https://example.com/x", "mode": "all"})
        self.assertFalse(result.get("isError"))
        payload = _payload(result)
        self.assertIn("metadata", payload)
        self.assertEqual(payload["metadata"].get("title"), "T")
        self.assertTrue(payload["tables"])

    def test_fetch_failure_is_tool_error(self):
        import extract
        with patch.object(extract, "_extract_fetch",
                          lambda url, mc, t: {"success": False, "error": "timeout"}):
            result = mcp_handlers.execute_tool("argo_extract", {"url": "https://x.example"})
        self.assertTrue(result.get("isError"))


class TestArgoAnswer(unittest.TestCase):
    def test_answer_passthrough(self):
        import answer
        fake = {"answer": "42", "citations": [{"url": "https://x"}]}
        with patch.object(answer, "seltz_answer",
                          lambda *a, **k: (fake, "")):
            result = mcp_handlers.execute_tool("argo_answer", {"query": "什么是对的"})
        self.assertFalse(result.get("isError"))
        self.assertEqual(_payload(result)["answer"], "42")

    def test_upstream_error_is_tool_error(self):
        import answer
        with patch.object(answer, "seltz_answer",
                          lambda *a, **k: (None, "未知 scope")):
            result = mcp_handlers.execute_tool("argo_answer", {"query": "x"})
        self.assertTrue(result.get("isError"))


class TestArgoLocalSearchScopeGuard(unittest.TestCase):
    """argo_local_search 的宽泛根守卫（2026-09-27）。

    缺省 path="~" 时旧实现对整个 home 跑 rg——实测 30 s 后报错返回，与 CLI
    侧已修掉的 22.6 s 事故同一形态。守卫必须在发起子进程之前拒绝，且显式
    传的窄路径不受影响（打桩 seek.py 不存在来证明没有起进程）。
    """

    def test_default_home_path_rejected(self):
        result = mcp_handlers.execute_tool(
            "argo_local_search", {"query": "x", "max_results": 2})
        payload = _payload(result)
        self.assertEqual(payload["count"], 0)
        self.assertTrue(any("过宽" in e for e in payload["errors"]),
                        f"宽泛根应被拒绝并说清原因: {payload['errors']}")

    def test_tmp_path_rejected(self):
        result = mcp_handlers.execute_tool(
            "argo_local_search", {"query": "x", "path": "/tmp"})
        payload = _payload(result)
        self.assertTrue(any("过宽" in e for e in payload["errors"]))

    def test_narrow_path_not_blocked_by_guard(self):
        """窄路径必须过守卫（子进程照常起；这里用不存在的 seek 路径证明守卫放行）。"""
        import os
        import tempfile
        narrow = tempfile.mkdtemp(prefix="argo-narrow-")
        with patch("mcp_handlers._seek_py", return_value="/nonexistent-seek.py"):
            result = mcp_handlers.execute_tool(
                "argo_local_search", {"query": "x", "path": narrow})
        payload = _payload(result)
        self.assertFalse(any("过宽" in e for e in payload["errors"]),
                         "窄路径不该被守卫拦截")
        self.assertTrue(os.path.isdir(narrow))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""test_mcp_decoupled_modules.py — 验证拆分后的 mcp_compact, mcp_local, mcp_warm 核心契约。"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_compact
import mcp_local
import mcp_warm


class TestMcpCompact(unittest.TestCase):
    def test_trim_snippet(self):
        self.assertIsNone(mcp_compact._trim_snippet(None))
        self.assertIsNone(mcp_compact._trim_snippet(""))
        self.assertEqual(mcp_compact._trim_snippet("abc", 5), "abc")
        self.assertEqual(mcp_compact._trim_snippet("abcdefgh", 4), "abcd")

    def test_compact_search_result(self):
        raw = {
            "query": "test query",
            "engine": "auto",
            "count": 1,
            "results": [
                {"title": "Title 1", "url": "https://example.com/1", "snippet": "A" * 200, "source": "bing", "score": 0.88}
            ],
            "fetch_required": True,
        }
        res = mcp_compact._compact_search_result(raw, summary=True)
        self.assertEqual(res["query"], "test query")
        self.assertTrue(res["fetch_required"])
        self.assertEqual(len(res["results"]), 1)
        self.assertLessEqual(len(res["results"][0]["snippet"]), 80)

    def test_cap_extract_output(self):
        big_tables = [[["cell"] * 5 for _ in range(5)] for _ in range(20)]
        out = {"tables": big_tables, "metadata": {"title": "x" * 600}}
        capped = mcp_compact._cap_extract_output(out)
        self.assertEqual(len(capped["tables"]), 12)
        self.assertTrue(capped["tables_truncated"])
        self.assertLessEqual(len(capped["metadata"]["title"]), 500)


class TestMcpLocal(unittest.TestCase):
    def test_local_read_whitelist_and_preview(self):
        whitelist = mcp_local._local_read_whitelist()
        self.assertIsInstance(whitelist, list)

        # 尝试读取自身（如果不在白名单应抛出 PermissionError）
        with self.assertRaises(PermissionError):
            mcp_local._local_read_preview(__file__, max_chars=100)

    def test_seek_py_locatable(self):
        seek_path = mcp_local._seek_py()
        self.assertIsInstance(seek_path, str)


class TestMcpWarm(unittest.TestCase):
    def test_resolve_research_profile(self):
        # 模拟空的 profiles
        prof, key = mcp_warm._resolve_research_profile({"topic": "nonexistent_topic_123"}, lambda _: None)
        self.assertIsNone(prof)
        self.assertIsNone(key)


if __name__ == "__main__":
    unittest.main()

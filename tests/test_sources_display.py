#!/usr/bin/env python3
"""信源标准化 + 日常/研究展示边界。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import search as search_mod  # noqa: E402
import archive_run as ar  # noqa: E402


class TestBuildSources(unittest.TestCase):
    def test_build_sources_skips_empty_url(self):
        rows = [
            {"title": "A", "url": "https://a.example/", "source": "e1", "snippet": "sa"},
            {"title": "B", "url": "", "source": "e2"},
            {"title": "C", "url": "https://c.example/", "source": "e3"},
        ]
        src = search_mod.build_sources(rows)
        self.assertEqual(len(src), 2)
        self.assertEqual(src[0]["ref"], 1)
        self.assertEqual(src[1]["ref"], 2)
        self.assertEqual(src[0]["url"], "https://a.example/")


class TestFormatSerp(unittest.TestCase):
    def test_links_sink_to_bottom(self):
        payload = {
            "count": 2,
            "elapsed_ms": 12,
            "engine": "mock",
            "mode": "auto",
            "results": [
                {"title": "标题一", "url": "https://one.example/x", "snippet": "摘要一", "source": "m", "score": 0.9},
                {"title": "标题二", "url": "https://two.example/y", "snippet": "摘要二", "source": "m", "score": 0.8},
            ],
            "sources": search_mod.build_sources([
                {"title": "标题一", "url": "https://one.example/x", "source": "m", "snippet": "摘要一"},
                {"title": "标题二", "url": "https://two.example/y", "source": "m", "snippet": "摘要二"},
            ]),
        }
        text = search_mod.format_text_output(payload)
        self.assertIn("── 相关信源 ──", text)
        # 正文区在信源区之前出现标题
        i_title = text.index("标题一")
        i_src = text.index("── 相关信源 ──")
        self.assertLess(i_title, i_src)
        # 完整 URL 应出现在信源区（允许正文不刷 URL——至少信源区有）
        self.assertIn("https://one.example/x", text[i_src:])
        self.assertIn("[1]", text)


class TestArchivePolicy(unittest.TestCase):
    def test_archive_writes_sources_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = {
                "query": "q",
                "status": "completed",
                "engine": "mock",
                "results": [
                    {"title": "T", "url": "https://t.example/", "snippet": "s", "source": "m"},
                ],
                "count": 1,
            }
            meta = ar.write_search_archive(payload, root=root, tag="unit")
            sp = Path(meta["paths"]["sources"])
            self.assertTrue(sp.is_file())
            lines = sp.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("https://t.example/", lines[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

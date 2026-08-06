#!/usr/bin/env python3
"""local-search HTML 选择器 0 命中时的启发式回退解析。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sub-skills" / "local-search"))

from search_v3 import _fallback_extract  # noqa: E402


class TestFallbackExtract(unittest.TestCase):
    def test_extracts_result_like_links(self):
        html = """
        <html><body>
          <nav><a href="/login">登录入口</a></nav>
          <div class="result">
            <a href="https://example.com/a">Python asyncio 教程完整版</a>
            <p>异步编程入门指南</p>
          </div>
          <div class="result">
            <a href="/rel/path">相对路径标题足够长</a>
          </div>
          <a href="#frag">太短</a>
          <a href="javascript:void(0)">假链接标题足够长了吗</a>
        </body></html>
        """
        out = _fallback_extract(html, "local_bing", "https://www.bing.com", max_items=5)
        self.assertGreaterEqual(len(out), 1)
        urls = [r["url"] for r in out]
        self.assertTrue(any("example.com/a" in u for u in urls))
        # 相对路径应拼到 base
        self.assertTrue(any(u.startswith("https://www.bing.com/") for u in urls))
        for r in out:
            self.assertEqual(r["source"], "local_bing")
            self.assertTrue(r.get("_fallback"))
            self.assertLessEqual(r["score"], 0.45)

    def test_empty_html(self):
        self.assertEqual(_fallback_extract("", "x", "https://x.com"), [])


if __name__ == "__main__":
    unittest.main()

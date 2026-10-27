#!/usr/bin/env python3
"""crawl 编排测试（假抓取，不联网）。

锁定旧版四个实际缺陷：
  1. 每页只跟进前 5 个链接 → 现在全量跟进
  2. CSS/图片被当页面抓 → 前沿层过滤
  3. 同一页的不同正文重复输出 → 正文哈希去重（但重复页的链接仍要跟进）
  4. 正文固定截 500 字 → --max-chars / --full / --query 三档
外加范围判定：叶子页只覆盖所在栏目。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import crawl
from crawl_policy import HostGate


class FakeClock:
    def __call__(self):
        return 0.0

    def sleep(self, _seconds):
        return None


def _gate():
    return HostGate(interval=0.0, delay_resolver=lambda _url: None,
                    clock=FakeClock(), sleep=FakeClock().sleep)


def _page(body: str, content: str, title: str = "t") -> dict:
    return {"success": True, "html": f"<html><body>{body}</body></html>",
            "content": content, "title": title}


def _site() -> dict[str, dict]:
    start_body = ('<a href="/a">a</a><a href="/b">b</a>'
                  '<a href="/style.css">css</a><a href="/img.png">img</a>'
                  '<a href="/dup1">d1</a><a href="/dup2">d2</a>'
                  '<a href="/feed.xml">feed</a>'
                  '<a href="https://other.com/x">offsite</a>'
                  '<a href="/x/more/more/more">bad</a>'
                  + "".join(f'<a href="/p{i}">p{i}</a>' for i in range(1, 9)))
    pages = {
        "https://ex.com/start": _page(start_body, "HOME CONTENT " * 20, "home"),
        "https://ex.com/a": _page("", "ALPHA " * 50, "a"),
        "https://ex.com/b": _page("", "BETA " * 50, "b"),
        "https://ex.com/dup1": _page("", "DUPLICATE CONTENT " * 20, "d1"),
        "https://ex.com/dup2": _page('<a href="/after-dup">next</a>',
                                     "DUPLICATE CONTENT " * 20, "d2"),
        "https://ex.com/after-dup": _page("", "AFTER DUP " * 20, "after"),
        "https://ex.com/feed.xml": {"success": True,
                                    "html": "<?xml version='1.0'?><rss></rss>",
                                    "content": "xml", "title": "feed"},
    }
    for i in range(1, 9):
        pages[f"https://ex.com/p{i}"] = _page("", f"PAGE {i} " * 30, f"p{i}")
    return pages


class CrawlTest(unittest.TestCase):
    def setUp(self):
        self.pages = _site()
        self.calls: list[str] = []
        self._orig = crawl.fetch_page

        def fake_fetch(url, max_chars, timeout, raw=True):
            self.calls.append(url)
            return self.pages.get(url, {"success": False, "error": "404"})

        crawl.fetch_page = fake_fetch

    def tearDown(self):
        crawl.fetch_page = self._orig

    def _run(self, **kwargs):
        kwargs.setdefault("policy", _gate())
        kwargs.setdefault("max_pages", 20)
        kwargs.setdefault("max_depth", 2)
        kwargs.setdefault("timeout", 1)
        return crawl.crawl_bfs("https://ex.com/start", **kwargs)

    def test_all_links_followed_not_just_five(self):
        result = self._run()
        for i in range(1, 9):
            self.assertIn(f"https://ex.com/p{i}", self.calls)
        self.assertGreaterEqual(result["total"], 11)

    def test_assets_and_offsite_never_fetched(self):
        result = self._run()
        self.assertNotIn("https://ex.com/style.css", self.calls)
        self.assertNotIn("https://ex.com/img.png", self.calls)
        self.assertNotIn("https://other.com/x", self.calls)
        self.assertEqual(result["skipped"]["asset"], 2)
        self.assertGreaterEqual(result["skipped"]["offsite"], 1)
        self.assertGreaterEqual(result["skipped"]["pathological"], 1)

    def test_duplicate_content_emitted_once_but_links_followed(self):
        result = self._run()
        urls = [p["url"] for p in result["pages"]]
        self.assertIn("https://ex.com/dup1", urls)
        self.assertNotIn("https://ex.com/dup2", urls)
        self.assertEqual(result["skipped"]["duplicate"], 1)
        # 重复页面的链接仍要跟进，否则分页/列表页会整段丢失
        self.assertIn("https://ex.com/after-dup", self.calls)

    def test_non_html_skipped(self):
        result = self._run()
        self.assertGreaterEqual(result["skipped"]["non_html"], 1)
        self.assertNotIn("https://ex.com/feed.xml", [p["url"] for p in result["pages"]])

    def test_max_chars_and_full(self):
        short = self._run(max_pages=2, max_chars=50)
        self.assertTrue(all(len(p["content"]) <= 50 for p in short["pages"]))
        full = self._run(max_pages=2, max_chars=0)
        self.assertTrue(any(len(p["content"]) > 50 for p in full["pages"]))

    def test_query_focus_keeps_relevant_block(self):
        content = ("python asyncio event loop coroutine " * 80
                   + "\n\n"
                   + "cooking recipe kitchen tomato " * 80)
        self.pages["https://ex.com/start"] = _page("", content, "mixed")
        result = self._run(max_pages=1, query="asyncio")
        body = result["pages"][0]["content"].lower()
        self.assertIn("asyncio", body)
        self.assertNotIn("cooking", body)

    def test_scope_leaf_excludes_other_sections(self):
        self.pages["https://ex.com/blog/post.html"] = _page(
            '<a href="/blog/next.html">n</a><a href="/other/x">o</a>',
            "POST " * 30)
        self.pages["https://ex.com/blog/next.html"] = _page("", "NEXT " * 30)
        self.pages["https://ex.com/other/x"] = _page("", "OTHER " * 30)
        result = crawl.crawl_bfs("https://ex.com/blog/post.html", max_pages=5,
                                 max_depth=1, timeout=1, policy=_gate())
        urls = [p["url"] for p in result["pages"]]
        self.assertIn("https://ex.com/blog/next.html", urls)
        self.assertNotIn("https://ex.com/other/x", urls)
        self.assertGreaterEqual(result["skipped"]["out_of_scope"], 1)


if __name__ == "__main__":
    unittest.main()

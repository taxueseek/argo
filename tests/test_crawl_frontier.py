#!/usr/bin/env python3
"""crawl_frontier 前沿规则测试（纯函数，不联网）。

锁定四件事：
  1. URL 归一：追踪参数、会话 ID、参数序由 url_canon 兜底
  2. 资产与坏路径：CSS/JPG/PDF 不抓，重复路径段/过深/过长不抓
  3. 链接提取：全量提取（不是旧版前 5 个），按归一化去重
  4. 范围：域名 / 严格路径 / 叶子页上提到栏目
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from crawl_frontier import (
    Frontier,
    canonical,
    extract_links,
    is_asset,
    is_pathological,
    scope_prefix,
    strip_session_params,
)


class TestCanonical(unittest.TestCase):
    def test_tracking_params_dropped(self):
        url = canonical("https://ex.com/post?utm_source=x&id=7&fbclid=y")
        self.assertIn("id=7", url)
        self.assertNotIn("utm_source", url)
        self.assertNotIn("fbclid", url)

    def test_session_path_param_dropped(self):
        u = strip_session_params("https://ex.com/a;jsessionid=ABC123/b?id=1")
        self.assertNotIn("jsessionid", u)
        self.assertIn("/a/b", u)

    def test_session_query_param_dropped(self):
        u = strip_session_params("https://ex.com/a?id=1&PHPSESSID=deadbeef")
        self.assertIn("id=1", u)
        self.assertNotIn("PHPSESSID", u)

    def test_relative_link_absolutized(self):
        self.assertEqual(canonical("../b", "https://ex.com/a/c"),
                         "https://ex.com/b")


class TestAssetAndPathological(unittest.TestCase):
    def test_assets_rejected(self):
        for url in ("https://ex.com/a.css", "https://ex.com/a.JPG?x=1",
                    "https://ex.com/doc.pdf", "https://ex.com/font.woff2"):
            self.assertTrue(is_asset(url), url)

    def test_html_like_urls_allowed(self):
        for url in ("https://ex.com/post", "https://ex.com/a.html",
                    "https://ex.com/api/list?page=2"):
            self.assertFalse(is_asset(url), url)

    def test_repeated_segment_is_pathological(self):
        self.assertTrue(is_pathological("https://ex.com/a/more/more/more"))
        self.assertFalse(is_pathological("https://ex.com/a/b/c"))

    def test_depth_and_length_limits(self):
        self.assertTrue(is_pathological("https://ex.com/" + "/".join("abcdefghi")))
        self.assertTrue(is_pathological("https://ex.com/" + "x" * 2100))

    def test_query_length_limit(self):
        self.assertTrue(is_pathological("https://ex.com/a?" + "q" * 600))


class TestExtractLinks(unittest.TestCase):
    def test_all_links_not_just_five(self):
        body = "".join(f'<a href="/p{i}">p{i}</a>' for i in range(1, 9))
        links = extract_links(f"<html><body>{body}</body></html>", "https://ex.com/")
        self.assertEqual(len(links), 8)
        self.assertIn("https://ex.com/p8", links)

    def test_duplicates_and_fragments_collapsed(self):
        html = ('<a href="/a?utm_source=x">1</a>'
                '<a href="/a#top">2</a>'
                '<a href="https://ex.com/a">3</a>')
        links = extract_links(html, "https://ex.com/")
        self.assertEqual(links, ["https://ex.com/a"])

    def test_non_http_schemes_skipped(self):
        html = ('<a href="mailto:x@y.com">m</a><a href="javascript:void(0)">j</a>'
                '<a href="/ok">o</a>')
        self.assertEqual(extract_links(html, "https://ex.com/"),
                         ["https://ex.com/ok"])


class TestScope(unittest.TestCase):
    def test_leaf_widens_to_section(self):
        self.assertEqual(scope_prefix("https://ex.com/blog/post.html"), "/blog/")
        self.assertEqual(scope_prefix("https://ex.com/blog/post"), "/blog/")

    def test_trailing_slash_seed_keeps_prefix(self):
        self.assertEqual(scope_prefix("https://ex.com/docs/python/"), "/docs/python/")

    def test_auto_widens_path_scope_keeps_strict(self):
        self.assertEqual(scope_prefix("https://ex.com/docs/python"), "/docs/")
        self.assertEqual(scope_prefix("https://ex.com/docs/python", "path"),
                         "/docs/python/")

    def test_domain_scope_is_root(self):
        self.assertEqual(scope_prefix("https://ex.com/docs/python", "domain"), "/")

    def test_frontier_rejects_offsite_asset_out_of_scope(self):
        f = Frontier("https://ex.com/blog/post.html")
        self.assertEqual(f.accept("https://other.com/x"), (False, "offsite"))
        self.assertEqual(f.accept("https://ex.com/blog/a.css"), (False, "asset"))
        self.assertEqual(f.accept("https://ex.com/other/a"), (False, "out_of_scope"))
        self.assertEqual(f.accept("https://ex.com/blog/a"), (True, ""))

    def test_domain_scope_accepts_any_path(self):
        f = Frontier("https://ex.com/blog/post.html", scope="domain")
        self.assertEqual(f.accept("https://ex.com/other/a"), (True, ""))


if __name__ == "__main__":
    unittest.main()

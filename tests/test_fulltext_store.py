#!/usr/bin/env python3
"""全文存档回归门：预算只裁交付视图，不再销毁证据。

背景：`max_chars` 原本同时管「交给 Agent 多少」与「取回物留多少」。前者必须有界，
后者不该有损。合成一个机制的后果实测是——默认档 8,000 字抓 Wikipedia 的
32,789 字正文，剩下 75% 当场销毁，既无副本、无标记、也无法回读（同批 15 个
站点里 8 个被截断，最严重的丢掉 93%）。

本文件锁三件事：
  1. 截断必须可见（`truncated` / `full_length`）
  2. 被裁的部分必须可回读（`full_text_path` + `load`）
  3. 存档是增值层，任何失败都不得影响抓取本身
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_v3  # noqa: E402
import fulltext_store as fs  # noqa: E402


class TestFulltextStore(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="argo-ft-")
        os.environ["ARGO_FULLTEXT_DIR"] = self.dir

    def tearDown(self):
        os.environ.pop("ARGO_FULLTEXT_DIR", None)
        os.environ.pop("ARGO_FULLTEXT", None)

    def test_roundtrip(self):
        url = "https://example.com/a"
        body = "正文。" * 5000
        path = fs.save(url, body, "text")
        self.assertTrue(path and Path(path).is_file())
        self.assertEqual(fs.load(url, "text"), body)

    def test_kinds_do_not_overwrite(self):
        """正文与 HTML 是两份东西，键必须隔离。"""
        url = "https://example.com/b"
        fs.save(url, "正文内容" * 100, "text")
        fs.save(url, "<div>html</div>" * 100, "html")
        self.assertEqual(fs.load(url, "text"), "正文内容" * 100)
        self.assertEqual(fs.load(url, "html"), "<div>html</div>" * 100)

    def test_load_by_explicit_path(self):
        url = "https://example.com/c"
        p = fs.save(url, "可回读正文" * 50, "text")
        self.assertEqual(fs.load_path(p), "可回读正文" * 50)

    def test_disabled_by_env(self):
        os.environ["ARGO_FULLTEXT"] = "0"
        self.assertIsNone(fs.save("https://x.com/d", "content" * 100, "text"))
        self.assertFalse(fs.enabled())

    def test_oversized_not_archived(self):
        """超过单档上限的内容不留存——出现这种体积多半是误抓的数据文件。"""
        huge = "x" * (fs.MAX_FILE_BYTES + 10)
        self.assertIsNone(fs.save("https://x.com/e", huge, "text"))

    def test_missing_returns_none(self):
        self.assertIsNone(fs.load("https://never-fetched.example/x", "text"))
        self.assertIsNone(fs.load_path("/nonexistent/path.md"))

    def test_eviction_by_count(self):
        import time
        orig = fs.MAX_FILES
        fs.MAX_FILES = 3
        try:
            for i in range(6):
                fs.save(f"https://x.com/{i}", f"body {i} " * 100, "text")
                time.sleep(0.01)
            self.assertLessEqual(len(fs.entries()), 3)
        finally:
            fs.MAX_FILES = orig

    def test_index_reconciles_after_eviction(self):
        """淘汰发生后台账必须与现存正文对齐，死行清零（2026-11-14 收口）。

        历史缺口：台账只追加不截断，正文有 MAX_FILES 淘汰而台账没有——
        生产库实测 688 行里 386 行（56%）指向已淘汰文件。
        """
        import time
        idx = Path(self.dir) / "index.jsonl"
        orig = fs.MAX_FILES
        fs.MAX_FILES = 2
        try:
            for i in range(5):
                fs.save(f"https://y.com/{i}", f"body {i} " * 100, "text")
                time.sleep(0.01)
            alive = {e["path"] for e in fs.entries()}
            self.assertTrue(alive)
            lines = [ln for ln in
                     idx.read_text(encoding="utf-8").splitlines() if ln]
            self.assertEqual(
                len(lines), len(alive),
                f"台账行数 {len(lines)} != 现存正文数 {len(alive)}——死行未清")
            for ln in lines:
                self.assertIn(json.loads(ln)["path"], alive)
        finally:
            fs.MAX_FILES = orig

    def test_reconcile_survives_corrupt_lines(self):
        """台账里的脏行不得卡住对账：坏行丢弃，好行保留。"""
        fs.save("https://z.com/a", "body a " * 100, "text")
        idx = Path(self.dir) / "index.jsonl"
        idx.write_text("{broken json\n" + idx.read_text(encoding="utf-8"),
                       encoding="utf-8")
        fs._reconcile_index()
        lines = [ln for ln in idx.read_text(encoding="utf-8").splitlines() if ln]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["url"], "https://z.com/a")

    def test_save_never_raises(self):
        """写盘失败不得上抛——存档是增值能力，不是抓取的前置条件。"""
        os.environ["ARGO_FULLTEXT_DIR"] = "/proc/nonexistent-dir/x"
        self.assertIsNone(fs.save("https://x.com/f", "body" * 100, "text"))


class TestTruncationSignals(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="argo-ft-")
        os.environ["ARGO_FULLTEXT_DIR"] = self.dir

    def tearDown(self):
        os.environ.pop("ARGO_FULLTEXT_DIR", None)

    def test_truncation_is_visible(self):
        """只给一部分就必须说明还有多少，否则调用方会当全文用。"""
        text = "# 标题\n\n" + "正文段落。" * 2000
        r = fetch_v3._markdown_result("https://x.com/t", text, 8000, "http_md")
        self.assertEqual(r["length"], 8000)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["full_length"], len(text))
        self.assertTrue(Path(r["full_text_path"]).is_file())

    def test_no_truncation_no_archive(self):
        """没截断就没有副本被销毁，不该写盘。"""
        text = "# 短\n\n" + "正文。" * 100
        r = fetch_v3._markdown_result("https://x.com/u", text, 8000, "http_md")
        self.assertFalse(r["truncated"])
        self.assertEqual(r["full_length"], len(text))
        self.assertNotIn("full_text_path", r)

    def test_full_length_not_clobbered_by_html(self):
        """正文与 HTML 同次被裁时，`full_length` 必须指正文。

        历史 bug：两者都往同名键写，后写的 HTML 覆盖了正文的数字——
        实测把 32,789 字的正文报成 533,612 字节的 HTML。
        """
        html = "<html><body><article><p>" + "段落。" * 30000 + "</p></article></body></html>"
        r = fetch_v3._make_result("https://x.com/v", html, 8000, "http")
        self.assertEqual(r["length"], 8000)
        self.assertTrue(r["truncated"])
        self.assertLess(r["full_length"], len(html), "full_length 指向 HTML 了")
        self.assertEqual(r["full_length"], len(fetch_v3.extract_content(html, 0)[0]))
        self.assertIn("full_html_path", r)

    def test_title_read_from_full_text(self):
        """标题从完整正文取：交付视图被裁到很短时也要能取到标题。"""
        text = "---\ntitle: 完整标题\n---\n\n" + "正文。" * 500
        r = fetch_v3._markdown_result("https://x.com/w", text, 300, "http_md")
        self.assertEqual(r["title"], "完整标题")
        self.assertEqual(r["length"], 300)


class TestUnlimitedPath(unittest.TestCase):
    """`max_chars<=0` 表示「要全文」：不得被预算机制悄悄截断或换源。"""

    def test_extract_content_zero_means_unlimited(self):
        html = "<html><body><article><p>" + "段落。" * 8000 + "</p></article></body></html>"
        full, _ = fetch_v3.extract_content(html, 0)
        cut, _ = fetch_v3.extract_content(html, 500)
        self.assertEqual(len(cut), 500)
        self.assertGreater(len(full), 500)

    def test_cut_helper(self):
        self.assertEqual(fetch_v3._cut("abcdef", 3), "abc")
        self.assertEqual(fetch_v3._cut("abcdef", 0), "abcdef")
        self.assertEqual(fetch_v3._cut("abcdef", -1), "abcdef")
        self.assertEqual(fetch_v3._cut("", 5), "")

    def test_cache_cannot_satisfy_unlimited(self):
        """要全文的请求不得命中只存裁剪副本的缓存。"""
        hit = {"content": "x" * 8000, "_max_chars": 8000}
        self.assertTrue(fetch_v3._cache_content_too_short(hit, 0))
        self.assertFalse(fetch_v3._cache_content_too_short(hit, 8000))


class TestChallengeScanWindow(unittest.TestCase):
    def test_weak_marker_deep_in_document_is_not_a_challenge(self):
        """大页面中途出现裸词 cloudflare 不算反爬壳。

        实测 Wikipedia 的 HTML 首个命中在第 241,552 字符、Astro 在第 78,009
        字符，都在脚本与脚注里。扫全文的后果是「页面越长越像反爬壳」——
        同一站点把 --max-chars 调大就会悄悄换掉抓取方式。
        """
        body = ("<html><body><article><p>" + "段落内容。" * 20000
                + "</p><script>var cdn='cloudflare';</script></article></body></html>")
        r = fetch_v3._make_result("https://x.com/y", body, 0, "http")
        self.assertGreater(len(body), fetch_v3._CF_SCAN_CHARS * 2)
        self.assertFalse(fetch_v3._needs_browser(r), "长文档里的裸词被当成反爬壳")

    def test_real_challenge_in_head_still_detected(self):
        body = ("<html><head><title>Just a moment...</title></head><body>"
                "Checking your browser before accessing<hr></body></html>")
        r = fetch_v3._make_result("https://x.com/z", body, 8000, "http")
        self.assertTrue(fetch_v3._needs_browser(r))


if __name__ == "__main__":
    unittest.main()

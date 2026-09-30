#!/usr/bin/env python3
"""本地信息复用回归门：让「已经取回的东西」在任何后续动作里都可被察觉。

背景（2026-09-17 审计）：Agent 的信息工作流是「发现 → 获取 → 复用」三段。
前两段有缓存，第三段**没有入口**——已经被抓取过的 URL 再次出现在搜索结果里
时，结果只带核验分（`has_fetched_evidence` / `post_fetch_absorption`），
看不出「这篇的正文我本地就有」，更不知道全文在哪。想核对原文只能重新联网。

本文件锁三件事：
  1. 搜索结果必须标出本地已有的正文及其位置（零联网）
  2. 证据分与正文同条目存放，证据不得脱离正文单独存在
  3. 登录态正文的存档必须与公共分区隔离
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fulltext_store as fs  # noqa: E402
from cache import FETCH_EVIDENCE_KEY, SearchCache  # noqa: E402


class TestLocalBodyIndex(unittest.TestCase):
    """搜索结果里的「本地已有正文」标注。"""

    def setUp(self):
        # 恢复而非 pop：conftest 把 ARGO_STATE_DIR 指到会话隔离目录，
        # tearDown 一旦 pop 掉，本文件之后跑的测试会回落到真实用户缓存
        # （2026-09-30 实锤：污染生产 cache.db + 顺序依赖假红）。
        self._prev_state = os.environ.get("ARGO_STATE_DIR")
        os.environ["ARGO_STATE_DIR"] = tempfile.mkdtemp(prefix="argo-lb-")
        os.environ["ARGO_FULLTEXT_DIR"] = tempfile.mkdtemp(prefix="argo-lbft-")
        self.c = SearchCache()

    def tearDown(self):
        os.environ.pop("ARGO_FULLTEXT_DIR", None)
        if self._prev_state is None:
            os.environ.pop("ARGO_STATE_DIR", None)
        else:
            os.environ["ARGO_STATE_DIR"] = self._prev_state

    def test_empty_for_unknown_urls(self):
        self.assertEqual(self.c.local_status(["https://never.example/x"]), {})

    def test_reports_cached_body(self):
        """只缓存、无存档（未截断的页）：source=cache。"""
        self.c.set_fetch("https://a.com/1", {
            "url": "https://a.com/1", "content": "正文" * 100, "length": 8000,
            "success": True, "truncated": False, "full_length": 8000}, ttl=600)
        got = self.c.local_status(["https://a.com/1"])["https://a.com/1"]["body"]
        self.assertEqual(got["length"], 8000)
        self.assertFalse(got["truncated"])
        self.assertEqual(got["source"], "cache")
        self.assertNotIn("full_text_path", got)

    def test_reports_cache_plus_archive(self):
        """正文截断过：缓存给短视图，存档给全文——两者都要报出来。"""
        url = "https://a.com/1b"
        path = fs.save(url, "完整正文" * 1000, "text")
        self.c.set_fetch(url, {
            "url": url, "content": "正文" * 100, "length": 8000,
            "success": True, "truncated": True, "full_length": 20000,
            "full_text_path": path}, ttl=600)
        got = self.c.local_status([url])[url]["body"]
        self.assertEqual(got["source"], "cache+archive")
        self.assertTrue(got["truncated"])
        self.assertEqual(got["full_length"], 20000)
        self.assertEqual(got["full_text_path"], path)

    def test_reports_archive_when_cache_expired(self):
        """缓存有 TTL，存档按 LRU 存活更久——只有存档时也要报出来。"""
        url = "https://a.com/2"
        path = fs.save(url, "完整正文" * 500, "text")
        self.assertTrue(path)
        got = self.c.local_status([url]).get(url, {}).get("body")
        self.assertIsNotNone(got, "只有存档时也应报出本地已有正文")
        self.assertEqual(got["source"], "archive")
        self.assertEqual(got["full_text_path"], path)

    def test_failed_fetch_reported_as_unretrievable(self):
        """失败不是「本地有正文」，但**必须被报出来**。

        旧实现只报成功、失败等于查无此条，调用方于是看不出「这条试过了、
        取不到」——与「还没试过」无从区分。现在两者分开：无 body，有 retrieval。
        """
        self.c.set_fetch("https://a.com/3", {
            "url": "https://a.com/3", "content": "", "length": 0,
            "success": False, "error": "HTTP 404",
            "fetch_method": "http"}, ttl=600)
        got = self.c.local_status(["https://a.com/3"])["https://a.com/3"]
        self.assertNotIn("body", got, "失败不该被当成有正文")
        self.assertEqual(got["retrieval"]["status"], "poor")
        self.assertEqual(got["retrieval"]["reason"], "http")

    def test_robots_blocked_is_blocked_not_poor(self):
        """系统类失败（robots 禁止）与内容类失败必须分开。

        调用方对两者的处置不同：前者换源别耗，后者换源或降置信。
        实测 14% 的抓取建议指向 robots 明令禁止的地址——这一类必须在搜索
        阶段就被认出来，而不是等抓一次才知道。
        """
        self.c.set_fetch("https://a.com/4", {
            "url": "https://a.com/4", "content": "", "length": 0,
            "success": False, "error": "robots.txt 禁止抓取",
            "fetch_method": "robots_blocked"}, ttl=600)
        got = self.c.local_status(["https://a.com/4"])["https://a.com/4"]
        self.assertEqual(got["retrieval"]["status"], "blocked")
        self.assertEqual(got["retrieval"]["reason"], "robots")

    def test_known_poor_page_types(self):
        """登录墙/付费墙/JS 壳归为内容类失败，带原 page_type 作理由。"""
        for i, pt in enumerate(("auth_wall", "paywall", "js_shell")):
            u = f"https://a.com/poor/{i}"
            self.c.set_fetch(u, {"url": u, "content": "x" * 900, "length": 900,
                                 "success": True, "page_type": pt}, ttl=600)
            got = self.c.local_status([u])[u]
            self.assertEqual(got["retrieval"]["status"], "poor", pt)
            self.assertEqual(got["retrieval"]["reason"], pt)

    def test_short_body_is_poor(self):
        u = "https://a.com/short"
        self.c.set_fetch(u, {"url": u, "content": "x" * 50, "length": 50,
                             "success": True, "page_type": "article"}, ttl=600)
        self.assertEqual(self.c.local_status([u])[u]["retrieval"]["reason"],
                         "too_short")

    def test_good_body_has_no_retrieval_flag(self):
        u = "https://a.com/good"
        self.c.set_fetch(u, {"url": u, "content": "x" * 3000, "length": 3000,
                             "success": True, "page_type": "article"}, ttl=600)
        got = self.c.local_status([u])[u]
        self.assertIn("body", got)
        self.assertNotIn("retrieval", got, "正常结果不该被标为可用性问题")

    def test_batch_is_one_pass(self):
        for i in range(10):
            self.c.set_fetch(f"https://a.com/{i}", {
                "url": f"https://a.com/{i}", "content": "x" * 500,
                "length": 500, "success": True}, ttl=600)
        urls = [f"https://a.com/{i}" for i in range(10)] + ["https://a.com/none"]
        self.assertEqual(sum(1 for v in self.c.local_status(urls).values() if v.get("body")), 10)


class TestEvidenceSharesFetchEntry(unittest.TestCase):
    """消融后的存储契约：证据分是正文条目的子键，不是独立 kind。"""

    def setUp(self):
        # 同上：恢复 conftest 的会话隔离目录，不 pop（pop 泄漏到真实用户缓存）
        self._prev_state = os.environ.get("ARGO_STATE_DIR")
        os.environ["ARGO_STATE_DIR"] = tempfile.mkdtemp(prefix="argo-ev-")
        self.c = SearchCache()

    def tearDown(self):
        if self._prev_state is None:
            os.environ.pop("ARGO_STATE_DIR", None)
        else:
            os.environ["ARGO_STATE_DIR"] = self._prev_state

    def test_evidence_requires_body(self):
        """没有正文就不写证据，避免留下「正文为空却标记已核验」的 URL。"""
        self.c.set_evidence("https://b.com/1", {"absorption": 0.9})
        self.assertIsNone(self.c.get_evidence("https://b.com/1"))
        self.assertIsNone(self.c.get_fetch("https://b.com/1"),
                          "证据不该凭空造出正文条目")

    def test_evidence_roundtrip_with_body(self):
        self.c.set_fetch("https://b.com/2", {
            "url": "https://b.com/2", "content": "正文" * 50,
            "length": 100, "success": True}, ttl=600)
        self.c.set_evidence("https://b.com/2", {"absorption": 0.61,
                                                "word_count": 900})
        ev = self.c.get_evidence("https://b.com/2")
        self.assertEqual(ev["absorption"], 0.61)
        self.assertEqual(ev["word_count"], 900)
        self.assertEqual(ev["url"], "https://b.com/2")

    def test_evidence_does_not_replace_content(self):
        self.c.set_fetch("https://b.com/3", {
            "url": "https://b.com/3", "content": "原始正文" * 30,
            "length": 120, "success": True}, ttl=600)
        self.c.set_evidence("https://b.com/3", {"absorption": 0.5})
        self.assertEqual(self.c.get_fetch("https://b.com/3")["content"],
                         "原始正文" * 30)

    def test_key_name_is_module_level(self):
        """键名必须是模块级常量，不能挂在类上。

        测试会用工厂函数替换 SearchCache（monkeypatch），此时
        `SearchCache.EVIDENCE_KEY` 取不到属性，写入路径会**静默**失败。
        """
        self.assertIsInstance(FETCH_EVIDENCE_KEY, str)
        self.assertEqual(FETCH_EVIDENCE_KEY, "evidence")


class TestLoginPartitionIsolation(unittest.TestCase):
    """登录态正文的存档必须与公共分区隔离。"""

    def setUp(self):
        os.environ["ARGO_FULLTEXT_DIR"] = tempfile.mkdtemp(prefix="argo-lg-")

    def tearDown(self):
        os.environ.pop("ARGO_FULLTEXT_DIR", None)

    def test_login_kind_not_readable_from_public(self):
        url = "https://zhihu.com/question/1"
        fs.save(url, "登录态正文" * 500, "text_login")
        self.assertIsNotNone(fs.load(url, "text_login"))
        self.assertIsNone(fs.load(url, "text"),
                          "公共分区读到了登录态正文——隔离被打破")

    def test_login_and_public_coexist(self):
        url = "https://example.com/p"
        fs.save(url, "公开正文" * 100, "text")
        fs.save(url, "登录态正文" * 100, "text_login")
        self.assertEqual(fs.load(url, "text"), "公开正文" * 100)
        self.assertEqual(fs.load(url, "text_login"), "登录态正文" * 100)


class TestEgoSearchDeliverBody(unittest.TestCase):
    """ego-search 的正文交付：截断必须可见且可回读（登录态分区）。"""

    def setUp(self):
        os.environ["ARGO_FULLTEXT_DIR"] = tempfile.mkdtemp(prefix="argo-ego-")
        ego = str(ROOT / "sub-skills" / "ego-search" / "scripts")
        if ego not in sys.path:
            sys.path.insert(0, ego)

    def tearDown(self):
        os.environ.pop("ARGO_FULLTEXT_DIR", None)

    def test_short_body_untouched(self):
        import webbridge_adapter as wa
        body = "短正文" * 10
        out, extra = wa._deliver_body("https://x.com/a", body)
        self.assertEqual(out, body)
        self.assertFalse(extra["truncated"])

    def test_long_body_marked_and_archived(self):
        import webbridge_adapter as wa
        url = "https://x.com/b"
        body = "段落内容。" * 3000
        out, extra = wa._deliver_body(url, body)
        self.assertEqual(len(out), wa._BODY_LIMIT)
        self.assertTrue(extra["truncated"])
        self.assertEqual(extra["full_length"], len(body))
        self.assertTrue(Path(extra["full_text_path"]).is_file())
        # 完整正文在登录态分区可回读，公共分区看不到
        self.assertEqual(fs.load(url, wa._LOGIN_ARCHIVE_KIND), body)
        self.assertIsNone(fs.load(url, "text"))


if __name__ == "__main__":
    unittest.main()

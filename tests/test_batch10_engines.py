#!/usr/bin/env python3
"""批次十引擎测试 — toutiao 全文搜索 / ddg_site 购物族 / zhihu_global v2 / local_quark。

覆盖（全部离线，打桩打在读取处——模块属性 b10.http_open / b10._http_get_raw /
b10.get_env / b10.note_failure / b10._detect_anti_bot）：
  1. toutiao：data 列表解析（em 剥离、相对 url 拼接、publish_time→ISO、稿源媒体名）、
     data=null（shark 反爬/无结果）诚实空、网络失败诚实空
  2. ddg_site：缺 domain 报错、site: 前缀构造、解析 0 条才降级重试（网络失败/拦截页
     不重试）、跳转壳拆解与广告壳丢弃
  3. zhihu_global v2：带 site:/since 过滤时候选池取满（Count=20）再客户端截断、
     无过滤 Count=min(n,20)、时钟类错误给行动提示、HTTPError 暴露为 error item
  4. local_quark：parse_maps 注册存在 + 选择器对 fixture 能出 ≥3 条（防选择器漂移）

运行：
  python3 -m pytest tests/test_batch10_engines.py -v
"""

from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import engines_builders_batch10 as b10  # noqa: E402


def _fake_http_open(payload):
    """http_open 替身：payload 为 Exception 时抛出，否则返回固定响应体。"""
    import json as _json

    class _Resp:
        def read(self):
            if isinstance(payload, bytes):
                return payload
            if isinstance(payload, str):
                return payload.encode("utf-8")
            return _json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fn(req, timeout=None, engine="", **kw):
        if isinstance(payload, Exception):
            raise payload
        return _Resp()
    return _fn


TOUTIAO_PAYLOAD = {
    "message": "success",
    "data": [
        {"title": "argo <em>发布</em>新版本", "abstract": "本文介绍 <em>argo</em> 的搜索能力",
         "article_url": "https://www.toutiao.com/article/1", "publish_time": 1700000000,
         "source": "科技日报"},
        {"title": "无摘要条目", "abstract": "", "display_url": "/article/2",
         "behot_time": None, "source": ""},
    ],
}


class TestToutiao(unittest.TestCase):

    def test_parse_fields(self):
        with patch.object(b10, "http_open", _fake_http_open(TOUTIAO_PAYLOAD)):
            out = b10._build_toutiao_engine({"_name": "toutiao"})("argo", n=5)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "argo 发布新版本")  # em 已剥
        self.assertEqual(out[0]["snippet"], "本文介绍 argo 的搜索能力")
        self.assertEqual(out[0]["published_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(out[0]["site_name"], "科技日报")
        self.assertEqual(out[0]["source"], "toutiao")

    def test_shark_reject_is_honest_empty(self):
        # data=null（shark_decision=reject / 无结果）→ []，不伪装成功
        with patch.object(b10, "http_open", _fake_http_open({"message": "success", "data": None})):
            out = b10._build_toutiao_engine({"_name": "toutiao"})("argo", n=5)
        self.assertEqual(out, [])

    def test_network_failure_is_honest_empty(self):
        with patch.object(b10, "http_open", _fake_http_open(TimeoutError("t"))):
            out = b10._build_toutiao_engine({"_name": "toutiao"})("argo", n=5)
        self.assertEqual(out, [])


class TestDdgSite(unittest.TestCase):

    def _engine(self, domain="jd.com"):
        return b10._build_ddg_site_engine({"_name": "jd", "domain": domain})

    def test_missing_domain_raises(self):
        with self.assertRaises(ValueError):
            b10._build_ddg_site_engine({"_name": "bad"})

    def test_site_prefix_and_unwrap(self):
        seen = []

        def fake_raw(url, headers, to, engine=""):
            seen.append(url)
            return '<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fitem.jd.com%2F1.html&rut=x">机械键盘</a></div>'

        with patch.object(b10, "_http_get_raw", fake_raw), \
             patch.object(b10, "_detect_anti_bot", lambda h: False):
            out = self._engine()("机械键盘", n=5)
        self.assertIn("q=site%3Ajd.com", seen[0])
        self.assertEqual(out[0]["url"], "https://item.jd.com/1.html")

    def test_ad_shell_dropped(self):
        self.assertEqual(b10._unwrap_ddg_link(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fduckduckgo.com%2Fy.js%3Fad&rut=x"), "")
        self.assertEqual(b10._unwrap_ddg_link(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fitem.jd.com%2F1.html&rut=x"),
            "https://item.jd.com/1.html")

    def test_retry_only_on_parsed_empty(self):
        # 取到页面但解析 0 条 → 去 site: 重试（第二次 URL 无 site:）
        calls = []

        def fake_raw(url, headers, to, engine=""):
            calls.append(url)
            return "<html>没有结果的页面</html>"

        with patch.object(b10, "_http_get_raw", fake_raw), \
             patch.object(b10, "_detect_anti_bot", lambda h: False):
            self._engine()("稀缺词", n=5)
        self.assertEqual(len(calls), 2)
        self.assertIn("site%3Ajd.com", calls[0])
        self.assertNotIn("site%3A", calls[1])

    def test_no_retry_on_network_failure(self):
        calls = []

        def fake_raw(url, headers, to, engine=""):
            calls.append(url)
            return None  # 网络失败

        with patch.object(b10, "_http_get_raw", fake_raw):
            self._engine()("稀缺词", n=5)
        self.assertEqual(len(calls), 1)

    def test_blocked_attributed_not_retried(self):
        notes = []

        def fake_raw(url, headers, to, engine=""):
            return "<html>challenge page</html>"

        with patch.object(b10, "_http_get_raw", fake_raw), \
             patch.object(b10, "_detect_anti_bot", lambda h: True), \
             patch.object(b10, "note_failure",
                          lambda *a, **k: notes.append(a)):
            self._engine()("任何词", n=5)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][1], "blocked")

    def test_multi_domain_or(self):
        seen = []

        def fake_raw(url, headers, to, engine=""):
            seen.append(url)
            return None

        eng = b10._build_ddg_site_engine(
            {"_name": "pdd", "domain": ["pinduoduo.com", "yangkeduo.com"]})
        with patch.object(b10, "_http_get_raw", fake_raw):
            eng("纸巾", n=5)
        self.assertIn("site%3Apinduoduo.com", seen[0])
        self.assertIn("site%3Ayangkeduo.com", seen[0])


class TestZhihuGlobalV2(unittest.TestCase):

    def _engine(self):
        return b10._build_zhihu_global_engine({"_name": "zhihu_global"})

    def test_no_secret_is_honest_empty(self):
        with patch.object(b10, "get_env", lambda names: None):
            self.assertEqual(self._engine()("AI", n=5), [])

    def _capture_open(self, payload):
        seen = {}

        def _fn(req, timeout=None, engine="", **kw):
            seen["url"] = getattr(req, "full_url", "")
            seen["engine"] = engine
            class _R:
                def read(self):
                    return __import__("json").dumps(payload).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return _R()
        return _fn, seen

    def test_filtered_fills_pool_then_truncates(self):
        items = [{"Title": f"条目{i}", "Url": f"https://zhuanlan.zhihu.com/p/{i}"}
                 for i in range(20)]
        fn, seen = self._capture_open({"Code": 0, "Data": {"Items": items}})
        with patch.object(b10, "get_env", lambda names: "secret"), \
             patch.object(b10, "http_open", fn):
            out = self._engine()("site:zhuanlan.zhihu.com argo", n=5)
        self.assertIn("Count=20", seen["url"])       # 候选池取满
        self.assertIn("Filter=host", seen["url"])    # 站点限定下发
        self.assertEqual(len(out), 5)                # 客户端截断

    def test_unfiltered_count_is_n(self):
        fn, seen = self._capture_open({"Code": 0, "Data": {"Items": []}})
        with patch.object(b10, "get_env", lambda names: "secret"), \
             patch.object(b10, "http_open", fn):
            self._engine()("argo", n=5)
        self.assertIn("Count=5", seen["url"])
        self.assertNotIn("Filter=", seen["url"])

    def test_clock_error_gets_actionable_hint(self):
        fn, _ = self._capture_open({"Code": 30001, "Message": "timestamp expired"})
        with patch.object(b10, "get_env", lambda names: "secret"), \
             patch.object(b10, "http_open", fn):
            out = self._engine()("argo", n=5)
        self.assertIn("error", out[0])
        self.assertIn("时钟", out[0]["error"])

    def test_http_error_exposed(self):
        err = urllib.error.HTTPError("https://developer.zhihu.com/x", 401,
                                     "Unauthorized", None, None)
        with patch.object(b10, "get_env", lambda names: "secret"), \
             patch.object(b10, "http_open", _fake_http_open(err)):
            out = self._engine()("argo", n=5)
        self.assertIn("HTTP 401", out[0]["error"])


class TestLocalQuark(unittest.TestCase):

    QUARK_FIXTURE = """
    <div id="page">
      <div class="result sc_natural_result">
        <a class="qk-link-wrapper" href="https://github.com/taxueseek/argo">
          <span class="qk-title-text">argo 统一搜索工具</span></a>
        <p class="qk-paragraph-text">给 Agent 用的统一搜索层</p>
      </div>
      <div class="sc_structure_template_normal">
        <a class="qk-link-wrapper" href="https://argo.example.com/2">
          <span class="qk-title-text">argo 第二条</span></a>
        <p class="qk-paragraph-text">摘要二</p>
      </div>
      <div class="result sc_natural_result">
        <a class="qk-link-wrapper" href="https://argo.example.com/3">
          <span class="qk-title-text">argo 第三条</span></a>
      </div>
    </div>
    """

    def test_parse_maps_registered_and_selectors_work(self):
        from engines_base import _load_parse_maps
        mapping = _load_parse_maps().get("html", {}).get("local_quark")
        self.assertTrue(mapping, "local_quark 未注册进 parse_maps.yaml")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(self.QUARK_FIXTURE, "html.parser")
        picked = []
        for item in soup.select(mapping["container"]):
            t = item.select_one(mapping["title"])
            u = item.select_one(mapping["url"])
            if t and u:
                picked.append((t.get_text(strip=True), u.get("href")))
        self.assertGreaterEqual(len(picked), 3)
        self.assertEqual(picked[0][1], "https://github.com/taxueseek/argo")


if __name__ == "__main__":
    unittest.main()

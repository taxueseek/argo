#!/usr/bin/env python3
"""test_local_baidu_pipeline.py — 百度链路端到端（离线，桩网络层）。

锁定三件事（2026-09-26 修复的回归保护）：
  1. headers.Cookie 的 {UUID} 占位符：进程内一致、跨进程不同、不进 env 校验；
  2. tls_impersonate 声明 → HttpClient.get 收到 impersonate_profiles；
  3. resolve_redirects：baidu.com/link 壳逐条换真链，解不开的丢弃——
     不做这步聚合层 P0（evidence.is_serp_or_jump_url）会把结果整批清零。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import engines_base  # noqa: E402
from evidence import is_serp_or_jump_url  # noqa: E402

_FAKE_SERP = """
<html><body>
<div class="result c-container"><h3><a href="http://www.baidu.com/link?url=AAA">机械键盘推荐</a></h3>
<p class="c-abstract">客制化机械键盘选购指南</p></div>
<div class="result c-container"><h3><a href="http://www.baidu.com/link?url=BBB">轴体科普</a></h3>
<p class="c-abstract">青轴茶轴红轴区别</p></div>
<div class="result c-container"><h3><a href="https://direct.example.com/post">直链结果</a></h3>
<p class="c-abstract">不走跳转的结果</p></div>
<div class="result c-container"><h3><a href="https://direct.example.com/post2">填充一条</a></h3>
<p class="c-abstract">保证整体长度越过反爬检测的 500 字符闸（拦截页通常极短）</p></div>
</body></html>
"""


class TestUuidPlaceholder(unittest.TestCase):
    def test_stable_within_process(self):
        a = engines_base._resolve("BAIDUID={UUID}:FG=1", "q", 5)
        b = engines_base._resolve("BAIDUID={UUID}:FG=1", "q", 5)
        self.assertEqual(a, b)
        self.assertRegex(a, r"BAIDUID=[0-9A-F]{32}:FG=1")

    def test_not_treated_as_missing_env(self):
        from engine_env import missing_env_for
        spec = {"headers": {"Cookie": "BAIDUID={UUID}:FG=1"}}
        self.assertEqual(missing_env_for("local_baidu", spec), [])


class TestTlsImpersonatePlumbing(unittest.TestCase):
    def test_spec_flag_reaches_http_client(self):
        seen = {}
        real_get = engines_base.HttpClient if hasattr(engines_base, "HttpClient") else None

        class FakeResp(dict):
            pass

        def fake_get(self, url, extra_headers=None, follow_redirects=True,
                     engine=None, impersonate_profiles=None):
            seen["profiles"] = impersonate_profiles
            return {"status": 200, "headers": {}, "text": _FAKE_SERP,
                    "url": url, "elapsed_ms": 1}

        import http_client
        # conftest 全局 setdefault 关闭 HttpClient（套件默认走 urllib 保底），
        # 本测试锁的就是 HttpClient 管道 → 显式开旗
        with patch.dict(os.environ, {"ARGO_ENGINE_HTTP_CLIENT": "1"}), \
             patch.object(http_client.HttpClient, "get", fake_get):
            html = engines_base._http_get_raw(
                "https://www.baidu.com/s?wd=x", {}, 5,
                engine="local_baidu", tls_profiles=["chrome131"])
        self.assertIsNotNone(html)
        self.assertEqual(seen["profiles"], ["chrome131"])


class TestResolveRedirects(unittest.TestCase):
    def _engine(self):
        cfg_spec = {"_name": "local_baidu", "type": "html",
                    "url": "https://www.baidu.com/s", "query_param": "wd",
                    "resolve_redirects": True,
                    "tls_impersonate": ["chrome131"]}
        return engines_base._build_html_engine(cfg_spec)

    def test_jump_urls_resolved_and_unresolvable_dropped(self):
        import http_client

        class FakeLocResp:
            status_code = 302
            headers = {"Location": "https://www.example.com/real-post"}

        def fake_cffi_get(url, **kw):
            # 第一个 link 给真链，第二个 link 全档失败 → 该条应被丢弃
            if "AAA" in url:
                return FakeLocResp()
            raise ConnectionError("walled")

        calls = {}

        def fake_http_get_raw(url, headers, timeout, engine="?", tls_profiles=None):
            calls["tls"] = tls_profiles
            return _FAKE_SERP

        import curl_cffi.requests as cr
        with patch.object(engines_base, "_http_get_raw", fake_http_get_raw), \
             patch.object(cr, "get", fake_cffi_get):
            out = self._engine()("机械键盘", n=5)
        urls = [r["url"] for r in out]
        self.assertEqual(urls, ["https://www.example.com/real-post",
                                "https://direct.example.com/post",
                                "https://direct.example.com/post2"])
        self.assertEqual(calls["tls"], ["chrome131"])

    def test_resolved_urls_pass_pipeline_p0(self):
        self.assertTrue(is_serp_or_jump_url("http://www.baidu.com/link?url=AAA"))
        self.assertFalse(is_serp_or_jump_url("https://www.example.com/real-post"))


if __name__ == "__main__":
    unittest.main()

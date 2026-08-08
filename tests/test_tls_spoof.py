#!/usr/bin/env python3
"""TLS 指纹伪造层测试 — http_client.get_impersonated / fetch_v3 升级链插入。

原则：不联网。用 mock 替换 curl_cffi 与底层抓取函数，
验证指纹层在 HTTP 失败/疑似拦截时介入、成功时替代 CDP、开关可关。
"""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


class _FakeResp:
    """模拟 curl_cffi 响应。"""

    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}


def _fake_public_dns():
    """本机 DNS 可能被劫持到保留段（198.18.x.x），测试需固定公网解析。"""
    return patch(
        "url_safety.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM,
                       socket.IPPROTO_TCP, "", ("93.184.215.14", 0))],
    )


class TestImpersonateGuard(unittest.TestCase):
    """get_impersonated 的 SSRF 防护与失败路径。"""

    def test_private_url_rejected_without_request(self):
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=0)
        with patch("curl_cffi.requests.get", side_effect=AssertionError("不应发请求")):
            resp = client.get_impersonated("http://169.254.169.254/latest/meta-data/")
        self.assertEqual(resp.get("status"), 0)
        self.assertIn("SSRF", resp.get("error", ""))

    def test_curl_cffi_missing_graceful(self):
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=0)
        # 把 curl_cffi 从 sys.modules 置空 → import 抛 ImportError
        with _fake_public_dns(), \
             patch.dict("sys.modules", {"curl_cffi": None}):
            resp = client.get_impersonated("https://example.com/")
        self.assertEqual(resp.get("status"), 0)
        self.assertIn("curl_cffi not installed", resp.get("error", ""))


class TestImpersonateSuccess(unittest.TestCase):
    """get_impersonated 成功与指纹轮换。"""

    def test_first_profile_success(self):
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=0)
        calls = []

        def fake_get(url, **kw):
            calls.append(kw.get("impersonate"))
            return _FakeResp(200, "<html>ok</html>")

        with _fake_public_dns(), \
             patch("curl_cffi.requests.get", side_effect=fake_get):
            resp = client.get_impersonated("https://example.com/")
        self.assertEqual(resp.get("status"), 200)
        self.assertEqual(resp.get("impersonate"), "chrome")
        self.assertEqual(calls, ["chrome"])  # 首个指纹即成功，不浪费

    def test_rotate_until_success(self):
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=0)
        calls = []

        def fake_get(url, **kw):
            fp = kw.get("impersonate")
            calls.append(fp)
            if fp == "chrome":
                return _FakeResp(403, "blocked")
            return _FakeResp(200, "<html>passed</html>")

        with _fake_public_dns(), \
             patch("curl_cffi.requests.get", side_effect=fake_get):
            resp = client.get_impersonated("https://medium.com/some-post")
        self.assertEqual(resp.get("status"), 200)
        self.assertEqual(resp.get("impersonate"), "safari")
        self.assertEqual(calls, ["chrome", "safari"])

    def test_redirect_followed_with_safety(self):
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=0)
        responses = iter([
            _FakeResp(302, "", {"Location": "https://example.com/target"}),
            _FakeResp(200, "<html>landed</html>"),
        ])

        def fake_get(url, **kw):
            return next(responses)

        with _fake_public_dns(), \
             patch("curl_cffi.requests.get", side_effect=fake_get):
            resp = client.get_impersonated("https://example.com/start")
        self.assertEqual(resp.get("status"), 200)
        self.assertEqual(resp.get("url"), "https://example.com/target")


class TestFetchV3Integration(unittest.TestCase):
    """fetch_v3 升级链插入：HTTP 失败时 TLS 层介入。"""

    def _run_fetch(self, http_result, spoof_result=None,
                   browser_result=None, impersonate_env="1"):
        from fetch_v3 import fetch_v3
        mocks = {
            "fetch_v3._http_fetch": http_result,
            "fetch_v3._wayback_fetch": {"success": False},
        }
        if spoof_result is not None:
            mocks["fetch_v3._tls_spoof_fetch"] = spoof_result
        if browser_result is not None:
            mocks["fetch_v3._browser_fetch"] = browser_result

        with patch.dict("os.environ", {"ARGO_FETCH_IMPERSONATE": impersonate_env}), \
             patch("robots_guard.robots_blocked", return_value=False):
            with patch.multiple("fetch_v3", **{k: lambda *a, r=v, **kw: r for k, v in mocks.items()}):
                return fetch_v3("https://example.com/", skip_cache=True)

    def test_http_ok_no_tls_spoof(self):
        """HTTP 成功且内容健康 → 不触发 TLS 层。"""
        from fetch_v3 import fetch_v3
        http_ok = {"success": True, "content": "x" * 300, "html": "<p>" + "x" * 300 + "</p>",
                   "url": "https://example.com/", "fetch_method": "http"}
        with _fake_public_dns(), \
             patch.dict("os.environ", {"ARGO_FETCH_IMPERSONATE": "1"}), \
             patch("robots_guard.robots_blocked", return_value=False), \
             patch("fetch_v3._http_fetch", return_value=http_ok) as http_mock, \
             patch("fetch_v3._tls_spoof_fetch") as spoof_mock:
            fetch_v3("https://example.com/", skip_cache=True)
        http_mock.assert_called_once()
        spoof_mock.assert_not_called()

    def test_http_fail_tls_spoof_succeeds(self):
        """HTTP 失败 → TLS 层成功 → 结果带 tls_spoof 标记。"""
        from fetch_v3 import fetch_v3
        http_fail = {"success": False, "content": "", "html": "",
                     "url": "https://medium.com/x", "fetch_method": "http",
                     "error": "HTTP 403"}
        spoof_ok = {"success": True, "content": "y" * 300, "html": "<p>" + "y" * 300 + "</p>",
                    "url": "https://medium.com/x", "fetch_method": "tls_spoof",
                    "impersonate": "safari"}
        with _fake_public_dns(), \
             patch.dict("os.environ", {"ARGO_FETCH_IMPERSONATE": "1"}), \
             patch("robots_guard.robots_blocked", return_value=False), \
             patch("fetch_v3._http_fetch", return_value=http_fail), \
             patch("fetch_v3._tls_spoof_fetch", return_value=spoof_ok), \
             patch("fetch_v3._browser_fetch") as browser_mock:
            out = fetch_v3("https://medium.com/x", skip_cache=True)
        self.assertTrue(out.get("success"))
        self.assertEqual(out.get("fetch_method"), "tls_spoof")
        browser_mock.assert_not_called()  # TLS 成功则不再升级浏览器

    def test_impersonate_disabled_falls_through(self):
        """开关关闭 → 不触发 TLS 层。"""
        from fetch_v3 import fetch_v3
        http_fail = {"success": False, "content": "", "html": "",
                     "url": "https://example.com/", "fetch_method": "http",
                     "error": "HTTP 403"}
        with _fake_public_dns(), \
             patch.dict("os.environ", {"ARGO_FETCH_IMPERSONATE": "0"}), \
             patch("robots_guard.robots_blocked", return_value=False), \
             patch("fetch_v3._http_fetch", return_value=http_fail), \
             patch("fetch_v3._tls_spoof_fetch") as spoof_mock, \
             patch("fetch_v3._wayback_fetch", return_value={"success": False}), \
             patch("fetch_v3._browser_fetch", return_value={
                 "success": False, "content": "", "html": "",
                 "url": "https://example.com/", "fetch_method": "browser",
                 "error": "CDP error"}):
            fetch_v3("https://example.com/", skip_cache=True)
        spoof_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

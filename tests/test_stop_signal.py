#!/usr/bin/env python3
"""P0 回归测试：Retry-After 尊重 + 停止信号门禁（全 mock，不联网）。

覆盖：
  1. http_client.retry_after_seconds 解析规则
  2. get() 对 429/503 + Retry-After 的合规等待重试 / 放弃
  3. get_impersonated 对 429/503 停止信号立即返回、不轮换指纹
  4. fetch_v3 主链收到停止信号后不再升级 TLS/wayback/CDP
  5. fetch_v3 的 robots.txt 合规门禁
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


class TestRetryAfterParsing(unittest.TestCase):
    """retry_after_seconds 解析规则。"""

    def test_429_with_header(self):
        from http_client import retry_after_seconds
        self.assertEqual(retry_after_seconds(429, {"Retry-After": "5"}), 5.0)

    def test_503_with_header(self):
        from http_client import retry_after_seconds
        self.assertEqual(retry_after_seconds(503, {"Retry-After": "2"}), 2.0)

    def test_429_whitespace_header(self):
        from http_client import retry_after_seconds
        self.assertEqual(retry_after_seconds(429, {"Retry-After": " 3 "}), 3.0)

    def test_non_rate_status_ignores_header(self):
        from http_client import retry_after_seconds
        self.assertIsNone(retry_after_seconds(200, {"Retry-After": "5"}))

    def test_429_without_header(self):
        from http_client import retry_after_seconds
        self.assertIsNone(retry_after_seconds(429, {}))

    def test_429_over_threshold_returns_none(self):
        from http_client import retry_after_seconds
        self.assertIsNone(retry_after_seconds(429, {"Retry-After": "60"}))

    def test_non_numeric_header_returns_none(self):
        from http_client import retry_after_seconds
        self.assertIsNone(retry_after_seconds(
            429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))


class TestGetRespectsRetryAfter(unittest.TestCase):
    """get() 对 429/503 的合规重试行为。"""

    def _resp(self, status, headers=None):
        return {"status": status, "headers": headers or {}, "text": "",
                "url": "https://example.com/", "elapsed_ms": 1,
                "from_cache": False}

    def test_waits_retry_after_then_succeeds(self):
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=1, jitter=False)
        calls = {"n": 0}

        def fake_do_get(url, extra_headers=None, follow_redirects=True):
            calls["n"] += 1
            if calls["n"] == 1:
                return self._resp(429, {"Retry-After": "1"})
            return self._resp(200)

        with _fake_public_dns(), \
             patch.object(client, "_do_get", side_effect=fake_do_get), \
             patch("http_client.time.sleep") as m_sleep:
            resp = client.get("https://example.com/")
        self.assertEqual(resp["status"], 200)
        self.assertEqual(calls["n"], 2)
        m_sleep.assert_called_once_with(1.0)  # 等服务器说的时间，非盲退避

    def test_429_without_header_no_retry(self):
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=2, jitter=False)
        calls = {"n": 0}

        def fake_do_get(url, extra_headers=None, follow_redirects=True):
            calls["n"] += 1
            return self._resp(429)

        with _fake_public_dns(), \
             patch.object(client, "_do_get", side_effect=fake_do_get), \
             patch("http_client.time.sleep") as m_sleep:
            resp = client.get("https://example.com/")
        self.assertEqual(resp["status"], 429)
        self.assertEqual(calls["n"], 1)  # 无 Retry-After → 不重试
        m_sleep.assert_not_called()

    def test_retry_after_over_threshold_no_wait_no_retry(self):
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=2, jitter=False)
        calls = {"n": 0}

        def fake_do_get(url, extra_headers=None, follow_redirects=True):
            calls["n"] += 1
            return self._resp(503, {"Retry-After": "60"})

        with _fake_public_dns(), \
             patch.object(client, "_do_get", side_effect=fake_do_get), \
             patch("http_client.time.sleep") as m_sleep:
            resp = client.get("https://example.com/")
        self.assertEqual(resp["status"], 503)
        self.assertEqual(calls["n"], 1)  # 超阈值 → 直接放弃
        m_sleep.assert_not_called()

    def test_429_retry_after_exhausts_retries(self):
        """429+Retry-After 等待重试后仍 429 → 返回最后一次响应，不无限重试。"""
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=1, jitter=False)
        calls = {"n": 0}

        def fake_do_get(url, extra_headers=None, follow_redirects=True):
            calls["n"] += 1
            return self._resp(429, {"Retry-After": "1"})

        with _fake_public_dns(), \
             patch.object(client, "_do_get", side_effect=fake_do_get), \
             patch("http_client.time.sleep"):
            resp = client.get("https://example.com/")
        self.assertEqual(resp["status"], 429)
        self.assertEqual(calls["n"], 2)  # 初始 + 1 次等待重试


class TestImpersonatedStopSignal(unittest.TestCase):
    """get_impersonated 对 429/503 停止信号的处理。"""

    def test_429_stops_without_fingerprint_rotation(self):
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=0)
        calls = []

        def fake_get(url, **kw):
            calls.append(kw.get("impersonate"))
            return _FakeResp(429, "rate limited", {"Retry-After": "5"})

        with _fake_public_dns(), \
             patch("curl_cffi.requests.get", side_effect=fake_get):
            resp = client.get_impersonated("https://example.com/")
        self.assertEqual(resp["status"], 429)
        self.assertTrue(resp.get("stop_signal"))
        self.assertEqual(calls, ["chrome"])  # 试到第一个指纹即停，不轮换

    def test_503_stops_without_fingerprint_rotation(self):
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=0)
        calls = []

        def fake_get(url, **kw):
            calls.append(kw.get("impersonate"))
            return _FakeResp(503, "overloaded")

        with _fake_public_dns(), \
             patch("curl_cffi.requests.get", side_effect=fake_get):
            resp = client.get_impersonated("https://example.com/")
        self.assertEqual(resp["status"], 503)
        self.assertTrue(resp.get("stop_signal"))
        self.assertEqual(calls, ["chrome"])

    def test_403_still_rotates_to_success(self):
        """403 与指纹相关 → 保持轮换（回归保护）。"""
        from http_client import HttpClient
        client = HttpClient(timeout=2, max_retries=0)
        calls = []

        def fake_get(url, **kw):
            calls.append(kw.get("impersonate"))
            if len(calls) == 1:
                return _FakeResp(403, "forbidden")
            return _FakeResp(200, "<html>ok</html>")

        with _fake_public_dns(), \
             patch("curl_cffi.requests.get", side_effect=fake_get):
            resp = client.get_impersonated("https://example.com/")
        self.assertEqual(resp["status"], 200)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("stop_signal", resp)


class TestFetchV3StopSignalGate(unittest.TestCase):
    """fetch_v3 主链收到停止信号后不再升级 TLS/wayback/CDP。"""

    def _result(self, method, error, **extra):
        base = {"url": "https://example.com/", "content": "", "html": "",
                "length": 0, "title": "", "fetch_method": method,
                "success": False, "error": error}
        base.update(extra)
        return base

    def test_http_429_stops_all_upgrades(self):
        import fetch_v3
        stopped = self._result("http", "HTTP 429", status=429, stop_signal=True)
        with _fake_public_dns(), \
             patch("robots_guard.robots_blocked", return_value=False), \
             patch.object(fetch_v3, "_http_fetch", return_value=stopped) as m_http, \
             patch.object(fetch_v3, "_tls_spoof_fetch") as m_tls, \
             patch.object(fetch_v3, "_wayback_fetch") as m_wb, \
             patch.object(fetch_v3, "_browser_fetch") as m_br, \
             patch.object(fetch_v3, "_assess_quality", side_effect=lambda r: r):
            result = fetch_v3.fetch_v3("https://example.com/", skip_cache=True)
        self.assertFalse(result["success"])
        self.assertTrue(result.get("stop_signal"))
        m_http.assert_called_once()
        m_tls.assert_not_called()
        m_wb.assert_not_called()
        m_br.assert_not_called()

    def test_tls_429_stops_wayback_and_browser(self):
        import fetch_v3
        failed = self._result("http", "HTTP 403")
        stopped = self._result("tls_spoof", "HTTP 429", status=429,
                               stop_signal=True)
        with _fake_public_dns(), \
             patch("robots_guard.robots_blocked", return_value=False), \
             patch.object(fetch_v3, "_http_fetch", return_value=failed), \
             patch.object(fetch_v3, "_tls_spoof_fetch", return_value=stopped) as m_tls, \
             patch.object(fetch_v3, "_wayback_fetch") as m_wb, \
             patch.object(fetch_v3, "_browser_fetch") as m_br, \
             patch.object(fetch_v3, "_assess_quality", side_effect=lambda r: r):
            result = fetch_v3.fetch_v3("https://example.com/", skip_cache=True)
        self.assertTrue(result.get("stop_signal"))
        m_tls.assert_called_once()
        m_wb.assert_not_called()
        m_br.assert_not_called()

    def test_http_503_stops_all_upgrades(self):
        import fetch_v3
        stopped = self._result("http", "HTTP 503", status=503, stop_signal=True)
        with _fake_public_dns(), \
             patch("robots_guard.robots_blocked", return_value=False), \
             patch.object(fetch_v3, "_http_fetch", return_value=stopped), \
             patch.object(fetch_v3, "_tls_spoof_fetch") as m_tls, \
             patch.object(fetch_v3, "_wayback_fetch") as m_wb, \
             patch.object(fetch_v3, "_browser_fetch") as m_br, \
             patch.object(fetch_v3, "_assess_quality", side_effect=lambda r: r):
            result = fetch_v3.fetch_v3("https://example.com/", skip_cache=True)
        self.assertTrue(result.get("stop_signal"))
        m_tls.assert_not_called()
        m_wb.assert_not_called()
        m_br.assert_not_called()

    def test_no_stop_signal_still_upgrades(self):
        """无停止信号时保持原有升级链（回归保护）。"""
        import fetch_v3
        failed = self._result("http", "HTTP 403")
        tls_failed = self._result("tls_spoof", "HTTP 403")
        wb_ok = {"success": True, "content": "y" * 300,
                 "html": "<p>" + "y" * 300 + "</p>",
                 "url": "https://example.com/", "fetch_method": "wayback"}
        with _fake_public_dns(), \
             patch("robots_guard.robots_blocked", return_value=False), \
             patch.object(fetch_v3, "_http_fetch", return_value=failed), \
             patch.object(fetch_v3, "_tls_spoof_fetch",
                          return_value=tls_failed), \
             patch.object(fetch_v3, "_wayback_fetch", return_value=wb_ok) as m_wb, \
             patch.object(fetch_v3, "_browser_fetch") as m_br, \
             patch.object(fetch_v3, "_assess_quality", side_effect=lambda r: r):
            result = fetch_v3.fetch_v3("https://example.com/", skip_cache=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["fetch_method"], "wayback")
        m_wb.assert_called_once()
        m_br.assert_not_called()


class TestFetchV3RobotsGate(unittest.TestCase):
    """fetch_v3 的 robots.txt 合规门禁。"""

    def test_blocked_path_returns_robots_blocked(self):
        import fetch_v3
        with _fake_public_dns(), \
             patch("robots_guard.robots_blocked", return_value=True), \
             patch.object(fetch_v3, "_http_fetch") as m_http, \
             patch.object(fetch_v3, "_assess_quality", side_effect=lambda r: r):
            result = fetch_v3.fetch_v3("https://example.com/private/x",
                                       skip_cache=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["fetch_method"], "robots_blocked")
        self.assertIn("robots.txt", result.get("error", ""))
        m_http.assert_not_called()

    def test_allowed_path_normal_flow(self):
        import fetch_v3
        ok = {"success": True, "content": "x" * 300,
              "html": "<p>" + "x" * 300 + "</p>",
              "url": "https://example.com/", "fetch_method": "http"}
        with _fake_public_dns(), \
             patch("robots_guard.robots_blocked", return_value=False), \
             patch.object(fetch_v3, "_http_fetch", return_value=ok) as m_http, \
             patch.object(fetch_v3, "_assess_quality", side_effect=lambda r: r):
            result = fetch_v3.fetch_v3("https://example.com/", skip_cache=True)
        self.assertTrue(result["success"])
        m_http.assert_called_once()


if __name__ == "__main__":
    unittest.main()

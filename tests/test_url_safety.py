#!/usr/bin/env python3
"""SSRF 防护测试 — url_safety / http_client / fetch 入口拦截。"""

from __future__ import annotations

import sys
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from url_safety import check_url, is_private_ip, host_is_private_name, is_safe_fetch_url
from http_client import HttpClient


class TestUrlSafety(unittest.TestCase):
    """url_safety 核心判断。"""

    def test_private_ip_ranges(self):
        self.assertTrue(is_private_ip("127.0.0.1"))
        self.assertTrue(is_private_ip("10.0.0.5"))
        self.assertTrue(is_private_ip("172.16.0.1"))
        self.assertTrue(is_private_ip("192.168.1.1"))
        self.assertTrue(is_private_ip("169.254.169.254"))
        self.assertTrue(is_private_ip("100.64.0.1"))
        self.assertTrue(is_private_ip("198.18.0.1"))
        self.assertTrue(is_private_ip("::1"))
        self.assertTrue(is_private_ip("fe80::1"))
        self.assertTrue(is_private_ip("fc00::1"))
        self.assertFalse(is_private_ip("8.8.8.8"))
        self.assertFalse(is_private_ip("93.184.215.14"))
        self.assertFalse(is_private_ip("2606:2800:220:1::"))

    def test_private_hostnames(self):
        self.assertTrue(host_is_private_name("localhost"))
        self.assertTrue(host_is_private_name("intranet"))
        self.assertTrue(host_is_private_name("router.internal"))
        self.assertTrue(host_is_private_name("nas.local"))
        self.assertTrue(host_is_private_name("metadata.google.internal"))
        self.assertFalse(host_is_private_name("example.com"))
        self.assertFalse(host_is_private_name("www.wikipedia.org"))

    def test_scheme_whitelist(self):
        ok, _ = check_url("file:///etc/passwd")
        self.assertFalse(ok)
        ok, _ = check_url("ftp://example.com/")
        self.assertFalse(ok)
        ok, _ = check_url("javascript:alert(1)")
        self.assertFalse(ok)
        # 公网 IP 直连不依赖 DNS；沙箱 DNS 可能把域名解析到保留段
        ok, _ = check_url("https://93.184.215.14/a?b=c")
        self.assertTrue(ok)

    def _fake_public_dns(self):
        return patch(
            "url_safety.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM,
                           socket.IPPROTO_TCP, "", ("93.184.215.14", 0))],
        )

    def _fake_dns(self, ip_strs):
        """mock 域名解析为指定 IP 列表（v4/v6 混排）。"""
        infos = []
        for ip in ip_strs:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            infos.append((family, socket.SOCK_STREAM,
                          socket.IPPROTO_TCP, "", (ip, 0)))
        return patch("url_safety.socket.getaddrinfo", return_value=infos)

    def test_public_domain_allowed_with_public_dns(self):
        with self._fake_public_dns():
            self.assertTrue(is_safe_fetch_url("https://example.com/a?b=c"))

    def test_fake_ip_env_domain_allowed(self):
        """fake-ip/TUN 代理环境：域名全解析到占位段 → 放行。"""
        with self._fake_dns(["198.18.0.46", "198.18.0.47"]):
            self.assertTrue(is_safe_fetch_url("https://medium.com/"))
        with self._fake_dns(["fdfe:dcba:9876::2f"]):
            self.assertTrue(is_safe_fetch_url("https://www.quora.com/"))

    def test_fake_ip_mixed_public_allowed(self):
        """占位段 + 公网混排 → 放行（域名仍可路由到公网）。"""
        with self._fake_dns(["198.18.0.46", "93.184.215.14"]):
            self.assertTrue(is_safe_fetch_url("https://medium.com/"))

    def test_fake_ip_mixed_private_blocked(self):
        """占位段 + 真实私网混排 → 拦截（存在内网可达目标）。"""
        with self._fake_dns(["198.18.0.46", "10.0.0.5"]):
            self.assertFalse(is_safe_fetch_url("https://medium.com/"))

    def test_private_domain_dns_blocked(self):
        """域名解析到真实私有 IP → 拦截（回归保护）。"""
        with self._fake_dns(["192.168.1.1"]):
            self.assertFalse(is_safe_fetch_url("https://intranet.example/"))

    def test_private_urls_blocked(self):
        for url in (
            "http://localhost:6379/",
            "http://127.0.0.1:8000/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://metadata.google.internal/",
        ):
            self.assertFalse(
                is_safe_fetch_url(url), f"{url} 应被 SSRF 防护拦截"
            )

    def test_allow_private_env_override(self):
        with patch.dict("os.environ", {"ARGO_ALLOW_PRIVATE_URLS": "1"}):
            import url_safety as us
            self.assertTrue(us.allow_private())
            ok, _ = us.check_url("http://127.0.0.1:8000/")
            self.assertTrue(ok)

    def test_ipv4_mapped_ipv6_blocked(self):
        """IPv4-mapped IPv6（::ffff:a.b.c.d）落回 IPv4 私有段 → 拦截（ARGO-SEC-01 回归）。"""
        for url, ip in (
            ("http://[::ffff:127.0.0.1]/", "::ffff:127.0.0.1"),
            ("http://[::ffff:10.0.0.1]/", "::ffff:10.0.0.1"),
            ("http://[::ffff:192.168.1.1]/", "::ffff:192.168.1.1"),
            ("http://[::ffff:169.254.169.254]/", "::ffff:169.254.169.254"),
        ):
            with self._fake_dns([ip]):
                self.assertFalse(
                    is_safe_fetch_url(url), f"{url} 应被拦截（IPv4-mapped 绕过）"
                )

    def test_ipv4_mapped_public_allowed(self):
        """IPv4-mapped 公网地址 → 放行，不误伤正常流量。"""
        with self._fake_dns(["::ffff:8.8.8.8"]):
            self.assertTrue(is_safe_fetch_url("http://[::ffff:8.8.8.8]/"))

    def test_numeric_ip_literal_variants_blocked(self):
        """八进制 / 十六进制 / 单段整数 IP 字面量 → 规范化后拦截（ARGO-SEC-02 回归）。"""
        for url in (
            "http://0177.0.0.1/",     # 八进制 = 127.0.0.1
            "http://0x7f.0.0.1/",     # 十六进制 = 127.0.0.1
            "http://0x7f000001/",     # 单段十六进制 = 127.0.0.1
            "http://2130706433/",     # 单段十进制 = 127.0.0.1
            "http://0x0a000001/",     # = 10.0.0.1
            "http://0300.0.0.1/",     # 八进制 = 192.0.0.1（IETF 保留段）
        ):
            self.assertFalse(
                is_safe_fetch_url(url), f"{url} 应被拦截（数字字面量绕过）"
            )

    def test_normal_hostnames_unaffected(self):
        """常规域名不受字面量规范化影响。"""
        with self._fake_public_dns():
            self.assertTrue(is_safe_fetch_url("https://example.com/a?b=c"))
        with self._fake_dns(["192.168.1.1"]):
            self.assertFalse(is_safe_fetch_url("https://intranet.example/"))


class TestHttpClientGuard(unittest.TestCase):
    """HttpClient 入口拦截。"""

    def test_private_url_rejected_without_request(self):
        client = HttpClient(timeout=2, max_retries=0)
        resp = client.get("http://169.254.169.254/latest/meta-data/")
        self.assertEqual(resp.get("status"), 0)
        self.assertIn("SSRF", resp.get("error", ""))

    def test_curl_fallback_guard(self):
        client = HttpClient(timeout=2, max_retries=0)
        resp = client.get_with_curl("http://127.0.0.1:9999/")
        self.assertEqual(resp.get("status"), 0)
        self.assertIn("SSRF", resp.get("error", ""))


class TestFetchGuard(unittest.TestCase):
    """fetch 入口拦截（不联网）。"""

    def test_fetch_v3_blocks_private(self):
        from fetch_v3 import fetch_v3
        out = fetch_v3("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(out.get("success"))
        self.assertEqual(out.get("fetch_method"), "blocked")

    def test_fetch_page_blocks_private(self):
        from fetch import fetch_page
        out = fetch_page("http://127.0.0.1:8000/")
        self.assertFalse(out.get("success"))
        self.assertIn("SSRF", out.get("error", ""))


if __name__ == "__main__":
    unittest.main()

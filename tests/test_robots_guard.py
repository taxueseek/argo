#!/usr/bin/env python3
"""P1 回归测试：robots_guard 尊重层（全 mock，不联网）。

覆盖：
  1. Disallow 命中 → robots_blocked=True
  2. 允许路径 → False
  3. robots.txt 抓取失败 / 空内容 → False（容错放行）
  4. ARGO_RESPECT_ROBOTS=0 → 关闭，不抓取
  5. 进程内缓存复用（同域不重复抓）
  6. _fetch_robots_txt 的 HttpClient 抓取集成（200/404）
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


def _fake_public_dns():
    """本机 DNS 可能被劫持到保留段（198.18.x.x），测试需固定公网解析。"""
    return patch(
        "url_safety.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM,
                       socket.IPPROTO_TCP, "", ("93.184.215.14", 0))],
    )


class TestRobotsBlocked(unittest.TestCase):
    """robots_blocked 判定规则。"""

    def setUp(self):
        import robots_guard
        robots_guard.clear_cache()

    def test_disallow_path_blocked(self):
        import robots_guard
        robots = "User-agent: *\nDisallow: /private/\n"
        with patch.object(robots_guard, "_fetch_robots_txt",
                          return_value=robots):
            self.assertTrue(
                robots_guard.robots_blocked("https://example.com/private/x"))

    def test_allowed_path_passed(self):
        import robots_guard
        robots = "User-agent: *\nDisallow: /private/\n"
        with patch.object(robots_guard, "_fetch_robots_txt",
                          return_value=robots):
            self.assertFalse(
                robots_guard.robots_blocked("https://example.com/public"))

    def test_root_allow_all_passed(self):
        import robots_guard
        robots = "User-agent: *\nDisallow:\n"
        with patch.object(robots_guard, "_fetch_robots_txt",
                          return_value=robots):
            self.assertFalse(
                robots_guard.robots_blocked("https://example.com/anything"))

    def test_fetch_failure_passed(self):
        """robots.txt 抓取失败 → 容错放行。"""
        import robots_guard
        with patch.object(robots_guard, "_fetch_robots_txt",
                          return_value=None):
            self.assertFalse(robots_guard.robots_blocked("https://example.com/x"))

    def test_empty_robots_passed(self):
        import robots_guard
        with patch.object(robots_guard, "_fetch_robots_txt",
                          return_value="# no rules"):
            self.assertFalse(robots_guard.robots_blocked("https://example.com/x"))

    def test_env_disabled_skips_fetch(self):
        import robots_guard
        with patch.dict("os.environ", {"ARGO_RESPECT_ROBOTS": "0"}), \
             patch.object(robots_guard, "_fetch_robots_txt",
                          side_effect=AssertionError("开关关闭不应抓取")):
            self.assertFalse(robots_guard.robots_blocked("https://example.com/x"))

    def test_cache_reuses_parser_same_domain(self):
        """同域第二次判定不重复抓 robots.txt。"""
        import robots_guard
        robots = "User-agent: *\nDisallow: /a\n"
        with patch.object(robots_guard, "_fetch_robots_txt",
                          return_value=robots) as m_fetch:
            self.assertTrue(robots_guard.robots_blocked("https://example.com/a"))
            self.assertFalse(robots_guard.robots_blocked("https://example.com/b"))
        m_fetch.assert_called_once()

    def test_bad_url_no_host_passed(self):
        import robots_guard
        with patch.object(robots_guard, "_fetch_robots_txt",
                          side_effect=AssertionError("不应抓取")):
            self.assertFalse(robots_guard.robots_blocked("not-a-url"))

    def test_user_agent_asterisk_matches_any(self):
        """使用通配 UA：即使规则只写给特定 bot 也按 * 通用规则判定。"""
        import robots_guard
        robots = "User-agent: *\nDisallow: /search\n"
        with patch.object(robots_guard, "_fetch_robots_txt",
                          return_value=robots):
            self.assertTrue(
                robots_guard.robots_blocked("https://example.com/search/query"))


class TestFetchRobotsTxt(unittest.TestCase):
    """_fetch_robots_txt 的 HttpClient 抓取集成。"""

    def test_200_returns_text(self):
        import robots_guard
        fake_client = unittest.mock.MagicMock()
        fake_client.get.return_value = {
            "status": 200, "text": "User-agent: *\nDisallow: /x\n"}
        with _fake_public_dns(), \
             patch("http_client.HttpClient", return_value=fake_client):
            text = robots_guard._fetch_robots_txt("example.com", 2.0)
        self.assertIn("Disallow", text or "")
        # 抓 robots.txt 用固定合规 UA，不用随机轮换
        _, kwargs = fake_client.get.call_args
        self.assertIn("User-Agent", kwargs.get("extra_headers", {}))

    def test_404_returns_none(self):
        import robots_guard
        fake_client = unittest.mock.MagicMock()
        fake_client.get.return_value = {"status": 404, "text": "not found"}
        with _fake_public_dns(), \
             patch("http_client.HttpClient", return_value=fake_client):
            self.assertIsNone(robots_guard._fetch_robots_txt("example.com", 2.0))

    def test_5xx_returns_none(self):
        import robots_guard
        fake_client = unittest.mock.MagicMock()
        fake_client.get.return_value = {"status": 500, "text": ""}
        with _fake_public_dns(), \
             patch("http_client.HttpClient", return_value=fake_client):
            self.assertIsNone(robots_guard._fetch_robots_txt("example.com", 2.0))

    def test_network_error_returns_none(self):
        import robots_guard
        fake_client = unittest.mock.MagicMock()
        fake_client.get.side_effect = OSError("connection reset")
        with _fake_public_dns(), \
             patch("http_client.HttpClient", return_value=fake_client):
            self.assertIsNone(robots_guard._fetch_robots_txt("example.com", 2.0))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""net_proxy 出口调度单元测试：优先级矩阵 / 隧道构造 / 绝对 URL 选择器。

issue #13（2026-09-14）：http_client 直用 http.client，不认标准代理环境变量，
需代理站点（GitHub）抓取必然失败。修复=统一出口决策点 net_proxy，本测试
锁死解析优先级与连接构造契约。全 mock，无网络。
"""

from __future__ import annotations

import os
import sys
import unittest
import http.client
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import net_proxy  # noqa: E402

_PROXY = "http://127.0.0.1:7890"


def _cfg(rules=None, url=""):
    return {"url": url, "rules": rules or {}}


class TestResolvePriority(unittest.TestCase):
    def setUp(self):
        self._cfg_patch = patch.object(net_proxy, "_network_cfg",
                                       return_value=_cfg())
        self._cfg_patch.start()
        self.addCleanup(self._cfg_patch.stop)

    def test_override_wins_and_direct_sentinel(self):
        self.assertEqual(net_proxy.resolve_proxy("https://a.com", override=_PROXY), _PROXY)
        self.assertIsNone(net_proxy.resolve_proxy("https://a.com", override="direct"))

    def test_argo_env_used_without_rules(self):
        with patch.dict("os.environ", {"ARGO_PROXY": _PROXY}):
            self.assertEqual(net_proxy.resolve_proxy("https://bochaai.com"), _PROXY)

    def test_rules_direct_beats_argo_env(self):
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(rules={"bochaai.com": "direct"})), \
             patch.dict("os.environ", {"ARGO_PROXY": _PROXY}):
            self.assertIsNone(net_proxy.resolve_proxy("https://open.bochaai.com/x"))

    def test_rules_suffix_match_with_proxy(self):
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(rules={"github.com": _PROXY})), \
             patch.dict("os.environ", {"ARGO_PROXY": "http://other:1"}):
            # 更具体的域规则优先于全局 env
            self.assertEqual(net_proxy.resolve_proxy("https://api.github.com/z"), _PROXY)

    def test_argo_env_beats_config_url(self):
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(url="http://cfg:2")), \
             patch.dict("os.environ", {"ARGO_PROXY": _PROXY}):
            self.assertEqual(net_proxy.resolve_proxy("https://a.com"), _PROXY)

    def test_config_url_beats_standard_env(self):
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(url="http://cfg:2")), \
             patch.dict("os.environ",
                        {"ARGO_PROXY": "", "HTTPS_PROXY": "http://env:3",
                         "https_proxy": ""}):
            self.assertEqual(net_proxy.resolve_proxy("https://a.com"), "http://cfg:2")

    def test_standard_env_used_as_last_resort(self):
        """标准环境变量是最后保底。直接 patch getproxies/proxy_bypass，
        规避同名大小写变量在本机的真实串扰（3.14 后写者胜）。"""
        with patch("urllib.request.getproxies", return_value={"https": _PROXY}), \
             patch("urllib.request.proxy_bypass", return_value=False), \
             patch.dict("os.environ", {"ARGO_PROXY": ""}):
            self.assertEqual(net_proxy.resolve_proxy("https://a.com"), _PROXY)

    def test_no_proxy_bypass(self):
        with patch("urllib.request.getproxies", return_value={"https": _PROXY}), \
             patch("urllib.request.proxy_bypass", return_value=True):
            self.assertIsNone(net_proxy.resolve_proxy("https://a.com"))

    def test_no_config_no_env_direct(self):
        with patch.dict("os.environ",
                        {"ARGO_PROXY": "", "HTTPS_PROXY": "", "https_proxy": "",
                         "ALL_PROXY": "", "all_proxy": "", "HTTP_PROXY": "",
                         "http_proxy": ""}, clear=False):
            self.assertIsNone(net_proxy.resolve_proxy("https://a.com"))

    def test_all_proxy_fallback(self):
        """ALL_PROXY 在 getproxies() 里是 'all' 键（2026-09-28 实锤：此前
        resolve_proxy 只按 scheme 取，只配 ALL_PROXY 的用户全链路直连）。"""
        with patch("urllib.request.getproxies", return_value={"all": _PROXY}), \
             patch("urllib.request.proxy_bypass", return_value=False), \
             patch.dict("os.environ", {"ARGO_PROXY": ""}):
            self.assertEqual(net_proxy.resolve_proxy("https://a.com"), _PROXY)

    def test_all_proxy_respects_bypass(self):
        with patch("urllib.request.getproxies", return_value={"all": _PROXY}), \
             patch("urllib.request.proxy_bypass", return_value=True):
            self.assertIsNone(net_proxy.resolve_proxy("https://a.com"))

    def test_socks_proxy_rejected_loudly(self):
        """socks 代理在 open_connection 这条 urllib 出口必须显式报错，不是挂起。"""
        with self.assertRaises(ValueError):
            net_proxy.open_connection(urllib.parse.urlparse("https://a.com/x"),
                                      5.0, "socks5://127.0.0.1:1080")


class TestOpenConnection(unittest.TestCase):
    def test_https_via_proxy_sets_tunnel(self):
        parsed = urllib.parse.urlparse("https://github.com/x")
        seen = {}

        class FakeTunnelHTTPS:
            def __init__(self, host, port, timeout=None):
                seen["conn"] = (host, port)

            def set_tunnel(self, host, port):
                seen["tunnel"] = (host, port)

        with patch.object(http.client, "HTTPSConnection", FakeTunnelHTTPS):
            conn, via = net_proxy.open_connection(parsed, 5.0, _PROXY)
        self.assertTrue(via)
        self.assertEqual(seen["conn"], ("127.0.0.1", 7890))
        self.assertEqual(seen["tunnel"], ("github.com", 443))

    def test_http_via_proxy(self):
        parsed = urllib.parse.urlparse("http://example.com/a")
        conn, via = net_proxy.open_connection(parsed, 5.0, _PROXY)
        self.assertTrue(via)
        self.assertEqual(net_proxy.request_selector(parsed, "/a", via),
                         "http://example.com/a")

    def test_no_proxy_direct_https(self):
        parsed = urllib.parse.urlparse("https://github.com/x")
        conn, via = net_proxy.open_connection(parsed, 5.0, None)
        self.assertFalse(via)
        self.assertEqual(net_proxy.request_selector(parsed, "/x", via), "/x")


class TestOpenUrlIsProxyAware(unittest.TestCase):
    """`open_url` 是 urllib 类出口的唯一入口（issue #13 同类统一处理，2026-09-15）。

    背景：issue #13 修复时只覆盖了 `http_open`（引擎侧），fetch/job/health/
    pdf/readability/train/wx/search 共 13 处仍直接调 `urllib.request.urlopen`
    ——在「必须经代理才能出网」的环境里，这些出口一律连不上。统一处理后出口
    决策只有 net_proxy 一处，本类锁住「配了规则就走代理、没配就直连」。
    """

    def test_uses_proxy_opener_when_rule_matches(self):
        seen = {}

        class _FakeOpener:
            def open(self, req, timeout=None):
                seen["timeout"] = timeout
                seen["url"] = getattr(req, "full_url", req)
                return "RESP"

        def _fake_build(handler):
            seen["handler"] = handler
            return _FakeOpener()

        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(rules={"github.com": _PROXY})), \
             patch.object(urllib.request, "build_opener", _fake_build), \
             patch.object(urllib.request, "urlopen",
                          side_effect=AssertionError("不该走直连")):
            out = net_proxy.open_url("https://github.com/a/b", timeout=7.0)
        self.assertEqual(out, "RESP")
        self.assertEqual(seen["url"], "https://github.com/a/b")
        self.assertEqual(seen["timeout"], 7.0)
        self.assertEqual(seen["handler"].proxies, {"https": _PROXY})

    def test_falls_back_to_plain_urlopen_without_proxy(self):
        calls = {}

        def _fake_urlopen(req, timeout=None):
            calls["url"] = getattr(req, "full_url", req)
            calls["timeout"] = timeout
            return "DIRECT"

        with patch.object(net_proxy, "_network_cfg", return_value=_cfg()), \
             patch.object(urllib.request, "urlopen", _fake_urlopen), \
             patch.object(urllib.request, "build_opener",
                          side_effect=AssertionError("无代理不该建 opener")):
            with patch.dict("os.environ", {}, clear=True):
                out = net_proxy.open_url("https://example.com/x", timeout=3.0)
        self.assertEqual(out, "DIRECT")
        self.assertEqual(calls["url"], "https://example.com/x")
        self.assertEqual(calls["timeout"], 3.0)

    def test_accepts_request_object(self):
        req = urllib.request.Request("https://example.com/y")
        with patch.object(net_proxy, "_network_cfg", return_value=_cfg()), \
             patch.object(urllib.request, "urlopen",
                          lambda r, timeout=None: r):
            out = net_proxy.open_url(req, timeout=1.0)
        self.assertIs(out, req)

    def test_ignores_standard_env_proxy(self):
        """标准环境变量由 urlopen 自己认，本函数不得重复接管（会改 mock 契约）。"""
        with patch.object(net_proxy, "_network_cfg", return_value=_cfg()), \
             patch.dict("os.environ", {"HTTPS_PROXY": _PROXY}), \
             patch.object(urllib.request, "build_opener",
                          side_effect=AssertionError("不该重复接管标准环境变量")):
            self.assertIsNone(
                net_proxy.resolve_proxy("https://example.com", include_standard_env=False))


class TestEnvFileDrivesNetworkLayer(unittest.TestCase):
    """环境层必须真正驱动出口调度（2026-09-30 修复的失效开关）。

    argo 的配置源是 env 文件（ARGO_ENV_FILE / 平台配置根 .../argo/env），但
    `sync_envfile_to_environ()` 只在 MCP server 启动时调用，CLI 路径不同步。
    本模块此前直读 `os.environ`，于是**写在 env 文件里的代理开关在 CLI 下静默
    失效**——实测 ARGO_PROXY / ARGO_GDELT_PROXY 在 env 文件里 engine_env 读得到、
    net_proxy 读不到。同仓 engines_base._resolve（引擎 spec 的 {VAR} 展开）早已
    走 engine_env，net_proxy 是唯一的例外。

    本类锁死「env 文件里的值同样生效」，并覆盖 ${VAR} 的三条语义。
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._envfile = Path(self._tmp.name) / "env"
        self._envfile.write_text(
            "ARGO_PROXY=http://127.0.0.1:9998\n"
            "ARGO_GDELT_PROXY=http://127.0.0.1:9999\n"
            "ARGO_TEST_DIRECT=direct\n",
            encoding="utf-8")
        # 清掉 os.environ 与 engine_env 的 envfile 缓存，确保只从文件取值
        p = patch.dict("os.environ", {"ARGO_ENV_FILE": str(self._envfile)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        import engine_env
        engine_env.reset_envfile_cache()
        self.addCleanup(engine_env.reset_envfile_cache)
        self._cfg_patch = patch.object(net_proxy, "_network_cfg",
                                       return_value=_cfg())
        self._cfg_patch.start()
        self.addCleanup(self._cfg_patch.stop)

    def _without(self, *names):
        """确保这些名字不在 os.environ（只留 env 文件作为来源）。"""
        for n in names:
            os.environ.pop(n, None)

    def test_argo_proxy_from_env_file(self):
        self._without("ARGO_PROXY")
        self.assertEqual(net_proxy.resolve_proxy("https://bochaai.com"),
                         "http://127.0.0.1:9998")

    def test_rules_placeholder_from_env_file(self):
        self._without("ARGO_GDELT_PROXY")
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(rules={"gdeltproject.org": "${ARGO_GDELT_PROXY}"})):
            self.assertEqual(
                net_proxy.resolve_proxy("https://api.gdeltproject.org/api/v2/doc/doc"),
                "http://127.0.0.1:9999")

    def test_placeholder_unset_is_direct(self):
        """未设置 = 不设代理（None），不是空代理。"""
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(rules={"x.com": "${ARGO_NO_SUCH_VAR}"})):
            self.assertIsNone(net_proxy.resolve_proxy("https://x.com/a"))

    def test_placeholder_resolving_to_direct_is_direct(self):
        """env 值本身写成 direct 时也必须是直连，不能当代理字面量发出去。"""
        self._without("ARGO_TEST_DIRECT")
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(rules={"x.com": "${ARGO_TEST_DIRECT}"})):
            self.assertIsNone(net_proxy.resolve_proxy("https://x.com/a"))

    def test_global_url_supports_placeholder(self):
        """全局 url 与 rules 同语义（此前只有 rules 支持 ${VAR}）。

        必须先把 ARGO_PROXY 从 env 文件里去掉：按文档优先级它**应该**盖过
        config url（优先级 3 > 4），留着它测到的会是上一级，测不到本项。
        """
        self._without("ARGO_GLOBAL_PROXY")
        self._envfile.write_text("ARGO_GLOBAL_PROXY=http://127.0.0.1:7000\n",
                                 encoding="utf-8")
        import engine_env
        engine_env.reset_envfile_cache()
        with patch.object(net_proxy, "_network_cfg",
                          return_value=_cfg(url="${ARGO_GLOBAL_PROXY}")):
            self.assertEqual(net_proxy.resolve_proxy("https://example.com/z"),
                             "http://127.0.0.1:7000")

    def test_envfile_failure_falls_back_to_os_environ(self):
        """engine_env 不可用时退回 os.environ（行为与改造前一致，不阻断出口）。"""
        self._without("ARGO_PROXY")
        os.environ["ARGO_PROXY"] = "http://127.0.0.1:7777"
        self.addCleanup(os.environ.pop, "ARGO_PROXY", None)
        with patch.dict(sys.modules, {"engine_env": None}):
            self.assertEqual(net_proxy._argo_env("ARGO_PROXY"),
                             "http://127.0.0.1:7777")


if __name__ == "__main__":
    unittest.main()

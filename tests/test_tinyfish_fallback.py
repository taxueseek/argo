#!/usr/bin/env python3
"""
test_tinyfish_fallback.py — tinyfish 直连渲染层回归测试

覆盖 fetch_v3 第二级A（tinyfish，不打真实网络）：
  单层行为：开关默认开 / env 关；key 缺失 / 端点成功 / 空结果 / errors 透出。
  主链降级顺序：JS 壳里 tinyfish 优先于本地 Chrome；成功则短路浏览器；
    失败自动回退浏览器；need_html=True 或开关关闭时跳过 tinyfish。
"""

import json
import os
import sys
import urllib.error
import urllib.request

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_v3  # noqa: E402

_URL = "https://docs.example.com/guide"


# ─── 开关 ─────────────────────────────────────────────────────────────────────

def test_tinyfish_enabled_default_on(monkeypatch):
    monkeypatch.delenv("ARGO_FETCH_TINYFISH", raising=False)
    assert fetch_v3._tinyfish_enabled()


def test_tinyfish_enabled_env_off(monkeypatch):
    for v in ("0", "false", "False", "no"):
        monkeypatch.setenv("ARGO_FETCH_TINYFISH", v)
        assert not fetch_v3._tinyfish_enabled()


# ─── 单层：mock urlopen ───────────────────────────────────────────────────────

def _fake_urlopen(payload, status=200):
    """伪造 urllib.request.urlopen：返回带 payload JSON 的响应对象。"""
    body = json.dumps(payload).encode("utf-8")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return body

    def fake_urlopen(req, timeout=None):
        return _Resp()

    return fake_urlopen


def _isolate_envfile(monkeypatch):
    """屏蔽真实 ~/.config/argo/env：engine_env.get_env 会热读该文件兜底，
    不隔离则本机配了 TINYFISH_API_KEY 时 delenv 断言失效（环境依赖测试）。"""
    import engine_env
    monkeypatch.setattr(engine_env, "_envfile_path",
                        lambda: Path("/nonexistent/argo/env"))


def _set_key(monkeypatch):
    _isolate_envfile(monkeypatch)
    monkeypatch.setenv("TINYFISH_API_KEY", "sk-tinyfish-test")


def test_tinyfish_fetch_missing_key(monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    _isolate_envfile(monkeypatch)
    out = fetch_v3._tinyfish_fetch(_URL)
    assert out["success"] is False
    assert "TINYFISH_API_KEY not configured" in out["error"]
    assert out["fetch_method"] == "tinyfish"


def test_tinyfish_fetch_success(monkeypatch):
    _set_key(monkeypatch)
    body = "# Title\n\n" + "正文内容。" * 60
    monkeypatch.setattr("urllib.request.urlopen",
                        _fake_urlopen({"results": [{"text": body,
                                                    "title": "Guide"}]}))
    out = fetch_v3._tinyfish_fetch(_URL, max_chars=100)
    assert out["success"] is True
    assert out["fetch_method"] == "tinyfish"
    assert out["title"] == "Guide"
    assert len(out["content"]) == 100          # max_chars 截断
    assert out["html"] == ""                    # markdown-only，无 raw html


def test_tinyfish_fetch_network_error(monkeypatch):
    _set_key(monkeypatch)

    def boom(req, timeout=None):
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    out = fetch_v3._tinyfish_fetch(_URL)
    assert out["success"] is False and "timeout" in out["error"]


def test_tinyfish_fetch_empty_result(monkeypatch):
    _set_key(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen",
                        _fake_urlopen({"results": []}))
    out = fetch_v3._tinyfish_fetch(_URL)
    assert out["success"] is False and "empty result" in out["error"]


def test_tinyfish_fetch_empty_content(monkeypatch):
    _set_key(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen",
                        _fake_urlopen({"results": [{"text": "  ",
                                                    "title": ""}]}))
    out = fetch_v3._tinyfish_fetch(_URL)
    assert out["success"] is False and "empty content" in out["error"]


def test_tinyfish_fetch_reports_api_error(monkeypatch):
    """errors 非空时优先透出真实原因，而非笼统 empty。"""
    _set_key(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(
        {"results": [], "errors": [{"message": "rate limited"}]}))
    out = fetch_v3._tinyfish_fetch(_URL)
    assert out["success"] is False and "rate limited" in out["error"]


# ─── 主链集成：tinyfish 优先于浏览器，成功短路 / 失败回退 ───────────────────

@pytest.fixture()
def no_robots(monkeypatch):
    import robots_guard
    monkeypatch.setattr(robots_guard, "robots_blocked",
                        lambda url, timeout=5.0: False)


@pytest.fixture()
def chain_env(monkeypatch):
    """关闭 md 探测 / 移动分支 / TLS 指纹，直达 tinyfish↔浏览器降级段。"""
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "0")
    monkeypatch.setenv("ARGO_FETCH_MOBILE", "0")
    monkeypatch.setenv("ARGO_FETCH_IMPERSONATE", "0")
    _set_key(monkeypatch)


def _shell_result():
    """JS 风控壳：success 但取不到正文 → _needs_browser 判定需升级浏览器。"""
    return {"url": _URL, "content": "", "html": "<html><script>acrawler()"
            "</script></html>", "title": "", "length": 0, "success": True,
            "error": None, "fetch_method": "http"}


def _ok_result(method, extra=None):
    r = {"url": _URL, "content": "真实正文内容。" * 40, "html": "", "title": "t",
         "length": 200, "success": True, "error": None, "fetch_method": method}
    r.update(extra or {})
    return r


def test_tinyfish_adopted_and_short_circuits_browser(
        monkeypatch, no_robots, chain_env):
    monkeypatch.setattr(fetch_v3, "_http_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        _shell_result())

    def _no_browser(*a, **k):
        raise AssertionError("tinyfish 成功不应再起本地浏览器")

    monkeypatch.setattr(fetch_v3, "_browser_fetch", _no_browser)
    monkeypatch.setattr(fetch_v3, "_tinyfish_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        _ok_result("tinyfish"))
    out = fetch_v3.fetch_v3(_URL, skip_cache=True)
    assert out["fetch_method"] == "tinyfish"


def test_tinyfish_failure_falls_back_to_browser(
        monkeypatch, no_robots, chain_env):
    monkeypatch.setattr(fetch_v3, "_http_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        _shell_result())
    calls = {}

    def fake_browser(url, max_chars=8000, timeout=15.0, actions=None):
        calls["browser"] = True
        return _ok_result("chrome_cdp")

    monkeypatch.setattr(fetch_v3, "_browser_fetch", fake_browser)
    monkeypatch.setattr(fetch_v3, "_tinyfish_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        {"url": url, "content": "", "html": "", "title": "",
                         "length": 0, "success": False,
                         "error": "tinyfish empty content",
                         "fetch_method": "tinyfish"})
    out = fetch_v3.fetch_v3(_URL, skip_cache=True)
    assert calls.get("browser") and out["fetch_method"] == "chrome_cdp"


def test_tinyfish_missing_key_real_fallback(monkeypatch, no_robots, chain_env):
    """真实『没有 tinyfish key』：直连层快速失败，不抛异常、不掉链，
    正常降级到本地浏览器。不 mock _tinyfish_fetch，走真实 key 缺失路径。"""
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    _isolate_envfile(monkeypatch)
    monkeypatch.setattr(fetch_v3, "_http_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        _shell_result())
    calls = {"tinyfish": False, "browser": False}
    real_tf = fetch_v3._tinyfish_fetch

    def spy_tf(url, max_chars=8000, timeout=8.0):
        calls["tinyfish"] = True
        return real_tf(url, max_chars, timeout)

    monkeypatch.setattr(fetch_v3, "_tinyfish_fetch", spy_tf)

    def fake_browser(url, max_chars=8000, timeout=15.0, actions=None):
        calls["browser"] = True
        return _ok_result("chrome_cdp")

    monkeypatch.setattr(fetch_v3, "_browser_fetch", fake_browser)
    out = fetch_v3.fetch_v3(_URL, skip_cache=True)
    assert calls["tinyfish"] and calls["browser"]
    assert out["fetch_method"] == "chrome_cdp"


def test_tinyfish_stop_signal_halts_chain(monkeypatch, no_robots, chain_env):
    """HTTP 返回明确停止信号（429/503）→ 不因 tinyfish 空转放行，仍按
    stop_signal 停链（限速/过载不放大负载）。"""
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    _isolate_envfile(monkeypatch)
    stopped = _shell_result()
    stopped["success"] = False
    stopped["stop_signal"] = "rate_limited"
    monkeypatch.setattr(fetch_v3, "_http_fetch",
                        lambda url, max_chars=8000, timeout=8.0: stopped)

    def _no_tinyfish(*a, **k):
        raise AssertionError("stop_signal 已停链，不应再走 tinyfish")

    monkeypatch.setattr(fetch_v3, "_tinyfish_fetch", _no_tinyfish)
    monkeypatch.setattr(fetch_v3, "_browser_fetch",
                        lambda url, max_chars=8000, timeout=15.0, actions=None:
                        _ok_result("chrome_cdp"))
    out = fetch_v3.fetch_v3(_URL, skip_cache=True)
    assert out["fetch_method"] == "http" and out.get("stop_signal")


def test_need_html_skips_tinyfish(monkeypatch, no_robots, chain_env):
    monkeypatch.setattr(fetch_v3, "_http_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        _shell_result())

    def _no_tinyfish(*a, **k):
        raise AssertionError("need_html=True 不应走 tinyfish")

    monkeypatch.setattr(fetch_v3, "_tinyfish_fetch", _no_tinyfish)
    monkeypatch.setattr(fetch_v3, "_browser_fetch",
                        lambda url, max_chars=8000, timeout=15.0, actions=None:
                        _ok_result("chrome_cdp", {"html": "<html>hi</html>"}))
    out = fetch_v3.fetch_v3(_URL, skip_cache=True, need_html=True)
    assert out["fetch_method"] == "chrome_cdp"


def test_tinyfish_disabled_skips_to_browser(monkeypatch, no_robots, chain_env):
    monkeypatch.setenv("ARGO_FETCH_TINYFISH", "0")
    monkeypatch.setattr(fetch_v3, "_http_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        _shell_result())

    def _no_tinyfish(*a, **k):
        raise AssertionError("开关关闭不应走 tinyfish")

    monkeypatch.setattr(fetch_v3, "_tinyfish_fetch", _no_tinyfish)
    monkeypatch.setattr(fetch_v3, "_browser_fetch",
                        lambda url, max_chars=8000, timeout=15.0, actions=None:
                        _ok_result("chrome_cdp"))
    out = fetch_v3.fetch_v3(_URL, skip_cache=True)
    assert out["fetch_method"] == "chrome_cdp"


# ─── page_type 内容回退（markdown-only 源质量信号）──────────────────────────

def test_detect_page_type_markdown_only_article():
    # 需 >200 字才判 article；180 字（60×3）够不到阈值
    assert fetch_v3._detect_page_type("", "正文。" * 80) == "article"


def test_detect_page_type_markdown_only_short_unknown():
    assert fetch_v3._detect_page_type("", "短") == "unknown"


def test_detect_page_type_html_still_preferred():
    assert fetch_v3._detect_page_type(
        "<html><body><article>正文内容</article></body></html>", "长正文。" * 60
    ) == "article"


# ─── 与 content_signals 统一后的结构判定 ───────────────────────────────────
# 此前 fetch_quality 内联了第二套 detect_page_type（朴素正则、不认 URL），
# 与 content_signals.detect_page_type 行为分裂。统一后以下类型必须能被识别。

@pytest.mark.parametrize("html,expected", [
    ('<meta http-equiv="refresh" content="0;url=https://x.com">', "redirect"),
    ("<html><body>subscribe to continue reading</body></html>", "paywall"),
    ('<html><body><div id="forum">x</div></body></html>', "forum"),
    ('<html><body><div data-answerid="7">x</div></body></html>', "qa"),
    ('<html><body><div class="docusaurus">x</div></body></html>', "docs"),
])
def test_detect_page_type_structural_markers(html, expected):
    assert fetch_v3._detect_page_type(html, "短正文") == expected


def test_detect_page_type_list_needs_url_and_links():
    """list 判定依赖同域链接密度：无 host 时退化为 unknown，不误判。"""
    html = "<html><body>" + '<a href="/a/{}">t</a>'.format(1) * 30 + "</body></html>"
    # 无 url → 拿不到 host → 不做 list 判定
    assert fetch_v3._detect_page_type(html, "短") == "unknown"
    # 有 url → 同域链接 >= 20 且文本少 → list
    assert fetch_v3._detect_page_type(html, "短", "https://ex.com/") == "list"


def test_detect_page_type_matches_content_signals():
    """两套实现已合并：fetch_v3 结果必须等于 content_signals 单一真源。"""
    import content_signals
    from fetch_quality import _detect_page_type
    samples = [
        ("", "正文。" * 80),
        ("", "短"),
        ('<html class="docusaurus">x</html>', "正文"),
        ("<html>subscribe to continue</html>", "正文"),
        ("<html><article>正文</article></html>", "长正文。" * 60),
    ]
    for html, content in samples:
        assert _detect_page_type(html, content) == \
            content_signals.detect_page_type(html, "", content)["page_type"]

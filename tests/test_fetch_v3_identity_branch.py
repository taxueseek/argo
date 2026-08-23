#!/usr/bin/env python3
"""
test_fetch_v3_identity_branch.py — 抓取身份分支回归测试

覆盖 fetch_v3 两个新层级（不打真实网络）：
  第零级：{url}.md 变体探测（URL 规则 / Markdown 嗅探 / 命中跳链）
  第一级A2：移动端 UA 分支（失败/风控壳后采纳移动结果，TLS 层不再触发）
"""

import os
import sys
import time

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_v3  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_identity(tmp_path, monkeypatch):
    """身份记忆指向临时文件，测试不污染真实 ~/.cache。"""
    monkeypatch.setattr(fetch_v3, "_IDENTITY_PATH",
                        str(tmp_path / "identity.json"))
    monkeypatch.setattr(fetch_v3, "_identity_mem", {})
    monkeypatch.setattr(fetch_v3, "_identity_loaded", True)


# ─── .md 变体探测：URL 规则 ──────────────────────────────────────────────────

def test_md_variant_url_doc_page():
    assert (fetch_v3._md_variant_url("https://docs.example.com/guides/tools")
            == "https://docs.example.com/guides/tools.md")


def test_md_variant_url_trailing_slash():
    assert (fetch_v3._md_variant_url("https://docs.example.com/guides/")
            == "https://docs.example.com/guides.md")


def test_md_variant_url_skips_extension_paths():
    for url in ("https://x.com/a/b.html", "https://x.com/api/data.json",
                "https://x.com/file.PDF"):
        assert fetch_v3._md_variant_url(url) is None, url


def test_md_variant_url_skips_query_and_non_http():
    assert fetch_v3._md_variant_url("https://x.com/search?q=1") is None
    assert fetch_v3._md_variant_url("ftp://x.com/doc") is None


# ─── .md 变体探测：内容嗅探 ──────────────────────────────────────────────────

MD_BODY = "# Guide\n\n" + "正文段落。" * 60


def test_looks_like_markdown_accepts_md():
    assert fetch_v3._looks_like_markdown(MD_BODY)


def test_looks_like_markdown_rejects_html_sniff():
    assert not fetch_v3._looks_like_markdown("<!DOCTYPE html><html>" + MD_BODY)


def test_looks_like_markdown_rejects_short_text():
    assert not fetch_v3._looks_like_markdown("# short")


def test_md_variant_fetch_rejects_html_content_type(monkeypatch):
    import http_client

    def fake_get(self, url, extra_headers=None, follow_redirects=True):
        return {"status": 200, "headers": {"Content-Type": "text/html"},
                "text": "<html><body>" + "x" * 300 + "</body></html>",
                "url": url, "elapsed_ms": 1}

    monkeypatch.setattr(http_client.HttpClient, "get", fake_get)
    assert fetch_v3._md_variant_fetch(
        "https://docs.example.com/guide") is None


def test_md_variant_fetch_hits_markdown(monkeypatch):
    import http_client

    def fake_get(self, url, extra_headers=None, follow_redirects=True):
        # 探测请求必须用诚实 argo 身份，不得伪装浏览器/AI 爬虫
        ua = (extra_headers or {}).get("User-Agent", "")
        assert ua.startswith("argo-fetch-v3/")
        return {"status": 200,
                "headers": {"Content-Type": "text/markdown; charset=utf-8"},
                "text": MD_BODY, "url": url, "elapsed_ms": 1}

    monkeypatch.setattr(http_client.HttpClient, "get", fake_get)
    result = fetch_v3._md_variant_fetch("https://docs.example.com/guide")
    assert result is not None
    assert result["fetch_method"] == "md_variant"
    assert result["success"] and result["title"] == "Guide"
    assert result["content"].startswith("# Guide")


def test_md_variant_fetch_non_200_returns_none(monkeypatch):
    import http_client

    def fake_get(self, url, extra_headers=None, follow_redirects=True):
        return {"status": 404, "headers": {}, "text": "", "url": url,
                "elapsed_ms": 1}

    monkeypatch.setattr(http_client.HttpClient, "get", fake_get)
    assert fetch_v3._md_variant_fetch("https://docs.example.com/guide") is None


# ─── 开关 ────────────────────────────────────────────────────────────────────

def test_toggles_default_on(monkeypatch):
    monkeypatch.delenv("ARGO_FETCH_MD_VARIANT", raising=False)
    monkeypatch.delenv("ARGO_FETCH_MOBILE", raising=False)
    assert fetch_v3._md_variant_enabled() and fetch_v3._mobile_branch_enabled()


def test_toggles_env_off(monkeypatch):
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "0")
    monkeypatch.setenv("ARGO_FETCH_MOBILE", "0")
    assert not fetch_v3._md_variant_enabled()
    assert not fetch_v3._mobile_branch_enabled()


# ─── 移动优先主机门控 ────────────────────────────────────────────────────────

def test_mobile_first_host_builtin_match():
    assert fetch_v3._mobile_first_host(
        "https://www.iesdouyin.com/share/video/123/")
    assert fetch_v3._mobile_first_host("https://douyin.com/")


def test_mobile_first_host_no_match():
    assert not fetch_v3._mobile_first_host("https://docs.example.com/guide")
    assert not fetch_v3._mobile_first_host(
        "https://evil.example.com/?u=douyin.com")


def test_mobile_first_host_env_extension(monkeypatch):
    monkeypatch.setenv("ARGO_MOBILE_FIRST_HOSTS", "weibo.cn, xueqiu.com")
    assert fetch_v3._mobile_first_host("https://m.xueqiu.com/123")
    assert fetch_v3._mobile_first_host("https://weibo.cn/x")
    # 后缀伪装不能命中：必须是域名本身或子域
    assert not fetch_v3._mobile_first_host("https://fake.com/douyin.com")


# ─── 主链集成：md 命中即跳过整条反爬链 ───────────────────────────────────────

_URL = "https://docs.example.com/guide"


@pytest.fixture()
def no_robots(monkeypatch):
    import robots_guard
    monkeypatch.setattr(robots_guard, "robots_blocked",
                        lambda url, timeout=5.0: False)


def _fail_http(url, max_chars=8000, timeout=8.0):
    return {"url": url, "content": "", "html": "", "title": "",
            "length": 0, "success": False, "error": "HTTP 403",
            "fetch_method": "http"}


def _ok_result(method, extra=None):
    r = {"url": _URL, "content": "真实正文内容。" * 40, "html": "", "title": "t",
         "length": 200, "success": True, "error": None, "fetch_method": method}
    r.update(extra or {})
    return r


def test_fetch_v3_md_hit_skips_chain(monkeypatch, no_robots):
    monkeypatch.setattr(fetch_v3, "_md_variant_fetch",
                        lambda url, mc, to: _ok_result("md_variant"))
    def _no_chain(*a, **k):
        raise AssertionError("md 命中后不应再走 HTTP/TLS/wayback/browser 链")
    monkeypatch.setattr(fetch_v3, "_http_fetch", _no_chain)
    monkeypatch.setattr(fetch_v3, "_tls_spoof_fetch", _no_chain)
    monkeypatch.setattr(fetch_v3, "_mobile_http_fetch", _no_chain)
    out = fetch_v3.fetch_v3(_URL, skip_cache=True)
    assert out["fetch_method"] == "md_variant" and out.get("md_variant")


def test_fetch_v3_mobile_adopted_before_tls(monkeypatch, no_robots):
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "0")
    monkeypatch.setattr(fetch_v3, "_http_fetch", _fail_http)
    calls = {}

    def fake_mobile(url, max_chars=8000, timeout=8.0):
        calls["mobile"] = True
        return _ok_result("http_mobile", {"ua_profile": "mobile"})

    monkeypatch.setattr(fetch_v3, "_mobile_http_fetch", fake_mobile)

    def _no_tls(*a, **k):
        raise AssertionError("移动分支命中后不应升级 TLS 指纹层")

    def _no_wb(*a, **k):
        raise AssertionError("移动分支命中后不应回退 wayback")

    monkeypatch.setattr(fetch_v3, "_tls_spoof_fetch", _no_tls)
    monkeypatch.setattr(fetch_v3, "_wayback_fetch", _no_wb)
    out = fetch_v3.fetch_v3(_URL, skip_cache=True, use_browser_fallback=False)
    assert calls.get("mobile") and out["fetch_method"] == "http_mobile"
    assert out.get("ua_profile") == "mobile"


def test_fetch_v3_mobile_disabled_falls_to_tls(monkeypatch, no_robots):
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "0")
    monkeypatch.setenv("ARGO_FETCH_MOBILE", "0")
    monkeypatch.setattr(fetch_v3, "_http_fetch", _fail_http)

    def _no_mobile(*a, **k):
        raise AssertionError("开关关闭时不应发起移动端尝试")

    def fake_tls(url, max_chars=8000, timeout=8.0):
        return _ok_result("tls_spoof")

    monkeypatch.setattr(fetch_v3, "_mobile_http_fetch", _no_mobile)
    monkeypatch.setattr(fetch_v3, "_tls_spoof_fetch", fake_tls)
    monkeypatch.setattr(fetch_v3, "_wayback_fetch",
                        lambda *a, **k: _fail_http(*a[:1]))
    out = fetch_v3.fetch_v3(_URL, skip_cache=True, use_browser_fallback=False)
    assert out["fetch_method"] == "tls_spoof"


def test_fetch_v3_mobile_shell_not_adopted(monkeypatch, no_robots):
    """移动端拿到的仍是风控壳 → 不采纳，继续走 TLS 层。"""
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "0")
    monkeypatch.setattr(fetch_v3, "_http_fetch", _fail_http)

    shell = _ok_result("http_mobile")
    shell["content"] = ""          # 提取不出正文 = 壳
    shell["length"] = 0
    shell["html"] = "<html><head></head><body><script>acrawler()</script></body></html>"
    monkeypatch.setattr(fetch_v3, "_mobile_http_fetch",
                        lambda url, max_chars=8000, timeout=8.0: shell)

    monkeypatch.setattr(fetch_v3, "_tls_spoof_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        _ok_result("tls_spoof"))
    monkeypatch.setattr(fetch_v3, "_wayback_fetch",
                        lambda *a, **k: _fail_http(*a[:1]))
    out = fetch_v3.fetch_v3(_URL, skip_cache=True, use_browser_fallback=False)
    assert out["fetch_method"] == "tls_spoof"


def test_fetch_v3_douyin_mobile_first(monkeypatch, no_robots):
    """分流型已知站点：移动 UA 首发，桌面请求不得先发（防风控连坐）。"""
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "0")

    def _no_desktop(url, max_chars=8000, timeout=8.0):
        raise AssertionError("分流型站点不应先发桌面请求")

    monkeypatch.setattr(fetch_v3, "_http_fetch", _no_desktop)
    monkeypatch.setattr(
        fetch_v3, "_mobile_http_fetch",
        lambda url, max_chars=8000, timeout=8.0:
        _ok_result("http_mobile", {"ua_profile": "mobile"}))
    out = fetch_v3.fetch_v3("https://www.iesdouyin.com/share/video/123/",
                            skip_cache=True)
    assert out["fetch_method"] == "http_mobile"
    assert out.get("ua_profile") == "mobile"


# ─── 单次直连原则与身份记忆 ──────────────────────────────────────────────────

def test_fetch_v3_gated_skips_md_probe(monkeypatch, no_robots):
    """门控站不跑 .md 探测：少一次主机触碰，防连坐限速。"""
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "1")

    def _no_md(url, max_chars=8000, timeout=8.0):
        raise AssertionError("门控站不应发起 .md 探测")

    monkeypatch.setattr(fetch_v3, "_md_variant_fetch", _no_md)
    monkeypatch.setattr(fetch_v3, "_mobile_http_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        _ok_result("http_mobile"))
    out = fetch_v3.fetch_v3("https://www.iesdouyin.com/share/video/9/",
                            skip_cache=True)
    assert out["fetch_method"] == "http_mobile"


def test_fetch_v3_gated_fail_skips_tls(monkeypatch, no_robots):
    """门控站移动首发失败 → 跳过 TLS 直连，直接 Wayback/浏览器。"""
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "0")

    def _fail(url, max_chars=8000, timeout=8.0):
        r = _ok_result("http_mobile")
        r.update(success=False, content="", length=0)
        return r

    monkeypatch.setattr(fetch_v3, "_mobile_http_fetch", _fail)

    def _no_tls(*a, **k):
        raise AssertionError("门控站失败后不应再直连 TLS")

    monkeypatch.setattr(fetch_v3, "_tls_spoof_fetch", _no_tls)
    monkeypatch.setattr(fetch_v3, "_wayback_fetch",
                        lambda url, max_chars=8000, timeout=12.0:
                        _ok_result("wayback"))
    out = fetch_v3.fetch_v3("https://www.iesdouyin.com/share/video/9/",
                            skip_cache=True, use_browser_fallback=False)
    assert out["fetch_method"] == "wayback"


def test_identity_memory_routes_mobile_first(monkeypatch, no_robots):
    """非门控 host 但记忆命中 → 移动首发。"""
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "0")
    monkeypatch.setattr(fetch_v3, "_identity_mem",
                        {"learned.example.com": time.time() + 3600})

    def _no_desktop(url, max_chars=8000, timeout=8.0):
        raise AssertionError("身份记忆命中应移动首发，不发桌面请求")

    monkeypatch.setattr(fetch_v3, "_http_fetch", _no_desktop)
    monkeypatch.setattr(fetch_v3, "_mobile_http_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        _ok_result("http_mobile"))
    out = fetch_v3.fetch_v3("https://learned.example.com/v/1",
                            skip_cache=True)
    assert out["fetch_method"] == "http_mobile"
    assert out.get("ua_profile") == "mobile"


def test_generic_mobile_win_remembers_host(monkeypatch, no_robots):
    """未知站点桌面失败→移动成功的 organic 发现要写入记忆。"""
    monkeypatch.setenv("ARGO_FETCH_MD_VARIANT", "0")
    monkeypatch.setattr(fetch_v3, "_http_fetch", _fail_http)
    monkeypatch.setattr(fetch_v3, "_mobile_http_fetch",
                        lambda url, max_chars=8000, timeout=8.0:
                        _ok_result("http_mobile"))

    def _no_tls(*a, **k):
        raise AssertionError("移动已胜出不应升级 TLS")

    monkeypatch.setattr(fetch_v3, "_tls_spoof_fetch", _no_tls)
    out = fetch_v3.fetch_v3("https://organic.example.com/page/1",
                            skip_cache=True, use_browser_fallback=False)
    assert out["fetch_method"] == "http_mobile"
    assert fetch_v3._identity_is_mobile("organic.example.com")


def test_identity_persist_and_expiry(tmp_path, monkeypatch):
    p = tmp_path / "id.json"
    monkeypatch.setattr(fetch_v3, "_IDENTITY_PATH", str(p))
    monkeypatch.setattr(fetch_v3, "_identity_loaded", False)
    monkeypatch.setattr(fetch_v3, "_identity_mem", {})
    fetch_v3._identity_remember_mobile("a.example.com")
    assert p.exists()
    # 重载后仍命中
    monkeypatch.setattr(fetch_v3, "_identity_loaded", False)
    assert fetch_v3._identity_is_mobile("a.example.com")
    # 过期条目被过滤
    p.write_text('{"b.example.com": 1}')
    monkeypatch.setattr(fetch_v3, "_identity_loaded", False)
    assert not fetch_v3._identity_is_mobile("b.example.com")

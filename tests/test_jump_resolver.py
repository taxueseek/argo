#!/usr/bin/env python3
"""tests/test_jump_resolver.py — 跳转壳解析单元测试（2026-10-06）。

守两类落点提取与批量 API 契约：
  - 30x Location（baidu /link 形态）
  - 200 短页 JS 落点（搜狗对 Chrome 指纹的形态：234 字节
    window.location.replace 页；无会话时弹回首页必须不救回）
  - 批量：去重、cap、acceptable 复核、单条失败不拖累其余
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import jump_resolver as jr  # noqa: E402


# ── JS 落点提取 ────────────────────────────────────────────────────────────────

def test_extract_js_target_location_replace():
    body = '<script>window.location.replace("https://www.sohu.com/a/839781067_120411467")</script>'
    assert jr._extract_js_target(body) == "https://www.sohu.com/a/839781067_120411467"


def test_extract_js_target_href_and_meta_refresh():
    assert jr._extract_js_target('location.href="https://a.example.com/x"') \
        == "https://a.example.com/x"
    assert jr._extract_js_target(
        '<meta http-equiv="refresh" content="0;url=https://b.example.com/y">') \
        == "https://b.example.com/y"


def test_extract_js_target_unescapes_html_entities():
    body = 'window.location.replace("https://c.example.com/p?a=1&amp;b=2")'
    assert jr._extract_js_target(body) == "https://c.example.com/p?a=1&b=2"


def test_extract_js_target_no_match():
    assert jr._extract_js_target("<html><body>正文页</body></html>") == ""


# ── 落点可用性 ─────────────────────────────────────────────────────────────────

def test_same_origin_bounce_not_usable():
    """搜狗无会话时 302 弹回首页（Location: /）——不许把首页当落点。"""
    src = "https://www.sogou.com/link?url=abc"
    assert not jr._is_usable_target("https://www.sogou.com/", src)
    assert not jr._is_usable_target("/", src)
    assert jr._is_usable_target("https://www.sohu.com/a/1", src)


# ── 单条解析（HTTP 层打桩）───────────────────────────────────────────────────

def test_resolve_single_302(monkeypatch):
    def fake_fetch(url, timeout, profiles):
        return 302, {"Location": "https://real.example.com/a"}, ""
    monkeypatch.setattr(jr, "_fetch_no_redirect", fake_fetch)
    assert jr.resolve_jump_url("https://www.sogou.com/link?url=x") \
        == "https://real.example.com/a"


def test_resolve_single_200_js_page(monkeypatch):
    def fake_fetch(url, timeout, profiles):
        return 200, {}, 'window.location.replace("https://real.example.com/b")'
    monkeypatch.setattr(jr, "_fetch_no_redirect", fake_fetch)
    assert jr.resolve_jump_url("https://www.sogou.com/link?url=y") \
        == "https://real.example.com/b"


def test_resolve_single_failure_returns_empty(monkeypatch):
    def boom(url, timeout, profiles):
        raise OSError("network down")
    monkeypatch.setattr(jr, "_fetch_no_redirect", boom)
    assert jr.resolve_jump_url("https://www.sogou.com/link?url=z") == ""


# ── 批量解析 ──────────────────────────────────────────────────────────────────

def test_batch_resolves_and_dedups(monkeypatch):
    def fake_core(url, timeout, profiles):
        return url.replace("sogou.com/link?url=", "real.example.com/i=")
    monkeypatch.setattr(jr, "_resolve_core", fake_core)
    out = jr.resolve_jump_urls([
        "https://www.sogou.com/link?url=a",
        "https://www.sogou.com/link?url=a",  # 重复：只解析一次
        "https://www.sogou.com/link?url=b",
    ])
    assert out == {
        "https://www.sogou.com/link?url=a": "https://www.real.example.com/i=a",
        "https://www.sogou.com/link?url=b": "https://www.real.example.com/i=b",
    }


def test_batch_failures_isolated(monkeypatch):
    def flaky(url, timeout, profiles):
        if url.endswith("bad"):
            return ""
        return "https://real.example.com/ok"
    monkeypatch.setattr(jr, "_resolve_core", flaky)
    out = jr.resolve_jump_urls(["https://s.example.com/bad",
                                "https://s.example.com/good"])
    assert out == {"https://s.example.com/good": "https://real.example.com/ok"}


def test_batch_acceptable_callback_filters(monkeypatch):
    monkeypatch.setattr(jr, "_resolve_core",
                        lambda u, t, p: "https://www.sogou.com/web?x=1")
    out = jr.resolve_jump_urls(["https://www.sogou.com/link?url=a"],
                               acceptable=lambda t: "sogou.com" not in t)
    assert out == {}


def test_batch_cap_bounded(monkeypatch):
    seen = []
    def rec(url, timeout, profiles):
        seen.append(url)
        return "https://real.example.com/x"
    monkeypatch.setattr(jr, "_resolve_core", rec)
    jr.resolve_jump_urls([f"https://s.example.com/{i}" for i in range(50)])
    assert len(seen) == jr._BATCH_CAP

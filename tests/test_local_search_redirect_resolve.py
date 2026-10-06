#!/usr/bin/env python3
"""tests/test_local_search_redirect_resolve.py — sub-skill 跳转链落点解析接线回归。

守 resolve_redirects 契约在 local-search 子技能侧的接线（2026-10-06）：
  1. 声明 resolve_redirects 的引擎（local_sogou）：/link 跳转壳解析成
     真实正文 URL——不解析会在主链 serp_guard 整批被杀（中文链
     returned 10 → kept 1 的根因）。
  2. 解析失败的跳转壳按不可核验信源丢弃（engines_base 同款语义）。
  3. 非跳转链结果原样保留；解析器不可用时 fail-open 整段跳过。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "sub-skills" / "local-search", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import search_v3  # noqa: E402

_SOGOU_SPEC = {
    "type": "http", "enabled": True, "url": "https://www.sogou.com/web",
    "query_param": "query", "method": "GET", "timeout": 8, "format": "html",
    "resolve_redirects": True,
}

_SOGOU_HTML = """
<div class="vrwrap">
  <h3><a href="/link?url=AAA">可解析的结果</a></h3>
  <p class="str-text">摘要一</p>
</div>
<div class="vrwrap">
  <h3><a href="/link?url=BBB">解析失败的结果</a></h3>
  <p class="str-text">摘要二</p>
</div>
<div class="vrwrap">
  <h3><a href="https://direct.example.com/real">直链结果</a></h3>
  <p class="str-text">摘要三</p>
</div>
"""


def _run_search_one(monkeypatch, resolved_map):
    """打桩 _fetch（吃掉网络）与 resolve_jump_urls（吃掉解析网络）。"""
    monkeypatch.setattr(search_v3, "_fetch",
                        lambda *a, **k: _SOGOU_HTML)
    monkeypatch.setattr("jump_resolver.resolve_jump_urls",
                        lambda urls, **k: resolved_map)
    res, err = search_v3._search_one("local_sogou", "测试", n=5, timeout=8)
    return res, err


def test_jump_links_resolved_to_real_urls(monkeypatch):
    res, err = _run_search_one(monkeypatch, {
        "https://www.sogou.com/link?url=AAA": "https://www.sohu.com/a/1"})
    assert err == ""
    urls = [r["url"] for r in res]
    assert "https://www.sohu.com/a/1" in urls
    assert not any("sogou.com/link" in u for u in urls if "direct" not in u)


def test_unresolvable_jump_links_dropped(monkeypatch):
    """解析失败的跳转壳不进结果（engines_base 同款语义）；直链保留。"""
    res, _ = _run_search_one(monkeypatch, {
        "https://www.sogou.com/link?url=AAA": "https://www.sohu.com/a/1"})
    titles = [r.get("title") or "" for r in res]
    assert "解析失败的结果" not in titles
    assert "直链结果" in titles


def test_fail_open_when_resolver_unavailable(monkeypatch, ):
    """jump_resolver 不可导入时维持原样返回（主链 serp_guard 仍兜底）。"""
    import builtins
    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name == "jump_resolver":
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.setattr(search_v3, "_fetch", lambda *a, **k: _SOGOU_HTML)
    res, err = search_v3._search_one("local_sogou", "测试", n=5, timeout=8)
    assert err == "" and len(res) == 3


def test_no_flag_no_resolution(monkeypatch):
    """未声明 resolve_redirects 的引擎不触发解析（360 形态：跳转壳原样）。"""
    cfg = search_v3._load_config()
    spec = dict(cfg["engines"].get("local_sogou") or {})
    spec.pop("resolve_redirects", None)
    cfg2 = dict(cfg)
    cfg2["engines"] = dict(cfg["engines"])
    cfg2["engines"]["local_sogou"] = spec
    monkeypatch.setattr(search_v3, "_load_config", lambda: cfg2)
    monkeypatch.setattr(search_v3, "_fetch", lambda *a, **k: _SOGOU_HTML)
    calls = []

    import jump_resolver
    monkeypatch.setattr(jump_resolver, "resolve_jump_urls",
                        lambda urls, **k: calls.append(urls) or {})
    res, err = search_v3._search_one("local_sogou", "测试", n=5, timeout=8)
    assert err == "" and len(res) == 3
    assert calls == []

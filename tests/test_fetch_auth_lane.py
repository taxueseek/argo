#!/usr/bin/env python3
"""tests/test_fetch_auth_lane.py — fetch_v3 登录态车道接线

覆盖：站点有 profile → 强制浏览器 + 跳过缓存 + auth_profile 传入；
use_browser_fallback=False（批量爬取纪律）车道让路；_browser_fetch 的
登录态 provenance 标记 + assert_cacheable 拒绝入公共缓存。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def authed_env(tmp_path, monkeypatch):
    """隔离状态根 + 造一个已登录 profile；返回 profile 路径。"""
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ARGO_AUTH_FETCH", raising=False)
    monkeypatch.delenv("ARGO_FETCH_AUTH", raising=False)
    d = tmp_path / "state" / "browser-profiles" / "example.com"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"site": "example.com"}), encoding="utf-8")
    return str(d)


def _fake_browser_fetch(url, max_chars=8000, timeout=15.0, actions=None,
                        auth_profile=None):
    return {
        "url": url, "content": "body " * 10, "html": "", "title": "t",
        "length": 50, "success": True, "error": None,
        "fetch_method": "chrome_cdp",
        "_saw_auth_profile": auth_profile,
    }


def test_fetch_authed_site_forces_browser_and_skips_cache(authed_env, monkeypatch):
    import fetch_v3
    rec = MagicMock(side_effect=_fake_browser_fetch)
    monkeypatch.setattr(fetch_v3, "_browser_fetch", rec)
    cache_cls = MagicMock()
    with patch.dict(sys.modules, {"cache": MagicMock(SearchCache=cache_cls)}):
        out = fetch_v3.fetch_v3("https://www.example.com/page",
                                use_browser_fallback=True, deadline_s=1)
    rec.assert_called_once()
    kwargs = rec.call_args.kwargs
    assert kwargs.get("auth_profile") == authed_env, "登录态必须传到浏览器层"
    # 强制浏览器：匿名链（Wayback/tinyfish/jina…）不应被触碰 → 只此一次调用
    assert kwargs.get("actions") is None
    cache_cls.assert_not_called(), "登录态车道不得读写 URL 缓存"
    assert out.get("cached") is False


def test_fetch_batch_caller_overrides_auth_lane(authed_env, monkeypatch):
    """use_browser_fallback=False（crawl/evidence 纪律）→ 登录态车道让路。"""
    import fetch_v3
    rec = MagicMock(side_effect=_fake_browser_fetch)
    monkeypatch.setattr(fetch_v3, "_browser_fetch", rec)
    # .invalid 域 DNS 必败且无 SSRF 风险；整链快速失败，浏览器层不该被碰
    out = fetch_v3.fetch_v3("https://example.invalid/page",
                            use_browser_fallback=False, deadline_s=1)
    rec.assert_not_called()
    assert out.get("success") is not True


def test_browser_fetch_marks_login_provenance(authed_env):
    """拆出后的 fetch_browser.browser_fetch：带 profile → 登录态标记。"""
    from fetch_browser import browser_fetch
    fake_cdp = MagicMock()
    fake_cdp.get_html.return_value = "<html>x</html>"
    fake_cdp.get_text.return_value = "hello world"
    fake_cdp.get_title.return_value = "T"
    with patch("chrome_cdp.ChromeCDP") as cls:
        cls.return_value = fake_cdp
        out = browser_fetch("https://example.com/x", auth_profile=authed_env)
    cls.assert_called_once_with(auto_start=True, user_data_dir=authed_env)
    assert out["login_state_used"] is True
    assert out["cache_eligible"] is False
    fake_cdp.stop.assert_called_once()


def test_fetch_v3_alias_tracks_fetch_browser(authed_env, monkeypatch):
    """fetch_v3._browser_fetch 名字绑定指到拆出的实现（存量 monkeypatch 的锚点）。"""
    import fetch_v3
    assert fetch_v3._browser_fetch.__module__ == "fetch_browser"


def test_login_payload_rejected_by_cache_guard(authed_env):
    """cache.assert_cacheable 必须拒绝登录态载荷（防「标记接错字段」静默漏防）。"""
    from cache import assert_cacheable
    payload = {"url": "https://example.com/x", "content": "personal",
               "success": True, "login_state_used": True, "cache_eligible": False}
    with pytest.raises(Exception):
        assert_cacheable(payload, context="test")


def test_unauthed_browser_fetch_has_no_login_marker():
    """未认证浏览器抓取（既有行为）不得带登录态标记。"""
    from fetch_browser import browser_fetch
    fake_cdp = MagicMock()
    fake_cdp.get_html.return_value = "<html>x</html>"
    fake_cdp.get_text.return_value = "hello world"
    fake_cdp.get_title.return_value = "T"
    with patch("chrome_cdp.ChromeCDP") as cls:
        cls.return_value = fake_cdp
        out = browser_fetch("https://example.com/x")
    assert "login_state_used" not in out
    assert "cache_eligible" not in out

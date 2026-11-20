#!/usr/bin/env python3
"""tests/test_browser_auth.py — 持久 profile 登录态车道（A2）单元测试

覆盖：站点身份规范化（含穿越拒绝）、profile 查找、开关、login/status/logout
全流程（Chrome 进程打桩）、SingletonLock 探活。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from browser_auth import (  # noqa: E402
    auth_fetch_enabled,
    auth_profile_for_fetch,
    login,
    logout,
    profile_for,
    site_host,
    status,
)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state" / "browser-profiles"


def _make_profile(state_dir: Path, host: str = "example.com") -> Path:
    d = state_dir / host
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps({"site": host, "created": "2026-10-06T00:00:00"}), encoding="utf-8")
    return d


# ── 站点身份 ─────────────────────────────────────────────────────────────────

def test_site_host_normalization():
    assert site_host("https://www.Example.com/a?b=1#x") == "example.com"
    assert site_host("http://example.com:8080/p") == "example.com"
    assert site_host("example.com") == "example.com"
    assert site_host("localhost") == "localhost"


def test_site_host_rejects_traversal_and_garbage():
    for bad in ("..", ".", ".com", "", "not a site", "a b.com", "x/y/../z"):
        with pytest.raises(ValueError):
            site_host(bad)


# ── profile 查找 ─────────────────────────────────────────────────────────────

def test_profile_for_requires_meta_marker(state_dir):
    # 裸目录（上次登录失败的残骸）不算登录态
    (state_dir / "example.com").mkdir(parents=True)
    assert profile_for("https://example.com/x") is None
    _make_profile(state_dir)
    got = profile_for("https://www.example.com/a")
    assert got and got.endswith("example.com")


def test_profile_for_unknown_site(state_dir):
    assert profile_for("https://other.com") is None


def test_fetch_profile_for_switch(state_dir, monkeypatch):
    _make_profile(state_dir)
    monkeypatch.delenv("ARGO_AUTH_FETCH", raising=False)
    monkeypatch.delenv("ARGO_FETCH_AUTH", raising=False)
    assert auth_profile_for_fetch("example.com")  # 默认开
    monkeypatch.setenv("ARGO_AUTH_FETCH", "0")
    assert auth_profile_for_fetch("example.com") is None  # 开关关 → None（不破默认路径）


def test_fetch_profile_lane_yields_to_batch_callers(state_dir):
    """use_browser_fallback=False 的调用方（批量爬取纪律）→ 车道让路。"""
    _make_profile(state_dir)
    assert auth_profile_for_fetch("example.com", allow_browser_lane=False) is None


def test_auth_fetch_enabled_alias(monkeypatch):
    monkeypatch.setenv("ARGO_FETCH_AUTH", "0")
    assert auth_fetch_enabled() is False


# ── login / status / logout 全流程（Chrome 打桩）────────────────────────────

def test_login_writes_meta_and_status_lists(state_dir):
    fake = MagicMock()
    with patch("chrome_cdp._ChromeProcess", return_value=fake) as cls:
        out = login("https://www.example.com/login", confirm=lambda: None)
    assert out["ok"] is True
    cls.assert_called_once()
    kwargs = cls.call_args.kwargs
    # 可见窗口 + argo 自持 profile + 打开目标站
    assert kwargs["headless"] is False
    assert kwargs["user_data_dir"].endswith("example.com")
    assert kwargs["start_url"] == "https://www.example.com/login"
    # fake.start() 成功后 profile 目录已建；meta 落盘
    assert (state_dir / "example.com" / "meta.json").is_file()
    # 打桩 Chrome 不真写 profile：meta 由 argo 写，目录存在即可查到
    assert profile_for("example.com") is not None
    listing = status()
    assert [p["site"] for p in listing] == ["example.com"]


def test_login_start_failure_cleans_fresh_profile(state_dir):
    fake = MagicMock()
    fake.start.side_effect = RuntimeError("Chrome CDP failed to start on port 1")
    with patch("chrome_cdp._ChromeProcess", return_value=fake):
        out = login("example.com", confirm=lambda: None)
    assert out["ok"] is False
    assert not (state_dir / "example.com").exists(), "新建 profile 启动即败不该留裸目录"


def test_logout_removes_and_refuses_live_chrome(state_dir):
    d = _make_profile(state_dir)
    assert logout("example.com")["ok"] is True
    assert not d.exists()

    # Chrome 活着（SingletonLock 指向活 PID）→ 拒绝删
    d2 = _make_profile(state_dir, "live.com")
    os.symlink(f"somehost-{os.getpid()}", d2 / "SingletonLock")
    out = logout("live.com")
    assert out["ok"] is False and "正在使用" in out["error"]
    assert d2.exists()

    # 残锁（PID 已死）→ 放行
    d3 = _make_profile(state_dir, "dead.com")
    os.symlink("somehost-4194304", d3 / "SingletonLock")
    assert logout("dead.com")["ok"] is True


def test_logout_missing_profile(state_dir):
    out = logout("never-logged-in.com")
    assert out["ok"] is False


# ── 四态防抖判别、活体校验、评论封顶、CDP 捕获与诚实错误边界 ────────────────

def test_detect_page_auth_state_all_branches():
    import engine_states as es
    from browser_auth import detect_page_auth_state

    assert detect_page_auth_state("https://www.xiaohongshu.com/explore", "小红书", "正常内容") == es.PAGE_AUTH_READY
    assert detect_page_auth_state("https://www.xiaohongshu.com/explore", "小红书", "登录后查看全部内容") == es.PAGE_AUTH_LOGIN_WALL
    assert detect_page_auth_state(
        "https://www.xiaohongshu.com/search_result", "搜索", "请先登录", after_search=True,
    ) == es.PAGE_AUTH_SEARCH_RESTRICTED
    assert detect_page_auth_state("https://x.com/i/flow/login", "Login", "") == es.PAGE_AUTH_SIGNED_OUT
    assert detect_page_auth_state("https://x.com/explore", "安全验证", "请完成验证") == es.PAGE_AUTH_SECURITY_VERIFY


def test_wait_for_stable_page_auth_state_debounce():
    import engine_states as es
    from browser_auth import wait_for_stable_page_auth_state

    seq = iter([
        es.PAGE_AUTH_LOGIN_WALL,
        es.PAGE_AUTH_READY,
        es.PAGE_AUTH_READY,
        es.PAGE_AUTH_READY,
    ])
    got = wait_for_stable_page_auth_state(
        lambda: next(seq), settle_s=0, interval_s=0, consecutive=3, max_samples=7,
    )
    assert got == es.PAGE_AUTH_READY


def test_verify_live_session_and_missing_profile(state_dir):
    import engine_states as es
    from browser_auth import verify_live_session

    miss = verify_live_session("example.com", settle_s=0, interval_s=0)
    assert miss["ok"] is False and miss["state"] == es.PAGE_AUTH_SIGNED_OUT
    assert miss["retryable"] is False and miss["handoff_required"] is True

    _make_profile(state_dir, "x.com")
    fake_cdp = MagicMock()
    fake_cdp.get_url.return_value = "https://x.com/home"
    fake_cdp.get_title.return_value = "Home / X"
    fake_cdp.get_text.return_value = "timeline posts"
    ok_res = verify_live_session("x.com", cdp_factory=lambda: fake_cdp, settle_s=0, interval_s=0)
    assert ok_res["ok"] is True and ok_res["state"] == es.PAGE_AUTH_READY
    fake_cdp.stop.assert_called_once()


def test_bound_comments_caps_items_and_length():
    from browser_auth import bound_comments

    raw = [{"id": i, "text": "x" * 900} for i in range(35)]
    out = bound_comments(raw, max_items=30, max_chars=800)
    assert out["total"] == 35
    assert len(out["items"]) == 30
    assert out["truncated"] is True
    assert len(out["items"][0]["text"]) == 801


def test_cdp_network_capture_collects_json():
    from chrome_cdp import ChromeCDP

    cdp = ChromeCDP(auto_start=False)
    cdp._session = MagicMock()
    cdp._session.send.return_value = {"body": '{"items":[1,2]}', "base64Encoded": False}
    cdp.start_network_capture(["SearchTimeline"])
    cdp._on_event("Network.responseReceived", {
        "requestId": "req-1",
        "response": {"url": "https://x.com/i/api/graphql/abc/SearchTimeline", "status": 200, "mimeType": "application/json"},
    })
    cdp._on_event("Network.loadingFinished", {"requestId": "req-1"})
    captured = cdp.collect_captured_json()
    assert len(captured) == 1
    assert captured[0]["data"] == {"items": [1, 2]}


def test_fetch_browser_honest_error_on_login_wall(tmp_path):
    import engine_states as es
    from fetch_browser import browser_fetch

    fake_cdp = MagicMock()
    fake_cdp.get_url.return_value = "https://www.xiaohongshu.com/explore/123"
    fake_cdp.get_title.return_value = "小红书"
    fake_cdp.get_text.return_value = "登录后查看全部笔记"
    fake_cdp.get_html.return_value = "<div>登录后查看</div>"

    with patch("chrome_cdp.ChromeCDP", return_value=fake_cdp):
        res = browser_fetch("https://www.xiaohongshu.com/explore/123", auth_profile=str(tmp_path))
    assert res["success"] is False
    assert res["auth_state"] == es.PAGE_AUTH_LOGIN_WALL
    assert res["retryable"] is False
    assert res["handoff_required"] is True
    assert res["cache_eligible"] is False

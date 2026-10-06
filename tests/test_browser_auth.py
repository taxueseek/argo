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

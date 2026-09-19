#!/usr/bin/env python3
"""tests/test_cache_login_isolation.py — 登录态载荷不得进入公共 SearchCache"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from cache import (  # noqa: E402
    LoginCacheRejected,
    SearchCache,
    assert_cacheable,
    is_login_partition_payload,
)


@pytest.fixture
def cache(tmp_path):
    return SearchCache(db_path=str(tmp_path / "c.db"))


def test_is_login_partition_by_flags():
    assert is_login_partition_payload({"login_state_used": True})
    assert is_login_partition_payload({"cache_eligible": False})
    assert is_login_partition_payload({"auth_partition": "login"})
    assert is_login_partition_payload({"auth_partition": "login:zhihu.com"})
    assert is_login_partition_payload({"source": "ego-browser"})
    assert is_login_partition_payload({"engine": "ego_browser_bing"})
    assert not is_login_partition_payload({"source": "local_bing", "engine": "bing"})
    assert not is_login_partition_payload({"results": []})


def test_assert_cacheable_raises():
    with pytest.raises(LoginCacheRejected):
        assert_cacheable({"login_state_used": True})
    with pytest.raises(LoginCacheRejected):
        assert_cacheable({"cache_eligible": False})
    # 公共结果放行
    assert_cacheable({"results": [{"url": "https://a.com"}], "source": "bing"})


def test_set_rejects_login_payload(cache):
    with pytest.raises(LoginCacheRejected):
        cache.set(
            "q", "ego_browser_bing", 8,
            {
                "results": [{"title": "t", "url": "https://a.com"}],
                "login_state_used": True,
                "cache_eligible": False,
            },
        )
    assert cache.get("q", "ego_browser_bing", 8) is None


def test_set_rejects_ego_engine_name(cache):
    with pytest.raises(LoginCacheRejected):
        cache.set(
            "q", "ego_browser_baidu", 5,
            {"results": [{"title": "t", "url": "https://a.com"}]},
        )


def test_set_fetch_rejects_login_body(cache):
    with pytest.raises(LoginCacheRejected):
        cache.set_fetch(
            "https://zhihu.com/p/1",
            {
                "content": "private body",
                "login_state_used": True,
                "cache_eligible": False,
                "source": "ego-browser",
            },
        )
    assert cache.get_fetch("https://zhihu.com/p/1") is None


def test_set_fetch_allows_public_body(cache):
    cache.set_fetch(
        "https://example.com/public",
        {"content": "hello", "source": "http"},
    )
    hit = cache.get_fetch("https://example.com/public")
    assert hit is not None
    assert hit.get("content") == "hello"


def test_set_allows_public_combo(cache):
    payload = {
        "results": [{"title": "t", "url": "https://a.com", "snippet": "s"}],
        "source": "local_bing",
    }
    cache.set("public query", "local_bing", 8, payload)
    hit = cache.get("public query", "local_bing", 8)
    assert hit is not None
    assert hit.get("results")


def test_set_engine_rejects_ego_items(cache):
    with pytest.raises(LoginCacheRejected):
        cache.set_engine(
            "q", "bing", 5,
            [{"title": "t", "url": "https://a.com", "source": "ego-browser"}],
        )


# ── 逐条守卫：登录态条目不得从任何位置溜进公共缓存（2026-09-19 修复）──────
# 守卫此前只覆盖顶层载荷，两条真实缺口：
#   - SearchCache.set（combo，主写入路径）对 results[] **一条都不查**——实测把
#     login_state_used: True 放在结果列表第 1 条，照样写进公共库；
#   - set_engine 只查 results[:3]，第 4 条起不查。
# 逐条标记的生产者是存在的（candidate_envelope / plan 会在逐条结果上写
# login_state_used / auth_partition / cache_eligible），所以「硬拒绝」不能抽样。

def _items(n, login_at=None, field="login_state_used", value=True):
    """n 条干净结果；login_at 指定哪一条带登录态标记（None = 全干净）。"""
    out = [{"title": f"T{i}", "url": f"https://e/{i}"} for i in range(n)]
    if login_at is not None:
        out[login_at] = {**out[login_at], field: value}
    return out


@pytest.mark.parametrize("pos", [0, 3, 5])
def test_set_rejects_login_item_at_any_position(cache, pos):
    with pytest.raises(LoginCacheRejected):
        cache.set("q", "auto", 10, {"results": _items(6, pos)},
                  domain="general", mode="auto", depth="fast")


@pytest.mark.parametrize("pos", [0, 3, 5])
def test_set_engine_rejects_login_item_at_any_position(cache, pos):
    with pytest.raises(LoginCacheRejected):
        cache.set_engine("q", "octen", 10, _items(6, pos), domain="general")


def test_set_engine_rejects_auth_partition_beyond_first_three(cache):
    """auth_partition 是同一族标记，此前同样只查前三条。"""
    with pytest.raises(LoginCacheRejected):
        cache.set_engine("q", "octen", 10,
                         _items(6, 4, field="auth_partition",
                                value="login:zhihu.com"), domain="general")


def test_public_items_still_writable(cache):
    """对照面：逐条检查不得误拦正常结果。"""
    cache.set("q", "auto", 10,
              {"results": _items(6)}, domain="general",
              mode="auto", depth="fast")
    cache.set_engine("q2", "octen", 10, _items(6), domain="general")
    assert cache.get("q", "auto", 10, domain="general") is not None

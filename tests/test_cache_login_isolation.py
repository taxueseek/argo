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

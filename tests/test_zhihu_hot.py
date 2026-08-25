#!/usr/bin/env python3
"""tests/test_zhihu_hot.py — 知乎热榜（原 skill hot_list 迁入）"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_zhihu_hot_in_config():
    from config import load_config, get_engines

    engines = get_engines(load_config())
    assert "zhihu_hot" in engines
    spec = engines["zhihu_hot"]
    assert spec.get("enabled") is True
    assert "hot_list" in (spec.get("url") or "")
    assert spec.get("query_param") == ""
    assert str(spec.get("extra_params", {}).get("Limit", "")).find("n") >= 0
    assert int(spec.get("limit") or 0) == 100
    assert (spec.get("period") or "") == "day"


def test_route_zhihu_hot_list():
    from route import route_query
    from unittest.mock import patch

    # 路由契约测试不依赖真实密钥：注入 fake 使其通过 env_ready 门禁
    with patch.dict(
        os.environ, {"ARGO_ZHIHU_ACCESS_SECRET": "test-key-for-routing"},
        clear=False,
    ):
        r = route_query("知乎热榜", depth="fast", mode="fast")
    assert r.get("domain") == "zhihu_hot_list", r
    combo = r.get("engines_combo") or []
    assert "zhihu_hot" in combo
    assert combo[0] == "zhihu_hot"


def test_route_generic_hot_does_not_force_zhihu_first():
    """泛热搜仍以免费榜为主，避免日 100 次配额被刷光。"""
    from route import route_query

    r = route_query("今天热搜", depth="fast", mode="fast")
    assert r.get("domain") == "hot_trending", r
    combo = r.get("engines_combo") or []
    # fast 预算 2，zhihu_hot 排在末位，通常不进 combo
    assert combo[0] != "zhihu_hot"


def test_env_alias_shared_with_zhihu():
    from engine_env import required_env_for, KNOWN_ENV_ALIASES

    assert "zhihu_hot" in KNOWN_ENV_ALIASES
    req = required_env_for("zhihu_hot")
    assert any("ZHIHU" in x for x in req)


def test_live_zhihu_hot_if_key():
    """有密钥时打一次 hot_list，确认字段可解析。"""
    if not (os.environ.get("ZHIHU_ACCESS_SECRET") or os.environ.get("ARGO_ZHIHU_ACCESS_SECRET")):
        return
    from search import super_search

    out = super_search("知乎热榜", engine="zhihu_hot", n=5, skip_cache=True, mode="auto")
    results = out.get("results") or []
    assert len(results) >= 1, out
    assert results[0].get("title")
    assert "zhihu.com" in (results[0].get("url") or "") or results[0].get("url")

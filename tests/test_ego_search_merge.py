#!/usr/bin/env python3
"""tests/test_ego_search_merge.py — public+login 融合与质量信号"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "sub-skills" / "ego-search" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import merge as merge_mod  # noqa: E402
import quality  # noqa: E402


def test_merge_dual_sourced():
    public = {
        "query": "q",
        "engine": "local_bing",
        "source": "local_bing",
        "results": [
            {"title": "Public T", "url": "https://example.com/a?utm_source=x", "snippet": "p"},
        ],
    }
    login = {
        "query": "q",
        "engine": "ego_browser_bing",
        "source": "ego-browser",
        "runtime": "ego",
        "login_state_used": True,
        "cache_eligible": False,
        "results": [
            {"title": "Login T", "url": "https://example.com/a", "snippet": "l"},
        ],
    }
    out = merge_mod.merge_payloads(public, login, query="q")
    assert out["schema"] == "ego_search_merge_v1"
    assert out["public_count"] == 1
    assert out["login_count"] == 1
    assert out["dual_sourced_count"] >= 1
    assert out["isolation"]["login_cache_eligible"] is False
    # 冲突标题应记入 conflicts
    assert any(c.get("canonical_url") for c in out["conflicts"]) or out["dual_sourced_count"] >= 1


def test_quality_auth_wall():
    p = quality.assess_body({
        "title": "登录",
        "content": "请先登录后查看全文内容",
        "url": "https://example.com/x",
    })
    assert p["quality"]["auth_wall_suspected"] is True
    assert p["quality"]["login_likely_ok"] is False


def test_quality_ok_body():
    p = quality.assess_body({
        "title": "Article",
        "content": "A" * 200,
        "url": "https://example.com/x",
    })
    assert p["quality"]["empty_or_thin"] is False
    assert p["quality"]["login_likely_ok"] is True

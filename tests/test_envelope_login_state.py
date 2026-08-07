#!/usr/bin/env python3
"""tests/test_envelope_login_state.py — envelope 识别 ego / 登录态 provenance"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_envelope import attach_envelope, result_to_candidate  # noqa: E402


def test_result_to_candidate_ego_source():
    c = result_to_candidate(
        {
            "title": "t",
            "url": "https://www.zhihu.com/question/1",
            "snippet": "s",
            "source": "ego-browser",
            "login_state_used": True,
            "cache_eligible": False,
        },
        query="q",
        rank=1,
    )
    assert c["access"]["login_state_used"] is True
    assert c["access"]["visibility"] == "authenticated"
    assert any("login_state" in x for x in c["limitations"])


def test_result_to_candidate_public():
    c = result_to_candidate(
        {"title": "t", "url": "https://example.com", "snippet": "s", "source": "bing"},
        query="q",
        rank=1,
    )
    assert c["access"]["login_state_used"] is False
    assert c["access"]["visibility"] == "public"


def test_attach_envelope_route_login_flag():
    raw = {
        "query": "q",
        "engine": "ego_browser_bing",
        "source": "ego-browser",
        "login_state_used": True,
        "cache_eligible": False,
        "results": [
            {
                "title": "t",
                "url": "https://a.com",
                "snippet": "s",
                "source": "ego_browser_bing",
            },
        ],
        "count": 1,
    }
    out = attach_envelope(raw, query="q")
    assert out["routes"][0]["login_state_used"] is True
    assert any("SearchCache" in x or "login_state" in x for x in out["limitations"])
    assert out["candidates"][0]["access"]["login_state_used"] is True

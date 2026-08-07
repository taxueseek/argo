#!/usr/bin/env python3
"""tests/test_ego_search_runtime.py — 双运行时探测 + 融合字段 + webbridge 适配单元"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "sub-skills" / "ego-search" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import ego_search  # noqa: E402
import runtime as rt  # noqa: E402
import webbridge_adapter as wb  # noqa: E402


def test_stamp_has_fusion_fields():
    out = ego_search.stamp_login_provenance({"query": "q"}, runtime="ego")
    assert out["login_state_used"] is True
    assert out["cache_eligible"] is False
    assert out["search_partition"] == "login"
    assert out["merge_with_public_ok"] is True
    assert out["runtime"] == "ego"


def test_stamp_webbridge_source():
    out = ego_search.stamp_login_provenance({}, runtime="webbridge")
    assert out["source"] == "webbridge"
    assert out["runtime"] == "webbridge"


def test_detect_runtimes_structure(monkeypatch):
    monkeypatch.setattr(rt, "ego_available", lambda: {"available": True, "bin": "ego-browser"})
    monkeypatch.setattr(
        rt, "webbridge_available",
        lambda try_start=False: {"available": False, "url": "x", "error": "down"},
    )
    info = rt.detect_runtimes()
    assert info["preferred"] == "ego"
    assert info["login_search_ready"] is True


def test_resolve_prefers_webbridge_when_no_ego(monkeypatch):
    monkeypatch.setattr(rt, "ego_available", lambda: {"available": False})
    monkeypatch.setattr(
        rt, "webbridge_available",
        lambda try_start=False: {"available": True},
    )
    assert rt.resolve_runtime("auto") == "webbridge"
    assert rt.resolve_runtime("ego") == "none"


def test_search_routes_to_webbridge(monkeypatch, capsys):
    monkeypatch.setattr(rt, "resolve_runtime", lambda explicit=None: "webbridge")
    monkeypatch.setattr(
        wb, "search",
        lambda *a, **k: {
            "ok": True,
            "payload": {
                "query": "q",
                "engine": "webbridge_bing",
                "source": "webbridge",
                "results": [{"title": "t", "url": "https://a.com", "snippet": "s"}],
                "count": 1,
                "fetch_method": "browser",
            },
        },
    )
    args = SimpleNamespace(
        query="q", engine="bing", n=8, task_space="t",
        timeout=30, runtime="webbridge",
    )
    ego_search.cmd_search(args)
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "webbridge"
    assert out["runtime"] == "webbridge"
    assert out["cache_eligible"] is False
    assert out["merge_with_public_ok"] is True


def test_search_ego_fallback_to_webbridge(monkeypatch, capsys):
    monkeypatch.setattr(rt, "resolve_runtime", lambda explicit=None: "ego")
    monkeypatch.setattr(
        ego_search, "run_ego",
        lambda *a, **k: {"ok": False, "error": "ego_down"},
    )
    monkeypatch.setattr(
        rt, "webbridge_available",
        lambda try_start=False: {"available": True},
    )
    monkeypatch.setattr(
        wb, "search",
        lambda *a, **k: {
            "ok": True,
            "payload": {
                "query": "q",
                "engine": "webbridge_bing",
                "source": "webbridge",
                "results": [],
                "count": 0,
                "fetch_method": "browser",
            },
        },
    )
    args = SimpleNamespace(
        query="q", engine="bing", n=8, task_space="t",
        timeout=30, runtime="auto",
    )
    ego_search.cmd_search(args)
    out = json.loads(capsys.readouterr().out)
    assert out["runtime"] == "webbridge"


def test_status_includes_architecture(monkeypatch, capsys, tmp_path):
    ego_search.STATE_PATH = str(tmp_path / "pro-mode.json")
    monkeypatch.setattr(
        rt, "detect_runtimes",
        lambda try_start_webbridge=False: {
            "ego": {"available": True},
            "webbridge": {"available": True},
            "preferred": "ego",
            "login_search_ready": True,
            "install_hints": [],
        },
    )
    ego_search.cmd_status(SimpleNamespace(fix=False))
    out = json.loads(capsys.readouterr().out)
    assert out["architecture"] == "dual_runtime_complete"
    assert out["isolation"]["merge_at_analysis"] is True
    assert out["runtimes"]["preferred"] == "ego"

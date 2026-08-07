#!/usr/bin/env python3
"""tests/test_ego_search_safety.py — SSRF / URL 守卫 + 降级单入口"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "sub-skills" / "ego-search" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import ego_search  # noqa: E402
import safety  # noqa: E402


def test_blocks_file_scheme():
    v = safety.validate_browser_url("file:///etc/passwd", context="t")
    assert v["ok"] is False
    assert "scheme" in v["error"]


def test_blocks_localhost_by_default():
    v = safety.validate_browser_url("http://127.0.0.1:8080/secret", context="t")
    assert v["ok"] is False


def test_allows_https_public():
    v = safety.validate_browser_url("https://example.com/a", context="t")
    assert v["ok"] is True


def test_run_with_fallback_ego_ok(monkeypatch):
    monkeypatch.setattr(ego_search.rt, "resolve_runtime", lambda e=None: "ego")
    args = SimpleNamespace(runtime="auto")
    r, rt_name = ego_search.run_with_fallback(
        args,
        ego_fn=lambda: {"ok": True, "payload": {"x": 1}},
        wb_fn=lambda: {"ok": True, "payload": {"x": 2}},
        label="t",
    )
    assert rt_name == "ego"
    assert r["payload"]["x"] == 1


def test_run_with_fallback_degrades(monkeypatch):
    monkeypatch.setattr(ego_search.rt, "resolve_runtime", lambda e=None: "ego")
    monkeypatch.setattr(
        ego_search.rt, "webbridge_available",
        lambda try_start=False: {"available": True},
    )
    args = SimpleNamespace(runtime="auto")
    r, rt_name = ego_search.run_with_fallback(
        args,
        ego_fn=lambda: {"ok": False, "error": "down"},
        wb_fn=lambda: {"ok": True, "payload": {"via": "wb"}},
        label="t",
    )
    assert rt_name == "webbridge"
    assert r["payload"]["via"] == "wb"

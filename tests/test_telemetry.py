#!/usr/bin/env python3
"""tests/test_telemetry.py — P2-6 遥测字段格式 + 采集点落数"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
EGO_SCRIPTS = SKILL_DIR / "sub-skills" / "ego-search" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(EGO_SCRIPTS))

import telemetry  # noqa: E402


@pytest.fixture
def tele_dir(tmp_path, monkeypatch):
    """把遥测目录注入 tmp，隔离真实 ~/.cache。"""
    d = tmp_path / "telemetry"
    monkeypatch.setenv("ARGO_TELEMETRY_DIR", str(d))
    monkeypatch.setenv("ARGO_TELEMETRY", "1")
    return d


def _read_lines(d: Path, stream: str) -> list[dict]:
    p = d / f"{stream}.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


# ── 格式单测 ──────────────────────────────────────────────────────────────────

def test_emit_writes_append_only_jsonl(tele_dir):
    assert telemetry.emit("recovery", {"query": "q1", "triggered": True})
    assert telemetry.emit("recovery", {"query": "q2", "recovered": True})
    assert telemetry.emit("merge", {"merged_count": 3})

    rows = _read_lines(tele_dir, "recovery")
    assert len(rows) == 2
    assert rows[0]["query"] == "q1"          # 顺序保持
    assert rows[1]["recovered"] is True
    for r in rows:
        assert {"ts", "stream", "version"} <= set(r)  # 必填外壳字段
        assert r["stream"] == "recovery"
        assert r["version"] == 1
        assert r["ts"]                          # 非空时间戳
    assert _read_lines(tele_dir, "merge")[0]["merged_count"] == 3


def test_emit_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGO_TELEMETRY_DIR", str(tmp_path / "telemetry"))
    monkeypatch.setenv("ARGO_TELEMETRY", "0")
    assert telemetry.emit("recovery", {"query": "x"}) is False
    assert not (tmp_path / "telemetry" / "recovery.jsonl").exists()


def test_emit_failure_silent(tmp_path, monkeypatch):
    blocker = tmp_path / "telemetry"
    blocker.write_text("not a dir")  # 用同名文件堵住 mkdir
    monkeypatch.setenv("ARGO_TELEMETRY_DIR", str(blocker))
    monkeypatch.setenv("ARGO_TELEMETRY", "1")
    assert telemetry.emit("recovery", {"query": "x"}) is False  # 不抛异常


def test_tail_reads_recent(tele_dir):
    for i in range(15):
        telemetry.emit("route", {"engine": f"e{i}"})
    rows = telemetry.tail("route", n=10)
    assert len(rows) == 10
    assert rows[0]["engine"] == "e5"
    assert rows[-1]["engine"] == "e14"
    assert telemetry.tail("no_such_stream") == []


# ── 采集点：route 采样 ────────────────────────────────────────────────────────

def test_route_sample_emits(tele_dir, monkeypatch):
    from route import _sample_route
    monkeypatch.setattr("route._ROUTE_SAMPLE_RATE", 1)
    monkeypatch.setattr("route._route_sample_counter", 0)

    _sample_route({}, {
        "domain": "academic",
        "engine": "arxiv",
        "engines": ["arxiv", "anysearch"],
        "confidence": 0.9,
        "mode": "auto",
        "features": {
            "lang_override": "en",
            "primary_lang": "zh",
            "script": "cjk",
            "has_compare": False,
            "has_technical": True,
            "chinese_ratio": 0.6,
            "intents": ["academic"],
        },
    })
    rows = _read_lines(tele_dir, "route")
    assert len(rows) == 1
    r = rows[0]
    assert r["domain"] == "academic"
    assert r["engine"] == "arxiv"
    assert r["lang_override"] == "en"
    assert r["primary_lang"] == "zh"
    assert r["chinese_ratio"] == 0.6


def test_route_sample_skips_without_features(tele_dir, monkeypatch):
    from route import _sample_route
    monkeypatch.setattr("route._ROUTE_SAMPLE_RATE", 1)
    monkeypatch.setattr("route._route_sample_counter", 0)

    _sample_route({}, {"engine": "custom"})  # engine_override 直通无 features
    assert not (tele_dir / "route.jsonl").exists()


# ── 采集点：merge 融合 ────────────────────────────────────────────────────────

def test_merge_payloads_emits(tele_dir):
    import merge as merge_mod  # noqa: E402

    public = {
        "query": "q", "engine": "local_bing", "source": "local_bing",
        "results": [
            {"title": "Public T", "url": "https://example.com/a?utm_source=x", "snippet": "p"},
        ],
    }
    login = {
        "query": "q", "engine": "ego_browser_bing", "source": "ego-browser",
        "runtime": "ego", "login_state_used": True, "cache_eligible": False,
        "results": [
            {"title": "Login T", "url": "https://example.com/a", "snippet": "l"},
        ],
    }
    out = merge_mod.merge_payloads(public, login, query="q")

    rows = _read_lines(tele_dir, "merge")
    assert len(rows) == 1
    r = rows[0]
    assert r["public_count"] == out["public_count"] == 1
    assert r["login_count"] == out["login_count"] == 1
    assert r["merged_count"] == out["merged_count"]
    assert r["dual_sourced_count"] == out["dual_sourced_count"] >= 1
    assert r["conflicts"] == len(out["conflicts"])
    assert r["public_engine"] == "local_bing"
    assert r["login_engine"] == "ego_browser_bing"

#!/usr/bin/env python3
"""tests/test_search_v3_perf_fixes.py — 方案A止血包（2026-09-29）。

守五个修复，全部对应 2026-09-29 审查实测：
  1. _apply_time_window 入口归一化日期（ddgs news 的 date 形如
     2026-09-28T15:30:11+00:00，裸串喂 _date_key 会 ValueError 并炸掉
     整个引擎的结果收集）
  2. CLI 引擎成功路径补时间窗后过滤（此前 until 100% 失效：实测
     until=2026-08-01 返回 9 条全部晚于上限）
  3. 慢失败（TimeoutExpired）不重试（此前单引擎最坏 8s×2+0.3s=16.3s，
     实测空结果查询 15.8s；慢是相关信号，重试只翻倍墙钟）
  4. engines_used 按路由序输出（此前按线程完成序，同输入两次运行
     JSON 顺序不同，破坏可复现性）
  5. 健康探针 CLI which 短路（缺失二进制不再 spawn --help 子进程）
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "sub-skills" / "local-search", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import search_v3  # noqa: E402
from local_health_check import _check_cli_engine  # noqa: E402


# ── 1. 时间窗日期归一化 ────────────────────────────────────────────────────────

def test_apply_time_window_accepts_iso_t_dates():
    """带时间的 ISO 串此前直接 ValueError（引擎结果全损），现在正常过滤。"""
    rs = [{"title": "a", "published_at": "2026-09-28T15:30:11+00:00"}]
    assert len(search_v3._apply_time_window(rs, "7d", None)) == 1


def test_apply_time_window_filters_old_iso_t_dates():
    rs = [{"title": "old", "published_at": "2020-01-01T00:00:00+00:00"}]
    assert search_v3._apply_time_window(rs, "7d", None) == []


def test_apply_time_window_drops_unparseable_dates():
    """解析失败的日期按无日期处理（剔除），与时间窗语义一致。"""
    assert search_v3._apply_time_window(
        [{"title": "x", "published_at": "3 hours ago"}], "7d", None) == []
    assert search_v3._apply_time_window(
        [{"title": "x", "published_at": None}], "7d", None) == []


# ── 2. CLI 成功路径的时间窗后过滤 ─────────────────────────────────────────────

def test_cli_success_path_applies_time_window(monkeypatch):
    """until 此前在 CLI 成功路径 100% 失效（结果直接 return，不经过滤）。"""
    captured = {}

    def fake_cli(spec, query, n, timeout, since=None, until=None):
        captured["since"], captured["until"] = since, until
        return [{"title": "news", "url": "https://n.example.com/1",
                 "published_at": "2026-09-28T15:30:11+00:00",
                 "source": "local_ddgs_news"}], ""

    monkeypatch.setattr(search_v3, "_run_cli_engine", fake_cli)
    res, err = search_v3._search_one("local_ddgs_news", "q", n=5,
                                     until="2026-08-01")
    assert captured["until"] == "2026-08-01"
    assert err == ""
    assert res == []  # 命中日期晚于 until 上限 → 全部过滤（此前会原样返回）


def test_cli_success_path_keeps_in_window_results(monkeypatch):
    def fake_cli(spec, query, n, timeout, since=None, until=None):
        return [{"title": "fresh", "url": "https://n.example.com/2",
                 "published_at": "2026-09-28T10:00:00+00:00",
                 "source": "local_ddgs_news"}], ""

    monkeypatch.setattr(search_v3, "_run_cli_engine", fake_cli)
    res, err = search_v3._search_one("local_ddgs_news", "q", n=5, since="7d")
    assert err == "" and len(res) == 1
    assert res[0]["_engine"] == "local_ddgs_news"


# ── 3. 慢失败不重试 ────────────────────────────────────────────────────────────

def test_timeout_does_not_retry(monkeypatch):
    """TimeoutExpired 只调用一次子进程（此前重试一次 = 2 次 = 墙钟翻倍）。"""
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd=a[0] if a else "ddgs", timeout=8)

    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda cmd: "/usr/bin/ddgs")
    monkeypatch.setattr(search_v3.subprocess, "run", fake_run)
    results, err = search_v3._run_cli_engine(
        {"cli_command": "ddgs", "cli_args": ["text", "-q", "{query}"],
         "key": "k"}, "q", 5, 8)
    assert calls["n"] == 1
    assert results == []
    assert "timed out" in err


def test_fast_failure_still_retries(monkeypatch):
    """快失败（rc=0 + 错误标记）保留重试——那才是真间歇。"""
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1

        class P:
            returncode = 0
            stdout = "DDGSException: DecodeError" if calls["n"] == 1 \
                else '[{"title": "ok", "href": "https://x.example.com/1"}]'
            stderr = ""

        return P()

    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda cmd: "/usr/bin/ddgs")
    monkeypatch.setattr(search_v3.subprocess, "run", fake_run)
    results, err = search_v3._run_cli_engine(
        {"cli_command": "ddgs", "cli_args": ["text", "-q", "{query}"],
         "key": "k"}, "q", 5, 8)
    assert calls["n"] == 2
    assert err == "" and len(results) == 1


# ── 4. engines_used 路由序 ────────────────────────────────────────────────────

def test_engines_used_follows_routed_order(monkeypatch):
    """先提交的引擎更慢时，engines_used 仍按路由序（此前按完成序会倒置）。"""

    def fake_search_one(name, query, n=5, timeout=None, since=None, until=None):
        if name == "local_bing":
            time.sleep(0.25)
            return [{"title": "b", "url": "https://b.example.com/1",
                     "score": 0.9}], ""
        return [{"title": "y", "url": "https://y.example.com/1",
                 "score": 0.8}], ""

    monkeypatch.setattr(search_v3, "_search_one", fake_search_one)
    out = search_v3.search_engines("q", engines=["local_bing", "local_brave"],
                                   n=3, skip_cache=True, mode="deep")
    assert out["engines_used"] == ["local_bing", "local_brave"]


# ── 5. 健康探针 which 短路 ────────────────────────────────────────────────────

def test_cli_health_probe_shortcircuits_on_missing_binary(monkeypatch):
    def _no_spawn(*a, **k):
        raise AssertionError("二进制缺失时不应 spawn --help 子进程")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    rep = _check_cli_engine("local_x", {"cli_command": "definitely_missing_xyz_987"},
                            timeout=5)
    assert rep["available"] is False
    assert rep["fail_reason"] == "cli_not_found"
    assert rep["latency_ms"] == 0

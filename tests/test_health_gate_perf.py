#!/usr/bin/env python3
"""tests/test_health_gate_perf.py — 健康门改造（2026-09-29 方案B）。

守五个修复，全部对应 2026-09-29 审查实测：
  1. 自适应 TTL：连续成功指数放宽探针间隔（×2^min(streak,3)，上限 60min），
     任何失败立即回到基准——探针税（0.3~8s/轮）与反爬 canary 流量降一个量级
  2. consecutive_ok 计数：成功递增、失败归零
  3. 陈旧条目清理：健康文件此前只增不减（实测 49 天旧条目 + 测试引擎名残留）
  4. 批量落盘：一轮 N 引擎 N 次全文件重写 → 1 次
  5. 探针出口走 net_proxy（代理环境此前所有 HTTP 引擎被判 unavailable）
"""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "sub-skills" / "local-search", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import local_health_check  # noqa: E402
from engine_registry import EngineRegistry  # noqa: E402
from local_health_check import _effective_ttl, _fetch_probe  # noqa: E402


# ── 1. 自适应 TTL ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("streak,base,expected", [
    (0, 300.0, 300.0),      # 无成功记录：基准
    (1, 300.0, 600.0),      # 1 次成功：×2
    (3, 300.0, 2400.0),     # 3 次：×8
    (10, 300.0, 2400.0),    # 封顶 ×8（300s 基准到不了 60min 上限）
    (10, 600.0, 3600.0),    # 600×8=4800 → 硬上限 3600
    (2, 300.0, 1200.0),
])
def test_effective_ttl_ladder(streak, base, expected):
    assert _effective_ttl({"consecutive_ok": streak}, base) == expected


def test_effective_ttl_tolerates_missing_or_garbage_streak():
    assert _effective_ttl({}, 300.0) == 300.0
    assert _effective_ttl({"consecutive_ok": None}, 300.0) == 300.0


def test_stale_but_within_adaptive_ttl_skips_probe(tmp_path, monkeypatch):
    """6 分钟前探过 + 连续成功 3 次（有效 TTL 40min）→ 不应重新探针。"""
    reg = EngineRegistry(health_state_path=tmp_path / "h.json")
    reg._health["local_bing"] = {
        "last_checked": time.time() - 360, "available": True,
        "consecutive_ok": 3,
    }

    def _no_probe(*a, **k):
        raise AssertionError("自适应 TTL 内不应触发重新探针")

    monkeypatch.setattr(local_health_check, "run_health_check", _no_probe)
    avail = local_health_check.get_available_engines(registry=reg, engine_names=["local_bing"])
    assert avail == ["local_bing"]


# ── 2. consecutive_ok 计数 ────────────────────────────────────────────────────

def test_consecutive_ok_increments_and_resets(tmp_path):
    reg = EngineRegistry(health_state_path=tmp_path / "h.json")
    reg.update_availability("e", True, persist=False)
    reg.update_availability("e", True, persist=False)
    assert reg.get_health("e")["consecutive_ok"] == 2
    reg.update_availability("e", False, persist=False, fail_reason="x")
    assert reg.get_health("e")["consecutive_ok"] == 0
    assert reg.get_health("e")["consecutive_failures"] == 1


# ── 3. 陈旧条目清理 ───────────────────────────────────────────────────────────

def test_stale_entries_dropped_on_load(tmp_path):
    h = tmp_path / "h.json"
    old = time.time() - 49 * 86400   # 实测同类残留：49 天
    fresh = time.time() - 60
    h.write_text(json.dumps({
        "local_ddgs_books": {"last_checked": old, "available": True},
        "local_test_engine": {"last_checked": old, "available": True},
        "local_bing": {"last_checked": fresh, "available": True},
    }, ensure_ascii=False), encoding="utf-8")
    reg = EngineRegistry(health_state_path=h)
    assert "local_ddgs_books" not in reg._health
    assert "local_test_engine" not in reg._health
    assert "local_bing" in reg._health


# ── 4. 批量落盘 ───────────────────────────────────────────────────────────────

def test_health_round_saves_once(tmp_path, monkeypatch):
    reg = EngineRegistry(health_state_path=tmp_path / "h.json")
    saves = {"n": 0}
    orig_save = EngineRegistry._save_health

    def counting_save(self):
        saves["n"] += 1
        orig_save(self)

    monkeypatch.setattr(EngineRegistry, "_save_health", counting_save)
    monkeypatch.setattr(local_health_check, "check_engine",
                        lambda name, reg, **k: {
                            "name": name, "available": True, "status": 200,
                            "latency_ms": 10, "parse_ok": True, "text_sample": ""})
    reports = local_health_check.run_health_check(
        registry=reg, engine_names=["e1", "e2", "e3"])
    assert len(reports) == 3
    assert saves["n"] == 1  # 此前每引擎一次 = 3 次


# ── 5. 探针出口走 net_proxy ───────────────────────────────────────────────────

def test_probe_routes_through_net_proxy(monkeypatch):
    calls = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html>ok</html>"

        def getcode(self):
            return 200

    def fake_open_url(req, timeout=None):
        calls["n"] += 1
        return _Resp()

    fake = types.ModuleType("net_proxy")
    fake.open_url = fake_open_url
    monkeypatch.setitem(sys.modules, "net_proxy", fake)
    status, latency, text, fail = _fetch_probe("http://example.com/probe", {})
    assert calls["n"] == 1
    assert status == 200 and text == "<html>ok</html>" and fail is None

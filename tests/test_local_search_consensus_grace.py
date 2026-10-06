#!/usr/bin/env python3
"""tests/test_local_search_consensus_grace.py — 够数早停的共识宽限语义（2026-10-06）。

守的行为：fast/auto 下已收结果 ≥ n 时，先等 _CONSENSUS_GRACE_S 收第二引擎
共识，宽限到点仍够数即放行——不再死等慢引擎（实测 pg vacuum：yandex 1.9s
给足 5 条，旧规则死等 yahoo 到 4s 才停）。同时锁三条红线：

  1. 宽限到点放行：慢引擎被取消，不拖墙钟，其结果不进 engines_used
  2. 共识优先：两个引擎都在宽限内完成时，两个都用（RRF 跨引擎共识不丢）
  3. deep/budget 不早停：配额/质量契约不变，收满才返回
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "sub-skills" / "local-search", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import search_v3  # noqa: E402


def _patch_search_one(monkeypatch, delays: dict[str, float], counts: dict[str, int]):
    """按引擎名注入延迟与结果条数。"""
    def fake(name, query, n=5, timeout=None, since=None, until=None):
        time.sleep(delays.get(name, 0))
        cnt = counts.get(name, 0)
        res = [{"title": f"{name} {i}", "url": f"https://x.example.com/{name}/{i}"}
               for i in range(cnt)]
        return res, None
    monkeypatch.setattr(search_v3, "_search_one", fake)


def test_grace_releases_after_slow_second_engine(monkeypatch):
    """快引擎 0.1s 给足 n 条，慢引擎 3s：宽限到点放行，不等慢引擎。"""
    _patch_search_one(monkeypatch,
                      delays={"fast": 0.1, "slow": 3.0},
                      counts={"fast": 5, "slow": 5})
    t0 = time.time()
    out = search_v3.search_engines(
        "测试", engines=["fast", "slow"], n=5, skip_cache=True, mode="auto")
    elapsed = time.time() - t0
    assert elapsed < 2.0, f"宽限未生效：实耗 {elapsed:.2f}s（在死等慢引擎）"
    assert "fast" in out["engines_used"]
    assert "slow" not in out["engines_used"]
    assert len(out["results"]) >= 5


def test_consensus_wins_when_second_engine_lands_in_grace(monkeypatch):
    """两引擎都在宽限内完成（0.1s / 0.5s）：都用上，共识不丢。"""
    _patch_search_one(monkeypatch,
                      delays={"fast": 0.1, "second": 0.5},
                      counts={"fast": 5, "second": 3})
    out = search_v3.search_engines(
        "测试", engines=["fast", "second"], n=5, skip_cache=True, mode="auto")
    assert set(out["engines_used"]) == {"fast", "second"}, out["engines_used"]


def test_no_early_stop_in_deep_mode(monkeypatch):
    """mode=deep：不早停，慢引擎结果也收（质量优先）。"""
    _patch_search_one(monkeypatch,
                      delays={"fast": 0.05, "slow": 1.2},
                      counts={"fast": 5, "slow": 5})
    t0 = time.time()
    out = search_v3.search_engines(
        "测试", engines=["fast", "slow"], n=5, skip_cache=True, mode="deep")
    elapsed = time.time() - t0
    assert elapsed >= 1.0, "deep 模式不应早停"
    assert set(out["engines_used"]) == {"fast", "slow"}


def test_abundant_first_engine_skips_grace(monkeypatch):
    """自适应免宽限（2026-10-06）：首引擎给足 2n 条时共识边际价值低于
    宽限墙钟，立即放行——3s 慢引擎不该被等哪怕 1s。"""
    _patch_search_one(monkeypatch,
                      delays={"rich": 0.05, "slow": 3.0},
                      counts={"rich": 12, "slow": 5})
    t0 = time.time()
    out = search_v3.search_engines(
        "测试", engines=["rich", "slow"], n=5, skip_cache=True, mode="auto")
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"2n 免宽限未生效：实耗 {elapsed:.2f}s"
    assert "rich" in out["engines_used"]
    assert "slow" not in out["engines_used"]

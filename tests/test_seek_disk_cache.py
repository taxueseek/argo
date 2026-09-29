#!/usr/bin/env python3
"""tests/test_seek_disk_cache.py — 跨进程 seek 落盘缓存（2026-09-29 H3）。

守四件事：
  1. put/get 往返一致（结果列表原样存取）
  2. TTL 过期即失效（300s，与进程内缓存同语义）
  3. 容量上限 64 + 最旧淘汰；损坏缓存文件整表重建不炸
  4. 端到端：内存缓存清空后第二次 _run_local_seek 走落盘层——seek 模块
     的 run_query 根本不被调用（patch 成炸弹来证明）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SEEK = ROOT / "sub-skills" / "local-seek" / "scripts" / "seek.py"
for p in (ROOT / "scripts",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import local_seek  # noqa: E402

SCRIPTS = ROOT / "scripts"

_HITS = [{"title": "/tmp/a.py", "url": "file:///tmp/a.py#1",
          "snippet": "x", "source": "local_files", "score": 0.9, "kind": "local"}]


@pytest.fixture()
def cache_path(tmp_path, monkeypatch):
    p = tmp_path / "local_seek_cache.json"
    monkeypatch.setattr(local_seek, "_seek_disk_cache_path", lambda: p)
    local_seek._LOCAL_SEEK_CACHE.clear()
    yield p
    local_seek._LOCAL_SEEK_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolate_disk_cache_for_e2e(tmp_path, monkeypatch):
    """端到端用例也要隔离落盘路径（其余用例用上面的 cache_path 显式覆盖）。"""
    monkeypatch.setattr(local_seek, "_seek_disk_cache_path",
                        lambda: tmp_path / "e2e_cache.json")


def test_put_get_roundtrip(cache_path):
    local_seek._seek_disk_cache_put("k1", _HITS)
    assert local_seek._seek_disk_cache_get("k1") == _HITS


def test_ttl_expiry(cache_path):
    local_seek._seek_disk_cache_put("k1", _HITS)
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    data["k1"]["ts"] = 0  # 伪造为远古写入
    cache_path.write_text(json.dumps(data), encoding="utf-8")
    assert local_seek._seek_disk_cache_get("k1") is None


def test_corrupt_file_rebuilt_on_put(cache_path):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{not json", encoding="utf-8")
    assert local_seek._seek_disk_cache_get("k1") is None  # 读坏不炸
    local_seek._seek_disk_cache_put("k1", _HITS)          # 写侧整表重建
    assert local_seek._seek_disk_cache_get("k1") == _HITS


def test_capacity_cap_evicts_oldest(cache_path):
    for i in range(66):
        local_seek._seek_disk_cache_put(f"k{i:02d}", _HITS)
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(data) <= local_seek._SEEK_DISK_CACHE_MAX
    assert "k00" not in data and "k01" not in data   # 最旧的被淘汰
    assert "k65" in data                              # 最新的保留


def test_second_process_run_served_from_disk(monkeypatch):
    """端到端：内存缓存清空后，第二次调用必须走落盘层（不触碰 seek）。"""
    seek_py = str(SEEK)
    mod = local_seek._load_seek_module(seek_py)

    def _boom(*a, **k):
        raise AssertionError("落盘缓存命中时不应触碰 seek.run_query")

    hits1 = local_seek._run_local_seek("cache_key", 3, search_dir=str(SCRIPTS))
    assert len(hits1) == 3
    monkeypatch.setattr(mod, "run_query", _boom)
    local_seek._LOCAL_SEEK_CACHE.clear()  # 只清内存层，落盘层还在
    hits2 = local_seek._run_local_seek("cache_key", 3, search_dir=str(SCRIPTS))
    assert len(hits2) == 3
    assert hits2[0]["score"] == 0.9

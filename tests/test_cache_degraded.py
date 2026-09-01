#!/usr/bin/env python3
"""test_cache_degraded.py — 缓存层降级（不可用时不拖垮调用方）回归测试。

缓存是加速层而非功能依赖：目录不可建 / 数据库只读 / 权限受限 / 磁盘满时，
SQLiteCache 应整层降级为 no-op（读恒未命中、写静默丢弃），而不是抛异常
把上层搜索/抓取一起拖崩。
"""

import os
import sqlite3
import stat
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import cache  # noqa: E402


@pytest.fixture()
def readonly_dir(tmp_path):
    """一个确实不可写的目录（模拟只读挂载 / 沙箱 / 权限受限）。"""
    d = tmp_path / "ro-cache"
    d.mkdir()
    os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)  # r-x：不可写
    yield str(d / "cache.db")
    os.chmod(d, stat.S_IRWXU)  # 还原，保证 tmp_path 可清理


# ─── 降级触发 ─────────────────────────────────────────────────────────────────

def test_construct_on_readonly_dir_degrades_instead_of_raising(readonly_dir):
    """只读目录：构造不抛异常，标记为 degraded。"""
    c = cache.SQLiteCache(db_path=readonly_dir)
    assert c.degraded is True
    assert c.degraded_reason


def test_readonly_db_file_degrades(tmp_path):
    """数据库文件本身只读：同样降级而非崩溃。"""
    db = tmp_path / "cache.db"
    db.touch()
    os.chmod(db, stat.S_IRUSR)  # 只读文件
    try:
        c = cache.SQLiteCache(db_path=str(db))
        assert c.degraded is True
    finally:
        os.chmod(db, stat.S_IRUSR | stat.S_IWUSR)


# ─── 降级后读写语义 ───────────────────────────────────────────────────────────

def test_degraded_get_is_miss_not_error(readonly_dir):
    c = cache.SQLiteCache(db_path=readonly_dir)
    assert c.get("any-key") is None          # 未命中，不抛异常
    assert c.stats["misses"] >= 1


def test_degraded_set_is_silent_noop(readonly_dir):
    c = cache.SQLiteCache(db_path=readonly_dir)
    c.set("k", "q", "engine", 1, {"a": 1})   # 静默丢弃
    assert c.get("k") is None


def test_degraded_clear_and_find_similar_are_noop(readonly_dir):
    c = cache.SQLiteCache(db_path=readonly_dir)
    c.clear()
    assert c.find_similar("任意查询") == []


def test_degraded_stats_reports_degraded_flag(readonly_dir):
    c = cache.SQLiteCache(db_path=readonly_dir)
    s = c.stats
    assert s["degraded"] is True
    assert s["entries"] == 0
    assert s["degraded_reason"]


def test_degraded_size_mb_is_zero(readonly_dir):
    c = cache.SQLiteCache(db_path=readonly_dir)
    assert c.size_mb == 0.0


# ─── 正常路径不受影响 ─────────────────────────────────────────────────────────

def test_healthy_cache_roundtrip(tmp_path):
    """可写目录：功能完全正常（回归护栏，确保降级逻辑没有污染正常路径）。"""
    db = str(tmp_path / "cache.db")
    c = cache.SQLiteCache(db_path=db)
    assert c.degraded is False
    c.set("k1", "query", "engine", 5, {"hello": "world"})
    assert c.get("k1") == {"hello": "world"}
    assert c.stats["entries"] == 1
    assert c.stats["hits"] == 1
    assert "degraded" not in c.stats


def test_healthy_evict_and_clear(tmp_path):
    db = str(tmp_path / "cache.db")
    c = cache.SQLiteCache(db_path=db)
    c.set("k1", "q1", "e", 1, {"v": 1})
    c.set("k2", "q2", "e", 1, {"v": 2})
    assert c.stats["entries"] == 2
    c.clear(older_than_hours=0)   # cutoff=now，全清
    assert c.stats["entries"] == 0


# ─── 双层入口 SearchCache 同样不崩 ────────────────────────────────────────────

def test_searchcache_survives_readonly_db(readonly_dir):
    """上层 SearchCache 在 L2 降级时仍可构造并工作（L1 内存层照常）。"""
    sc = cache.SearchCache(db_path=readonly_dir)
    assert sc._l2.degraded is True
    # L1 内存层不受影响，读写仍可用
    sc.set("q", "engine", 5, {"results": [{"title": "t"}]}, domain="general")
    assert sc.get("q", "engine", 5, domain="general") is not None

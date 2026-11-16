#!/usr/bin/env python3
"""tests/test_cache_schema_migration.py — 缓存 schema 迁移回归门（2026-11-17）。

治什么：SCHEMA_VERSION 早退让旧库跳过列迁移，升级后炸
`OperationalError: no such column: mode/domain`。

- c685513（2026-09-19）给 search_cache 加 mode/depth 列，没 bump SCHEMA_VERSION；
- 4026fab（2026-10-21，v2.10.0）在 _init_db 加了「版本号相等即 return」的早退。
  两者叠加：凡 schema_version='2' 但缺后加列的旧库（v2.0.0 2026-07-19 到
  c685513 之间写出的），升级到 ≥v2.10.0 后迁移被早退跳过，find_similar
  （WHERE mode=? AND depth=?）与写入即 OperationalError。

修复：SCHEMA_VERSION bump 3。旧库与早退失配 → 走一次幂等迁移补齐全部列。
本文件用真实文件库锁两条历史形态 + 修复后自愈 + 新库口径。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from cache import SQLiteCache  # noqa: E402

_V2_TABLE = """
CREATE TABLE search_cache (
    key TEXT PRIMARY KEY, query TEXT NOT NULL, engine TEXT NOT NULL,
    max_results INTEGER NOT NULL, value_blob BLOB NOT NULL,
    compressed INTEGER DEFAULT 0, created_at REAL NOT NULL,
    accessed_at REAL NOT NULL
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '2');
"""


def _make_legacy_db(tmp_path: Path, *, with_domain_ttl: bool) -> str:
    """造一张 v2 旧形态库：schema_version='2'，列集停在加 mode/depth 之前。

    with_domain_ttl=False 复现更老的形态（连 domain/ttl 都没有）——4026fab
    的早退对这类库同样生效，炸点从 mode 提前到 domain。
    """
    db = str(tmp_path / ("legacy_" + ("full" if with_domain_ttl else "old") + ".db"))
    conn = sqlite3.connect(db)
    if with_domain_ttl:
        conn.executescript(_V2_TABLE.replace(
            "compressed INTEGER DEFAULT 0,",
            "compressed INTEGER DEFAULT 0, domain TEXT DEFAULT 'general',"
            " ttl INTEGER DEFAULT 3600,"))
    else:
        conn.executescript(_V2_TABLE)
    conn.commit()
    conn.close()
    return db


def _columns(db: str) -> list[str]:
    return [r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(search_cache)")]


@pytest.mark.parametrize("with_domain_ttl", [True, False],
                         ids=["v2_missing_mode_depth", "v2_missing_domain_too"])
def test_legacy_v2_db_self_heals_on_bump(tmp_path, with_domain_ttl):
    """schema_version='2' 旧库打开后自动补齐全部缺失列，find_similar 可用。"""
    db = _make_legacy_db(tmp_path, with_domain_ttl=with_domain_ttl)
    cache = SQLiteCache(db_path=db)
    assert not cache.degraded, cache.degraded_reason
    cols = _columns(db)
    for col in ("domain", "ttl", "mode", "depth"):
        assert col in cols, f"迁移后仍缺列 {col}（cols={cols}）"
    ver = sqlite3.connect(db).execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert ver == (str(SQLiteCache.SCHEMA_VERSION),), \
        f"迁移后 meta.schema_version 未更新：{ver}"
    # 炸点回归：c685513 引入的软命中查询在旧库上必须可用
    assert cache.find_similar("测试查询") == []


def test_all_four_columns_present_on_fresh_db(tmp_path):
    """新库一次建全（含 mode/depth），meta 写当前版本。"""
    db = str(tmp_path / "fresh.db")
    cache = SQLiteCache(db_path=db)
    assert not cache.degraded
    for col in ("domain", "ttl", "mode", "depth"):
        assert col in _columns(db)
    assert sqlite3.connect(db).execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone() == (
        str(SQLiteCache.SCHEMA_VERSION),)


def test_migration_is_idempotent(tmp_path):
    """迁移幂等：同一旧库反复 init 不再改表，二次打开直接早退。"""
    db = _make_legacy_db(tmp_path, with_domain_ttl=True)
    first = SQLiteCache(db_path=db)
    assert not first.degraded
    snapshot = _columns(db)
    second = SQLiteCache(db_path=db)
    assert not second.degraded
    assert _columns(db) == snapshot


def test_write_and_find_similar_roundtrip_after_migration(tmp_path):
    """迁移后的旧库走一次真实读写：写入带 mode/depth，软命中按列隔离。"""
    db = _make_legacy_db(tmp_path, with_domain_ttl=True)
    cache = SQLiteCache(db_path=db)
    cache.set("k1", "深拷贝 与 浅拷贝 区别", engine="octen",
              max_results=5, value={"results": [{"title": "t"}]}, mode="fast",
              depth="fast")
    # 不同 mode 的软命中不得串味（历史行 mode 为空串，天然不参与）
    assert cache.find_similar("深拷贝 与 浅拷贝 的区别", mode="auto") == []

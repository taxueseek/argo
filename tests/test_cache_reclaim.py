#!/usr/bin/env python3
"""test_cache_reclaim.py — 缓存空间回收（驱逐收敛 + 过期回收）回归测试。

覆盖两个此前**实测会静默损坏缓存**的行为：

1. 驱逐循环的量纲错配（2026-09-19 复现）。`_evict_if_needed` 用文件页数
   （page_count × page_size）判定超限，却用 payload 净长做扣减；本库
   `auto_vacuum=0` 且全仓无 VACUUM，页数永不下降，于是 while 永不收敛——
   库一旦超过阈值，每次 set() 把整库（含刚写入的那一行）删空，此后每次
   写入重复清空，缓存永久 100% miss。本文件锁住「超限后仍然可读写」。
2. 过期行从不回收。实测线上 5521 行里 5444 行（98.6%）已过期，占 payload
   的 98.3%；不回收则 find_similar 与淘汰扫描永远扫过这些不可见的行。

两个用例都不联网、不碰生产状态目录（conftest 已隔离 ARGO_STATE_DIR）。
"""

import os
import sqlite3
import sys
import time

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import cache  # noqa: E402


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
    finally:
        conn.close()


def _payload(db_path):
    """不可压缩载荷：`_serialize` 对 >阈值 的内容做 gzip，重复字符会压成
    几字节，量不出真实的库体积。"""
    import random
    import string
    rnd = random.Random(20260919)
    return "".join(rnd.choice(string.printable) for _ in range(3000))


@pytest.fixture()
def small_limit(tmp_path, monkeypatch):
    """把阈值缩到 1MB，让「超限」在几十次写入内可复现（逻辑与 100MB 完全一致）。"""
    monkeypatch.setattr(cache, "MAX_DB_SIZE_MB", 1.0)
    return str(tmp_path / "cache.db")


def test_eviction_converges_and_cache_stays_usable(small_limit):
    """超限驱逐后：库不被清空、刚写入的条目仍可读回、库有界。

    修复前此用例红：600 次写入后 rows == 0，且此后每次 set() 都被自己删掉，
    `get()` 恒为 None。
    """
    sc = cache.SQLiteCache(db_path=small_limit, ttl=99999)
    payload = _payload(small_limit)

    for i in range(600):
        sc.set(f"k{i:04d}", f"q{i}", "e", 5,
               {"results": [{"title": payload}]}, "general", 99999)

    after_burst = _rows(small_limit)
    assert after_burst > 0, "驱逐把整库删空了（量纲错配复发）"

    # 继续写入不应把库越删越小 —— 修复前这里每写一条就归零
    for j in range(3):
        sc.set(f"t{j}", "t", "e", 5,
               {"results": [{"title": payload}]}, "general", 99999)
        assert _rows(small_limit) >= after_burst, (
            f"第 {j + 1} 次写入后又开始清库：rows={_rows(small_limit)}"
        )

    # 缓存仍然功能可用：写入 → 读回
    sc.set("keep", "keep", "e", 5, {"results": [{"title": "alive"}]},
           "general", 99999)
    hit = sc.get("keep")
    assert hit is not None, "驱逐后缓存永久失效（读回为 None）"
    assert hit["results"][0]["title"] == "alive"


def test_eviction_is_bounded_by_limit(small_limit):
    """驱逐后库体积受阈值约束，不会无限增长。"""
    sc = cache.SQLiteCache(db_path=small_limit, ttl=99999)
    payload = _payload(small_limit)
    for i in range(900):
        sc.set(f"b{i:04d}", f"q{i}", "e", 5,
               {"results": [{"title": payload}]}, "general", 99999)
    size_mb = os.path.getsize(small_limit) / 1024 / 1024
    # 留出 SQLite 页对齐 + 未检查点 WAL 的余量
    assert size_mb < cache.MAX_DB_SIZE_MB * 2.5, f"库体积失控：{size_mb:.2f} MB"


def test_expired_rows_are_reclaimed(small_limit, monkeypatch):
    """过期行按节流间隔回收，未过期行一条不动。"""
    sc = cache.SQLiteCache(db_path=small_limit, ttl=99999)

    for i in range(40):
        sc.set(f"live{i}", f"q{i}", "e", 5, {"r": [1]}, "general", 99999)
    for i in range(40):
        sc.set(f"dead{i}", f"q{i}", "e", 5, {"r": [1]}, "general", 1)
    assert _rows(small_limit) == 80

    time.sleep(1.2)  # 让 TTL=1s 的那批真正过期

    # 把节流戳拨到 2 小时前 —— 等价于「距上次清扫已过一个间隔」
    conn = sqlite3.connect(small_limit)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                 ("expiry_swept_at", str(time.time() - 7200)))
    conn.commit()
    conn.close()

    sc.set("trigger", "t", "e", 5, {"r": [1]}, "general", 99999)

    conn = sqlite3.connect(small_limit)
    live = conn.execute(
        "SELECT COUNT(*) FROM search_cache WHERE key LIKE 'live%'").fetchone()[0]
    dead = conn.execute(
        "SELECT COUNT(*) FROM search_cache WHERE key LIKE 'dead%'").fetchone()[0]
    conn.close()
    assert dead == 0, f"过期行未被回收：还剩 {dead} 条"
    assert live == 40, f"未过期行被误删：只剩 {live} 条"


def test_expiry_sweep_is_throttled(small_limit):
    """节流生效：间隔内不重复清扫（避免把全表扫描放进每次写入的热路径）。"""
    sc = cache.SQLiteCache(db_path=small_limit, ttl=99999)
    sc.set("a", "a", "e", 5, {"r": [1]}, "general", 99999)

    conn = sqlite3.connect(small_limit)
    stamp = conn.execute("SELECT value FROM meta WHERE key = ?",
                         ("expiry_swept_at",)).fetchone()
    conn.close()
    assert stamp is not None, "首次写入应记下清扫戳"

    # 紧接着再写一条：戳不应被刷新（说明没重复清扫）
    sc.set("b", "b", "e", 5, {"r": [1]}, "general", 99999)
    conn = sqlite3.connect(small_limit)
    stamp2 = conn.execute("SELECT value FROM meta WHERE key = ?",
                          ("expiry_swept_at",)).fetchone()
    conn.close()
    assert stamp2[0] == stamp[0], "节流未生效：每次写入都在全表清扫"


def test_accessed_at_index_exists(small_limit):
    """accessed_at 必须有索引：淘汰与 find_similar 都按它排序，
    缺索引会退化成全表扫描 + 临时 B 树。"""
    cache.SQLiteCache(db_path=small_limit, ttl=99999)
    conn = sqlite3.connect(small_limit)
    idx = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")]
    evict_plan = [r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT key FROM search_cache "
        "ORDER BY accessed_at ASC LIMIT 50")]
    # find_similar 是「WHERE domain = ? AND mode = ? AND depth = ?
    # ORDER BY accessed_at DESC」
    similar_plan = [r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT key FROM search_cache "
        "WHERE domain = 'general' AND mode = 'auto' AND depth = 'fast' "
        "ORDER BY accessed_at DESC LIMIT 50")]
    conn.close()
    assert "idx_search_cache_accessed" in idx, f"缺索引，现有：{idx}"
    assert "idx_search_cache_scope_accessed" in idx, f"缺作用域复合索引，现有：{idx}"
    assert not any("TEMP B-TREE" in p for p in evict_plan), \
        f"淘汰排序仍在临时 B 树：{evict_plan}"
    assert not any("TEMP B-TREE" in p for p in similar_plan), \
        f"find_similar 排序仍在临时 B 树：{similar_plan}"


def test_periodic_sweep_reclaims_disk(small_limit, monkeypatch):
    """定期清扫后，空闲页够多时必须真的把文件缩回去。

    删行只把页放进 freelist，文件停在高水位。线上实测：10.1MB 的 cache.db
    里活数据只有 0.12MB（98.6% 的行已过期）——不回收就白占着 10MB。

    测的是「检查点之后的主库文件」：WAL 模式下 VACUUM 的重写先进 -wal，主
    文件要到检查点才反映新大小。不先检查点就量会看到「VACUUM 没生效」的
    假象（本用例第一版就是这么误报的）。
    """
    monkeypatch.setattr(cache.SQLiteCache, "_RECLAIM_MIN_BYTES", 64 * 1024)
    sc = cache.SQLiteCache(db_path=small_limit, ttl=99999)
    payload = _payload(small_limit)

    for i in range(120):
        sc.set(f"d{i:03d}", f"q{i}", "e", 5,
               {"results": [{"title": payload}]}, "general", 1)  # TTL=1s
    time.sleep(1.2)
    grown = _main_file_bytes(small_limit)

    conn = sqlite3.connect(small_limit)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                 ("expiry_swept_at", str(time.time() - 7200)))
    conn.commit()
    conn.close()

    sc.set("after", "after", "e", 5, {"results": [{"title": payload}]},
           "general", 99999)

    shrunk = _main_file_bytes(small_limit)
    assert shrunk < grown, (
        f"定期清扫未回收磁盘：{grown / 1024:.0f}KB -> {shrunk / 1024:.0f}KB")
    assert _rows(small_limit) == 1, "只应剩下未过期的那一条"


def _main_file_bytes(db_path):
    """检查点之后的主库文件大小（WAL 模式下这才是真实占用）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return os.path.getsize(db_path)


def test_slow_query_ttl_amplification_is_capped():
    """慢查询的 TTL 延长上限一律 2×，与档位无关。

    历史 bug（2026-09-19 定位）：延长逻辑写成 if/else 两支，else 支
    （base_ttl > 900）没有上限，而 multiplier 最大 8。evergreen 档
    （image_search / geo_places / book_search，base=86400s）的慢查询因此拿到
    691200s＝**8 天** TTL，慢查询结果以「新鲜」的样子交付一整周。注释一直
    写的是「最多 2×」，代码只有一半兑现。

    打到真实实现 `search._slow_query_ttl`（不是把公式抄一遍）：抄公式的
    写法在实现回退时照样绿，等于没锁。
    """
    import search as search_mod
    from cache import SearchCache

    sc = SearchCache()
    for domain in ("image_search", "geo_places", "book_search", "general",
                   "academic", "news_realtime"):
        base = sc.resolve_ttl(domain)
        assert base > 0, f"{domain} 的 base TTL 非正"
        for elapsed in (2001, 4000, 9000, 60000):
            eff = search_mod._slow_query_ttl(base, elapsed)
            assert eff <= base * 2, (
                f"{domain}: elapsed={elapsed}ms 时 TTL 放大到 {eff}s"
                f"（{eff / 86400:.1f} 天），超过 2× 上限（base={base}s）")
            assert eff >= base, f"{domain}: 延长不得缩短 TTL（{eff} < {base}）"
    # 耗时不足 2s 不延长；≥4s 走满 2×
    assert search_mod._slow_query_ttl(3600, 0) == 3600
    assert search_mod._slow_query_ttl(3600, 4000) == 7200
    assert search_mod._slow_query_ttl(3600, 600000) == 7200

#!/usr/bin/env python3
"""
cache.py — Unified Search v2 双层缓存引擎

功能：
  L1: 内存 LRU 热缓存（1000 条），避免同进程重复查询
  L2: SQLite 持久化缓存（TTL 可配置），跨进程复用
  分级 TTL：financial / news / realtime / general / research / evergreen
  大值 gzip 压缩（> 1KB）
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from typing import Optional

_sqlite3 = None


def _get_sqlite3():
    """延迟导入 sqlite3，避免重量级 import 链（sqlite3 → _sqlite3 → zlib → bz2 → lzma）。"""
    global _sqlite3
    if _sqlite3 is None:
        import sqlite3
        _sqlite3 = sqlite3
    return _sqlite3

try:
    from config import get_cache_config
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from config import get_cache_config

# 本地状态目录唯一来源（env ARGO_STATE_DIR → config cache.db_path 父目录 → 旧路径）
import argo_paths
import engine_states  # 状态词表唯一真源：缓存层级（2026-11-09 收口）
from cache_guard import (
    DegradedCacheRejected,
    RECOVERED_DEGRADED_TTL,
    assert_not_degraded,
    assert_not_failed_state,  # 写入守卫（2026-09-27 拆出；失败态守卫 2026-09-29 增补）
)
from cache_key_vdom import cache_key_vdom  # 引擎级垂直域维度（--domain/--sub_domain）
from cli_io import dumps
from except_sets import IO_BENIGN, SHAPE_BENIGN


# ── 常量 ──────────────────────────────────────────────────────────────────────

# 由唯一来源派生，不再字面量拼 ~/.cache/unified-search。
# 用 db_path() 而非 state_path()：config.yaml 显式改写 db_path 时尊重用户配置。
DEFAULT_DB_PATH = str(argo_paths.db_path())
DEFAULT_TTL = 3600
MAX_MEMORY_ITEMS = 1000
MAX_DB_SIZE_MB = 200
COMPRESSION_THRESHOLD = 768
COMPRESSION_LEVEL = 1

# 分级 TTL（秒）
CACHE_TIERS = {
    "financial": 300,    # 5 分钟
    "news": 600,         # 10 分钟
    "realtime": 900,     # 15 分钟
    "general": 3600,     # 1 小时
    "research": 7200,    # 2 小时
    "evergreen": 86400,  # 24 小时
}

# 当天缓存策略：仅 evergreen/research 可延长至日末。
# general 不再日末延长，避免「今日热点」等泛中文域被错误拉到超长 TTL。
SAME_DAY_ELIGIBLE_TIERS = {"research", "evergreen"}

# 时效敏感查询硬上限（秒）— 覆盖 domain 误分到 general 的情况
REALTIME_TTL_CAP = 900

# query domain → TTL tier 映射
DOMAIN_TIER_MAP = {
    "stock_query": "financial",
    "fund_query": "financial",
    "financial_news": "news",
    "zhihu_content": "general",
    "tech_deep": "research",
    "english_tech": "research",
    "news_realtime": "realtime",
    "hot_trending": "realtime",
    "zhihu_hot_list": "realtime",
    "general_search": "general",
    "chinese_general": "general",
    "chinese_tech_deep": "research",
    "fact_check": "research",
    "code_search": "research",
    "wechat_search": "news",
    "shopping": "general",
    "reference": "evergreen",
    "social": "general",
    "local_chinese": "general",
    "local_news": "news",
    "local_academic": "research",
    "local_code": "research",
    "local_reference": "evergreen",
    "local_general": "general",
    "stock": "financial",
    "fund": "financial",
    "news": "realtime",
    "tech": "research",
    "deep": "research",  # 别名：深度/研究类
    "general": "general",
    "auto": "general",
    "fetch": "general",
    # ── v2.7.3 补全：48 个无映射域按时效归类（此前全走 general 3600s，
    #    实时卡片/快讯/行情被缓存 1 小时后过期）──
    "ths_hot_search": "realtime",
    "cls_telegraph_search": "realtime",
    "em_news_search": "news",
    "jin10_flash": "realtime",
    "modal_card": "realtime",       # 油价/金价/车票实时值卡片
    "us_stock": "financial",
    "macro_data": "financial",
    "crypto_search": "financial",
    "weather_query": "realtime",
    "aviation_weather": "realtime",
    "global_event": "news",
    # 稳定型：学术/百科/参考类放宽到 research（提高命中率）
    "hackernews_search": "news",
    "scholar_search": "research",
    "chem_search": "research",
    "protein_search": "research",
    "patent_search": "research",
    "earth_science": "research",
    "academic": "research",
    "cn_encyclopedia": "evergreen",
    "dictionary_search": "evergreen",
    "book_search": "evergreen",
    "film_search": "evergreen",
    "anime_encyclopedia": "evergreen",
    "web_archive": "evergreen",
    # 稳定型：技术/社区/百科类（补全剩余 24 个，避免默认 general 一刀切）
    "stackoverflow_search": "research",
    "v2ex_search": "research",
    "cn_tech_community": "research",
    "package_search": "research",
    "web_docs": "research",
    "ml_models": "research",
    "ai_model": "research",
    "species_search": "evergreen",
    "rfc_search": "evergreen",
    "us_legal": "evergreen",
    "legal": "evergreen",
    "wenshu_query": "evergreen",
    "medical": "research",
    "game_search": "evergreen",
    "prediction_market": "realtime",
    "sports_search": "news",
    "geo_places": "evergreen",
    "org_entity": "evergreen",
    "media_search": "evergreen",
    "image_search": "evergreen",
    "entity_search": "evergreen",
    "semantic_discovery": "research",
    "meme_slang": "evergreen",
    "company_search": "research",
}


# ── 命中策略判据（2026-10-16 拆至 cache_similarity.py）─────────────────────────
# 归一化 / minhash 相似度 / 限定符拆分 / 时效检测四件事整段搬走（判据层，
# 零 IO）。此处 re-export：既有 `from cache import query_similarity` 等
# import 点与 `monkeypatch.setattr(cache, ...)` 的测试面都保持不变——
# 调用方经本模块全局名查找，patch 照常生效（拆模块老规矩，同 cache_guard）。
from cache_similarity import (  # noqa: F401
    _hash_token,
    _MINHASH_PERM,
    _ngrams,
    _signature,
    is_freshness_sensitive_query,
    normalize_query,
    query_similarity,
    split_qualifiers,
)


# ── LRU 内存缓存 ───────────────────────────────────────────────────────────────

class LRUCache:
    """基于 OrderedDict 的简单 LRU。"""

    def __init__(self, max_size: int = MAX_MEMORY_ITEMS):
        self._max_size = max_size
        self._store: OrderedDict[str, dict] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            if key in self._store:
                self._hits += 1
                self._store.move_to_end(key)
                return self._store[key]
            self._misses += 1
            return None

    def set(self, key: str, value: dict):
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = value
            else:
                if len(self._store) >= self._max_size:
                    self._store.popitem(last=False)
                self._store[key] = value

    def remove(self, key: str) -> None:
        """移除指定键（不存在时静默）。"""
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._store),
                "hit_rate": round(self._hits / max(self._hits + self._misses, 1), 3),
            }


# ── SQLite 持久化缓存 ──────────────────────────────────────────────────────────

class SQLiteCache:
    """SQLite 持久化缓存，支持 TTL 过期、大小限制、gzip 压缩。"""

    SCHEMA_VERSION = 2

    def __init__(self, db_path: str = DEFAULT_DB_PATH, ttl: int = DEFAULT_TTL,
                 max_size_mb: float | None = None):
        self._db_path = os.path.expanduser(db_path)
        self._ttl = ttl
        # 驱逐阈值：显式参数 > 模块常量。**在 __init__ 取值而不是每次驱逐时读**，
        # 这样测试 monkeypatch 模块常量（既有小库复现手法，见 test_cache_reclaim）
        # 依然生效；生产侧的上限由 SearchCache 从 config.yaml 传下来。
        self._max_size_mb = (float(max_size_mb)
                             if isinstance(max_size_mb, (int, float))
                             and not isinstance(max_size_mb, bool)
                             and max_size_mb > 0
                             else float(MAX_DB_SIZE_MB))
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        # 缓存是加速层而非功能依赖：目录不可建/不可写（只读挂载、沙箱、
        # 权限受限、磁盘满）时降级为“不可用”而不是抛异常拖垮调用方。
        self._degraded_reason: str | None = None
        # :memory: 必须单连接（每次 connect(":memory:") 都是独立空库）
        self._mem_conn: _get_sqlite3().Connection | None = None
        # :memory: / 空路径 / URI 无需建目录
        if self._db_path not in (":memory:", "") and not self._db_path.startswith("file:"):
            parent = os.path.dirname(self._db_path)
            if parent:
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError as e:
                    self._degraded_reason = f"cache dir unavailable: {e}"
        if self._db_path == ":memory:":
            # check_same_thread=False：SQLiteCache 随 SearchCache 常驻，MCP 长驻
            # 进程里第一次碰缓存的线程未必是建连接的那个线程，默认的
            # 「同线程校验」会直接抛 ProgrammingError 让整层内存缓存不可用。
            # 这里放开校验是安全的——所有 _mem_conn 访问都在 self._lock
            # （RLock，见上方 __init__）之内串行化，不存在真正的并发使用。
            self._mem_conn = _get_sqlite3().connect(":memory:", timeout=10,
                                             check_same_thread=False)
            self._mem_conn.execute("PRAGMA synchronous=NORMAL")
        if self._degraded_reason is None:
            try:
                self._init_db()
            except _get_sqlite3().Error as e:
                # 只读数据库 / 权限不足 / 磁盘满：整层降级，不向上传播
                self._degraded_reason = f"cache db unavailable: {e}"
                self._close_mem_conn()

    def _close_mem_conn(self) -> None:
        if self._mem_conn is not None:
            try:
                self._mem_conn.close()
            except _get_sqlite3().Error:
                pass
            self._mem_conn = None

    @property
    def degraded(self) -> bool:
        """缓存层是否已降级（此时读写均为 no-op，调用方应当前作未命中）。"""
        return self._degraded_reason is not None

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    def _connect(self) -> _get_sqlite3().Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = _get_sqlite3().connect(self._db_path, timeout=10)
        argo_paths.apply_state_pragmas(conn)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextlib.contextmanager
    def _conn(self):
        """借一个连接：出作用域提交/回滚，并把**自建**连接关掉。

        为什么不是 `with self._connect() as conn:`：sqlite3 的连接上下文管理器
        管的是**事务**（正常退出 commit、异常 rollback），它**不关连接**。于是
        此前 9 处调用点每跑一次就新建并遗弃一条文件连接——进程内跑几千次缓存
        操作就攒下几千个 fd，长驻 MCP 进程迟早撞上 ulimit（EMFILE），而报错点
        会落在完全无关的 socket/文件调用上，极难归因。

        这里把「作用域」补成真正的作用域语义：commit → close。异常时先
        rollback 再关，事务状态不留半截。

        `:memory:` 是**共享**连接（每次 connect(":memory:") 都是独立空库），
        不能关——关掉等于把整层内存缓存作废。锁不在这里取：调用点有的持锁
        （读改写路径）、有的不持（_evict_if_needed / size_mb 只读统计），
        保持各自原语义。
        """
        conn = self._connect()
        shared = conn is self._mem_conn
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except _get_sqlite3().Error:
                pass
            raise
        finally:
            if not shared:
                try:
                    conn.close()
                except _get_sqlite3().Error:
                    pass

    def _init_db(self):
        if (self._db_path not in (":memory:", "")
                and not self._db_path.startswith("file:")
                and os.path.exists(self._db_path)
                and not os.access(self._db_path, os.W_OK)):
            raise _get_sqlite3().OperationalError("readonly database")
        with self._conn() as conn:
            try:
                if conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == (str(self.SCHEMA_VERSION),):
                    return
            except _get_sqlite3().Error:
                pass
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS search_cache (
                    key TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    max_results INTEGER NOT NULL,
                    domain TEXT DEFAULT 'general',
                    value_blob BLOB NOT NULL,
                    compressed INTEGER DEFAULT 0,
                    ttl INTEGER DEFAULT 3600,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            # 迁移：添加可能缺失的列
            cols = [r[1] for r in conn.execute("PRAGMA table_info(search_cache)")]
            if "domain" not in cols:
                conn.execute("ALTER TABLE search_cache ADD COLUMN domain TEXT DEFAULT 'general'")
            if "ttl" not in cols:
                conn.execute("ALTER TABLE search_cache ADD COLUMN ttl INTEGER DEFAULT 3600")
            # mode/depth：软命中隔离用（见 find_similar）。
            #
            # 默认空串而不是 'auto'/'fast'：这两列要回答的是「这条缓存是按哪个
            # mode/depth 写出来的」，而迁移前的历史行**无从得知**。默认成
            # auto/fast 等于替它们猜一个——deep 请求会软命中一条其实按 fast
            # 写的条目，正是要修的那个 bug。空串与任何真实 mode/depth 都不相等，
            # 于是历史行自然不参与软命中，等 TTL 到期回收即可（现在每小时扫一次）。
            if "mode" not in cols:
                conn.execute("ALTER TABLE search_cache ADD COLUMN mode TEXT DEFAULT ''")
            if "depth" not in cols:
                conn.execute("ALTER TABLE search_cache ADD COLUMN depth TEXT DEFAULT ''")
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_search_cache_expires ON search_cache(created_at);
                CREATE INDEX IF NOT EXISTS idx_search_cache_domain ON search_cache(domain);
                -- accessed_at 是两条热查询的排序列（淘汰取最旧、find_similar 取最新）。
                -- 没有它两者都退化成「全表扫描 + 临时 B 树排序」：EXPLAIN 实测
                -- `SCAN search_cache` + `USE TEMP B-TREE FOR ORDER BY`。
                CREATE INDEX IF NOT EXISTS idx_search_cache_accessed ON search_cache(accessed_at);
                -- find_similar 是「WHERE domain = ? AND mode = ? AND depth = ?
                -- ORDER BY accessed_at DESC」：过滤列与排序列合成一个索引，
                -- 否则 SQLite 仍要建临时 B 树排序（EXPLAIN 实测 `USE TEMP B-TREE
                -- FOR ORDER BY`）。旧的两列版 domain_accessed 由这条覆盖（前缀
                -- 相同），留着只是给每次写入多维护一棵 B 树。
                DROP INDEX IF EXISTS idx_search_cache_domain_accessed;
                CREATE INDEX IF NOT EXISTS idx_search_cache_scope_accessed
                    ON search_cache(domain, mode, depth, accessed_at);
            """)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         ("schema_version", str(self.SCHEMA_VERSION)))

    def _is_expired(self, created_at: float, ttl: int | None = None) -> bool:
        return (time.time() - created_at) > (ttl if ttl is not None else self._ttl)

    @staticmethod
    def _serialize(value: dict) -> tuple[bytes, int]:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(raw) > COMPRESSION_THRESHOLD:
            return gzip.compress(raw, COMPRESSION_LEVEL), 1
        return raw, 0

    @staticmethod
    def _deserialize(blob: bytes, compressed: int) -> dict:
        raw = gzip.decompress(blob) if compressed else blob
        return json.loads(raw.decode("utf-8"))

    def has_live(self, key: str) -> bool:
        """该键是否已有一条**未过期**条目。

        只读探测：不动 accessed_at、不计命中/未命中、不删过期行——它要回答的
        只是「现在覆盖它会不会毁掉有效数据」，不是「这次查询命中了没有」。
        用 get() 兼职探测会污染 LRU 的访问时间，也会让命中率统计失真。
        """
        if self._degraded_reason is not None:
            return False
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT created_at, ttl FROM search_cache WHERE key = ?", (key,)
                ).fetchone()
        return row is not None and not self._is_expired(row[0], row[1])

    def get(self, key: str) -> Optional[dict]:
        if self._degraded_reason is not None:
            # 降级：一律视作未命中。缓存是加速层，缺它只该变慢，不该变错。
            self._misses += 1
            return None
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT value_blob, compressed, created_at, ttl FROM search_cache WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    self._misses += 1
                    return None
                value_blob, compressed, created_at, ttl = row
                if self._is_expired(created_at, ttl):
                    conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
                    conn.commit()
                    self._misses += 1
                    return None
                conn.execute("UPDATE search_cache SET accessed_at = ? WHERE key = ?",
                             (time.time(), key))
                conn.commit()
                self._hits += 1
                return self._deserialize(value_blob, compressed)

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        if not keys or self._degraded_reason is not None:
            return {}
        out: dict[str, dict] = {}
        with self._lock, self._conn() as conn:
            ph = ",".join("?" for _ in keys)
            for k, blob, comp, ts, ttl in conn.execute(
                f"SELECT key, value_blob, compressed, created_at, ttl "
                f"FROM search_cache WHERE key IN ({ph})", tuple(keys)
            ):
                if not self._is_expired(ts, ttl):
                    out[k] = self._deserialize(blob, comp)
        return out

    def set(self, key: str, query: str, engine: str, max_results: int,
            value: dict, domain: str = "general", ttl: int | None = None,
            mode: str = "", depth: str = ""):
        if self._degraded_reason is not None:
            return
        with self._lock:
            blob, compressed = self._serialize(value)
            now = time.time()
            effective_ttl = ttl if ttl is not None else self._ttl
            with self._conn() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO search_cache
                       (key, query, engine, max_results, domain, mode, depth,
                        value_blob, compressed, ttl, created_at, accessed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, query, engine, max_results, domain, mode, depth,
                     blob, compressed, effective_ttl, now, now),
                )
                conn.commit()
            # 驱逐失败不阻断写入方：缓存是加速层，缺它只该变慢不该变错。
            # 与 self._degraded_reason 的 fail-open 语义同构——DB 损坏/磁盘满/WAL
            # 异常时，宁可让下次搜索多写一条也不让 execute_search 在最后一步 traceback。
            try:
                self._evict_if_needed()
            except _get_sqlite3().Error:
                pass

    # 过期行回收的节流间隔。扫描是 O(rows) 全表（`created_at + ttl < ?` 不可
    # 走索引），放进每次 set() 的热路径会白付约 6ms/次；稳态下每小时一次足够
    # 把库压在低位。
    _EXPIRY_SWEEP_INTERVAL_S = 3600.0
    # VACUUM 门槛：空闲页占比超过它才付一次整库重写（固定字节门槛两头都不对）。
    _RECLAIM_FREE_RATIO = 0.2

    def _expiry_sweep_due(self, conn) -> bool:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?",
                               ("expiry_swept_at",)).fetchone()
            return (not row
                    or (time.time() - float(row[0])) > self._EXPIRY_SWEEP_INTERVAL_S)
        except (_get_sqlite3().Error, TypeError, ValueError):
            return True  # 读不出来就扫一次，宁可多扫不积压

    @staticmethod
    def _mark_expiry_swept(conn) -> None:
        try:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         ("expiry_swept_at", str(time.time())))
            conn.commit()
        except _get_sqlite3().Error:
            pass

    def _evict_if_needed(self):
        """空间回收三步：过期行清扫（每小时节流）→ LRU 淘汰（按 payload 口径）→ VACUUM。"""
        with self._conn() as conn:
            limit_bytes = self._max_size_mb * 1024 * 1024
            if self._expiry_sweep_due(conn):
                conn.execute(
                    "DELETE FROM search_cache WHERE created_at + ttl < ?",
                    (time.time(),))
                conn.commit()
                self._mark_expiry_swept(conn)

            page_bytes = conn.execute(
                "SELECT (SELECT page_count FROM pragma_page_count)"
                "      * (SELECT page_size  FROM pragma_page_size)"
            ).fetchone()[0] or 0
            if page_bytes > limit_bytes:
                target_bytes = int(limit_bytes * 0.8)
                payload = conn.execute(
                    "SELECT COALESCE(SUM(LENGTH(value_blob)), 0) FROM search_cache"
                ).fetchone()[0] or 0
                while payload > target_bytes:
                    rows = conn.execute(
                        "SELECT key, LENGTH(value_blob) FROM search_cache "
                        "ORDER BY accessed_at ASC LIMIT 50"
                    ).fetchall()
                    if not rows:
                        break
                    conn.executemany("DELETE FROM search_cache WHERE key = ?",
                                     [(k,) for k, _ in rows])
                    payload -= sum((sz or 0) for _, sz in rows)
                    conn.commit()

            if self._free_bytes(conn) > self._RECLAIM_FREE_RATIO * (page_bytes or 1):
                conn.execute("VACUUM")

    @staticmethod
    def _free_bytes(conn) -> int:
        """空闲页占用的字节数（O(1)，用于决定是否值得 VACUUM）。"""
        return conn.execute(
            "SELECT (SELECT freelist_count FROM pragma_freelist_count)"
            "      * (SELECT page_size      FROM pragma_page_size)"
        ).fetchone()[0] or 0

    def clear(self, older_than_hours: int = 24):
        if self._degraded_reason is not None:
            return
        with self._lock:
            cutoff = time.time() - older_than_hours * 3600
            with self._conn() as conn:
                conn.execute("DELETE FROM search_cache WHERE created_at < ?", (cutoff,))
                conn.commit()

    def find_similar(self, query: str, engine: str = "auto",
                     domain: str = "general", limit: int = 50,
                     threshold: float = 0.7,
                     mode: str = "auto", depth: str = "fast") -> list[dict]:
        """近重复查询软命中：扫描最近缓存，minhash 相似度 ≥ threshold 的条目。

        返回 [{key, query, similarity}]，按相似度降序。
        隔离契约：
          - engine 精确匹配（多引擎组合 a+b / a,b 不参与软命中）
          - 限定符集合必须一致，仅对载荷计算相似度
          - domain / mode / depth 精确匹配
        """
        nq = normalize_query(query)
        nq_quals, nq_payload = split_qualifiers(nq)
        base_len = len(nq_payload)
        if self._degraded_reason is not None:
            return []
        # engine 精确匹配（请求身份不同 → 结果不可互换，见上方 docstring）
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT key, query, engine, domain, ttl, created_at "
                    "FROM search_cache "
                    "WHERE domain = ? AND mode = ? AND depth = ? AND engine = ? "
                    "ORDER BY accessed_at DESC LIMIT ?",
                    (domain, mode, depth, engine, limit),
                ).fetchall()
        candidates = []
        for key, cached_q, cached_engine, cached_dom, ttl, created_at in rows:
            if not cached_q or cached_q == nq:
                continue
            if self._is_expired(created_at, ttl):
                continue
            # 多引擎请求不参与软命中：融合产物 ≠ 任何单引擎结果集。
            # `+` 是历史写法（engine 列曾存路由出来的组合），`,` 是现在
            # 请求身份的形状（`--engine a,b` 的 engine_request 就是原串），
            # 两个都挡——只挡 `+` 会让逗号多引擎请求偷偷进入软命中。
            if "+" in (cached_engine or "") or "," in (cached_engine or ""):
                continue
            cnq = normalize_query(cached_q)
            c_quals, c_payload = split_qualifiers(cnq)
            # 限定符隔离：过滤器不同 → 结果全集不同，任何相似度都不成立
            if c_quals != nq_quals:
                continue
            clen = len(c_payload)
            # 长度约束：差异过大（>50%）不可能是近重复
            if base_len > 0 and abs(clen - base_len) / max(base_len, 1) > 0.5:
                continue
            # 比载荷而非整串：限定符已在上面单独比对，留在串里只会稀释判据
            sim = query_similarity(nq_payload, c_payload)
            if sim >= threshold:
                candidates.append({
                    "key": key, "query": cached_q, "similarity": round(sim, 3),
                })
        candidates.sort(key=lambda x: -x["similarity"])
        return candidates

    @property
    def stats(self) -> dict:
        if self._degraded_reason is not None:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(self._hits + self._misses, 1), 3),
                "size_mb": 0.0,
                "entries": 0,
                "degraded": True,
                "degraded_reason": self._degraded_reason,
            }
        with self._lock:
            with self._conn() as conn:
                row = conn.execute("SELECT COUNT(*), SUM(LENGTH(value_blob)) FROM search_cache").fetchone()
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(self._hits + self._misses, 1), 3),
                "size_mb": round((row[1] or 0) / 1024 / 1024, 2),
                "entries": row[0] or 0,
            }

    @property
    def size_mb(self) -> float:
        # O(1) 页统计代替 O(rows) 全表 blob 扫描。MAX_DB_SIZE_MB 语义本就是
        # 「磁盘容量阈值」，新口径（文件占用）比旧口径（净 payload 求和）更贴原意；
        # 实测差异 ~50%（7.4MB vs 4.9MB），驱逐会更早触发一点，对 100MB 阈值无实质影响。
        # 旧实现每次 `_evict_if_needed` 冷启动 21.4ms、温 10ms；新实现 <0.1ms。
        if self._degraded_reason is not None:
            return 0.0
        with self._conn() as conn:
            row = conn.execute(
                "SELECT (SELECT page_count FROM pragma_page_count)"
                "      * (SELECT page_size  FROM pragma_page_size)"
            ).fetchone()
        return (row[0] or 0) / 1024 / 1024


# ── 双层缓存入口 ───────────────────────────────────────────────────────────────

# 空结果正缓存极短 TTL（秒）— 避免把失败固化成「无结果」；fetch URL 默认 1 小时
EMPTY_RESULT_TTL = 45
FETCH_DEFAULT_TTL = 3600

# 证据分在 fetch 条目里的子键名。做成模块级常量而不是类属性：类会被测试
# 用工厂函数替换（monkeypatch SearchCache），此时 `SearchCache.KEY` 取不到，
# 写入路径会静默失败——静默正是这类改动最难发现的地方。
FETCH_EVIDENCE_KEY = "evidence"

# 取数内容管线版本——它是 fetch 缓存键的一部分，不是元数据。
#
# 为什么必须进键：正文缓存按 URL 复用，但「同一个 URL 抓出来的正文长什么样」
# 取决于产出方式（提取算法、是否走内容协商、降级链顺序）。这些一改，旧条目
# 就不再是同一份内容。不进键的后果是——改进上线后，先前缓存过的 URL 继续返回
# 旧的低质正文，新旧并存且无法从外部诊断（用户只会觉得「时好时坏」）。
#
# 因此：凡改动正文产出方式，把这个数字 +1。旧条目自然失联、TTL 到期回收，
# 无需手工清库，也不会把「缓存没刷新」误判成「优化没生效」。
FETCH_PIPELINE_VERSION = 2


class LoginCacheRejected(ValueError):
    """登录态 / 不可缓存载荷禁止写入公共 SearchCache。"""


def is_login_partition_payload(payload: object) -> bool:
    """是否为登录态分区载荷（不得进入公共 unified-search 缓存）。

    判定（任一命中）：
      - login_state_used is True
      - cache_eligible is False
      - auth_partition 以 login 开头（如 login / login:zhihu.com）
      - source / engine 含 ego-browser / ego_browser（浏览器登录态检索）
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("login_state_used") is True:
        return True
    if payload.get("cache_eligible") is False:
        return True
    auth = payload.get("auth_partition")
    if isinstance(auth, str) and auth.lower().startswith("login"):
        return True
    for key in ("source", "engine", "backend"):
        val = payload.get(key)
        if isinstance(val, str):
            low = val.lower()
            if "ego-browser" in low or "ego_browser" in low:
                return True
    return False


def assert_cacheable(payload: object, *, context: str = "cache") -> None:
    """公共 SearchCache 写入守卫：登录态结果硬拒绝。

    登录态检索（ego-search 等）必须走独立分区或默认不缓存 body；
    禁止污染 ~/.cache/unified-search/cache.db。
    """
    if is_login_partition_payload(payload):
        raise LoginCacheRejected(
            f"{context}: login-partition / cache_eligible=false payload "
            "must not enter public SearchCache"
        )


def assert_results_cacheable(results: object, *, context: str) -> None:
    """逐条检查结果列表——**不抽样**。

    2026-09-19 修复：守卫此前只覆盖顶层载荷，于是两条真实缺口：

      - `SearchCache.set`（combo，主写入路径）对 `results[]` **一条都不查**——
        实测把 `login_state_used: True` 放在结果列表第 1 条，照样写进公共库；
      - `set_engine` 只查 `results[:3]`，第 4 条起不查。

    「硬拒绝」被降级成「抽样拒绝」，而登录态标记的生产者是存在的
    （candidate_envelope / plan 会在**逐条**结果上写 login_state_used /
    auth_partition / cache_eligible）。条目数本来就有上限（几十条），
    逐条检查的代价可忽略，没有理由抽样。
    """
    if not isinstance(results, list):
        return
    for item in results:
        if isinstance(item, dict):
            assert_cacheable(item, context=context)


class SearchCache:
    """
    双层缓存引擎：L1 LRU + L2 SQLite

    缓存键（v2.4.1）= SHA256(kind|norm_query|engine|domain|mode|depth)[:32]
      - query 归一化后入 key（空白/大小写）
      - 不含 max_results：支持柔性命中（cached_n >= requested_n 可截断返回）
      - depth / mode 隔离，防 fast/deep、budget 污染
      - 时效敏感 query 强制 TTL ≤ REALTIME_TTL_CAP
      - 登录态载荷（login_state_used / cache_eligible=false / ego-browser）
        在 set / set_engine / set_fetch 入口硬拒绝，与公共缓存隔离

    分层：
      combo 结果 / per-engine 结果 / fetch URL（前缀区分）
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH, ttl: int = DEFAULT_TTL):
        cfg = get_cache_config()
        # 允许测试传入显式 db_path 覆盖配置
        if db_path != DEFAULT_DB_PATH:
            self._db_path = os.path.expanduser(db_path)
        else:
            # ARGO_STATE_DIR 是硬开关，不能被磁盘 config.yaml 的 db_path 盖掉；
            # 未设置时才尊重 config 的显式配置。
            self._db_path = str(argo_paths.db_path())
        self._ttl = cfg.get("ttl", ttl)
        # config.yaml 的 `cache.enabled` / `cache.max_size_mb` 此前是**死配置**：
        # 两个键写在配置里、默认值也定义了，代码却从不读——关缓存关不掉、改上限
        # 不生效，只能改源码。现在两处都接线（2026-10-15）。
        self._enabled = bool(cfg.get("enabled", True))
        self._max_size_mb = cfg.get("max_size_mb")
        self._l1 = LRUCache(max_size=MAX_MEMORY_ITEMS)
        self._l2 = SQLiteCache(db_path=self._db_path, ttl=self._ttl,
                               max_size_mb=self._max_size_mb)
        self._miss_memo: tuple[float, frozenset[str]] = (0.0, frozenset())

    @staticmethod
    def _key(query: str, engine: str, max_results: int = 0, domain: str = "general",
             mode: str = "auto", depth: str = "fast", kind: str = "combo",
             since: str | None = None, until: str | None = None,
             **vdom) -> str:
        """生成缓存键。max_results 不参与 key（柔性命中）；kind 区分 combo/engine/fetch。"""
        raw = f"{kind}|{normalize_query(query)}|{engine}|{domain}|{mode}|{depth}"
        for tag, val in (("since", since), ("until", until), *cache_key_vdom(vdom)):
            if val:
                raw += f"|{tag}={val}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def resolve_ttl(domain: str = "general", query: str | None = None) -> int:
        """根据 domain（及可选 query 时效信号）返回基础 TTL（秒）。"""
        tier = DOMAIN_TIER_MAP.get(domain, "general")
        ttl = CACHE_TIERS.get(tier, DEFAULT_TTL)
        if query and is_freshness_sensitive_query(query):
            ttl = min(ttl, REALTIME_TTL_CAP)
        # 时效域硬上限，防止调用方传入超长 base_ttl 后绕过
        if tier in ("financial", "news", "realtime"):
            cap = {"financial": 300, "news": 600, "realtime": REALTIME_TTL_CAP}[tier]
            ttl = min(ttl, cap)
        return ttl

    @staticmethod
    def _seconds_until_end_of_day() -> int:
        """距本地当天 23:59:59 的剩余秒数（aware 而非 naive 是为 DST 切换日）。"""
        import datetime
        now = datetime.datetime.now().astimezone()
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return max(int((end_of_day - now).total_seconds()), 60)

    def _resolve_effective_ttl(self, domain: str, base_ttl: int | None = None,
                               query: str | None = None) -> int:
        """解析有效 TTL：仅 research/evergreen 可日末延长；时效 query 强制 cap。"""
        tier = DOMAIN_TIER_MAP.get(domain, "general")
        if base_ttl is not None:
            ttl = base_ttl
        else:
            ttl = self.resolve_ttl(domain, query=query)

        # 时效敏感查询：禁止日末延长，硬 cap
        if query and is_freshness_sensitive_query(query):
            return min(ttl, REALTIME_TTL_CAP)

        if tier in ("financial", "news", "realtime"):
            cap = {"financial": 300, "news": 600, "realtime": REALTIME_TTL_CAP}[tier]
            return min(ttl, cap)

        if tier in SAME_DAY_ELIGIBLE_TIERS and base_ttl is None:
            return max(ttl, self._seconds_until_end_of_day())
        return ttl

    def _disabled(self) -> bool:
        """`cache.enabled: false` —— 整层缓存停用。

        语义是「当没有缓存」而不是「有缓存但不写」：读恒 miss（每次调用都
        真的去打上游）、写一律丢弃、不落盘。关掉写却不关读最危险——旧条目会
        在 TTL 内继续被交付，用户以为「缓存已关」但拿到的仍是陈货。
        """
        return not self._enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _read(self, key: str) -> Optional[dict]:
        if self._disabled():
            return None
        # 返回浅拷贝 + results 列表拷贝：缓存持有数据的所有权，下游对 results
        # 的原地改写（如 _engine 标记、rerank 字段）不得污染 store。
        # 实测：50KB payload 的 deepcopy 约 5-15ms，浅拷贝 <0.5ms。
        hit = self._l1.get(key)
        if hit is not None:
            ttl = hit.get("_ttl", 0)
            if ttl > 0 and time.time() - hit.get("_ts", 0) < ttl:
                out = dict(hit)
                out["results"] = [dict(r) if isinstance(r, dict) else r
                                   for r in (hit.get("results") or [])]
                out["_cache_level"] = engine_states.CACHE_LEVEL_L1
                return out
            self._l1.remove(key)
        if time.monotonic() - self._miss_memo[0] < 0.05 and key in self._miss_memo[1]:
            return None

        hit = self._l2.get(key)
        if hit is not None:
            self._l1.set(key, hit)
            out = dict(hit)
            out["results"] = [dict(r) if isinstance(r, dict) else r
                              for r in (hit.get("results") or [])]
            out["_cache_level"] = engine_states.CACHE_LEVEL_L2
            return out
        return None

    def _read_many(self, keys: list[str]) -> dict[str, dict]:
        if not keys or self._disabled():
            return {}
        out, miss, now = {}, [], time.time()
        for k in dict.fromkeys(keys):
            hit = self._l1.get(k)
            if hit is not None and hit.get("_ttl", 0) > 0 and now - hit.get("_ts", 0) < hit.get("_ttl", 0):
                out[k] = hit
            else:
                if hit is not None:
                    self._l1.remove(k)
                miss.append(k)
        if miss:
            l2_hits = self._l2.get_many(miss)
            for k, hit in l2_hits.items():
                self._l1.set(k, hit)
                out[k] = hit
            self._miss_memo = (time.monotonic(), frozenset(k for k in miss if k not in l2_hits))
        return out

    def _write(self, key: str, query: str, engine: str, max_results: int,
               value: dict, domain: str, ttl: int,
               mode: str = "", depth: str = "") -> None:
        if self._disabled():
            return
        self._miss_memo = (0.0, frozenset())
        payload = {**value, "_domain": domain, "_ttl": ttl, "_ts": time.time(),
                   "_max_results": max_results}
        self._l1.set(key, payload)
        self._l2.set(key, query, engine, max_results, payload, domain=domain,
                     ttl=ttl, mode=mode, depth=depth)

    @staticmethod
    def _soft_slice(hit: dict, max_results: int) -> Optional[dict]:
        """柔性命中：缓存条数足够则截断返回；不足则 miss 以便升级拉取。"""
        results = hit.get("results")
        if results is None:
            # fetch 等非 results 形态直接返回
            return hit
        cached_n = int(hit.get("_max_results") or len(results) or 0)
        if cached_n >= max_results or len(results) >= max_results:
            out = dict(hit)
            out["results"] = list(results)[:max_results]
            out["count"] = len(out["results"])
            out["_soft_hit"] = True
            return out
        return None  # 需要更多条 → 视为 miss

    def get(self, query: str, engine: str, max_results: int,
            domain: str = "general", mode: str = "auto",
            depth: str = "fast") -> Optional[dict]:
        """先查 L1，未命中再查 L2。支持 depth 隔离 + max_results 柔性命中。"""
        if self._disabled():
            # 提前返回：_read 已被 guard 挡住，但下面的语义软命中会直接问 L2，
            # 不在这里拦就会「读返回 None 却仍交出一条旧缓存」。
            return None
        key = self._key(query, engine, max_results, domain, mode, depth, kind="combo")
        hit = self._read(key)
        if hit is None:
            # 语义软命中：精确 miss 时，minhash 找近重复查询的缓存
            if domain != "general" or mode == "auto":
                try:
                    similar = self._l2.find_similar(query, engine, domain,
                                                    mode=mode, depth=depth)
                    for cand in similar:
                        s_hit = self._l2.get(cand["key"])
                        if s_hit is None:
                            continue
                        sres = s_hit.get("results")
                        if isinstance(sres, list) and not sres:
                            # 负缓存（空结果条目）不进软命中：那是**别的查询**
                            # 的瞬时失败，交出去后上层拿到 cached=true、count=0，
                            # 无法区分「本查询没结果」与「别的查询失败了」
                            # （2026-10-17 实测：sim=0.75 时「苹果 2025 营收」
                            # 会软命中「苹果 2025 年营收」的负缓存）。
                            continue
                        sliced = self._soft_slice(s_hit, max_results)
                        if sliced is not None:
                            out = dict(sliced)
                            out["_cache_level"] = engine_states.CACHE_LEVEL_L2
                            out["_semantic_hit"] = True
                            out["_semantic_similarity"] = cand["similarity"]
                            out["_semantic_query"] = cand["query"]
                            # L1 只回填「本请求 key 视角」的载荷（含来源与软命中标记），
                            # 不回填他人原始 s_hit：后者既无归属标记也无 TTL 约束，
                            # 会在 L2 过期后继续以本 key 的身份存活，把一次软命中
                            # 固化成硬命中（v2.4.2 修复）。
                            self._l1.set(key, out)
                            return out
                except IO_BENIGN:
                    pass
            return None
        sliced = self._soft_slice(hit, max_results)
        return sliced

    def set(self, query: str, engine: str, max_results: int, results: dict,
            domain: str = "general", ttl: int | None = None, mode: str = "auto",
            depth: str = "fast", recovered: bool = False):
        """写入双层缓存。空结果强制短 TTL；时效 query 强制 cap。

        自适应 TTL：内容稳定的查询自动延长 TTL（上限为域 TTL），
        内容频繁变化则保持短 TTL，兼顾命中率与新鲜度。
        登录态 / cache_eligible=false 载荷硬拒绝（LoginCacheRejected）。

        recovered=True（恢复链成功产出的批次）：整批退化时不硬拒，降级为
        短 TTL 写入（RECOVERED_DEGRADED_TTL）——理由见下方守卫块注释。
        """
        assert_cacheable(results, context="SearchCache.set")
        # engine 名本身也可能标记登录态源
        assert_cacheable({"engine": engine, "source": engine}, context="SearchCache.set")
        result_list = results.get("results") if isinstance(results, dict) else None
        # 逐条查：登录态标记的生产者在**逐条**结果上（见 assert_results_cacheable）
        assert_results_cacheable(result_list, context="SearchCache.set")
        # 退化守卫：上游抖动返回的残次品不得固化（见 is_degraded_results）。
        # 例外（2026-10-17）：recovered 批次不整批拒写——硬拒把「难查询的恢复
        # 成果」与「上游抖动残次品」一同挡在缓存外，重复查询 miss 后每次重付
        # dispatch+恢复链全程（实测同查询 21s 内 3 次恢复链，越难查越进不去
        # 缓存的结构性循环）。降级为短 TTL：重复查询有命中，低相关不长期固化。
        recovered_degraded = False
        try:
            assert_not_degraded(result_list, context="SearchCache.set")
        except DegradedCacheRejected:
            if not recovered:
                raise
            recovered_degraded = True
        # 失败态守卫（2026-09-29）：缺密钥/熔断/鉴权/配额且无有效结果不写负缓存
        assert_not_failed_state(results, context="SearchCache.set")
        is_empty = isinstance(result_list, list) and len(result_list) == 0
        key = self._key(query, engine, max_results, domain, mode, depth, kind="combo")
        if is_empty:
            # 瞬时失败不得销毁好数据（2026-09-19 修复）。
            #
            # EMPTY_RESULT_TTL 的意图是「别把一次失败固化成『这个查询没结果』」，
            # 但写入走的是 INSERT OR REPLACE——于是同键上一条还有一小时寿命的
            # 有效缓存，会被一次网络抖动产生的空结果整条覆盖掉（实测复现）。
            # 负缓存仍然要留（那正是短 TTL 的用途），只是**不覆盖已有活条目**：
            # 有活条目说明上一次取到了东西，它比这次的失败更可信。
            if self._l2.has_live(key):
                return
            effective_ttl = EMPTY_RESULT_TTL if ttl is None else min(ttl, EMPTY_RESULT_TTL)
        else:
            effective_ttl = self._resolve_effective_ttl(domain, ttl, query=query)
            effective_ttl = self._adaptive_ttl(
                query, engine, domain, effective_ttl, result_list,
                mode=mode, depth=depth,
            )
            if recovered_degraded:
                effective_ttl = min(effective_ttl, RECOVERED_DEGRADED_TTL)
        self._write(key, query, engine, max_results, results, domain, effective_ttl,
                    mode=mode, depth=depth)

    def _adaptive_ttl(self, query: str, engine: str, domain: str,
                      base_ttl: int, result_list: list,
                      mode: str = "auto", depth: str = "fast") -> int:
        """自适应 TTL：对比同查询旧缓存内容哈希，稳定则延长 TTL。

        内容稳定（哈希一致）→ TTL 延长到 base_ttl * 2（上限域 TTL）；
        内容变化 → 保持 base_ttl。仅对非时效查询生效，避免影响新鲜度。

        旧键必须与实际写入键同 mode/depth，否则 fast/budget 等模式下
        永远查不到上一轮缓存，自适应延长静默失效。
        """
        if not query or is_freshness_sensitive_query(query):
            return base_ttl
        if not result_list:
            return base_ttl
        try:
            key = self._key(query, engine, 0, domain, mode, depth, kind="combo")
            old = self._l2.get(key)
            if old is None:
                return base_ttl
            old_results = old.get("results") if isinstance(old, dict) else None
            if not isinstance(old_results, list) or not old_results:
                return base_ttl
            # 内容指纹：title+url 前 N 条
            def _fp(rs: list) -> tuple:
                return tuple(
                    (r.get("url", ""), (r.get("title", "") or "")[:50])
                    for r in rs[:5]
                )
            if _fp(old_results) == _fp(result_list):
                cap = {"financial": 300, "news": 600, "realtime": REALTIME_TTL_CAP}.get(
                    DOMAIN_TIER_MAP.get(domain, "general"))
                # 稳定 → 延长 2×base；有硬帽层不超帽、不反向压缩日末延长；无帽层直接 2×
                return min(base_ttl * 2, max(base_ttl, cap)) if cap else base_ttl * 2
            return base_ttl
        except SHAPE_BENIGN:
            return base_ttl

    # ── per-engine 结果缓存 ──────────────────────────────────────────────────

    def get_engine(self, query: str, engine: str, max_results: int,
                   domain: str = "general", mode: str = "auto",
                   depth: str = "fast", since: str | None = None,
                   until: str | None = None, **vdom) -> Optional[list]:
        key = self._key(query, engine, max_results, domain, mode, depth, kind="engine",
                        since=since, until=until, **vdom)
        hit = self._read(key)
        if hit is None:
            return None
        sliced = self._soft_slice(hit, max_results)
        if sliced is None:
            return None
        return list(sliced.get("results") or [])

    def set_engine(self, query: str, engine: str, max_results: int,
                   results: list, domain: str = "general", mode: str = "auto",
                   depth: str = "fast", ttl: int | None = None,
                   since: str | None = None, until: str | None = None, **vdom):
        assert_cacheable({"engine": engine, "source": engine}, context="SearchCache.set_engine")
        assert_results_cacheable(results, context="SearchCache.set_engine")
        assert_not_degraded(results, context="SearchCache.set_engine")
        is_empty = not results
        key = self._key(query, engine, max_results, domain, mode, depth, kind="engine",
                        since=since, until=until, **vdom)
        if is_empty and self._l2.has_live(key):
            # 瞬时空结果不得覆盖同键仍有寿命的好条目（同族：combo 层 2026-09-19 修复）
            return
        if is_empty:
            effective_ttl = EMPTY_RESULT_TTL if ttl is None else min(ttl, EMPTY_RESULT_TTL)
        else:
            effective_ttl = self._resolve_effective_ttl(domain, ttl, query=query)
        self._write(key, query, engine, max_results, {"results": results}, domain,
                    effective_ttl, mode=mode, depth=depth)

    # ── fetch URL 缓存 ───────────────────────────────────────────────────────

    def get_fetch(self, url: str) -> Optional[dict]:
        return self._read(self._fetch_key(url))

    def set_fetch(self, url: str, payload: dict, ttl: int = FETCH_DEFAULT_TTL):
        """写入 fetch 缓存。登录态正文硬拒绝，防止同 URL 登录页污染公共库。"""
        assert_cacheable(payload, context="SearchCache.set_fetch")
        self._write(self._fetch_key(url), url, "fetch", 0, payload, "fetch", ttl)

    # 取数可用性：把「取不到」与「取到了但没用」分开。
    #
    # 依据是同一批本地记录——正文字段来自 fetch 条目，反面字段同样来自它。
    # 分流的理由：调用方对这两种失败要做的事完全不同。
    #   blocked（系统类：robots / 明确拒绝）→ 换源，别在它身上耗
    #   poor   （内容类：登录墙 / 付费墙 / JS 壳 / 极短）→ 换源或降置信
    # 两者都不该与「还没试过」混为一谈——实测 14% 的抓取建议指向 robots
    # 明令禁止的地址，搜索阶段就能判定，却仍然被当作「值得一抓」。
    _POOR_PAGE_TYPES = ("auth_wall", "paywall", "js_shell")
    _POOR_MIN_CHARS = 200

    def local_status(self, urls: list[str]) -> dict[str, dict]:
        """一次遍历给出每个 URL 的本地正文与取数可用性。零联网。

        返回 {url: {"body": {...}|None, "retrieval": {...}}}，只含本地有记录的 URL。
        """
        out: dict[str, dict] = {}
        if not urls or self._disabled():
            return out
        try:
            from fulltext_store import path_for as _ft_path
        except ImportError:
            _ft_path = None
        try:
            from robots_guard import known_blocked as _known_blocked
        except ImportError:
            _known_blocked = None
        try:
            hits = self._read_many([self._fetch_key(u) for u in urls if u])
        except IO_BENIGN + SHAPE_BENIGN:
            hits = {}

        for url in urls:
            if not url:
                continue
            hit = hits.get(self._fetch_key(url))
            entry: dict = {}

            # ① 反面记录：以前抓过、结果不可用
            if hit and not hit.get("success"):
                err = str(hit.get("error") or "")
                _robots = hit.get("fetch_method") == engine_states.FETCH_ROBOTS_BLOCKED
                ret = {"status": "blocked" if _robots else "poor",
                       "reason": "robots" if _robots
                       else (hit.get("fetch_method") or "fetch_failed")}
                if err:
                    ret["detail"] = err[:120]
                entry["retrieval"] = ret
            elif hit and hit.get("success"):
                pt = str(hit.get("page_type") or "")
                ln = int(hit.get("length") or 0)
                if pt in self._POOR_PAGE_TYPES:
                    entry["retrieval"] = {"status": "poor", "reason": pt,
                                          "detail": f"length={ln}"}
                elif ln < self._POOR_MIN_CHARS:
                    entry["retrieval"] = {"status": "poor", "reason": "too_short",
                                          "detail": f"length={ln}"}

            # ② 正面记录：本地已有正文
            cached = bool(hit and hit.get("success"))
            body: dict = {}
            if cached:
                body = {
                    "length": hit.get("length", 0),
                    "truncated": bool(hit.get("truncated")),
                    "full_length": hit.get("full_length", hit.get("length", 0)),
                }
                if hit.get("full_text_path"):
                    body["full_text_path"] = hit["full_text_path"]
            if _ft_path is not None and not body.get("full_text_path"):
                try:
                    fp = _ft_path(url, "text")
                    if fp.is_file():
                        body["full_text_path"] = str(fp)
                        if not cached:
                            body["truncated"] = False
                            body["full_length"] = fp.stat().st_size
                except OSError:
                    pass
            if body:
                body["source"] = ("cache+archive" if cached
                                  and "full_text_path" in body
                                  else "archive" if not cached else "cache")
                entry["body"] = body

            # ③ 只有「取不到」的判定才需要 robots 兜底：正文都不在，谈何可用
            if "retrieval" not in entry and _known_blocked is not None:
                try:
                    if _known_blocked(url) is True:
                        entry["retrieval"] = {"status": "blocked", "reason": "robots"}
                except Exception:  # 侧信道：robots 只读判定失败按「不知道」处理（规则见 except_sets）
                    pass

            if entry:
                out[url] = entry
        return out

    def _fetch_key(self, url: str) -> str:
        """正文缓存键。读写必须共用此函数——分头拼 key 会静默读写失联。

        管线版本经 mode 位并入（见 FETCH_PIPELINE_VERSION 的说明）。
        """
        return self._key(url, "fetch", 0, "fetch", "auto",
                         f"pv{FETCH_PIPELINE_VERSION}", kind="fetch")

    # ── URL 证据分：作为 fetch 条目的一个子键存放（2026-09-17 消融）
    #
    # 原本是独立的 kind（evidence），实测与 fetch 条目 **6/10 字段重复**、
    # 同一个 result 字典、同一时刻、同一套 TTL 映射各写一次。拆开的代价：
    #   - 两个存储 + 两条写路径 + 两条读路径 + 两份 TTL 映射
    #   - **可能失配**：fetch 缓存拒绝登录态内容时（assert_cacheable 抛错），
    #     evidence 仍在另一个 try 块里写成功 → 「证据在、正文不在」，
    #     于是 verify 永久跳过这个 URL，把没核验过的当成已核验过的。
    # 合并后 1:1 由构造保证。代价实测：冷 L1 下多读 20 条含正文的行
    # 约 +0.19 ms（1.52 → 1.71 ms），可忽略。
    EVIDENCE_KEY = FETCH_EVIDENCE_KEY

    def get_evidence(self, url: str) -> Optional[dict]:
        """读 URL 的正文级证据分（从 fetch 条目投影）。未命中返回 None。"""
        hit = self._read(self._fetch_key(url))
        ev = (hit or {}).get(self.EVIDENCE_KEY)
        if not ev:
            return None
        # 兼容旧形状：读侧统一带上 url，调用方无需知道存储位置
        return {**ev, "url": (hit or {}).get("url") or url}

    def set_evidence(self, url: str, evidence: dict, ttl: int | None = None):
        """把证据分并入该 URL 的 fetch 条目。

        没有 fetch 条目时**不写**——证据分描述的是「这份正文有多可信」，
        正文都不在，单独留一个分数只会让调用方误判「这条已核验过」。
        这正是合并要消除的那个失配。
        """
        if not evidence:
            return
        with self._l2._lock:
            hit = self._read(self._fetch_key(url))
            if not hit:
                return
            merged = {k: v for k, v in hit.items() if not str(k).startswith("_")}
            merged[self.EVIDENCE_KEY] = {k: v for k, v in evidence.items()
                                         if k not in ("url",) and not str(k).startswith("_")}
            merged["_max_chars"] = hit.get("_max_chars", 0)
            try:
                self._write(self._fetch_key(url), url, "fetch", 0, merged, "fetch",
                            ttl if ttl is not None else FETCH_DEFAULT_TTL)
            except IO_BENIGN + SHAPE_BENIGN:
                pass

    def clear(self, older_than_hours: int = 24):
        if self._disabled():
            return
        self._l2.clear(older_than_hours=older_than_hours)

    @property
    def stats(self) -> dict:
        if self._disabled():
            # 关缓存时命中断言恒为 0：报「0 命中率」比报一个没人写过的
            # l1/l2 计数更解释得通（否则用户会以为缓存坏了）。
            return {
                "enabled": False,
                "hits": 0, "misses": 0, "hit_rate": 0.0,
                "size_mb": 0.0, "entries": 0,
                "l1": self._l1.stats, "l2": self._l2.stats,
            }
        l1 = self._l1.stats
        l2 = self._l2.stats
        # 一次 _read 就是一次查找：L1 交付即命中，L1 没交付（未命中或已过期）
        # 才下探 L2；L2 也拿不到才算真 miss。所以 miss 数恒等于 l2["misses"]，
        # 命中数 = 总查找 − miss。
        #
        # 此前 hits 用 l1.hits + l2.hits、分母却用 l1.misses：L1 未命中的每一笔
        # 都会再记一次 l2 命中，于是分母里既有这一笔的 hit 又有它的 l1.miss，
        # 命中率被系统性压低——L2 越有效，报出来的数字越差。
        lookups = l1["hits"] + l1["misses"]
        misses = l2["misses"]
        hits = lookups - misses
        return {
            "enabled": True,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / max(lookups, 1), 3),
            "size_mb": l2["size_mb"],
            "entries": l2["entries"],
            "l1": l1,
            "l2": l2,
        }


# ── CLI 入口 ──────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="Unified Search v2 缓存管理")
    sub = parser.add_subparsers(dest="cmd")
    p_get = sub.add_parser("get")
    p_get.add_argument("query")
    p_get.add_argument("engine", nargs="?", default="auto")
    p_get.add_argument("max_results", nargs="?", type=int, default=5)
    p_get.add_argument("--domain", default="general")
    p_set = sub.add_parser("set")
    p_set.add_argument("query")
    p_set.add_argument("engine")
    p_set.add_argument("max_results", type=int)
    p_set.add_argument("value_json")
    p_set.add_argument("--domain", default="general")
    p_clear = sub.add_parser("clear")
    p_clear.add_argument("--older-than", type=int, default=24)
    sub.add_parser("stats")
    args = parser.parse_args()
    cache = SearchCache()
    if args.cmd == "get":
        hit = cache.get(args.query, args.engine, args.max_results, domain=args.domain)
        print(dumps({"hit": hit is not None, "data": hit}))
    elif args.cmd == "set":
        cache.set(args.query, args.engine, args.max_results, json.loads(args.value_json), domain=args.domain)
        print('{"ok": true}')
    elif args.cmd == "clear":
        cache.clear(older_than_hours=args.older_than)
        print('{"ok": true}')
    elif args.cmd == "stats":
        print(dumps(cache.stats))
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()

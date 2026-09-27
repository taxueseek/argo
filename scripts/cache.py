#!/usr/bin/env python3
"""
cache.py — Unified Search v2 双层缓存引擎

功能：
  L1: 内存 LRU 热缓存（100 条），避免同进程重复查询
  L2: SQLite 持久化缓存（TTL 可配置），跨进程复用
  分级 TTL：financial / news / realtime / general / research / evergreen
  大值 gzip 压缩（> 1KB）
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import os
import re
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
from cache_guard import assert_not_degraded  # 退化写入守卫（2026-09-27 拆出）
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
COMPRESSION_THRESHOLD = 1024
COMPRESSION_LEVEL = 6

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
FRESHNESS_QUERY_RE = None  # 延迟编译，见 is_freshness_sensitive_query

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


def normalize_query(query: str) -> str:
    """缓存键用查询归一化：折叠空白、全半角空格、两端 trim、小写英文字母。

    不改变语义实体大小写敏感场景时仍用 lower；中文不受影响。
    目标：同一问句不同空白/大小写命中同一 key。
    """
    if not query:
        return ""
    # 全角空格 → 半角；连续空白折叠
    q = query.replace("\u3000", " ").strip()
    q = re.sub(r"\s+", " ", q)
    return q.casefold()


# ── 近重复查询检测（minhash 字符 n-gram）────────────────────────────────────

_NGRAM_N = 3
# 置换数。**看起来偏低，但不要单独调它**——它与下游软命中阈值 0.7 是一套
# 标定，动一个必须同时重标另一个，否则会把「检索上一年的缓存」放进来。
#
# 2026-09-19 实测记录（起因是「提到 32 更准」，结论是**保持 8 不改**）：
# K=8 的估计确实有偏且方差大——四条中文对的真实 Jaccard 是
# 0.275/0.444/0.500/0.375，K=8 估成 0.250/0.750/0.375/0.375（最大误差 0.306，
# 双向发散）；换 32 后为 0.281/0.438/0.562/0.375，明显贴住真值。
# 成本也不是障碍：`_signature` 带 lru_cache，比对是逐位比较（实测 0.41us/次，
# 不随 K 变），提高 K 只增加一次性签名成本（K=8:0.171ms、K=32:0.832ms 每条
# 查询，相对 1000–3000ms 的单次搜索可忽略）。
#
# 拦路石在**阈值**：0.7 正是照着 K=8 的偏高估计定的（下方 find_similar 的
# docstring 写「单字差异约 0.75」，而真值只有 0.444）。把 K 提到 32 后，要维持
# 原有软命中范围就得把阈值降到 ~0.35，而实测该阈值下
#     苹果 2025 营收 ↔ 苹果 2024 营收 = 0.469 ≥ 0.35 → 命中
# 即「今年的查询会命中去年的缓存」。这比「估计偏一点」严重得多——软命中的
# 前提是结果集可互换，年份不同则不可互换。两侧一起保持原状。
#
# 要走这条路，正确顺序是先换能区分数字/年份的判据（词级或数字感知 shingle），
# 再重标阈值；单独提 K 只是把偏差从「估计层」搬到「阈值层」。
_MINHASH_PERM = 8  # 置换数（越多越准，越少越快；8 对查询级足够）


def _ngrams(s: str, n: int = _NGRAM_N) -> set[str]:
    """字符级 n-gram（中文按字，英文按字符，无需分词）。"""
    s = re.sub(r"\s+", "", s)
    return {s[i:i + n] for i in range(max(len(s) - n + 1, 1))}


def _hash_token(t: str, seed: int) -> int:
    """带种子的简单哈希（minhash 置换模拟）。"""
    h = seed * 1315423911
    for c in t:
        h = (h ^ ord(c)) * 1099511628211 & 0xFFFFFFFFFFFFFFFF
    return h


@functools.lru_cache(maxsize=2048)
def _signature(s: str) -> tuple[int, ...]:
    """查询的 minhash 签名：每个置换下全部 n-gram 的最小哈希。

    这是「查询 → 签名」的纯函数，按查询串记忆化。提出来的意义在于：
    `query_similarity(q1, q2)` 会被拿去和缓存里**多条**候选比对，旧写法每次
    都重算两边的签名——一次搜索实测 28,552 次 `_hash_token` 调用，绝大多数
    是同一批 token 的重复置换。签名化后每条查询的签名全进程只算一次，
    且比对本身从 O(K × |tokens|) 降到 O(K)（K=置换数=8）。
    长驻进程（MCP server）里同一查询反复比对时收益为常数级。
    """
    grams = _ngrams(s)
    if not grams:
        return ()
    return tuple(min(_hash_token(t, seed) for t in grams)
                 for seed in range(_MINHASH_PERM))


def query_similarity(q1: str, q2: str) -> float:
    """两查询的字符 n-gram minhash 近似 Jaccard 相似度（0-1）。

    中文「苹果 2025 营收」vs「苹果 2025 年营收」这类近重复查询
    会得到高相似度（>0.7），用于语义缓存软命中。

    实现 = 两个签名逐位置相等的比例（minhash 估计 Jaccard 的标准做法）。
    """
    a = _signature(q1)
    b = _signature(q2)
    if not a or not b:
        return 0.0
    hits = sum(1 for x, y in zip(a, b) if x == y)
    return hits / _MINHASH_PERM


# 结构化限定符（keywords:pi-package / site:zhihu.com / author:bcoe）。
# 值里不含 `/` 是刻意的：URL 的 `https:` 因此不被当成限定符——fetch/evidence
# 的 URL 近重复软命中（`.../x` 与 `.../x.md`，相似度 0.875）是既有的正确
# 行为，不能被这次修复误伤。键以字母开头则排除 `10:30` 这类时间写法。
_QUALIFIER_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_.-]*:[A-Za-z0-9_*.+-]+")


def split_qualifiers(query: str) -> tuple[frozenset[str], str]:
    """把查询拆成（限定符集合，载荷）。

    限定符是**过滤器**而不是内容：两条查询的限定符集合不同，结果全集就不同，
    再高的词面相似度也不构成可互换的理由。

    为什么要拆：字符 n-gram 相似度会被长公共前缀主导。实测
    `keywords:pi-package mcp` 与 `keywords:pi-package memory` 整串相似度
    0.875（远超 0.7 阈值），而判别词只占几个字符——两条不同的检索需求被判成
    近重复，软命中把 memory 的结果当成 mcp 的结果交了出去（2026-09-18 修复）。
    拆开后比的是载荷（`mcp` vs `memory`，相似度 0.000），判据恢复有效。
    """
    text = query or ""
    quals = frozenset(q.lower() for q in _QUALIFIER_RE.findall(text))
    payload = re.sub(r"\s+", " ", _QUALIFIER_RE.sub(" ", text)).strip()
    return quals, payload


def is_freshness_sensitive_query(query: str) -> bool:
    """检测查询是否时效敏感（今日/实时/盘中/快讯等）。"""
    global FRESHNESS_QUERY_RE
    if FRESHNESS_QUERY_RE is None:
        FRESHNESS_QUERY_RE = re.compile(
            r"(今日|今天|昨晚|昨夜|本周|本月|实时|即时|最新|刚刚|"
            r"盘中|盘前|盘后|快讯|直播|热点新闻|头条|"
            r"today|tonight|breaking|live\s*update|just\s*now|"
            r"right\s*now|this\s*(morning|week|month))",
            re.I,
        )
    return bool(FRESHNESS_QUERY_RE.search(query or ""))


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

    def __init__(self, db_path: str = DEFAULT_DB_PATH, ttl: int = DEFAULT_TTL):
        self._db_path = os.path.expanduser(db_path)
        self._ttl = ttl
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

    def _init_db(self):
        with self._connect() as conn:
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
            with self._connect() as conn:
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
            with self._connect() as conn:
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

    def set(self, key: str, query: str, engine: str, max_results: int,
            value: dict, domain: str = "general", ttl: int | None = None,
            mode: str = "", depth: str = ""):
        if self._degraded_reason is not None:
            return
        with self._lock:
            blob, compressed = self._serialize(value)
            now = time.time()
            effective_ttl = ttl if ttl is not None else self._ttl
            with self._connect() as conn:
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
    # 定期清扫后是否 VACUUM 的门槛：空闲页到这个量才值得付一次整库重写。
    _RECLAIM_MIN_BYTES = 4 * 1024 * 1024

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
        """空间回收三步：过期行 → LRU 淘汰 → VACUUM。

        **历史 bug（2026-09-19 复现）**：判定读的是文件页数
        （page_count × page_size），扣减的却是 payload 净长。而本库
        `auto_vacuum=0` 且全仓无 VACUUM，页数**永不下降**——库一旦超过
        MAX_DB_SIZE_MB，`while total_bytes > target_bytes` 就永不收敛：
        每次 set() 把整库（含刚写入的那一行）删空，此后每次写入重复清空，
        缓存永久 100% miss。修法是让扣减与判定同源——进入淘汰循环后一律
        用 payload 口径，删完再 VACUUM 把文件真正缩回去（不 VACUUM 的话
        下一轮 set() 会看到同样的页数并重删一遍，正是这个 bug 的另一半）。

        另一半是**过期行**：它们对任何调用方都不可见（get() 命中即删），
        实测线上 5521 行里 5444 行（98.6%）已过期、占 payload 的 98.3%。
        不回收不只是占盘——find_similar 与淘汰扫描都要扫过这些永不命中的行。
        按节流间隔回收，稳态每小时一次。
        """
        with self._connect() as conn:
            limit_bytes = MAX_DB_SIZE_MB * 1024 * 1024

            # 三条触发各自独立，代价都是 O(1)：
            #   过期清扫（节流到每小时一次，因为它要全表扫）
            #   LRU 淘汰（文件超限时）
            #   纯回收（空闲页够多时——清扫刚删完一大片就会命中这条，
            #          不把它独立出来，回收就会被节流挡到下一个清扫点，
            #          文件白停在高水位一小时）
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
                # 按最近访问时间淘汰最旧行；扣减口径与判定同源，循环必然收敛
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

            # 删行不会让文件变小：页进 freelist，文件停在高水位。不回收的话
            # 稳态下会留着一整块「只有空闲页」的库——实测线上 10.1MB 文件里
            # 活数据只有 0.12MB。空闲页不够多就不付 VACUUM 的整库重写代价。
            if self._free_bytes(conn) >= self._RECLAIM_MIN_BYTES:
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
            with self._connect() as conn:
                conn.execute("DELETE FROM search_cache WHERE created_at < ?", (cutoff,))
                conn.commit()

    def find_similar(self, query: str, engine: str = "auto",
                     domain: str = "general", limit: int = 50,
                     threshold: float = 0.7,
                     mode: str = "auto", depth: str = "fast") -> list[dict]:
        """近重复查询软命中：扫描最近缓存，minhash 相似度 ≥ threshold 的条目。

        返回 [{key, query, similarity}]，按相似度降序。用于语义缓存——
        「苹果 2025 营收」可软命中「苹果 2025 年营收」的缓存。
        扫描限制 limit 条最近查询，控制成本。

        阈值 0.7：中文字符级 n-gram 下，「营收」vs「年营收」这类
        单字差异相似度约 0.75，0.7 可捕捉近重复且排除无关查询（≈0）。

        engine 隔离（v2.4.2 修复）：SQL 必须按 engine 列过滤。此前形参收了
        engine 但 WHERE 子句只用 domain，软命中因此跨引擎串味——实测
        `--engine v2ex` 可命中 bilibili 缓存；fetch 与 evidence 同 domain 下
        URL 词面相似（如 x 与 x.md，相似度 0.875）会互相串正文。
        组合键（多引擎拼接的 `a+b`）不参与软命中：组合结果集是融合产物，
        与任何单引擎缓存都不可互换。

        **engine 现在是精确匹配，不再是「auto 通配」**（2026-09-19 随之调整）：
        engine 列从「路由出来的引擎组合」改存「请求侧身份」（用户点名的引擎
        或 auto，见 search.execute_search 的 cache_engine_key 说明）。语义变了，
        通配的含义也跟着变——以前 `auto` 不是列里会出现的值，通配分支其实
        从未被走到（组合列要么是 `a+b` 要么是单引擎名）；现在 `auto` 是真实的
        请求身份，再通配就等于让「自动路由」的请求去吃「显式指定 pypi」的
        缓存，正是 v2.4.2 要挡的跨引擎串味。请求不同 → 结果不可互换。

        限定符隔离（2026-09-18 修复）：限定符是过滤器而非内容，整串相似度
        会被它主导——`keywords:pi-package mcp` 与 `keywords:pi-package
        memory` 整串相似度 0.875，判别词只占几个字符，于是软命中把另一条
        查询的结果交了出去（实测 `--engine npm` 查 mcp 拿到 memory 那批包）。
        现在先要求限定符集合相同，再比**载荷**的相似度；无限定符的查询载荷
        即整串，行为不变。

        mode/depth 隔离（2026-09-19 修复）：这是同一类串味的第三处，前两处
        （engine、限定符）修的时候漏了它。`_key` 刻意把 mode/depth 编进键、
        类 docstring 也承诺「depth / mode 隔离，防 fast/deep、budget 污染」，
        但软命中的 WHERE 只有 domain+engine：`--depth deep` 请求会软命中一条
        按 fast 写的条目（或反过来），拿到的结果集与请求档位不匹配，而结果里
        还报 `cached: true`。现在 WHERE 带上 mode/depth 精确匹配——它们本来
        就是「结果集全集的组成部分」，与限定符同理：档位不同，结果就不可互换。
        """
        nq = normalize_query(query)
        nq_quals, nq_payload = split_qualifiers(nq)
        base_len = len(nq_payload)
        if self._degraded_reason is not None:
            return []
        # engine 精确匹配（请求身份不同 → 结果不可互换，见上方 docstring）
        with self._lock:
            with self._connect() as conn:
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
            with self._connect() as conn:
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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT (SELECT page_count FROM pragma_page_count)"
                "      * (SELECT page_size  FROM pragma_page_size)"
            ).fetchone()
        return (row[0] or 0) / 1024 / 1024


# ── 双层缓存入口 ───────────────────────────────────────────────────────────────

# 空结果正缓存极短 TTL（秒）— 避免把失败固化成「无结果」
EMPTY_RESULT_TTL = 45
# fetch URL 默认 TTL
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
        self._l1 = LRUCache(max_size=MAX_MEMORY_ITEMS)
        self._l2 = SQLiteCache(db_path=self._db_path, ttl=self._ttl)

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

    def _read(self, key: str) -> Optional[dict]:
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
                out["_cache_level"] = "L1"
                return out
            self._l1.remove(key)

        hit = self._l2.get(key)
        if hit is not None:
            self._l1.set(key, hit)
            out = dict(hit)
            out["results"] = [dict(r) if isinstance(r, dict) else r
                              for r in (hit.get("results") or [])]
            out["_cache_level"] = "L2"
            return out
        return None

    def _write(self, key: str, query: str, engine: str, max_results: int,
               value: dict, domain: str, ttl: int,
               mode: str = "", depth: str = "") -> None:
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
                        sliced = self._soft_slice(s_hit, max_results)
                        if sliced is not None:
                            out = dict(sliced)
                            out["_cache_level"] = "L2"
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
            depth: str = "fast"):
        """写入双层缓存。空结果强制短 TTL；时效 query 强制 cap。

        自适应 TTL：内容稳定的查询自动延长 TTL（上限为域 TTL），
        内容频繁变化则保持短 TTL，兼顾命中率与新鲜度。
        登录态 / cache_eligible=false 载荷硬拒绝（LoginCacheRejected）。
        """
        assert_cacheable(results, context="SearchCache.set")
        # engine 名本身也可能标记登录态源
        assert_cacheable({"engine": engine, "source": engine}, context="SearchCache.set")
        result_list = results.get("results") if isinstance(results, dict) else None
        # 逐条查：登录态标记的生产者在**逐条**结果上（见 assert_results_cacheable）
        assert_results_cacheable(result_list, context="SearchCache.set")
        # 退化守卫：上游抖动返回的残次品不得固化（见 is_degraded_results）
        assert_not_degraded(result_list, context="SearchCache.set")
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
                # 稳定 → 延长（上限为 base 的 2 倍，不超域上限）
                return min(base_ttl * 2, self.resolve_ttl(domain, query=query) * 2)
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
        if is_empty:
            effective_ttl = EMPTY_RESULT_TTL if ttl is None else min(ttl, EMPTY_RESULT_TTL)
        else:
            effective_ttl = self._resolve_effective_ttl(domain, ttl, query=query)
        key = self._key(query, engine, max_results, domain, mode, depth, kind="engine",
                        since=since, until=until, **vdom)
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
        if not urls:
            return out
        try:
            from fulltext_store import path_for as _ft_path
        except ImportError:
            _ft_path = None
        try:
            from robots_guard import known_blocked as _known_blocked
        except ImportError:
            _known_blocked = None

        for url in urls:
            if not url:
                continue
            try:
                hit = self._read(self._fetch_key(url))
            except IO_BENIGN + SHAPE_BENIGN:
                hit = None
            entry: dict = {}

            # ① 反面记录：以前抓过、结果不可用
            if hit and not hit.get("success"):
                err = str(hit.get("error") or "")
                ret = {"status": "blocked" if hit.get("fetch_method") == "robots_blocked"
                       else "poor",
                       "reason": "robots" if hit.get("fetch_method") == "robots_blocked"
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
        self._l2.clear(older_than_hours=older_than_hours)

    @property
    def stats(self) -> dict:
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

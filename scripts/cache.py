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

import copy
import gzip
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Optional

try:
    from config import get_cache_config
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from config import get_cache_config


# ── 常量 ──────────────────────────────────────────────────────────────────────

DEFAULT_DB_PATH = "~/.cache/unified-search/cache.db"
DEFAULT_TTL = 3600
MAX_MEMORY_ITEMS = 100
MAX_DB_SIZE_MB = 100
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
    "english_tech": "research",
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


def query_similarity(q1: str, q2: str) -> float:
    """两查询的字符 n-gram minhash 近似 Jaccard 相似度（0-1）。

    中文「苹果 2025 营收」vs「苹果 2025 年营收」这类近重复查询
    会得到高相似度（>0.7），用于语义缓存软命中。
    """
    a = _ngrams(q1)
    b = _ngrams(q2)
    if not a or not b:
        return 0.0
    # minhash 估计 Jaccard：各置换下两集合最小哈希相等的比例
    hits = 0
    for seed in range(_MINHASH_PERM):
        if min(_hash_token(t, seed) for t in a) == min(_hash_token(t, seed) for t in b):
            hits += 1
    return hits / _MINHASH_PERM


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
        # :memory: 必须单连接（每次 connect(":memory:") 都是独立空库）
        self._mem_conn: sqlite3.Connection | None = None
        # :memory: / 空路径 / URI 无需建目录
        if self._db_path not in (":memory:", "") and not self._db_path.startswith("file:"):
            parent = os.path.dirname(self._db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        if self._db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", timeout=10)
            self._mem_conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
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
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_search_cache_expires ON search_cache(created_at);
                CREATE INDEX IF NOT EXISTS idx_search_cache_domain ON search_cache(domain);
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

    def get(self, key: str) -> Optional[dict]:
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
            value: dict, domain: str = "general", ttl: int | None = None):
        with self._lock:
            blob, compressed = self._serialize(value)
            now = time.time()
            effective_ttl = ttl if ttl is not None else self._ttl
            with self._connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO search_cache
                       (key, query, engine, max_results, domain, value_blob, compressed, ttl, created_at, accessed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, query, engine, max_results, domain, blob, compressed, effective_ttl, now, now),
                )
                conn.commit()
            self._evict_if_needed()

    def _evict_if_needed(self):
        """超过 100MB 时删除最旧的记录"""
        if self.size_mb <= MAX_DB_SIZE_MB:
            return
        with self._connect() as conn:
            target_mb = MAX_DB_SIZE_MB * 0.8
            while self.size_mb > target_mb:
                row = conn.execute("SELECT key FROM search_cache ORDER BY accessed_at ASC LIMIT 1").fetchone()
                if not row:
                    break
                conn.execute("DELETE FROM search_cache WHERE key = ?", (row[0],))
                conn.commit()

    def clear(self, older_than_hours: int = 24):
        with self._lock:
            cutoff = time.time() - older_than_hours * 3600
            with self._connect() as conn:
                conn.execute("DELETE FROM search_cache WHERE created_at < ?", (cutoff,))
                conn.commit()

    def find_similar(self, query: str, engine: str = "auto",
                     domain: str = "general", limit: int = 50,
                     threshold: float = 0.7) -> list[dict]:
        """近重复查询软命中：扫描最近缓存，minhash 相似度 ≥ threshold 的条目。

        返回 [{key, query, similarity}]，按相似度降序。用于语义缓存——
        「苹果 2025 营收」可软命中「苹果 2025 年营收」的缓存。
        扫描限制 limit 条最近查询，控制成本。

        阈值 0.7：中文字符级 n-gram 下，「营收」vs「年营收」这类
        单字差异相似度约 0.75，0.7 可捕捉近重复且排除无关查询（≈0）。
        """
        nq = normalize_query(query)
        base_len = len(nq)
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT key, query, domain, ttl, created_at "
                    "FROM search_cache WHERE domain = ? "
                    "ORDER BY accessed_at DESC LIMIT ?",
                    (domain, limit),
                ).fetchall()
        candidates = []
        for key, cached_q, cached_dom, ttl, created_at in rows:
            if not cached_q or cached_q == nq:
                continue
            if self._is_expired(created_at, ttl):
                continue
            clen = len(normalize_query(cached_q))
            # 长度约束：差异过大（>50%）不可能是近重复
            if base_len > 0 and abs(clen - base_len) / max(base_len, 1) > 0.5:
                continue
            sim = query_similarity(nq, normalize_query(cached_q))
            if sim >= threshold:
                candidates.append({
                    "key": key, "query": cached_q, "similarity": round(sim, 3),
                })
        candidates.sort(key=lambda x: -x["similarity"])
        return candidates

    @property
    def stats(self) -> dict:
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
        with self._connect() as conn:
            row = conn.execute("SELECT SUM(LENGTH(value_blob)) FROM search_cache").fetchone()
        return (row[0] or 0) / 1024 / 1024


# ── 双层缓存入口 ───────────────────────────────────────────────────────────────

# 空结果正缓存极短 TTL（秒）— 避免把失败固化成「无结果」
EMPTY_RESULT_TTL = 45
# fetch URL 默认 TTL
FETCH_DEFAULT_TTL = 3600


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
            self._db_path = os.path.expanduser(cfg.get("db_path", db_path))
        self._ttl = cfg.get("ttl", ttl)
        self._l1 = LRUCache(max_size=MAX_MEMORY_ITEMS)
        self._l2 = SQLiteCache(db_path=self._db_path, ttl=self._ttl)

    @staticmethod
    def _key(query: str, engine: str, max_results: int = 0, domain: str = "general",
             mode: str = "auto", depth: str = "fast", kind: str = "combo",
             since: str | None = None, until: str | None = None) -> str:
        """生成缓存键。max_results 不参与 key（柔性命中）；kind 区分 combo/engine/fetch。"""
        nq = normalize_query(query)
        raw = f"{kind}|{nq}|{engine}|{domain}|{mode}|{depth}"
        # 时间窗并入 key：同一 query 不同 since/until 不串缓存
        if since:
            raw += f"|since={since}"
        if until:
            raw += f"|until={until}"
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
        """计算距离当天 23:59:59 的剩余秒数。"""
        import datetime
        now = datetime.datetime.now()
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
        # 返回深拷贝：缓存持有数据的所有权，下游对 results 的原地改写
        # （如 search.py 的 _engine 标记、rerank/权威度字段）不得污染 store
        hit = self._l1.get(key)
        if hit is not None:
            ttl = hit.get("_ttl", 0)
            if ttl > 0 and time.time() - hit.get("_ts", 0) < ttl:
                out = copy.deepcopy(hit)
                out["_cache_level"] = "L1"
                return out
            self._l1.remove(key)

        hit = self._l2.get(key)
        if hit is not None:
            self._l1.set(key, hit)
            out = copy.deepcopy(hit)
            out["_cache_level"] = "L2"
            return out
        return None

    def _write(self, key: str, query: str, engine: str, max_results: int,
               value: dict, domain: str, ttl: int) -> None:
        payload = {**value, "_domain": domain, "_ttl": ttl, "_ts": time.time(),
                   "_max_results": max_results}
        self._l1.set(key, payload)
        self._l2.set(key, query, engine, max_results, payload, domain=domain, ttl=ttl)

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
                    similar = self._l2.find_similar(query, engine, domain)
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
                            self._l1.set(key, s_hit)
                            return out
                except Exception:
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
        is_empty = isinstance(result_list, list) and len(result_list) == 0
        if is_empty:
            effective_ttl = EMPTY_RESULT_TTL if ttl is None else min(ttl, EMPTY_RESULT_TTL)
        else:
            effective_ttl = self._resolve_effective_ttl(domain, ttl, query=query)
            effective_ttl = self._adaptive_ttl(
                query, engine, domain, effective_ttl, result_list,
                mode=mode, depth=depth,
            )
        key = self._key(query, engine, max_results, domain, mode, depth, kind="combo")
        self._write(key, query, engine, max_results, results, domain, effective_ttl)

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
        except Exception:
            return base_ttl

    # ── per-engine 结果缓存 ──────────────────────────────────────────────────

    def get_engine(self, query: str, engine: str, max_results: int,
                   domain: str = "general", mode: str = "auto",
                   depth: str = "fast", since: str | None = None,
                   until: str | None = None) -> Optional[list]:
        key = self._key(query, engine, max_results, domain, mode, depth, kind="engine",
                        since=since, until=until)
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
                   since: str | None = None, until: str | None = None):
        assert_cacheable({"engine": engine, "source": engine}, context="SearchCache.set_engine")
        if isinstance(results, list):
            for item in results[:3]:
                if isinstance(item, dict):
                    assert_cacheable(item, context="SearchCache.set_engine")
        is_empty = not results
        if is_empty:
            effective_ttl = EMPTY_RESULT_TTL if ttl is None else min(ttl, EMPTY_RESULT_TTL)
        else:
            effective_ttl = self._resolve_effective_ttl(domain, ttl, query=query)
        key = self._key(query, engine, max_results, domain, mode, depth, kind="engine",
                        since=since, until=until)
        self._write(key, query, engine, max_results, {"results": results}, domain, effective_ttl)

    # ── fetch URL 缓存 ───────────────────────────────────────────────────────

    def get_fetch(self, url: str) -> Optional[dict]:
        key = self._key(url, "fetch", 0, "fetch", "auto", "any", kind="fetch")
        return self._read(key)

    def set_fetch(self, url: str, payload: dict, ttl: int = FETCH_DEFAULT_TTL):
        """写入 fetch 缓存。登录态正文硬拒绝，防止同 URL 登录页污染公共库。"""
        assert_cacheable(payload, context="SearchCache.set_fetch")
        key = self._key(url, "fetch", 0, "fetch", "auto", "any", kind="fetch")
        self._write(key, url, "fetch", 0, payload, "fetch", ttl)

    def clear(self, older_than_hours: int = 24):
        self._l2.clear(older_than_hours=older_than_hours)

    @property
    def stats(self) -> dict:
        l1 = self._l1.stats
        l2 = self._l2.stats
        total_hits = l1["hits"] + l2["hits"]
        return {
            "hits": total_hits,
            "misses": l1["misses"],
            "hit_rate": round(total_hits / max(total_hits + l1["misses"], 1), 3),
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
        print(json.dumps({"hit": hit is not None, "data": hit}, ensure_ascii=False))
    elif args.cmd == "set":
        cache.set(args.query, args.engine, args.max_results, json.loads(args.value_json), domain=args.domain)
        print('{"ok": true}')
    elif args.cmd == "clear":
        cache.clear(older_than_hours=args.older_than)
        print('{"ok": true}')
    elif args.cmd == "stats":
        print(json.dumps(cache.stats, ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()

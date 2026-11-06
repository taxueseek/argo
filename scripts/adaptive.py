#!/usr/bin/env python3
"""
adaptive.py — Unified Search v2 自适应学习引擎

增强（v2）：
  - success × latency × cost 三维评分
  - 7天滑动窗口
  - SQLite 持久化（跨进程复用）
  - 预算模式感知（高 cost 引擎在 budget 模式下降权）

评分公式：
  score = success_rate × latency_factor × cost_factor × quality_factor
  latency_factor = min(1.0, 2000 / avg_latency_ms)  # 2s 内满分
  cost_factor = free=1.0, low=0.85, paid=0.6
  quality_factor = clamp(0.75 + 0.5 × avg_quality, 0.75, 1.25)  # 无质量数据时 = 1.0

quality（2026-09-27 接通）：结果相关度代理 ∈ [0,1]，由 search 层用查询词元
覆盖率零成本算好后回写。修复「只学快慢、不学好坏」盲区——`engine_perf`
的 `quality` 列自建库起就存在、却全仓无人写入也无人读取，1978 行实测数据
全为 0.0：反馈信号只有 success/latency/cost，于是「成功返回一堆无关结果」
与「成功返回精准结果」在评分上完全等价，而这是多数免密钥源的常态。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional
from cli_io import dumps

# ── 路径 ──────────────────────────────────────────────────────────────────────

def _state_dir() -> Path:
    """状态目录（惰性派生，支持 ARGO_STATE_DIR 覆盖）。"""
    import argo_paths
    return argo_paths.ensure_state_dir()


DB_DIR = _state_dir()  # 兼容旧引用
DB_PATH = DB_DIR / "adaptive.db"
WINDOW_DAYS = 7

# 数据过时阈值：窗口内有历史数据但最近一次调用距今超过该值，
# 视为历史快照失效（引擎可能已恢复健康），返回中性分让其重新进入组合。
# 被降权引擎不会出现在组合里 → 没有新记录 → 分数永远卡低（死锁），
# 此机制类似熔断器的 half-open 探测，是自适应学习的标准恢复通道。
STALE_AFTER_SECONDS = 24 * 3600

# ── 成本分级因子 ─────────────────────────────────────────────────────────────

COST_FACTORS = {"free": 1.0, "low": 0.85, "paid": 0.6}

# 质量因子的取值域：±25%。刻意不放大——质量是四个维度里最噪的一个
# （词元覆盖率只是相关度代理），让它单独颠覆 success×latency×cost 的
# 既有排序会把「一次词面巧合」变成引擎生死。先降权，不封杀。
QUALITY_FACTOR_MIN = 0.75
QUALITY_FACTOR_MAX = 1.25


def quality_factor(avg_quality: float | None, sum_quality: float | int | None) -> float:
    """质量因子：把窗口内平均相关度映射到 ±25% 的乘数。

    `sum_quality` 是**判定「有没有数据」的口径**，不是冗余参数：它取
    `SUM(quality)`，而空结果/未回写的行 quality=0（列的 DEFAULT 0.0），
    于是「平均值的分母」与「有数据的行数」并不一致——只有 sum>0 才能
    说明确有质量回写。若改用 `avg > 0` 判定，窗口内全是未回写的老数据时
    avg 恰为 0.0，会被误读成「质量极差」而把全仓引擎一次性降权
    （本次升级前正是这个状态：1978 行 quality 全 0.0）。故一律看 sum。

    - 无任何质量记录（sum=0）→ 1.0，中性，不惩罚存量数据
    - 差结果（avg≈0.1）→ 0.80
    - 好结果（avg≥0.8）→ 1.15
    """
    if not sum_quality:
        return 1.0
    avg = float(avg_quality or 0.0)
    return max(QUALITY_FACTOR_MIN,
               min(QUALITY_FACTOR_MAX, 0.75 + 0.5 * avg))


def marginal_contribution(merged: list[dict] | None, k: int,
                          participants: list[str]) -> dict[str, float]:
    """终榜边际贡献：每个参与引擎在融合后 top-k 里实际占了多少份额。

    为什么不用词面相关度：旧口径给每个引擎记 `result_relevance(query, res)`，
    五个源返回同一批结果时五个都拿高分——它奖励的是「看着相关」，不是
    「对最终答案有贡献」。这里改为覆盖博弈的 Shapley 值：top-k 每条结果
    价值 1，由 `consensus_engines` 里的引擎均分。独家结果整条归一个源，
    被五个源同时召回的结果每个源只分到 1/5，同质冗余源的分数因此自然下降。

    归一到「公平份额」：q = 0.5 × 份额占比 × 参与引擎数，截到 [0, 1]。恰好
    拿到公平份额（含单引擎查询）记 0.5，`quality_factor` 映射成 1.0，中性；
    两倍公平份额及以上记 1.0；没有一条进入 top-k 记 0.01（地板，原因见下）。两个源返回完全
    相同的结果时各记 0.5，冗余不加分。

    `merged` 为空时返回 {}：结果被后置过滤清空不是任何源的责任，调用方
    据此记 None（不参与质量评分）。纯内存计算，O(k × 共识数)。
    """
    top = [r for r in (merged or [])[:max(int(k or 0), 1)] if isinstance(r, dict)]
    names = [e for e in dict.fromkeys(participants or []) if e]
    if not top or not names:
        return {}
    share = dict.fromkeys(names, 0.0)
    for r in top:
        cons = [e for e in dict.fromkeys(r.get("consensus_engines") or []) if e]
        if not cons:
            src = r.get("_engine") or r.get("source") or ""
            cons = [src] if src else []
        for e in cons:
            if e in share:
                share[e] += 1.0 / len(cons)
    scale = 0.5 * len(names) / len(top)
    # 地板 0.01 而不是 0：`quality_factor` 以 SUM(quality)>0 判「有无质量数据」，
    # 一个从不进终榜的源若全记 0，会被当成「无数据」拿中性 1.0，反而逃过降权。
    return {e: round(max(0.01, min(1.0, v * scale)), 4) for e, v in share.items()}


def scenario_of(decision: dict | None) -> dict[str, str]:
    """从路由决策里抽 (domain, lang) 场景标签，供 record 分桶使用。

    独立成函数（而非写在 search_pipeline 的 record 调用里）是为了让
    search_pipeline 的 record 调用保持 3 行——它是祖父文件（行数上限门禁），
    把场景抽取塞进调用点会让它超限。
    """
    d = decision or {}
    feats = d.get("features") or {}
    return {
        "domain": str(d.get("domain") or ""),
        "lang": str(feats.get("primary_lang") or ""),
    }


class AdaptiveLearner:
    """自适应学习引擎：追踪引擎表现并输出推荐分数。"""

    # get_score 在 route 热路径上可能每请求多次调用；内存缓存 + 复用连接
    SCORE_CACHE_TTL = 30.0

    def __init__(self):
        self._lock = threading.Lock()
        self._local = threading.local()
        # (engine, domain, lang, purpose) -> (score, samples, expires_at)
        self._score_cache: dict[
            tuple[str, str, str, str], tuple[float, int, float]] = {}
        self._session_engines: set[str] = set()
        self._batch_depth = 0
        self._batch_dirty = False
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _prune_foreign(self, conn: sqlite3.Connection) -> None:
        """清理非配置残留引擎（与 quota._prune_foreign_engines 同构，保护本实例已写入名）。"""
        try:
            from quota import _config_engine_names
            known = _config_engine_names()
        except Exception:
            known = None
        if not known:
            return
        keep = known | getattr(self, "_session_engines", set())
        foreign = [r[0] for r in conn.execute("SELECT DISTINCT engine FROM engine_perf")
                   if r[0] not in keep]
        if foreign:
            ph = ",".join("?" for _ in foreign)
            conn.execute(f"DELETE FROM engine_perf WHERE engine IN ({ph})", tuple(foreign))

    @contextlib.contextmanager
    def batch(self):
        """合并多次 record 为单次 SQLite commit，保持 record 调用面不变。"""
        with self._lock:
            self._batch_depth = getattr(self, "_batch_depth", 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._batch_depth = max(0, getattr(self, "_batch_depth", 1) - 1)
                if self._batch_depth == 0 and getattr(self, "_batch_dirty", False):
                    conn = self._connect()
                    self._prune_foreign(conn)
                    conn.commit()
                    self._batch_dirty = False

    def _connect(self) -> sqlite3.Connection:
        """线程本地复用连接，避免 route 每次 open/close SQLite。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
        import argo_paths
        argo_paths.apply_state_pragmas(conn)
        self._local.conn = conn
        return conn

    def _init_db(self):
        conn = self._connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS engine_perf (
                engine TEXT NOT NULL,
                success INTEGER NOT NULL,
                latency_ms REAL NOT NULL,
                cost REAL NOT NULL DEFAULT 0.0,
                created_at REAL NOT NULL,
                empty INTEGER NOT NULL DEFAULT 0
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(engine_perf)")]
        if "created_at" not in cols:
            conn.execute("ALTER TABLE engine_perf ADD COLUMN created_at REAL DEFAULT 0")
        if "cost" not in cols:
            conn.execute("ALTER TABLE engine_perf ADD COLUMN cost REAL DEFAULT 0.0")
        if "empty" not in cols:
            # 老库补列：历史行 empty=0，等价于旧口径（空结果也算失败），
            # 不做回溯改写——我们无法知道那些行当时到底是「空」还是「错」。
            conn.execute("ALTER TABLE engine_perf ADD COLUMN empty INTEGER DEFAULT 0")
        if "quality" not in cols:
            # 同理补列。历史行 quality=0 表示「当时没回写质量」，不是「质量为零」——
            # 评分侧以 SUM(quality) > 0 判定有无数据，故这批行不会拉低任何引擎。
            conn.execute("ALTER TABLE engine_perf ADD COLUMN quality REAL DEFAULT 0.0")
        if "domain" not in cols:
            # 场景分桶（2026-10-24）：domain/lang 让学习器能表达「这个引擎在
            # A 域强、B 域弱」。历史行留空串 = 「未分桶」，回退全局分。
            conn.execute("ALTER TABLE engine_perf ADD COLUMN domain TEXT NOT NULL DEFAULT ''")
        if "lang" not in cols:
            conn.execute("ALTER TABLE engine_perf ADD COLUMN lang TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_perf_engine_time ON engine_perf(engine, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_perf_engine_scenario "
            "ON engine_perf(engine, domain, lang, created_at)"
        )
        cutoff = time.time() - WINDOW_DAYS * 86400
        conn.execute("DELETE FROM engine_perf WHERE created_at < ?", (cutoff,))
        self._prune_foreign(conn)
        conn.commit()

    def record(self, engine: str, success: bool, latency_ms: float, cost: float = 0.0,
               empty: bool = False, quality: float | None = None,
               domain: str = "", lang: str = ""):
        """记录一次引擎调用结果。

        ``domain`` / ``lang`` 是**可选场景标签**（默认空 = 不回退旧调用方）：
        给出时按 (engine, domain, lang) 分桶学习，让「引擎在 A 域强、B 域弱」
        可表达；不给时与旧口径完全一致（全局单桶）。见 ``get_score``。

        ``empty=True`` 表示「引擎正常，但这次查询它没有结果」——与「失败」是
        两回事，见 `get_score` 的说明。默认 False 保持旧调用方行为不变。

        ``quality`` 是本次结果的相关度代理 ∈ [0,1]（调用方用词元覆盖率算好
        回写，见 search_pipeline 的回填点）。None = 拿不到质量信号，此时
        落 0.0 而**不是**当作「质量为零」参与评分——`quality_factor` 以
        `SUM(quality) > 0` 判定有无数据，故未回写的行不会拖累任何引擎。
        """
        q = 0.0 if quality is None else max(0.0, min(1.0, float(quality)))
        dom = str(domain or "")[:64]
        lg = str(lang or "")[:16]
        with self._lock:
            if not hasattr(self, "_session_engines"):
                self._session_engines = set()
            self._session_engines.add(engine)
            conn = self._connect()
            conn.execute(
                "INSERT INTO engine_perf "
                "(engine, success, latency_ms, cost, created_at, empty, quality, domain, lang) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (engine, 1 if success else 0, latency_ms, cost, time.time(),
                 1 if empty else 0, q, dom, lg),
            )
            if getattr(self, "_batch_depth", 0) > 0:
                self._batch_dirty = True
            else:
                conn.commit()
            # 清掉该引擎的全部缓存键（含场景分桶键），见 get_score 的缓存口径
            for _k in [k for k in self._score_cache if k[0] == engine]:
                self._score_cache.pop(_k, None)

    def _score_from_row(self, row, purpose: str = "daily") -> float:
        """把一条聚合行转成 0~1 推荐分（口径见 get_score）。

        `purpose="research"` 把延迟悬崖从 2s 抬到 8s：2s 是**日常档的等待
        政策**（慢源不该定义第一响应），不是质量判断。研究场景里同一台源
        4s 交出的扎实证据值得等；沿用 2s 悬崖会让「慢而高质」的源在深研
        排序里系统性输给「快而泛」的源——正是 2026-10-26 审查发现的偏差。
        """
        total, judged, judged_ok, avg_latency, avg_cost, avg_q, sum_q, last_ts = row
        total = int(total or 0)
        judged = int(judged or 0)
        if not total:
            return 0.5
        last_ts = float(last_ts or 0)
        if last_ts and (time.time() - last_ts) > STALE_AFTER_SECONDS:
            return 0.5
        if judged == 0:
            return 0.5
        success_rate = (judged_ok or 0) / judged
        latency_cap = 8000.0 if purpose == "research" else 2000.0
        latency_factor = min(1.0, latency_cap / max(avg_latency or latency_cap, 1))
        cost_factor = max(0.3, 1.0 - (avg_cost or 0.0) * 10)
        return round(success_rate * latency_factor * cost_factor
                     * quality_factor(avg_q, sum_q), 4)

    def _global_row(self, engine: str):
        cutoff = time.time() - WINDOW_DAYS * 86400
        return self._connect().execute(
            "SELECT COUNT(*), "
            "       SUM(CASE WHEN empty = 0 THEN 1 ELSE 0 END), "
            "       SUM(CASE WHEN empty = 0 THEN success ELSE 0 END), "
            "       AVG(latency_ms), AVG(cost), "
            "       AVG(CASE WHEN empty = 0 THEN quality END), SUM(quality), MAX(created_at) "
            "FROM engine_perf WHERE engine = ? AND created_at > ?",
            (engine, cutoff),
        ).fetchone()

    def _bucket_row(self, engine: str, domain: str, lang: str):
        cutoff = time.time() - WINDOW_DAYS * 86400
        return self._connect().execute(
            "SELECT COUNT(*), "
            "       SUM(CASE WHEN empty = 0 THEN 1 ELSE 0 END), "
            "       SUM(CASE WHEN empty = 0 THEN success ELSE 0 END), "
            "       AVG(latency_ms), AVG(cost), "
            "       AVG(CASE WHEN empty = 0 THEN quality END), SUM(quality), MAX(created_at), "
            "       SUM(CASE WHEN empty = 1 THEN 1 ELSE 0 END) "
            "FROM engine_perf WHERE engine = ? AND created_at > ? "
            "AND domain = ? AND lang = ?",
            (engine, cutoff, domain, lang),
        ).fetchone()

    @staticmethod
    def heb_ucb_score(score: float, samples: int,
                      floor: float = 0.15, c_ucb: float = 0.35) -> float:
        """HEB-DCB 经验贝叶斯置信上界（防小样本垂直源饿死）。

        对尚未跌破死源地板（score >= floor）的低样本引擎补偿后验标准差：
          UCB(s, n) = s + c_ucb * sqrt(max(s*(1-s), 0.04) / (n + 3))
        随样本数 n 增长探索奖励以 1/sqrt(n) 自然衰减，死源（< floor）不获补偿。
        """
        if score < floor:
            return score
        import math
        n = max(0, int(samples or 0))
        var = max(score * (1.0 - score), 0.04)
        return round(min(1.0, score + c_ucb * math.sqrt(var / (n + 3.0))), 4)

    def get_score(self, engine: str, domain: str | None = None,
                  lang: str | None = None, purpose: str = "daily") -> float:
        """获取引擎的综合推荐分数（0.0 ~ 1.0）。

        **全局层**（不传 domain/lang）：只按 `empty=0` 算成功率，防止垂直源
        被跨域无召回查询误杀。

        **场景层**（给 `domain` / `lang`）：全局分 × 桶内健康度收缩 × 场景
        召回率，见 `_blend_bucket`。某源在特定场景持续空结果只压低该场景分。

        `quality` 列自 2026-11-07 起记的是终榜边际贡献（`marginal_contribution`），
        不再是词面相关度。
        """
        return self.get_score_with_samples(
            engine, domain, lang, purpose)[0]

    def get_score_with_samples(self, engine: str, domain: str | None = None,
                               lang: str | None = None,
                               purpose: str = "daily") -> tuple[float, int]:
        """同 get_score，额外返回有效样本数。

        选源层需要区分「0.5 分是因为没数据」与「0.5 分是因为确实平庸」：
        对无样本的源，实测先验应当做主，而不是被自适应默认分稀释。样本数
        取场景桶的有效观测数（桶为空时取全局 judged）。
        """
        key = (engine, domain or "", lang or "", purpose)
        now = time.time()
        cached = self._score_cache.get(key)
        if cached and cached[2] > now:
            return cached[0], cached[1]

        with self._lock:
            cached = self._score_cache.get(key)
            if cached and cached[2] > now:
                return cached[0], cached[1]
            grow = self._global_row(engine)
            score = self._score_from_row(grow, purpose)
            samples = int((grow[1] if grow else 0) or 0)
            if domain or lang:
                score, samples = self._blend_bucket(
                    score, samples,
                    self._bucket_row(engine, domain or "", lang or ""), purpose)

        self._score_cache[key] = (score, samples, now + self.SCORE_CACHE_TTL)
        return score, samples

    # 场景桶向全局分收缩的先验强度：桶内有效观测 n 条时桶的权重为 n/(n+2)。
    # n=1 → 0.33，n=3 → 0.6，n=6 → 0.75，样本越多越信桶，无需分段常数。
    BUCKET_PRIOR_N = 2.0
    # 场景召回的 Beta 先验：未观测时召回率视为 1（不先验地惩罚任何源）。
    RECALL_PRIOR_OK = 2.0

    def _blend_bucket(self, g_score: float, g_samples: int, brow,
                      purpose: str) -> tuple[float, int]:
        """场景桶分 = 健康度 H × 场景召回率 R，两个因子各自做经验贝叶斯收缩。

        - H：桶内有判定样本（非空结果）时，按 `BUCKET_PRIOR_N` 向全局分收缩；
          没有判定样本时 H 就是全局分。
        - R：Beta 后验均值 (a + 有结果次数) / (a + 有结果次数 + 空结果次数)。
          某源在这个 (domain, lang) 下持续空结果，只压低这一桶，全局分不动。

        替换此前的三条分支（w=0.3/0.5 固定权重、空结果另走 rho_recall 并把
        基准截到 0.5、w 封顶 0.75）。桶完全无行时 H=全局分、R=1，与只看全局
        逐位一致。
        """
        if not brow:
            return g_score, g_samples
        b_judged = int(brow[1] or 0)
        b_ok = int(brow[2] or 0)
        b_empty = int(brow[8] or 0) if len(brow) > 8 else 0
        if b_judged + b_empty == 0:
            return g_score, g_samples
        health = g_score
        if b_judged > 0:
            b_health = self._score_from_row(brow[:8], purpose)
            w = b_judged / (b_judged + self.BUCKET_PRIOR_N)
            health = (1 - w) * g_score + w * b_health
        a = self.RECALL_PRIOR_OK
        recall = (a + b_ok) / (a + b_ok + b_empty)
        return round(health * recall, 4), b_judged + b_empty

    def get_ranking(self) -> list[tuple[str, float]]:
        """获取所有引擎的推荐排名（降序）。空结果的排除口径同 `get_score`。"""
        with self._lock:
            cutoff = time.time() - WINDOW_DAYS * 86400
            conn = self._connect()
            rows = conn.execute(
                "SELECT engine, "
                "       SUM(CASE WHEN empty = 0 THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN empty = 0 THEN success ELSE 0 END), "
                "       AVG(latency_ms), AVG(cost), AVG(quality), SUM(quality) "
                "FROM engine_perf WHERE created_at > ? GROUP BY engine",
                (cutoff,),
            ).fetchall()

        results = []
        for engine, judged, judged_ok, avg_latency, avg_cost, avg_q, sum_q in rows:
            if not judged:
                continue
            success_rate = (judged_ok or 0) / judged
            latency_factor = min(1.0, 2000.0 / max(avg_latency or 2000, 1))
            cost_factor = max(0.3, 1.0 - (avg_cost or 0.0) * 10)
            score = round(success_rate * latency_factor * cost_factor
                          * quality_factor(avg_q, sum_q), 4)
            results.append((engine, score))

        results.sort(key=lambda x: -x[1])
        return results

    def get_stats(self) -> dict:
        """获取所有引擎的统计信息。"""
        cutoff = time.time() - WINDOW_DAYS * 86400
        conn = self._connect()
        rows = conn.execute(
            "SELECT engine, COUNT(*), AVG(success), AVG(latency_ms), AVG(cost), "
            "       AVG(quality), SUM(quality) "
            "FROM engine_perf WHERE created_at > ? GROUP BY engine",
            (cutoff,),
        ).fetchall()

        stats = {}
        for engine, total, avg_success, avg_latency, avg_cost, avg_q, sum_q in rows:
            # 质量相关度：avg 是窗口内均值；`quality_tracked` 标明这批数据
            # 里是否真的有质量回写——没有时 avg_q 恒为 0，看数会误读成
            # 「这个源质量极差」，而实际是「一直没测过」。
            stats[engine] = {
                "calls": total,
                "success_rate": round(avg_success or 0, 3),
                "avg_latency_ms": round(avg_latency or 0, 1),
                "avg_cost": round(avg_cost or 0, 6),
                "avg_quality": round(avg_q or 0, 3),
                "quality_tracked": bool(sum_q),
                "quality_factor": round(quality_factor(avg_q, sum_q), 3),
                "score": self.get_score(engine),
            }
        return stats

    def should_use(self, engine: str, threshold: float = 0.3) -> bool:
        """判断引擎是否值得使用（分数高于阈值）。"""
        return self.get_score(engine) >= threshold

    def latency_percentile(self, engine: str, p: float = 0.75,
                           min_samples: int = 5) -> Optional[float]:
        """窗口内「正常交付」延迟的分位数（毫秒）；样本不足/过时/无数据 → None。

        口径比 `get_score` 的 judged 更严（judged 仅 empty=0，这里是
        其子集）：只统计 **success=1 且 empty=0** 的
        行——「这个源在它能回答的查询上多快」。空结果的低延迟会把分位数拉低，
        但空结果并不解决对冲竞速（sufficiency 判据不放行），把它算进来会让
        宽限窗对「只会秒回空结果」的源收得过紧，反而多发对冲调用。

        用途（2026-10-11，调度微手术）：给对冲宽限窗一个「快引擎收窄、慢引擎
        不动」的自适应窗，见 engine_dispatch._adaptive_grace。只做读侧，不缓存
        ——每次编排至多一次查询（窗口内样本量小，全量取回后本地排序，免去依赖
        SQLite 版本的 percentile 扩展）。

        调用次数说明（2026-10-16 修订）：此处原文写「每次编排至多一次」，
        随 straggler_policy.straggler_tail 接入后不再成立——分层尾宽会在
        每次交付时刻对「还没回来的引擎」逐个取值（2 引擎编排约 1 次，多引
        擎编排最坏 ≈ 各次交付的 waiting 数之和）。默认关（ARGO_ADAPTIVE_TAIL）
        时不发生；开启后每引擎每次约 0.07ms（实测 200 次调用 warm），代价
        相对引擎 RTT 可忽略，但判「多不多」时按这个口径，别按旧句子。
        """
        if not engine:
            return None
        try:
            q = float(p)
        except (TypeError, ValueError):
            return None
        if not 0.0 < q < 1.0:
            return None
        now = time.time()
        cutoff = now - WINDOW_DAYS * 86400
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT latency_ms, created_at FROM engine_perf "
                "WHERE engine = ? AND created_at > ? "
                "  AND success = 1 AND empty = 0",
                (engine, cutoff),
            ).fetchall()
        if not rows or len(rows) < max(1, int(min_samples)):
            return None
        # 数据过时恢复：与 get_score 同判据——窗口内有行但最近一次调用距今
        # 过久，说明这张快照已不能代表引擎现状（可能已恢复/已换网络环境）。
        if (now - max(float(ts) for _, ts in rows)) > STALE_AFTER_SECONDS:
            return None
        lats = sorted(float(lat) for lat, _ in rows)
        idx = min(len(lats) - 1, max(0, int(round(q * (len(lats) - 1)))))
        return lats[idx]


# ── 模块级单例 ─────────────────────────────────────────────────────────────────

_learner: Optional[AdaptiveLearner] = None


def get_learner() -> AdaptiveLearner:
    global _learner
    if _learner is None:
        _learner = AdaptiveLearner()
    return _learner


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    learner = get_learner()
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(dumps(learner.get_stats()))
    elif len(sys.argv) > 1 and sys.argv[1] == "rank":
        ranking = learner.get_ranking()
        for engine, score in ranking:
            print(f"{engine:<15} {score:.4f}")
    else:
        print("用法: python3 adaptive.py stats|rank")

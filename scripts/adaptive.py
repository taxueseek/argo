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


class AdaptiveLearner:
    """自适应学习引擎：追踪引擎表现并输出推荐分数。"""

    # get_score 在 route 热路径上可能每请求多次调用；内存缓存 + 复用连接
    SCORE_CACHE_TTL = 30.0

    def __init__(self):
        self._lock = threading.Lock()
        self._local = threading.local()
        self._score_cache: dict[str, tuple[float, float]] = {}  # engine -> (score, expires_at)
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_perf_engine_time ON engine_perf(engine, created_at)"
        )
        cutoff = time.time() - WINDOW_DAYS * 86400
        conn.execute("DELETE FROM engine_perf WHERE created_at < ?", (cutoff,))
        conn.commit()

    def record(self, engine: str, success: bool, latency_ms: float, cost: float = 0.0,
               empty: bool = False, quality: float | None = None):
        """记录一次引擎调用结果。

        ``empty=True`` 表示「引擎正常，但这次查询它没有结果」——与「失败」是
        两回事，见 `get_score` 的说明。默认 False 保持旧调用方行为不变。

        ``quality`` 是本次结果的相关度代理 ∈ [0,1]（调用方用词元覆盖率算好
        回写，见 search_pipeline 的回填点）。None = 拿不到质量信号，此时
        落 0.0 而**不是**当作「质量为零」参与评分——`quality_factor` 以
        `SUM(quality) > 0` 判定有无数据，故未回写的行不会拖累任何引擎。
        """
        q = 0.0 if quality is None else max(0.0, min(1.0, float(quality)))
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO engine_perf "
                "(engine, success, latency_ms, cost, created_at, empty, quality) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (engine, 1 if success else 0, latency_ms, cost, time.time(),
                 1 if empty else 0, q),
            )
            conn.commit()
            self._score_cache.pop(engine, None)

    def get_score(self, engine: str) -> float:
        """获取引擎的综合推荐分数（0.0 ~ 1.0）。

        **只按「有明确结果」的调用算成功率**（`empty=0`）。

        为什么要把空结果排除：分数被用来决定「这个源该不该继续用」，
        而「这次查询它没东西」说明的是**查询与源不匹配**，不是源坏了。
        两者混在一起会误杀：实测 github 在窗口内 63 次调用、成功率 6%，
        失败归因全是 empty——它被用来回答各种查询，只有少数落进它的覆盖范围；
        而它却在 `local_code` / `package_search` 这些**声明要用它**的域里
        因分数低于 0.3 被整个剔除。同类还有 wikipedia(0.17)、
        openalex(0.29)、hackernews(0.10)、twitter(0.17)、open_library(0.17)。

        延迟与成本仍按全部调用统计——空结果也是真实开销。
        """
        now = time.time()
        cached = self._score_cache.get(engine)
        if cached and cached[1] > now:
            return cached[0]

        with self._lock:
            cached = self._score_cache.get(engine)
            if cached and cached[1] > now:
                return cached[0]
            cutoff = now - WINDOW_DAYS * 86400
            conn = self._connect()
            row = conn.execute(
                "SELECT COUNT(*), "
                "       SUM(CASE WHEN empty = 0 THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN empty = 0 THEN success ELSE 0 END), "
                "       AVG(latency_ms), AVG(cost), "
                "       AVG(quality), SUM(quality) "
                "FROM engine_perf WHERE engine = ? AND created_at > ?",
                (engine, cutoff),
            ).fetchone()
        total, judged, judged_ok, avg_latency, avg_cost, avg_q, sum_q = row
        total = int(total or 0)
        judged = int(judged or 0)
        if not total:
            score = 0.5  # 无数据时中性分
        else:
            # 数据过时恢复：最后一次调用距今过久 → 中性分，给恢复探测机会
            last_row = conn.execute(
                "SELECT MAX(created_at) FROM engine_perf WHERE engine = ? AND created_at > ?",
                (engine, cutoff),
            ).fetchone()
            last_ts = float(last_row[0] or 0)
            if last_ts and (now - last_ts) > STALE_AFTER_SECONDS:
                score = 0.5
            elif judged == 0:
                # 窗口内全是空结果：没有任何可判定健康与否的调用 → 中性分。
                # 给中性分而不是 0：否则「每次都只是没搜到」会把源打死。
                score = 0.5
            else:
                success_rate = (judged_ok or 0) / judged
                latency_factor = min(1.0, 2000.0 / max(avg_latency or 2000, 1))
                cost_factor = max(0.3, 1.0 - (avg_cost or 0.0) * 10)
                q_factor = quality_factor(avg_q, sum_q)
                score = round(success_rate * latency_factor * cost_factor
                              * q_factor, 4)

        self._score_cache[engine] = (score, now + self.SCORE_CACHE_TTL)
        return score

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

#!/usr/bin/env python3
"""
adaptive.py — Unified Search v2 自适应学习引擎

增强（v2）：
  - success × latency × cost 三维评分
  - 7天滑动窗口
  - SQLite 持久化（跨进程复用）
  - 预算模式感知（高 cost 引擎在 budget 模式下降权）

评分公式：
  score = success_rate × latency_factor × cost_factor
  latency_factor = min(1.0, 2000 / avg_latency_ms)  # 2s 内满分
  cost_factor = free=1.0, low=0.85, paid=0.6
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

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
        conn.execute("PRAGMA journal_mode=WAL")
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
                created_at REAL NOT NULL
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(engine_perf)")]
        if "created_at" not in cols:
            conn.execute("ALTER TABLE engine_perf ADD COLUMN created_at REAL DEFAULT 0")
        if "cost" not in cols:
            conn.execute("ALTER TABLE engine_perf ADD COLUMN cost REAL DEFAULT 0.0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_perf_engine_time ON engine_perf(engine, created_at)"
        )
        cutoff = time.time() - WINDOW_DAYS * 86400
        conn.execute("DELETE FROM engine_perf WHERE created_at < ?", (cutoff,))
        conn.commit()

    def record(self, engine: str, success: bool, latency_ms: float, cost: float = 0.0):
        """记录一次引擎调用结果。"""
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO engine_perf (engine, success, latency_ms, cost, created_at) VALUES (?, ?, ?, ?, ?)",
                (engine, 1 if success else 0, latency_ms, cost, time.time()),
            )
            conn.commit()
            self._score_cache.pop(engine, None)

    def get_score(self, engine: str) -> float:
        """获取引擎的综合推荐分数（0.0 ~ 1.0）。"""
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
                "SELECT COUNT(*), AVG(success), AVG(latency_ms), AVG(cost) "
                "FROM engine_perf WHERE engine = ? AND created_at > ?",
                (engine, cutoff),
            ).fetchone()
        total, avg_success, avg_latency, avg_cost = row
        if not total or total == 0:
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
            else:
                success_rate = avg_success or 0.5
                latency_factor = min(1.0, 2000.0 / max(avg_latency or 2000, 1))
                cost_factor = max(0.3, 1.0 - (avg_cost or 0.0) * 10)
                score = round(success_rate * latency_factor * cost_factor, 4)

        self._score_cache[engine] = (score, now + self.SCORE_CACHE_TTL)
        return score

    def get_ranking(self) -> list[tuple[str, float]]:
        """获取所有引擎的推荐排名（降序）。"""
        with self._lock:
            cutoff = time.time() - WINDOW_DAYS * 86400
            conn = self._connect()
            rows = conn.execute(
                "SELECT engine, COUNT(*), AVG(success), AVG(latency_ms), AVG(cost) "
                "FROM engine_perf WHERE created_at > ? GROUP BY engine",
                (cutoff,),
            ).fetchall()

        results = []
        for engine, total, avg_success, avg_latency, avg_cost in rows:
            if not total:
                continue
            success_rate = avg_success or 0.5
            latency_factor = min(1.0, 2000.0 / max(avg_latency or 2000, 1))
            cost_factor = max(0.3, 1.0 - (avg_cost or 0.0) * 10)
            score = round(success_rate * latency_factor * cost_factor, 4)
            results.append((engine, score))

        results.sort(key=lambda x: -x[1])
        return results

    def get_stats(self) -> dict:
        """获取所有引擎的统计信息。"""
        cutoff = time.time() - WINDOW_DAYS * 86400
        conn = self._connect()
        rows = conn.execute(
            "SELECT engine, COUNT(*), AVG(success), AVG(latency_ms), AVG(cost) "
            "FROM engine_perf WHERE created_at > ? GROUP BY engine",
            (cutoff,),
        ).fetchall()

        stats = {}
        for engine, total, avg_success, avg_latency, avg_cost in rows:
            stats[engine] = {
                "calls": total,
                "success_rate": round(avg_success or 0, 3),
                "avg_latency_ms": round(avg_latency or 0, 1),
                "avg_cost": round(avg_cost or 0, 6),
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
        print(json.dumps(learner.get_stats(), ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "rank":
        ranking = learner.get_ranking()
        for engine, score in ranking:
            print(f"{engine:<15} {score:.4f}")
    else:
        print("用法: python3 adaptive.py stats|rank")

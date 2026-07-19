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

DB_DIR = Path.home() / ".cache" / "unified-search"
DB_PATH = DB_DIR / "adaptive.db"
WINDOW_DAYS = 7

# ── 成本分级因子 ─────────────────────────────────────────────────────────────

COST_FACTORS = {"free": 1.0, "low": 0.85, "paid": 0.6}


class AdaptiveLearner:
    """自适应学习引擎：追踪引擎表现并输出推荐分数。"""

    def __init__(self):
        self._lock = threading.Lock()
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS engine_perf (
                    engine TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    cost REAL NOT NULL DEFAULT 0.0,
                    created_at REAL NOT NULL
                )
            """)
            # 检查列是否存在（迁移）
            cols = [r[1] for r in conn.execute("PRAGMA table_info(engine_perf)")]
            if "created_at" not in cols:
                conn.execute("ALTER TABLE engine_perf ADD COLUMN created_at REAL DEFAULT 0")
            if "cost" not in cols:
                conn.execute("ALTER TABLE engine_perf ADD COLUMN cost REAL DEFAULT 0.0")
            # 检查索引
            conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_engine_time ON engine_perf(engine, created_at)")
            # 清理过期数据
            cutoff = time.time() - WINDOW_DAYS * 86400
            conn.execute("DELETE FROM engine_perf WHERE created_at < ?", (cutoff,))

    def record(self, engine: str, success: bool, latency_ms: float, cost: float = 0.0):
        """记录一次引擎调用结果。"""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO engine_perf (engine, success, latency_ms, cost, created_at) VALUES (?, ?, ?, ?, ?)",
                    (engine, 1 if success else 0, latency_ms, cost, time.time()),
                )
                conn.commit()

    def get_score(self, engine: str) -> float:
        """获取引擎的综合推荐分数（0.0 ~ 1.0）。"""
        with self._lock:
            cutoff = time.time() - WINDOW_DAYS * 86400
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*), AVG(success), AVG(latency_ms), AVG(cost) FROM engine_perf WHERE engine = ? AND created_at > ?",
                    (engine, cutoff),
                ).fetchone()
        total, avg_success, avg_latency, avg_cost = row
        if not total or total == 0:
            return 0.5  # 无数据时中性分

        success_rate = avg_success or 0.5
        latency_factor = min(1.0, 2000.0 / max(avg_latency or 2000, 1))
        # 成本因子：平均成本越低越好
        cost_factor = max(0.3, 1.0 - (avg_cost or 0.0) * 10)

        return round(success_rate * latency_factor * cost_factor, 4)

    def get_ranking(self) -> list[tuple[str, float]]:
        """获取所有引擎的推荐排名（降序）。"""
        with self._lock:
            cutoff = time.time() - WINDOW_DAYS * 86400
            with self._connect() as conn:
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
        ranking = self.get_ranking()
        cutoff = time.time() - WINDOW_DAYS * 86400
        with self._connect() as conn:
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

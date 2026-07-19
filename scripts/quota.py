#!/usr/bin/env python3
"""
quota.py — Unified Search v2 配额管理器

增强（v2）：
  - 成本追踪（cost_tier × cost_per_call）
  - 预算模式感知
  - 配额 + 成本联合决策

追踪各引擎 API 的配额消耗、错误率、成本，
用于路由决策时的配额感知惩罚。
"""

from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from typing import Optional

# ── 路径 ──────────────────────────────────────────────────────────────────────

SKILL_DIR = Path(__file__).parent.parent
BACKENDS_DIR = SKILL_DIR / "backends"
QUOTA_PROFILES_PATH = BACKENDS_DIR / "quota_profiles.json"
QUOTA_STATE_DIR = Path.home() / ".cache" / "unified-search"
QUOTA_STATE_PATH = QUOTA_STATE_DIR / "quota.json"


class QuotaManager:
    """配额追踪与消耗速率计算（v2）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._profiles: dict = {}
        self._state: dict = {}
        self._load_profiles()
        self._load_state()

    def _load_profiles(self) -> None:
        if QUOTA_PROFILES_PATH.exists():
            try:
                self._profiles = json.loads(QUOTA_PROFILES_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                self._profiles = {}

    def _load_state(self) -> None:
        if QUOTA_STATE_PATH.exists():
            try:
                self._state = json.loads(QUOTA_STATE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                self._state = {}

    def _save_state(self) -> None:
        QUOTA_STATE_DIR.mkdir(parents=True, exist_ok=True)
        QUOTA_STATE_PATH.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2)
        )

    def record(self, engine: str, success: bool = True, credits: int = 1) -> None:
        """记录一次 API 调用。"""
        with self._lock:
            if engine not in self._state:
                self._state[engine] = {
                    "used": 0, "limit": 0, "calls": [],
                    "errors": 0, "last_reset": time.time(), "total_cost": 0.0,
                }
            self._state[engine]["used"] += credits
            self._state[engine]["calls"].append(time.time())
            if not success:
                self._state[engine]["errors"] += 1
            # 累加成本
            cost = self.get_cost_per_call(engine)
            self._state[engine]["total_cost"] = self._state[engine].get("total_cost", 0.0) + cost

            # 只保留最近 1 小时的时间戳
            cutoff = time.time() - 3600
            self._state[engine]["calls"] = [
                t for t in self._state[engine]["calls"] if t > cutoff
            ]
            self._save_state()

    def get_remaining_ratio(self, engine: str) -> float:
        """获取配额剩余比例。无限配额返回 1.0。"""
        profile = self._profiles.get(engine, {})
        state = self._state.get(engine, {})
        limit = profile.get("limit")
        if limit is None:
            return 1.0
        used = state.get("used", 0)
        period = profile.get("period", "day")
        last_reset = state.get("last_reset", 0)
        now = time.time()

        # 按周期重置
        if period == "month" and now - last_reset > 30 * 86400:
            state["used"] = 0
            state["last_reset"] = now
            self._save_state()
            used = 0
        elif period == "day" and now - last_reset > 86400:
            state["used"] = 0
            state["last_reset"] = now
            self._save_state()
            used = 0
        return max(0.0, (limit - used) / limit)

    def get_current_rpm(self, engine: str) -> float:
        """获取最近 1 分钟的调用速率。"""
        state = self._state.get(engine, {})
        now = time.time()
        return len([t for t in state.get("calls", []) if now - t < 60])

    def get_error_rate(self, engine: str) -> float:
        """获取最近 1 小时的错误率。"""
        state = self._state.get(engine, {})
        total = len(state.get("calls", []))
        if total == 0:
            return 0.0
        return state.get("errors", 0) / total

    def is_available(self, engine: str, mode: str = "auto") -> bool:
        """检查引擎是否可用（配额未耗尽且未触发限频 + 预算模式）。"""
        qr = self.get_remaining_ratio(engine)
        if qr <= 0:
            return False

        profile = self._profiles.get(engine, {})
        qps = profile.get("qps")
        if qps is not None:
            rpm = self.get_current_rpm(engine)
            if rpm >= qps * 60:
                return False

        # budget 模式禁用付费引擎
        if mode in ("fast", "budget"):
            cost_tier = profile.get("cost_tier", "free")
            if cost_tier == "paid":
                return False

        return True

    def get_cost_per_call(self, engine: str) -> float:
        """获取单次调用的成本（单位：美元）。"""
        profile = self._profiles.get(engine, {})
        credits = profile.get("credits_per_search", 1)
        cost = profile.get("cost_per_call", 0.0)
        return credits * cost

    def get_total_cost(self, engine: str) -> float:
        """获取引擎累计成本。"""
        return self._state.get(engine, {}).get("total_cost", 0.0)

    def get_stats(self) -> dict:
        """获取所有引擎的配额统计。"""
        stats = {}
        for engine in self._profiles:
            if engine.startswith("_") or not isinstance(self._profiles[engine], dict):
                continue
            profile = self._profiles[engine]
            stats[engine] = {
                "remaining_ratio": round(self.get_remaining_ratio(engine), 2),
                "rpm": self.get_current_rpm(engine),
                "error_rate": round(self.get_error_rate(engine), 3),
                "available": self.is_available(engine),
                "cost_per_call": self.get_cost_per_call(engine),
                "total_cost": round(self.get_total_cost(engine), 6),
                "used": self._state.get(engine, {}).get("used", 0),
                "limit": profile.get("limit", "∞"),
                "cost_tier": profile.get("cost_tier", "free"),
            }
        return stats


# ── 模块级单例 ─────────────────────────────────────────────────────────────────

_manager: Optional[QuotaManager] = None


def get_quota_manager() -> QuotaManager:
    global _manager
    if _manager is None:
        _manager = QuotaManager()
    return _manager


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    mgr = get_quota_manager()
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(mgr.get_stats(), ensure_ascii=False, indent=2))
    else:
        print("用法: python3 quota.py stats")

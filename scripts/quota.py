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
def _state_dir() -> Path:
    """状态目录（惰性派生，支持 ARGO_STATE_DIR 覆盖）。"""
    import argo_paths
    return argo_paths.ensure_state_dir()


QUOTA_STATE_DIR = _state_dir()  # 兼容旧引用
QUOTA_STATE_PATH = QUOTA_STATE_DIR / "quota.json"


class QuotaManager:
    """配额追踪与消耗速率计算（v2）。"""

    # 远端配额周期候选；本地 period=second/minute 只是限频口径，不代表远端
    # 配额周期（火山免费额度按日），过短一律按 24h 保守处理
    _PERIOD_SECONDS = {"hour": 3600, "day": 86400, "month": 30 * 86400}

    def __init__(self):
        self._lock = threading.Lock()
        self._profiles: dict = {}
        self._state: dict = {}
        self._load_profiles()
        self._load_state()
        # 热读监视器（跨进程）：其他进程（CLI/另一客户端 server）改写
        # profiles/state 后，本进程下一次带锁访问自动重读——配额自愈与
        # 远端耗尽标记无需重启即全局可见。基线在初始加载后建立。
        try:
            from hot_state import HotFile
            self._profiles_hot = HotFile(QUOTA_PROFILES_PATH)
            self._state_hot = HotFile(QUOTA_STATE_PATH)
            # 基线与 init 加载的内存态对齐（load 已读过磁盘）：预建签名，
            # 消除 HotFile「首次 changed 只建基线」把 init 之后、首次访问之前
            # 的他进程写入吃掉的窗口
            self._profiles_hot.reset()
            self._state_hot.reset()
            self._profiles_hot.changed()
            self._state_hot.changed()
        except Exception:
            self._profiles_hot = None
            self._state_hot = None

    def _fresh_locked(self) -> None:
        """带锁调用：磁盘文件签名变化即重读（调用方须已持锁）。"""
        try:
            if self._profiles_hot is not None and self._profiles_hot.changed():
                self._load_profiles()
            if self._state_hot is not None and self._state_hot.changed():
                self._load_state()
        except Exception:
            pass

    def _load_profiles(self) -> None:
        if QUOTA_PROFILES_PATH.exists():
            try:
                self._profiles = json.loads(QUOTA_PROFILES_PATH.read_bytes())
            except (json.JSONDecodeError, OSError):
                self._profiles = {}

    def _load_state(self) -> None:
        if QUOTA_STATE_PATH.exists():
            try:
                self._state = json.loads(QUOTA_STATE_PATH.read_bytes())
            except (json.JSONDecodeError, OSError):
                # 损坏不清空：保留旧状态（配额/限频记忆），仅告警
                import sys
                print(f"[quota] 状态文件损坏，保留旧状态: {QUOTA_STATE_PATH}",
                      file=sys.stderr)

    def _save_state(self) -> None:
        QUOTA_STATE_DIR.mkdir(parents=True, exist_ok=True)
        # 原子写：tmp + replace，避免并发 torn write 损坏配额状态
        tmp = QUOTA_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2))
        tmp.replace(QUOTA_STATE_PATH)

    def record(self, engine: str, success: bool = True, credits: int = 1) -> None:
        """记录一次 API 调用。"""
        with self._lock:
            self._fresh_locked()
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
        """获取配额剩余比例。无限配额返回 1.0。

        整体持锁：周期重置的写 + _save_state 与 record() 并发安全。
        """
        with self._lock:
            self._fresh_locked()
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

    def mark_remote_exhausted(self, engine: str, reason: str = "",
                              period: str | None = None) -> None:
        """远端明示「配额耗尽」（如火山 10406 Free quota exhausted）时调用。

        设计目标：配额问题不需要人工改配置——标记后路由组合层全模式排除
        该引擎，备用源自然接管；到下一周期边界惰性自愈（is_available /
        is_hard_down 检查时清除），恢复后引擎自动回归。提前恢复（如充值）
        可执行 `python3 scripts/quota.py reset <engine>`。
        """
        with self._lock:
            self._fresh_locked()
            st = self._state.setdefault(engine, {
                "used": 0, "limit": 0, "calls": [],
                "errors": 0, "last_reset": time.time(), "total_cost": 0.0,
            })
            profile = self._profiles.get(engine, {})
            p = period or profile.get("period") or "day"
            seconds = self._PERIOD_SECONDS.get(p, 86400)
            if seconds < 3600:
                seconds = 86400
            st["remote_exhausted"] = {
                "until": time.time() + seconds,
                "reason": (reason or "")[:200],
                "marked_at": time.time(),
            }
            self._save_state()

    def clear_remote_exhausted(self, engine: str) -> bool:
        """手动清除远端耗尽标记（充值后提前恢复）。"""
        with self._lock:
            # 与 record/mark 同口径：先热读磁盘，防止用陈旧内存态覆盖他进程写入
            self._fresh_locked()
            st = self._state.get(engine)
            if st and "remote_exhausted" in st:
                st.pop("remote_exhausted", None)
                self._save_state()
                return True
            return False

    def _refresh_remote_state_locked(self, engine: str, now: float) -> None:
        """周期边界自愈（调用方须已持锁）。"""
        st = self._state.get(engine) or {}
        mark = st.get("remote_exhausted")
        # mark 残缺（手工编辑/截断成非 dict）时按过期处理：清掉坏标记自愈
        if mark and not isinstance(mark, dict):
            st.pop("remote_exhausted", None)
            self._save_state()
            return
        if mark and now >= float(mark.get("until") or 0):
            st.pop("remote_exhausted", None)
            self._save_state()

    def is_remote_exhausted(self, engine: str) -> bool:
        with self._lock:
            self._fresh_locked()
            self._refresh_remote_state_locked(engine, time.time())
            return "remote_exhausted" in (self._state.get(engine) or {})

    def is_hard_down(self, engine: str) -> bool:
        """配额意义上不可用：远端耗尽或本地剩余为 0。

        与限频/预算无关——路由组合层用它做全模式排除；
        is_available 的限频/付费判断不在此列。
        """
        if self.is_remote_exhausted(engine):
            return True
        return self.get_remaining_ratio(engine) <= 0

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
        if self.is_remote_exhausted(engine):
            return False
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
            # 残缺状态防御：state JSON 手工编辑/截断时 mark 可能非 dict，
            # 与 _refresh_remote_state_locked 的 .get 口径保持一致
            raw_mark = (self._state.get(engine) or {}).get("remote_exhausted")
            mark = raw_mark if isinstance(raw_mark, dict) else None
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
                "remote_exhausted_until": (
                    round(float(mark["until"])) if mark else None),
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
    elif len(sys.argv) > 2 and sys.argv[1] == "reset":
        # 充值后提前恢复：清除远端配额耗尽标记
        ok = mgr.clear_remote_exhausted(sys.argv[2])
        print(f"{'✅ 已清除' if ok else 'ℹ️ 无标记'}: {sys.argv[2]}")
    else:
        print("用法: python3 quota.py stats | python3 quota.py reset <engine>")

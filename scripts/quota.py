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
from typing import Any, Optional

import argo_paths
from cli_io import dumps

# ── 路径 ──────────────────────────────────────────────────────────────────────

SKILL_DIR = Path(__file__).parent.parent
BACKENDS_DIR = SKILL_DIR / "backends"
QUOTA_PROFILES_PATH = BACKENDS_DIR / "quota_profiles.json"


def _state_dir() -> Path:
    """状态目录（惰性派生，支持 ARGO_STATE_DIR 覆盖）。"""
    return argo_paths.ensure_state_dir()


QUOTA_STATE_DIR = _state_dir()  # 兼容旧引用
QUOTA_STATE_PATH = QUOTA_STATE_DIR / "quota.json"


class QuotaManager:
    """配额追踪与消耗速率计算（v2）。"""

    # 远端配额周期候选；本地 period=second/minute 只是限频计算方式，不代表远端
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
            # 基线与 init 加载的内存态保持一致（load 已读过磁盘）：预建签名，
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
        """原子写状态（临时文件名进程内唯一，见 argo_paths.atomic_write_json）。

        旧实现用固定 `quota.json.tmp`：多进程（CLI 与 MCP server 并行、
        或评测脚本）同时写时互相搬走/删除对方的 tmp，replace 抛
        FileNotFoundError，且失败方本次计数直接丢失。
        """
        argo_paths.atomic_write_json(QUOTA_STATE_PATH, self._state)

    def _mutate_locked(self, mutator) -> None:
        """跨进程安全的「重读 → 改 → 写入文件」序列。

        进程内 threading.Lock 只挡得住同进程线程；CLI / MCP server /
        评测脚本三者并行时，各自读到旧状态、各自 +1、后写者覆盖前写者，
        计数直接丢失。这里在文件锁内重读最新磁盘态，保证增量不丢。
        """
        with argo_paths.file_lock(QUOTA_STATE_PATH):
            self._load_state()
            mutator()
            argo_paths.atomic_write_json(QUOTA_STATE_PATH, self._state)

    def record(self, engine: str, success: bool = True, credits: int = 1) -> None:
        """记录一次 API 调用（单条，跨进程安全）。"""
        self.record_many([(engine, success)], credits=credits)

    @staticmethod
    def _prune_locked(st: dict, now: float) -> None:
        """滑动窗口修剪：calls 与 errors 必须同窗口同步修剪。

        历史坑：只修剪 calls 却让 errors 永久累计，导致
        ①错误率分子/分母不同窗口（实测可算出 300%）；
        ②真实状态里出现 errors > used 的反常（github used=1/errors=78）。
        窗口内没有调用 = 没有观测，此时 errors 也必须归零。
        """
        cutoff = now - 3600
        kept = [t for t in st.get("calls", []) if t > cutoff]
        st["calls"] = kept
        # errors 没有逐条时刻，无法精确保持一致：上界取窗口内调用数，
        # 窗口清空则归零。保证 errors 恒 ≤ 窗口 calls。
        st["errors"] = min(st.get("errors", 0), len(kept))

    def record_many(self, entries, *, credits: int = 1) -> None:
        """批量记录（entries: (engine, success) 可迭代），只写入文件一次。

        批次搜索一次为每个引擎各写一次状态，而每次写都是「全量序列化 +
        rename」。合并后写盘次数从 N 降到 1，且整批在同一个文件锁内完成。
        """
        entries = list(entries)
        if not entries:
            return
        now = time.time()

        def _apply() -> None:
            for engine, success in entries:
                st = self._state.setdefault(engine, {
                    "used": 0, "limit": 0, "calls": [],
                    "errors": 0, "last_reset": now, "total_cost": 0.0,
                })
                st["used"] = st.get("used", 0) + credits
                st.setdefault("calls", []).append(now)
                if not success:
                    st["errors"] = st.get("errors", 0) + 1
                st["total_cost"] = st.get("total_cost", 0.0) + self.get_cost_per_call(engine)
                self._prune_locked(st, now)

        self._mutate_locked(_apply)

    def get_remaining_ratio(self, engine: str) -> float:
        """获取配额剩余比例。无限配额返回 1.0。

        整体持锁：周期重置的写 + _save_state 与 record() 并发安全。
        """
        with self._lock:
            self._fresh_locked()
            profile = self._profiles.get(engine, {})
            state = self._state.get(engine, {})
            used = state.get("used", 0)
            period = profile.get("period", "day")
            last_reset = state.get("last_reset", 0)
            now = time.time()

            # 按周期重置（先于 limit 判空：null 引擎计数也要按周期归零，
            # 否则遥测永久累计、无周期语义）
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
            limit = profile.get("limit")
            if limit is None:
                return 1.0
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
            # 与 record/mark 同计算方式：先热读磁盘，防止用陈旧内存态覆盖他进程写入
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

    def remote_exhausted_marks(self) -> dict[str, dict[str, Any]]:
        """处于「远端配额耗尽」状态的引擎 → {reason, until}。

        一次性快照，供 `--list-engines` 展示：逐引擎调 is_remote_exhausted 会
        重复走热读检查。顺带做过期自愈（与 _refresh_remote_state_locked 同计算方式），
        坏标记（非 dict）按过期处理。
        """
        with self._lock:
            self._fresh_locked()
            now = time.time()
            out: dict[str, dict[str, Any]] = {}
            for engine, st in list(self._state.items()):
                if not isinstance(st, dict):
                    continue
                mark = st.get("remote_exhausted")
                if not isinstance(mark, dict):
                    continue
                if now >= float(mark.get("until") or 0):
                    st.pop("remote_exhausted", None)
                    self._save_state()
                    continue
                out[engine] = {
                    "reason": str(mark.get("reason") or ""),
                    "until": float(mark.get("until") or 0),
                }
            return out

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
        with self._lock:
            state = self._state.get(engine, {})
            now = time.time()
            return len([t for t in state.get("calls", []) if now - t < 60])

    def get_error_rate(self, engine: str) -> float:
        """最近 1 小时的错误率。

        旧实现分子分母不同窗口：errors 是**累计**值（从不衰减），calls
        只保留最近 1 小时。两次失败后哪怕窗口内全是成功调用，算出来也会
        >1（实测 1 次成功 + 历史 3 次错 → 3.0 = 300%）。

        现在改为同窗口：窗口内没有样本时返回 0.0（无观测 ≠ 高错误率）。
        """
        with self._lock:
            state = self._state.get(engine, {})
            cutoff = time.time() - 3600
            window_calls = [t for t in state.get("calls", []) if t > cutoff]
            if not window_calls:
                return 0.0
            return min(1.0, state.get("errors", 0) / len(window_calls))

    def is_available(self, engine: str, mode: str = "auto") -> bool:
        """检查引擎是否可用（配额未耗尽且未触发限频 + 预算模式）。"""
        with self._lock:
            return self._is_available_locked(engine, mode)

    def _is_available_locked(self, engine: str, mode: str = "auto") -> bool:
        """is_available 的持锁内部版本（调用方必须已持有 self._lock）。

        将 is_remote_exhausted / get_remaining_ratio / get_current_rpm 的
        检查合并到单次持锁中，消除 TOCTOU 间隙。
        """
        self._fresh_locked()
        # is_remote_exhausted 的内联版本（不重复获取锁）
        self._refresh_remote_state_locked(engine, time.time())
        if "remote_exhausted" in (self._state.get(engine) or {}):
            return False
        # get_remaining_ratio 的内联版本（不重复获取锁）
        profile = self._profiles.get(engine, {})
        state = self._state.get(engine, {})
        used = state.get("used", 0)
        period = profile.get("period", "day")
        last_reset = state.get("last_reset", 0)
        now = time.time()
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
        limit = profile.get("limit")
        if limit is not None:
            qr = max(0.0, (limit - used) / limit)
            if qr <= 0:
                return False
        # get_current_rpm 的内联版本（不重复获取锁）
        qps = profile.get("qps")
        if qps is not None:
            rpm = len([t for t in state.get("calls", []) if now - t < 60])
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
        with self._lock:
            return self._state.get(engine, {}).get("total_cost", 0.0)

    def get_stats(self) -> dict:
        """获取所有引擎的配额统计。"""
        stats = {}
        for engine in self._profiles:
            if engine.startswith("_") or not isinstance(self._profiles[engine], dict):
                continue
            profile = self._profiles[engine]
            # 残缺状态防御：state JSON 手工编辑/截断时 mark 可能非 dict，
            # 与 _refresh_remote_state_locked 的 .get 计算方式保持一致
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
        print(dumps(mgr.get_stats()))
    elif len(sys.argv) > 2 and sys.argv[1] == "reset":
        # 充值后提前恢复：清除远端配额耗尽标记
        ok = mgr.clear_remote_exhausted(sys.argv[2])
        print(f"{'✅ 已清除' if ok else 'ℹ️ 无标记'}: {sys.argv[2]}")
    else:
        print("用法: python3 quota.py stats | python3 quota.py reset <engine>")


class _QuotaBatch:
    """一次搜索的配额记账收集器（累积 → 一次性写入文件）。

    为什么不是每引擎各写一次：每次 record 都是「全量状态序列化 + rename」，
    一次 5 引擎搜索即 5 次全量写。合并后写盘次数从 N 降到 1，且整批在
    同一个跨进程文件锁内完成（`QuotaManager.record_many`）。

    失败静默：记账属于观测层，任何异常都不得拖累搜索主路径。
    """

    def __init__(self) -> None:
        self._entries: list[tuple[str, bool]] = []

    def add(self, engine: str, success: bool) -> None:
        self._entries.append((engine, success))

    def flush(self) -> None:
        entries, self._entries = self._entries, []
        if not entries:
            return
        try:
            from quota import get_quota_manager
            get_quota_manager().record_many(entries)
        except Exception:
            pass

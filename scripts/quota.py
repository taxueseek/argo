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


_STATE_DIR_CACHE: tuple[str, Path] | None = None


def _state_dir() -> Path:
    """状态目录（惰性派生 + 按根记忆化，支持 ARGO_STATE_DIR 覆盖）。

    按 `argo_paths.state_root()` 的解析结果键控：测试在运行期切换
    ARGO_STATE_DIR（conftest 全局隔离 + 若干用例的临时覆盖）时缓存自然失效，
    不会把上一个用例的目录当成事实。
    """
    global _STATE_DIR_CACHE
    root = argo_paths.state_root()
    key = str(root)
    if _STATE_DIR_CACHE is None or _STATE_DIR_CACHE[0] != key:
        _STATE_DIR_CACHE = (key, argo_paths.ensure_state_dir())
    return _STATE_DIR_CACHE[1]


def _state_path() -> Path:
    """quota.json 的完整路径（惰性解析，见下方 __getattr__ 的说明）。

    解析顺序：模块属性 `QUOTA_STATE_PATH`（测试与旧调用方直接赋值覆盖，
    这是它们既有的隔离契约）→ 惰性派生。PEP 562 的 `__getattr__` 只服务
    「属性不存在时」的外部读取，模块内的裸全局名查找看不见它，所以内部
    引用统一走本函数，赋值覆盖与惰性派生两条路都尊重。
    """
    override = globals().get("QUOTA_STATE_PATH")
    if override is not None:
        return override
    return _state_dir() / "quota.json"


def __getattr__(name: str) -> Any:
    # import 期零副作用（2026-09-27）：这两个名字曾在模块级求值，而
    # `ensure_state_dir()` 会 mkdir——于是每次 `import quota`（route →
    # route_combo 全链路，含纯缓存命中的 CLI 调用）都在 import 阶段碰一次
    # 文件系统。改成 PEP 562 惰性属性：旧引用 `quota.QUOTA_STATE_PATH`
    # 的语义不变（首次使用时才派生），import 不再产生任何 I/O。
    # 注意：外部**赋值** `quota.QUOTA_STATE_PATH = p` 会落进模块命名空间，
    # 之后的读取与内部 _state_path() 都会尊重它（见该函数 docstring）。
    if name == "QUOTA_STATE_DIR":
        return _state_dir()
    if name == "QUOTA_STATE_PATH":
        return _state_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
            self._state_hot = HotFile(_state_path())
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
        if _state_path().exists():
            try:
                self._state = json.loads(_state_path().read_bytes())
            except (json.JSONDecodeError, OSError):
                # 损坏不清空：保留旧状态（配额/限频记忆），仅告警
                import sys
                print(f"[quota] 状态文件损坏，保留旧状态: {_state_path()}",
                      file=sys.stderr)

    def _save_state(self) -> None:
        """原子写状态（临时文件名进程内唯一，见 argo_paths.atomic_write_json）。

        旧实现用固定 `quota.json.tmp`：多进程（CLI 与 MCP server 并行、
        或评测脚本）同时写时互相搬走/删除对方的 tmp，replace 抛
        FileNotFoundError，且失败方本次计数直接丢失。
        """
        argo_paths.atomic_write_json(_state_path(), self._state)

    def _mutate_locked(self, mutator) -> None:
        """跨进程安全的「重读 → 改 → 写入文件」序列。

        进程内 threading.Lock 只挡得住同进程线程；CLI / MCP server /
        评测脚本三者并行时，各自读到旧状态、各自 +1、后写者覆盖前写者，
        计数直接丢失。这里在文件锁内重读最新磁盘态，保证增量不丢。
        """
        with argo_paths.file_lock(_state_path()):
            self._load_state()
            mutator()
            argo_paths.atomic_write_json(_state_path(), self._state)

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

    def _period_elapsed(self, engine: str, now: float) -> bool:
        """该引擎的配额周期是否已过（只判断，不改状态）。"""
        profile = self._profiles.get(engine, {})
        state = self._state.get(engine)
        if not isinstance(state, dict):
            return False
        span = 30 * 86400 if profile.get("period", "day") == "month" else 86400
        return now - float(state.get("last_reset") or 0) > span

    def _reset_period_locked(self, engine: str, now: float) -> None:
        """周期重置的 load-modify-write，**调用方须已持文件锁**。

        get_remaining_ratio / is_available 此前各抄了一份这段逻辑，也各抄了
        一份「threading.Lock 下改完就 _save_state」的写法——于是周期边界那一刻
        的并发写会互相覆盖整个状态文件。统一走这里：先判是否过期，过期才在
        锁内重读并归零。
        """
        with argo_paths.file_lock(_state_path()):
            self._load_state()
            if not self._period_elapsed(engine, now):
                return
            state = self._state.get(engine)
            if not isinstance(state, dict):
                return
            state["used"] = 0
            state["last_reset"] = now
            argo_paths.atomic_write_json(_state_path(), self._state)

    def get_remaining_ratio(self, engine: str) -> float:
        """获取配额剩余比例。无限配额返回 1.0。"""
        with self._lock:
            self._fresh_locked()
            # 按周期重置（先于 limit 判空：null 引擎计数也要按周期归零，
            # 否则用量计数永久累计、无周期语义）。重置在文件锁内落盘。
            if self._period_elapsed(engine, time.time()):
                self._reset_period_locked(engine, time.time())
            profile = self._profiles.get(engine, {})
            state = self._state.get(engine, {})
            limit = profile.get("limit")
            if limit is None:
                return 1.0
            return max(0.0, (limit - state.get("used", 0)) / limit)

    def mark_remote_exhausted(self, engine: str, reason: str = "",
                              period: str | None = None) -> None:
        """远端明示「配额耗尽」（如火山 10406 Free quota exhausted）时调用。

        设计目标：配额问题不需要人工改配置——标记后路由组合层全模式排除
        该引擎，备用源自然接管；到下一周期边界惰性自愈（is_available /
        is_hard_down 检查时清除），恢复后引擎自动回归。提前恢复（如充值）
        可执行 `python3 scripts/quota.py reset <engine>`。
        """
        with self._lock:
            def _apply() -> None:
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
            self._mutate_locked(_apply)

    def clear_remote_exhausted(self, engine: str) -> bool:
        """手动清除远端耗尽标记（充值后提前恢复）。"""
        cleared = False
        with self._lock:
            def _apply() -> None:
                nonlocal cleared
                st = self._state.get(engine)
                if st and "remote_exhausted" in st:
                    st.pop("remote_exhausted", None)
                    cleared = True
            # 锁内重读：不清也可能命中（他进程已清），故只在真删掉时写盘
            with argo_paths.file_lock(_state_path()):
                self._load_state()
                _apply()
                if cleared:
                    argo_paths.atomic_write_json(_state_path(), self._state)
        return cleared

    def _refresh_remote_state_locked(self, engine: str, now: float) -> None:
        """周期边界自愈（调用方须已持锁）。

        走 _mutate_locked 而非就地 _save_state：这是 load-modify-write，
        只用 threading.Lock 时 CLI 与 MCP 并发会互相覆盖整个状态文件。
        """
        st = self._state.get(engine) or {}
        mark = st.get("remote_exhausted")
        # mark 残缺（手工编辑/截断成非 dict）时按过期处理：清掉坏标记自愈
        if mark and (not isinstance(mark, dict) or now >= float(mark.get("until") or 0)):
            def _drop() -> None:
                (self._state.get(engine) or {}).pop("remote_exhausted", None)
            self._mutate_locked(_drop)

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
            stale: list[str] = []
            for engine, st in list(self._state.items()):
                if not isinstance(st, dict):
                    continue
                mark = st.get("remote_exhausted")
                if not isinstance(mark, dict):
                    continue
                if now >= float(mark.get("until") or 0):
                    stale.append(engine)
                    continue
                out[engine] = {
                    "reason": str(mark.get("reason") or ""),
                    "until": float(mark.get("until") or 0),
                }
            # 过期自愈是 load-modify-write：与其他写路径一样必须在文件锁内，
            # 否则与并发的 record/mark 互相覆盖（丢计数或丢标记）。
            if stale:
                def _drop() -> None:
                    for eng in stale:
                        (self._state.get(eng) or {}).pop("remote_exhausted", None)
                self._mutate_locked(_drop)
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
        now = time.time()
        if self._period_elapsed(engine, now):
            self._reset_period_locked(engine, now)
            state = self._state.get(engine, {})
        used = state.get("used", 0)
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
        # 取出但不立即清空：写盘失败时把这批放回队首，下次 flush 会重试。
        # 原实现 `entries, self._entries = self._entries, []` 先清空再写，
        # 于是 record_many 一旦抛异常（磁盘满 / 状态文件被换 inode），这批
        # 记账**永久消失且无任何痕迹**——配额被系统性少记，用户只会看到
        # 「额度仿佛变多了」，而那正是最需要报警的信号。
        # 代价：失败时本批滞留缓冲，可能被重复计入；但重复计入的方向是
        # 「高估已用量」（引擎被限得更狠），远好过「低估已用量」（超用）。
        if not self._entries:
            return
        entries = list(self._entries)
        try:
            from quota import get_quota_manager
            get_quota_manager().record_many(entries)
        except Exception:
            return
        # 只在成功后丢弃已写入的前缀，保留 flush 期间新 add 的条目
        del self._entries[:len(entries)]

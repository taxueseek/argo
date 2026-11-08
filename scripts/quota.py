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
import os
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

# 次数额度的环境覆盖白名单（DSH 设置页「付费搜索」分组写入）。
#
# 为什么按引擎列名而不是一个 JSON：面板是「一行一个 KEY=VALUE」的形态，
# 每个数字字段要独占一行才能被现有的 envMap/白名单门禁识别；一个 JSON
# 大字段既无法在面板上分引擎显示，也会让 env gate 失去可判定的锚点。
#
# 语义：值 = 该引擎配额周期内的次数上限；0 = 不限制（None）；空/非法 = 保持
# config.yaml 的声明值。周期（day/month）不在面板改——它决定的是「这条限制
# 按天还是按月」，与引擎的商业额度形态绑定，换周期需要同时换文案与默认值。
_QUOTA_LIMIT_ENV: tuple[tuple[str, str], ...] = (
    ("ARGO_QUOTA_ANYSEARCH_LIMIT", "anysearch"),   # 日
    ("ARGO_QUOTA_ZHIHU_LIMIT", "zhihu"),           # 日
    ("ARGO_QUOTA_YUANBAO_LIMIT", "yuanbao"),       # 月
    ("ARGO_QUOTA_TAVILY_LIMIT", "tavily"),         # 月
)


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


def _config_engine_names() -> set[str] | None:
    """config.yaml 里声明的**全部**引擎名（含未启用的）；取不到返回 None。

    刻意取全部而不是 `get_engines()`（它只返回 enabled=True 的）：判据是
    「配置里有没有这个名字」，不是「现在能不能用」。按后者删会把「临时缺密钥 /
    熔断禁用」的源的历史计数一起抹掉，而它们恢复后要接着用这份账。
    """
    try:
        from config import load_config
        engines = (load_config() or {}).get("engines")
    except Exception as e:
        import sys
        sys.stderr.write(
            f"[quota] 配置加载失败（{type(e).__name__}），本次跳过清理\n")
        return None
    return set(engines) if isinstance(engines, dict) else None


def _prune_foreign_engines(state: dict, protect: set[str]) -> dict:
    """丢掉配置里根本不存在的引擎名；判据不可用时原样返回（fail-safe）。

    why（2026-09-27 实测）：本机 quota.json 419 条里 195 条是测试/探针残留
    （`eng_x`、`benchmark_a`、`p_<8hex>`、`nope`…），而且**只增不减**——每跑一次
    测试或评测就往里加几个名字并永久留存。它不改变配额判断（那些名字不会被路由
    到），但让「引擎用量账本」这件事从根上不可信：账本里近一半的行不指向任何
    真实来源。

    `protect` 是**本次正在写入**的名字，永不删：测试替身（`quick_primary`）与
    评测脚本（`benchmark_a`）都靠这个口子写非配置名，删掉它们等于让「写进了
    什么」与「读回来什么」不一致——实测三处并发/窗口用例因此变红。删的只是
    「本次没提到、配置里也不存在」的历史残留。

    位置选在唯一写入口 `record_many` 的更改函数里：判据一旦成立，下一次真实
    搜索就会顺带自愈，不需要额外的清理命令，也不会与并发写打架。
    """
    known = _config_engine_names()
    if not known:
        return state
    return {name: st for name, st in state.items()
            if name in known or name in protect}


def _at_or_over_budget(cost_used: float, budget: float) -> bool:
    """预算是否用完：按 6 位小数比较，消掉单价累加的浮点漂移。

    实测：Exa $0.01 × 1000 次 = 9.999999999999831，直接 `>= 10` 会判「还没
    用完」，额度显示 10.0 却还能再打一次。预算是对外承诺的数字，比较口径
    必须与展示口径一致（budget_status 也是 round 6）。
    """
    return round(float(cost_used), 6) >= round(float(budget), 6)


# 远端配额报错的默认冷却（秒）：不依赖 profile 的 period 字段（那是本地记账
# 周期）。远端配额报错分两类，“多久能好”取决于归因类别，而不取决于“这条限制
# 是按天还是按月”：
#   - 预充值余额不足（HTTP 402 insufficient balance / “not enough money”）：
#     账户余额问题，充值或换 Key 立即恢复，绝不会到月底自动恢复。锁 30 天是
#     最坏组合（#42 实测：充值后每次调用 55ms 返回 skipped，自愈日期写着下月）。
#     默认 1h——够覆盖一次误充/换 Key 的操作窗口，又不至于让用户干等。
#   - 免费额度耗尽（10406 Free quota exhausted 等）：多数按下月重置，沿用既有
#     period 口径（默认 24h 兜底，避免 second/minute 的限频周期被误当配额周期）。
# 换 Key 自愈（key_fp 对比）对两类都生效，见 _mark_expired。
_BALANCE_QUOTA_COOLDOWN_S = 3600
_DEFAULT_QUOTA_COOLDOWN_S = 86400


def _quota_cooldown_seconds(category: str | None, reason: str,
                            period: str | None) -> int:
    """远端配额耗尽的冷却秒数（单一计算点，mark 与文案都读它）。

    余额不足类固定短冷却、与 period 解耦；其余按 period 映射，过短按 24h 兜底。
    坏值一律回落默认，不让一次异常把某种引擎永久锁死。
    """
    if category == "quota_exhausted" and _looks_like_balance_error(reason):
        return _BALANCE_QUOTA_COOLDOWN_S
    seconds = {"hour": 3600, "day": 86400, "month": 30 * 86400}.get(
        str(period or "day"), _DEFAULT_QUOTA_COOLDOWN_S)
    return seconds if seconds >= 3600 else _DEFAULT_QUOTA_COOLDOWN_S


def _looks_like_balance_error(reason: str) -> bool:
    """错误文本是否是「账户余额不足」（预充值用尽，充值即恢复）。"""
    m = str(reason or "").lower()
    return any(k in m for k in ("insufficient balance", "insufficient funds",
                                "not enough money", "余额不足", "402"))


def _required_candidates(engine: str, spec: dict[str, Any]) -> list[str]:
    """引擎 key 的候选环境变量名（含 ARGO_ 变体与占位符别名展开）。

    与 engine_env.missing_env_for 的候选构造同源；这里只是把「候选」摊平成
    一条可读的名单，供取指纹用。无 required_env 时返回空（由调用方回退）。
    """
    from engine_env import PLACEHOLDER_ALIASES
    explicit = spec.get("required_env")
    if not explicit:
        return []
    out: list[str] = []
    for item in explicit:
        if isinstance(item, list):
            out.extend(str(x) for x in item if x)
        elif isinstance(item, str) and item:
            if item in PLACEHOLDER_ALIASES:
                out.extend(PLACEHOLDER_ALIASES[item])
            elif item.startswith("ARGO_"):
                out.extend([item, item[len("ARGO_"):]])
            else:
                out.extend([f"ARGO_{item}", item])
    return out


class QuotaManager:
    """配额追踪与消耗速率计算（v2）。"""


    # 远端配额周期候选（模块级 _quota_cooldown_seconds 是唯一计算点；本地
    # period=second/minute 只是限频口径，不代表远端配额周期，过短按 24h 兜底）
    _PERIOD_SECONDS = {"hour": 3600, "day": 86400, "month": 30 * 86400}

    def __init__(self):
        self._lock = threading.Lock()
        self._profiles: dict = {}
        self._state: dict = {}
        self._last_save_ts: float = 0.0  # 写盘节流时间戳
        self._last_save_interval: float = 30.0  # 写盘节流间隔（秒）
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

    def _fresh_locked(self, engine: str | None = None) -> None:
        """带锁调用：磁盘文件签名变化即重读（调用方须已持锁）。

        同一次路由遍历（~0.3ms 内对 230+ 个不同 engine 逐一查询）共用首轮
        stat 结果，避免单次 route_query 产生 900+ 次重复 stat；同一引擎再次
        查询或超过 2ms 立即重新 stat，保持跨进程热读立即可见（test_hot_state）。
        """
        now = time.monotonic()
        seen = getattr(self, "_fresh_seen", None)
        if (engine is not None and seen is not None and engine not in seen
                and (now - getattr(self, "_fresh_ts", 0.0)) < 0.002):
            seen.add(engine)
            return
        self._fresh_ts = now
        self._fresh_seen = {engine} if engine is not None else set()
        try:
            import budget as _budget
            b_sig = _budget.budget_signature()
        except Exception:
            b_sig = None
        try:
            if (self._profiles_hot is not None and self._profiles_hot.changed()) \
                    or (b_sig is not None and b_sig != getattr(self, "_budget_sig", None)):
                self._load_profiles()
            if self._state_hot is not None and self._state_hot.changed():
                self._load_state()
        except Exception as e:
            import sys
            sys.stderr.write(
                f"[quota] 热文件检测失败（{type(e).__name__}）\n")

    def _load_profiles(self) -> None:
        if QUOTA_PROFILES_PATH.exists():
            try:
                self._profiles = json.loads(QUOTA_PROFILES_PATH.read_bytes())
            except (json.JSONDecodeError, OSError):
                self._profiles = {}
        # 月度预算覆盖（2026-10-21）：只改 profile 的 budget_* 与 period，
        # 不碰 quota_profiles.json（那是随包发版的派生件）。拼错/无预算的引擎
        # 不受影响，符合「其他默认不改」。
        try:
            import budget as _budget
            _budget.apply_to_profiles(self._profiles)
            self._budget_sig = _budget.budget_signature()
        except Exception:
            self._budget_sig = None
        self._apply_quota_env_overrides()

    def _apply_quota_env_overrides(self) -> None:
        """把面板写入的 ARGO_QUOTA_<ENGINE>_LIMIT 叠到次数额度上。

        只改内存里的 profile，不写 quota_profiles.json（派生件随包发版）。
        非法/空值静默忽略：面板与环境变量都可能被手工编辑，配额读取失败
        不应让整次路由崩掉。0 = 显式不限制。
        """
        for env_name, engine in _QUOTA_LIMIT_ENV:
            raw = (os.environ.get(env_name) or "").strip()
            if not raw:
                continue
            try:
                value = int(raw)
            except ValueError:
                continue
            if value < 0:
                continue
            profile = self._profiles.get(engine)
            if not isinstance(profile, dict):
                continue
            profile["limit"] = value if value > 0 else None

    def _load_state(self) -> None:
        if _state_path().exists():
            try:
                self._state = json.loads(_state_path().read_bytes())
            except (json.JSONDecodeError, OSError):
                # 损坏不清空：保留旧状态（配额/限频记忆），仅告警
                import sys
                print(f"[quota] 状态文件损坏，保留旧状态: {_state_path()}",
                      file=sys.stderr)

    def _save_state(self, force: bool = False) -> None:
        """原子写状态（临时文件名进程内唯一，见 argo_paths.atomic_write_json）。

        ⚠️ 生产写入一律走 _mutate_locked（文件锁内「重读→改→写」）。本方法
        是**无锁读改写 + 30s 节流**：在锁序列里调用它，节流窗口内的变更会被
        后写者整体覆盖——复刻 017b01a 在 circuit_breaker 修掉的跨进程丢更新。
        当前仅测试引用（test_audit_fixes 直调、其余打桩跳写）；接入任何生产
        路径前必须先包 file_lock 并去掉节流语义。

        旧实现用固定 `quota.json.tmp`：多进程（CLI 与 MCP server 并行、
        或评测脚本）同时写时互相搬走/删除对方的 tmp，replace 抛
        FileNotFoundError，且失败方本次计数直接丢失。

        写盘节流（2026-09-27）：identity memory 已有 30s 节流，quota 此前没有。
        一次 5 引擎搜索触发 5 次 record_many，每次都是全量序列化 + rename。
        加节流后，30s 内的多次写只落盘最后一次，磁盘 I/O 降 60-80%。
        状态丢失窗口 30s（进程崩溃时），可接受：配额计数是观测层，不是安全边界。
        """
        now = time.time()
        if not force and (now - self._last_save_ts) < self._last_save_interval:
            return
        argo_paths.atomic_write_json(_state_path(), self._state)
        self._last_save_ts = now

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
            # 自愈：丢掉「本次没提到、配置里也不存在」的历史残留
            # （见 _prune_foreign_engines 的 why）。放在重读之后、逐条记账之前。
            self._state = _prune_foreign_engines(
                self._state, {name for name, _ok in entries})
            for engine, success in entries:
                st = self._state.setdefault(engine, {
                    "used": 0, "limit": 0, "calls": [],
                    "errors": 0, "last_reset": now, "total_cost": 0.0,
                    "cost_used": 0.0,
                })
                st["used"] = st.get("used", 0) + credits
                st.setdefault("calls", []).append(now)
                if not success:
                    st["errors"] = st.get("errors", 0) + 1
                else:
                    # 只对成功调用计费：DeepSeek 等按成功请求收费，失败也计入
                    # 会让预算被网络抖动假消耗（2026-10-21 修）。
                    _cost = self.get_cost_per_call(engine)
                    st["cost_used"] = st.get("cost_used", 0.0) + _cost
                    st["total_cost"] = st.get("total_cost", 0.0) + _cost
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
            state["cost_used"] = 0.0
            state["last_reset"] = now
            argo_paths.atomic_write_json(_state_path(), self._state)

    def get_remaining_ratio(self, engine: str) -> float:
        """获取配额剩余比例。无限配额返回 1.0。"""
        with self._lock:
            self._fresh_locked(engine)
            # 按周期重置（先于 limit 判空：null 引擎计数也要按周期归零，
            # 否则用量计数永久累计、无周期语义）。重置在文件锁内落盘。
            if self._period_elapsed(engine, time.time()):
                self._reset_period_locked(engine, time.time())
            profile = self._profiles.get(engine, {})
            state = self._state.get(engine, {})
            limit = profile.get("limit")
            ratios: list[float] = []
            if limit is not None:
                ratios.append(max(0.0, (limit - state.get("used", 0)) / limit))
            budget = profile.get("budget_amount")
            if budget:
                cost_used = float(state.get("cost_used", 0.0))
                if _at_or_over_budget(cost_used, budget):
                    ratios.append(0.0)
                else:
                    ratios.append(max(0.0,
                                      (float(budget) - cost_used) / float(budget)))
            return min(ratios) if ratios else 1.0

    @staticmethod
    def _engine_key_fp(engine: str) -> str:
        """当前引擎生效密钥的短指纹（8 hex）；无密钥或读取失败返回空串。

        **必须与实际取 key 的地方同源**（issue #42）：指纹的用途是「换 Key 就
        自愈」，所以它算的必须是引擎运行时真正读的那个环境变量。此前用
        resolve_env_name 按 `<ENGINE>_API_KEY` 拼名，与 spec.required_env 对不上
        的 13 个引擎（deepseek_search 读 DEEPSEEK_API_KEY、metaso_* 读
        ARGO_METASO_API_KEY、you 读 YDC_API_KEY 等）指纹恒为空串，于是
        `_mark_expired` 的换 Key 分支永远不触发，换新 Key 也解不了熔断。
        改为与 search._missing_env_for 同一条链：先取 spec，用
        required_env_for/missing_env_for 的候选解析逻辑取值，无 spec 再回退
        resolve_env_name。任一候选有值即算命中——与「路由认为它可用」同口径。
        """
        try:
            import hashlib
            from engine_env import get_env, missing_env_for, resolve_env_name
            spec = None
            target = engine.split(":", 1)[-1] if ":" in engine else engine
            try:
                from config import get_engines
                engines_map = get_engines()
                spec = engines_map.get(engine) or engines_map.get(target)
            except Exception:
                spec = None
            val = ""
            if spec is not None:
                # required_env 声明的是「主名」，但候选链（PLACEHOLDER_ALIASES /
                # ARGO_ 变体）里任一有值都算配置了 key——与 missing_env_for 同源。
                missing = missing_env_for(target, spec)
                if not missing:
                    # 有声明且都齐了：取主名的实际值算指纹
                    from engine_env import required_env_for
                    for name in required_env_for(target, spec):
                        cand = [name] if name.startswith("ARGO_") else [f"ARGO_{name}", name]
                        got = get_env(cand)
                        if got:
                            val = got
                            break
                else:
                    # 声明了但缺：仍尝试按声明候选读（可能只有可选键齐）
                    val = get_env(_required_candidates(target, spec))
            if not val:
                val = get_env(resolve_env_name(target))
            return (hashlib.sha256(val.encode("utf-8")).hexdigest()[:8]
                    if val else "")
        except Exception:
            return ""

    @classmethod
    def _mark_expired(cls, engine: str, mark: Any, now: float) -> bool:
        """远端耗尽标记是否已失效（周期到期、数据损坏、或已更换新 API Key）。"""
        if not isinstance(mark, dict) or now >= float(mark.get("until") or 0):
            return True
        marked_at = float(mark.get("marked_at") or 0)
        if marked_at > 0 and _looks_like_balance_error(str(mark.get("reason") or "")):
            if now >= marked_at + _BALANCE_QUOTA_COOLDOWN_S:
                return True
        saved_fp = mark.get("key_fp")
        if saved_fp:
            if cls._engine_key_fp(engine) != saved_fp:
                return True
        elif "key_fp" in mark and cls._engine_key_fp(engine):
            return True
        return False

    def mark_remote_exhausted(self, engine: str, reason: str = "",
                              period: str | None = None,
                              category: str | None = None) -> None:
        """远端明示「配额耗尽」（如火山 10406 Free quota exhausted）时调用。

        设计目标：配额问题不需要人工改配置——标记后路由组合层全模式排除
        该引擎，备用源自然接管；到下一周期边界或更换新 Key 时惰性自愈（is_available /
        is_hard_down 检查时清除），恢复后引擎自动回归。提前恢复（如原 Key 充值）
        可执行 `python3 scripts/quota.py reset <engine>`。

        `category`（如 "quota_exhausted"）与 `reason` 一起决定冷却时长：余额不足
        类（402 insufficient balance）与月度预算周期解耦、固定短冷却，避免一次
        欠费把引擎锁死 30 天（issue #42）。
        """
        key_fp = self._engine_key_fp(engine)
        with self._lock:
            def _apply() -> None:
                st = self._state.setdefault(engine, {
                    "used": 0, "limit": 0, "calls": [],
                    "errors": 0, "last_reset": time.time(), "total_cost": 0.0,
                })
                profile = self._profiles.get(engine, {})
                p = period or profile.get("period") or "day"
                seconds = _quota_cooldown_seconds(category, reason, p)
                st["remote_exhausted"] = {
                    "until": time.time() + seconds,
                    "reason": (reason or "")[:200],
                    "marked_at": time.time(),
                    "key_fp": key_fp,
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
        """周期边界或换 Key 自愈（调用方须已持锁）。

        走 _mutate_locked 而非就地 _save_state：这是 load-modify-write，
        只用 threading.Lock 时 CLI 与 MCP 并发会互相覆盖整个状态文件。
        """
        st = self._state.get(engine) or {}
        mark = st.get("remote_exhausted")
        if mark is not None and self._mark_expired(engine, mark, now):
            def _drop() -> None:
                (self._state.get(engine) or {}).pop("remote_exhausted", None)
            self._mutate_locked(_drop)

    def is_remote_exhausted(self, engine: str) -> bool:
        with self._lock:
            self._fresh_locked(engine)
            self._refresh_remote_state_locked(engine, time.time())
            return "remote_exhausted" in (self._state.get(engine) or {})

    def remote_exhausted_marks(self) -> dict[str, dict[str, Any]]:
        """处于「远端配额耗尽」状态的引擎 → {reason, until}。

        一次性快照，供 `--list-engines` 展示：逐引擎调 is_remote_exhausted 会
        重复走热读检查。顺带做过期/换 Key 自愈（与 _refresh_remote_state_locked 同计算方式），
        坏标记（非 dict）按过期处理。
        """
        with self._lock:
            self._fresh_locked()
            now = time.time()
            out: dict[str, dict[str, Any]] = {}
            stale: list[str] = []
            for engine, st in list(self._state.items()):
                if not isinstance(st, dict) or "remote_exhausted" not in st:
                    continue
                mark = st.get("remote_exhausted")
                if self._mark_expired(engine, mark, now):
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
        with self._lock:
            self._fresh_locked(engine)
            now = time.time()
            self._refresh_remote_state_locked(engine, now)
            st = self._state.get(engine) or {}
            if "remote_exhausted" in st:
                return True
            if self._period_elapsed(engine, now):
                self._reset_period_locked(engine, now)
                st = self._state.get(engine) or {}
            limit = (self._profiles.get(engine) or {}).get("limit")
            if limit is not None and (limit - st.get("used", 0)) <= 0:
                return True
            budget = (self._profiles.get(engine) or {}).get("budget_amount")
            return budget is not None and float(st.get("cost_used", 0.0)) >= float(budget)

    def is_over_budget(self, engine: str) -> bool:
        """只判「月度预算是否用完」，不看次数/限频/远端配额。

        调度层用它给显式调用一条可行动的错误（「本月预算已用完 ¥5.00/¥5.00」），
        与 is_hard_down 的「配额意义上不可用」分开，避免把限流也报成预算。
        """
        with self._lock:
            self._fresh_locked(engine)
            now = time.time()
            if self._period_elapsed(engine, now):
                self._reset_period_locked(engine, now)
            profile = self._profiles.get(engine) or {}
            budget = profile.get("budget_amount")
            if budget is None:
                return False
            st = self._state.get(engine) or {}
            return _at_or_over_budget(st.get("cost_used", 0.0), budget)

    def budget_status(self, engine: str) -> dict[str, Any]:
        """单引擎预算快照（O(1)，避免 over_budget_error 为拼错误文案扫全表 280 引擎）。"""
        with self._lock:
            self._fresh_locked(engine)
            profile = self._profiles.get(engine) or {}
            st = self._state.get(engine) or {}
            return {
                "budget": profile.get("budget_amount"),
                "budget_currency": profile.get("budget_currency"),
                "cost_used": round(float(st.get("cost_used", 0.0)), 6),
            }

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
        self._fresh_locked(engine)
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
        budget = profile.get("budget_amount")
        if budget is not None and _at_or_over_budget(
                state.get("cost_used", 0.0), budget):
            return False
        # get_current_rpm 的内联版本（不重复获取锁）
        qps = profile.get("qps")
        if qps is not None:
            rpm = len([t for t in state.get("calls", []) if now - t < 60])
            if rpm >= qps * 60:
                return False
        # fast/budget 禁用按量计费引擎。判据必须连 api 档一起拦——7 个按量源
        # （exa/octen/parallel/seltz/tavily/you/zhihu_global）声明的都是 api 档，
        # 只认 paid 会让「免费优先 / 配额控制」两个模式照样选中它们并烧额度。
        if mode in ("fast", "budget") and profile.get("cost_tier") in ("api", "paid"):
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
                "period": profile.get("period"),
                # 月度预算（原生币种）：amount=None 表示未设预算、沿用次数额度
                "budget": profile.get("budget_amount"),
                "budget_currency": profile.get("budget_currency"),
                "cost_used": round((self._state.get(engine) or {}).get("cost_used", 0.0), 6),
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

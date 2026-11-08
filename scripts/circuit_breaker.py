#!/usr/bin/env python3
"""circuit_breaker.py — 引擎熔断 + 查询级负缓存

吸收 Hound 的 circuit-breaker 思路：
  - 连续失败 / 空结果 → 打开熔断，冷却期内跳过该引擎
  - 查询级负缓存：同一 query+engine 短 TTL 内不再打网络

状态持久化：<状态目录>/circuit_breaker.json（由 argo_paths 派生）
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

# 本地状态目录唯一来源（env ARGO_STATE_DIR → config cache.db_path 父目录 → 旧路径）
import argo_paths as _paths

# 默认状态路径**调用期解析**，不在导入期派生。
#
# 为什么：`ARGO_STATE_DIR` 在导入后才被改写是常态（测试用临时目录验证路径派生、
# 多工作树/子进程切换状态根）。导入期常量会让「记录」与「读取」落到两个不同文件，
# 且同一个类对默认路径给出两个答案。2026-11-02 的 `ec5b959` 只在消费侧（search_v3
# 调用期解析工厂）堵住了症状，源头这一处仍在——实测只改 `ARGO_STATE_DIR`、不重导入
# 时 `CircuitBreaker()._path` 仍指向旧目录。
#
# 用 PEP 562 模块 `__getattr__` 暴露 `STATE_PATH`：每次访问都按当前环境解析，
# 既保持「`circuit_breaker.STATE_PATH` 可读、可 patch」的既有契约（测试与
# monkeypatch 依赖它），又不再把值钉在导入那一刻。
def _default_state_path() -> str:
    return str(_paths.state_path("circuit_breaker.json"))


def __getattr__(name: str):
    if name == "STATE_PATH":
        return str(_default_state_path())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# 熔断参数
FAILURE_THRESHOLD = 2          # 连续失败次数
OPEN_SECONDS = 60              # 熔断冷却
EMPTY_NEGATIVE_TTL = 45        # 空结果负缓存（秒）
ERROR_NEGATIVE_TTL = 30        # 错误负缓存（秒）
HALF_OPEN_PROBE = True         # 冷却后允许一次探测

# 自适应禁用（v2.7）
DISABLE_AFTER_OPENS = 3        # 连续 open 达此次数 → 自动禁用（持久跳过）
DISABLE_COOLDOWN_SECONDS = 3600  # 禁用后 1h 内不自动恢复（避免频繁探测）

# auth 归因直达禁用（2026-11-08）：401/403/410 这类结构性失败不会因等待
# 自愈（rerank:bocha 实测累计 18 次开闸、每次冷却到期都付一次 403 RTT），
# 与按 opens 阶梯爬到 DISABLE_AFTER_OPENS 的 error 类不同——连续 2 次 auth
# 归因失败即直达 disabled，恢复通道=换 key（指纹变化即自愈）/ revive / 1h 探测。
AUTH_DISABLE_AFTER = 2
# auth/quota 类归因的判定集合：与 engine_failure 的分类常量同源（那边是
# 「归类」，这边是「据此调整熔断策略」——边界仍是 engine_failure 只归类、
# circuit_breaker 掌管策略）。
_AUTH_ESCALATE_CATEGORIES = frozenset({"auth", "quota_exhausted"})

# 空结果基线破位（2026-11-08）：empty 按设计不驱动开闸（防误伤易空引擎），
# 但「历史常非空 + 近期连续空 + 跨查询」是健康引擎异常变空的信号（met_museum
# /search 410 被吞成空结果死了 5 周无人知）。只对 ever_nonempty 的引擎生效
# ——从零空历史起步的引擎（含从未成功过的新源）不受影响，这是与统一阈值
# 的本质区别：判定用引擎自己的历史，不用全局常数。
EMPTY_BREACH_STREAK = 3          # 破位需累计的 empty 次数
EMPTY_BREACH_MIN_QUERIES = 2     # 且需跨 ≥2 个不同查询（同查询重复空不算）
EMPTY_RECENT_WINDOW = 7 * 86400  # 只看近 7 天的 empty（更早的不能代表「近期」）


def _engine_key_fp_safe(engine: str) -> str:
    """引擎当前生效密钥的短指纹；失败/无 key 返回空串（不阻断熔断主流程）。

    复用 quota._engine_key_fp：那边已与 search._missing_env_for 同链对齐
    （issue #42，13 个引擎的候选名解析坑在那里修过），这里不重算第二遍。
    惰性导入防循环（quota ← → circuit_breaker 互不顶层依赖）。
    """
    try:
        from quota import get_quota_manager
        return get_quota_manager()._engine_key_fp(engine)
    except Exception:
        return ""


class CircuitBreaker:
    """进程内 + 磁盘共享的引擎熔断器。"""

    def __init__(self, state_path: str | None = None):
        # 读模块属性而非 `_default_state_path()`：`STATE_PATH` 可被 monkeypatch，
        # 且每次构造都按当前 ARGO_STATE_DIR 解析（见模块 `__getattr__`）。
        # 注意用 `globals().get`：函数体内的裸名查找不触发 PEP 562 `__getattr__`。
        self._path = state_path if state_path else (
            globals().get("STATE_PATH") or _default_state_path())
        self._lock = threading.RLock()
        self._engines: dict[str, dict[str, Any]] = {}
        self._neg: dict[str, dict[str, Any]] = {}  # key → {expires, status}
        self._last_save_ts: float = 0.0  # 最近一次成功落盘时刻（观测用）
        self._sig: tuple[int, int] | None = None
        self._load()

    def _file_sig(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self._path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _fresh_locked(self) -> None:
        sig = self._file_sig()
        if sig != self._sig and (sig is not None or self._sig is not None):
            if sig is None:
                self._engines = {}
                self._sig = None
            else:
                self._load()

    def _load(self) -> None:
        try:
            # 直接 open 而非 os.path.exists + open：后者有 check-then-act 竞态
            # （文件可能在两者之间被删除），且 FileNotFoundError 已被外层捕获。
            # with 关闭句柄：原 open(...).read() 靠 CPython 引用计数兜住，
            # 在别的解释器实现上会泄漏 fd（SIM115）
            with open(self._path, encoding="utf-8") as f:
                data = json.loads(f.read())
            self._engines = data.get("engines") or {}
            self._sig = self._file_sig()
            # 负缓存仅进程内有效，不从磁盘恢复（避免长期脏状态）
        except FileNotFoundError:
            self._sig = None  # 文件不存在，保留空态
        except Exception as e:
            # 解析失败**不清空**已有记忆：清空 = 已被判死的源重新进路由、
            # auto-disable 全部重置，而且没有任何提示（用户看到的是「今天
            # 网络又不行了」）。与 quota.py 的处理方式保持一致——那边
            # 明确「损坏不清空，保留旧状态」，这里此前相反：坏文件一出现就悄悄清零。
            # 首次加载（_engines 为空）时保留空态是安全的：没有记忆可丢。
            import sys as _sys
            print(f"[circuit-breaker] 状态文件不可解析（{type(e).__name__}），"
                  f"保留内存态 {len(self._engines)} 条：{self._path}",
                  file=_sys.stderr)

    # 引擎熔断 ────────────────────────────────────────────────────────────

    def status(self, engine: str) -> dict[str, Any]:
        """只读查询引擎熔断状态（不推进 half-open 探测、不写入文件）。

        供路由层做「配额/熔断感知沉底」：主引擎熔断打开时自动切换到
        相近备选，正常路径组合集合不变，缓存键不变，速度零影响。
        """
        with self._lock:
            self._fresh_locked()
            st = self._engines.get(engine) or {}
            state = st.get("state", "closed")
            opened_at = float(st.get("opened_at") or 0)
            return {
                "state": state,
                "failures": int(st.get("failures") or 0),
                "opened_at": opened_at,
                "last_kind": st.get("last_kind"),
                "last_attribution": st.get("last_attribution"),
                # 基线破位画像（2026-11-08）：历史常非空的引擎近期连续空结果
                # 且跨查询——疑似失效，供排序降权与 stats 分诊；不是熔断，
                # 一次真实成功即解除。
                "suspected_stale": self._suspected_stale_locked(st),
                "auth_disabled": bool(st.get("auth_disabled")),
                "auth_streak": int(st.get("auth_streak") or 0),
                # 用 time.time()（墙钟）而非 time.monotonic()：熔断状态持久化到文件，
                # 跨进程恢复时 opened_at 必须可比较。monotonic 在不同进程间基准不同，
                # 换用会导致跨进程冷却判断错误。时钟回拨风险可控（只会让冷却提前结束）。
                "cooldown_remain": max(0, int(OPEN_SECONDS - (time.time() - opened_at)))
                if state == "open" else 0,
            }

    @staticmethod
    def _suspected_stale_locked(st: dict[str, Any]) -> bool:
        """空结果基线破位判定（锁内调用，纯读）。

        三个条件缺一不可：引擎曾交出过非空结果（ever_nonempty， 易空引擎
        的「常态空」不触发）；近 7 天 empty 累计达 EMPTY_BREACH_STREAK；
        这些 empty 跨 ≥EMPTY_BREACH_MIN_QUERIES 个不同查询（同一查询重复
        空是「这个查询没结果」，不是「引擎坏了」）。
        """
        if not st.get("ever_nonempty"):
            return False
        now = time.time()
        recent = [(fp, ts) for fp, ts in (st.get("empty_recent") or [])
                  if now - float(ts) <= EMPTY_RECENT_WINDOW]
        if len(recent) < EMPTY_BREACH_STREAK:
            return False
        distinct = {fp for fp, _ in recent if fp}
        return len(distinct) >= EMPTY_BREACH_MIN_QUERIES

    def allow(self, engine: str) -> tuple[bool, str]:
        """是否允许调用该引擎。返回 (allowed, reason)。"""
        with self._lock:
            self._fresh_locked()
            st = self._engines.get(engine) or {}
            state = st.get("state", "closed")
            opened_at = float(st.get("opened_at") or 0)

            # 自适应禁用：disabled 引擎直接拒绝（不再 half-open 探测，省超时）。
            # 但冷却期（DISABLE_COOLDOWN_SECONDS）过后自动转 half_open 探测，
            # 避免引擎恢复后永久无入口（B4）。disabled_at=0 视为旧数据，直接放行探测。
            if state == "disabled":
                # auth 禁用的换 key 自愈：401/403 的恢复方式是「修好凭证」，
                # 而配 key 是离散事件——指纹变了就说明修好了，立即放行，
                # 不用等 1h 冷却（与 quota 的 key_fp 自愈同一思路）。
                if st.get("auth_disabled") and st.get("key_fp"):
                    cur = _engine_key_fp_safe(engine)
                    if cur and cur != st.get("key_fp"):
                        def _key_healed() -> None:
                            fresh = self._engines.get(engine) or {}
                            fresh.update({"state": "closed", "failures": 0,
                                          "opens": 0, "auth_streak": 0})
                            fresh.pop("auth_disabled", None)
                            fresh.pop("key_fp", None)
                            fresh["last_ok"] = time.time()
                            self._engines[engine] = fresh
                        self._mutate_locked(_key_healed)
                        return True, "auth_key_changed"
                disabled_at = float(st.get("disabled_at") or 0)
                if disabled_at == 0 or \
                        (time.time() - disabled_at) >= DISABLE_COOLDOWN_SECONDS:
                    def _reenable_probe() -> None:
                        # 锁内重读后再改：_reload_engines 刚整体替换过
                        # self._engines，闭包里的旧 st 会把另一进程刚落盘的
                        # 状态盖回去（017b01a 修的那类丢更新在 allow() 的漏网处）。
                        fresh = self._engines.get(engine) or {}
                        fresh["state"] = "half_open"
                        fresh["disabled_at"] = time.time()  # 本次探测起点，失败则重新计冷却
                        self._engines[engine] = fresh
                    self._mutate_locked(_reenable_probe)
                    return True, "half_open_reenable"
                return False, "auto_disabled"

            if state == "open":
                # 冷却期已过 → half-open 探测（除非已连续多次 open 触发自动禁用）
                if time.time() - opened_at >= OPEN_SECONDS:
                    opens = int(st.get("opens") or 0)
                    disabled_at = float(st.get("disabled_at") or 0)
                    # 禁用条件：连续 open 达阈值，且距离上次禁用已超冷却（首次 disabled_at=0 视为可禁用）
                    can_disable = opens >= DISABLE_AFTER_OPENS and \
                        (disabled_at == 0 or
                         (time.time() - disabled_at) >= DISABLE_COOLDOWN_SECONDS)
                    if can_disable:
                        def _auto_disable() -> None:
                            fresh = self._engines.get(engine) or {}  # 同上：锁内重读
                            fresh["state"] = "disabled"
                            fresh["disabled_at"] = time.time()
                            self._engines[engine] = fresh
                        self._mutate_locked(_auto_disable)
                        return False, "auto_disabled"
                    # half-open：允许一次探测
                    def _half_open() -> None:
                        fresh = self._engines.get(engine) or {}  # 同上：锁内重读
                        fresh["state"] = "half_open"
                        self._engines[engine] = fresh
                    self._mutate_locked(_half_open)
                    return True, "half_open_probe"
                remain = int(OPEN_SECONDS - (time.time() - opened_at))
                return False, f"circuit_open:{remain}s"
            return True, "closed"

    def _mutate_locked(self, mutator) -> None:
        """跨进程安全的「重读 → 改 → 写入」序列（与 quota._mutate_locked 同形）。

        进程内 RLock 只挡得住同进程线程；CLI / MCP server / 评测脚本三者并行时，
        各自在构造期读了一次旧状态、各自 +1、后写者覆盖前写者，计数直接丢失。
        这里在文件锁内重读最新磁盘态再改再写，增量才不丢。fail-open：锁层
        出问题绝不阻断搜索主路径（同 argo_paths.file_lock 的契约）。
        """
        with self._lock:
            applied = False
            try:
                with _paths.file_lock(Path(self._path)):
                    self._reload_engines()
                    mutator()
                    applied = True
                    _paths.atomic_write_json(
                        Path(self._path),
                        {"engines": self._engines, "updated": time.time()},
                        indent=None,
                    )
                    self._sig = self._file_sig()
                    self._last_save_ts = time.time()
            except Exception:
                # 锁/写盘失败：若变更尚未落到内存则补一次，保证本进程内行为
                # 正确；已应用过就绝不再跑（否则计数会翻倍）。丢的只是跨进程
                # 可见性，下一次成功写入会带上。
                if not applied:
                    try:
                        mutator()
                    except Exception:
                        pass

    def _reload_engines(self) -> None:
        """重读磁盘上的引擎态（只在文件锁内调用）。解析失败保留内存态。"""
        try:
            # 直接 open 而非 os.path.exists + open：消灭 check-then-act 竞态
            with open(self._path, encoding="utf-8") as f:
                self._engines = (json.loads(f.read()).get("engines") or {})
            self._sig = self._file_sig()
        except FileNotFoundError:
            self._sig = None  # 文件不存在，保留内存态
        except Exception:
            pass

    def record_success(self, engine: str) -> None:
        """引擎成功返回：本已健康 closed 且无失败计数时内存短路（零文件锁、零写盘）。"""
        with self._lock:
            self._fresh_locked()
            st = self._engines.get(engine)
            if st is None:
                # 首次成功也是一次「交出非空结果」：基线在此立起（record_success
                # 只在交出可用结果时被调用，见 engine_dispatch 的贡献者判据）。
                self._engines[engine] = {"state": "closed", "failures": 0, "opens": 0,
                                         "ever_nonempty": True}
                return
            if (st.get("state", "closed") == "closed"
                    and not st.get("failures")
                    and not st.get("opens")
                    and not st.get("empty_streak")
                    and not st.get("empty_recent")):
                # 已健康且无任何待清理状态：内存短路，零写盘
                return

        def _m() -> None:
            self._engines[engine] = {
                "state": "closed",
                "failures": 0,
                "opens": 0,          # 重置连续 open 计数
                # ever_nonempty 是基线（「这引擎交出过非空结果」），粘性字段：
                # 空结果破位判定（_suspected_stale_locked）依赖它，成功不擦除，
                # 否则每次成功都会把基线抹掉、破位永远立不起来。破位的解除靠
                # empty_recent 清空（下面），不靠抹基线。
                "ever_nonempty": True,
                "empty_recent": [],   # 空结果破位解除：一次真实成功即清零
                "auth_streak": 0,     # 连续 auth 归因失败计数同步归零
            }
        self._mutate_locked(_m)

    def record_failure(self, engine: str, kind: str = "error",
                       attribution: dict[str, Any] | None = None,
                       query: str = "") -> None:
        """kind: error | timeout | empty | blocked | rate-limited

        attribution：可选的失败归因（失败现场记录，含 category/reason/detail）。
        kind 是熔断策略用的粗粒度标签，attribution 是给「为什么坏」用的细粒度
        事实——两者维度不同，不能互相推导（把 kind 当响应文本再归类，只能得到
        unknown）。持久化后 `--list-engines --detail` 才能显示真实原因。
        query：仅 empty 用——按查询指纹记录近期空结果，供基线破位判定做
        「跨 ≥2 个不同查询」的去重（同一查询反复空不构成引擎失效证据）。

        empty 语义是「该查询无结果」——查询级信号，不是引擎级故障。
        它仍可触发 60s 短冷却（防止重复打无效源），但不累计 opens，
        因此永远不会驱动 auto-disable（否则聚合引擎/local 源等易空
        引擎会被误判为持续故障而静默禁用）。

        blocked / rate-limited 语义是「源站行为，不是引擎坏了」——前者是
        请求在到达内容前被拦截（反爬/指纹/挑战页），后者是源端限流。
        同样只做 60s 短冷却、不累计 opens：把封锁/限流当引擎故障累计到
        auto-disable 是封错人——引擎实现没有问题，换客户端形态、
        等冷却或等源站策略变化即可恢复。
        """
        self._mutate_locked(
            lambda: self._mutate_failure(engine, kind, attribution, query))

    def _mutate_failure(self, engine: str, kind: str,
                        attribution: dict[str, Any] | None,
                        query: str = "") -> None:
        """`record_failure` 的状态变更体（只改内存，不落盘）。

        拆出来是为了让整段「重读 → 改 → 写」跑在同一个文件锁内：进程内
        RLock 只挡同进程线程，CLI / MCP server / 评测脚本并行时各自持有
        构造期读到的旧快照、各自 +1、后写者覆盖前写者。实测 4 进程 × 20 次
        record_failure 丢 48%（6 进程丢 35%，8 进程丢 44%），丢掉的正是
        `failures` / `opens` 增量——`DISABLE_AFTER_OPENS` 永远攒不够，
        真坏掉的引擎于是持续被派发。这与 quota._mutate_locked 同一处理。
        """
        with self._lock:
            st = self._engines.get(engine) or {"failures": 0, "state": "closed"}
            drives_opens = kind not in ("empty", "blocked", "rate-limited")
            # auth/quota 归因直达禁用（2026-11-08）：结构性失败（401/403/410、
            # 配额耗尽）不会因等待自愈，走 opens 阶梯只是反复付无效 RTT
            # （rerank:bocha 实测 opens=18）。连续 AUTH_DISABLE_AFTER 次 auth
            # 归因失败 → 直达 disabled；恢复=换 key（指纹自愈）/revive/1h 探测。
            category = (attribution or {}).get("category")
            if category in _AUTH_ESCALATE_CATEGORIES:
                st["auth_streak"] = int(st.get("auth_streak") or 0) + 1
            elif kind != "empty":
                # 空/限流/拦截类失败不重置 auth 连击（它们不是「auth 修好了」
                # 的证据），但也只有 auth/quota 归因本身推进连击。
                st["auth_streak"] = 0
            if (st.get("auth_streak", 0) >= AUTH_DISABLE_AFTER
                    and st.get("state") != "disabled"):
                st["state"] = "disabled"
                st["disabled_at"] = time.time()
                st["auth_disabled"] = True
                if category == "auth":
                    fp = _engine_key_fp_safe(engine)
                    if fp:
                        st["key_fp"] = fp
            # empty 权重低：两次 empty 才算一次 failure 贡献；同时按查询指纹
            # 记近期空结果，供基线破位判定（跨查询去重）
            if kind == "empty":
                st["empty_streak"] = int(st.get("empty_streak") or 0) + 1
                fp = ""
                if query:
                    import hashlib as _hl
                    fp = _hl.sha1(query.encode("utf-8")).hexdigest()[:12]
                recent = [(f, t) for f, t in (st.get("empty_recent") or [])
                          if time.time() - t <= EMPTY_RECENT_WINDOW]
                recent.append((fp, time.time()))
                st["empty_recent"] = recent[-6:]
                if st["empty_streak"] < 2:
                    self._engines[engine] = st
                    return
                st["empty_streak"] = 0
            st["failures"] = int(st.get("failures") or 0) + 1
            st["last_fail"] = time.time()
            st["last_kind"] = kind
            if attribution:
                st["last_attribution"] = dict(attribution)
            # empty（无结果）与 blocked（被拦截）都是查询级/源站级信号，不驱动
            # open 熔断：否则「空结果→open 60s→half_open→再 open」无效 churn，
            # 还占主位阻塞 6s。仅 error/timeout 驱动稳定 state 切换（空结果由
            # 负缓存短 TTL 保底）。2026-08 修复；blocked 2026-09 并入同语义。
            # 已因 auth 直达 disabled 的不再被 opens 块盖回 open（禁用优先）。
            if (drives_opens and st.get("state") != "disabled"
                    and (st["failures"] >= FAILURE_THRESHOLD
                         or st.get("state") == "half_open")):
                st["state"] = "open"
                st["opened_at"] = time.time()
                # 连续 open 计数：仅 error/timeout 计入（引擎级故障），
                # empty 不计入，避免「查询无结果」被误判为引擎持续故障
                st["opens"] = int(st.get("opens") or 0) + 1
            self._engines[engine] = st

    def reenable(self, engine: str) -> None:
        """外部主动恢复（用户测试通过 / 新环境确认）。"""

        def _m() -> None:
            self._engines[engine] = {
                "state": "closed", "failures": 0, "opens": 0,
                "last_ok": time.time(), "reenabled_at": time.time(),
            }

        self._mutate_locked(_m)

    def revive(self, engine: str) -> dict[str, Any]:
        """人工复活（argo engine revive）：清空该引擎的全部熔断/禁用/破位状态。

        与 reenable 的分工：revive 面向「auth 禁用 / 基线破位」这类证据型
        停用——修好凭证或换环境后由人或 CLI 显式叫醒。ever_nonempty 基线
        保留（它记录的是「这引擎历史上交出过结果」，复活不改变历史）。
        """
        cleared: dict[str, Any] = {}
        with self._lock:
            self._fresh_locked()
            st = self._engines.get(engine) or {}
            cleared = {"had_state": st.get("state", "closed"),
                       "auth_disabled": bool(st.get("auth_disabled")),
                       "suspected_stale": self._suspected_stale_locked(st)}
            base = {"state": "closed", "failures": 0, "opens": 0,
                    "last_ok": time.time(), "revived_at": time.time()}
            if st.get("ever_nonempty"):
                base["ever_nonempty"] = True

            def _m() -> None:
                self._engines[engine] = base
            self._mutate_locked(_m)
        return cleared

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """全部引擎熔断状态的只读快照（画像/诊断用，不推进探测）。"""
        with self._lock:
            self._fresh_locked()
            return {k: dict(v) for k, v in self._engines.items()}

    def record_note(self, engine: str,
                    attribution: dict[str, Any] | None = None) -> None:
        """只记录归因（「为什么不行」），完全不动熔断计数与状态。

        用于配额耗尽这类**既不是引擎故障、也不该算失败**的场景：配额状态机
        负责「停用多久」，这里只负责把原因留在可观测面上。此前 record_failure
        是唯一写归因的入口，导致 quota-exhausted 分支为了让配额接管而丢弃
        归因，博查 403 套餐额度不足永远显示 ready——P1 的原始动机场景。
        """
        if not attribution:
            return

        def _m() -> None:
            st = self._engines.get(engine) or {"failures": 0, "state": "closed"}
            st["last_attribution"] = dict(attribution)
            self._engines[engine] = st

        self._mutate_locked(_m)

    def auto_disabled(self) -> list[str]:
        """返回所有处于自动禁用状态的引擎。"""
        with self._lock:
            self._fresh_locked()
            return [e for e, st in self._engines.items()
                    if st.get("state") == "disabled"]

    # ── 查询级负缓存 ────────────────────────────────────────────────────────

    @staticmethod
    def _neg_key(query: str, engine: str) -> str:
        raw = f"neg|{query}|{engine}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def set_negative(self, query: str, engine: str, status: str = "no-results",
                     ttl: int | None = None) -> None:
        ttl = ttl if ttl is not None else (
            EMPTY_NEGATIVE_TTL if status == "no-results" else ERROR_NEGATIVE_TTL
        )
        key = self._neg_key(query, engine)
        with self._lock:
            self._neg[key] = {
                "expires": time.time() + ttl,
                "status": status,
                "engine": engine,
            }

    def get_negative(self, query: str, engine: str) -> Optional[dict[str, Any]]:
        key = self._neg_key(query, engine)
        with self._lock:
            hit = self._neg.get(key)
            if not hit:
                return None
            if time.time() >= float(hit.get("expires") or 0):
                self._neg.pop(key, None)
                return None
            return hit

    def clear_negative(self, query: str, engine: str) -> None:
        key = self._neg_key(query, engine)
        with self._lock:
            self._neg.pop(key, None)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            open_engines = [e for e, s in self._engines.items() if s.get("state") == "open"]
            return {
                "open_engines": open_engines,
                "tracked": len(self._engines),
                "neg_entries": len(self._neg),
            }


_breaker: CircuitBreaker | None = None


def get_breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker()
    return _breaker

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

STATE_PATH = str(_paths.state_path("circuit_breaker.json"))

# 熔断参数
FAILURE_THRESHOLD = 2          # 连续失败次数
OPEN_SECONDS = 60              # 熔断冷却
EMPTY_NEGATIVE_TTL = 45        # 空结果负缓存（秒）
ERROR_NEGATIVE_TTL = 30        # 错误负缓存（秒）
HALF_OPEN_PROBE = True         # 冷却后允许一次探测

# 自适应禁用（v2.7）
DISABLE_AFTER_OPENS = 3        # 连续 open 达此次数 → 自动禁用（持久跳过）
DISABLE_COOLDOWN_SECONDS = 3600  # 禁用后 1h 内不自动恢复（避免频繁探测）


class CircuitBreaker:
    """进程内 + 磁盘共享的引擎熔断器。"""

    def __init__(self, state_path: str = STATE_PATH):
        self._path = state_path
        self._lock = threading.RLock()
        self._engines: dict[str, dict[str, Any]] = {}
        self._neg: dict[str, dict[str, Any]] = {}  # key → {expires, status}
        self._last_save_ts: float = 0.0  # 最近一次成功落盘时刻（观测用）
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self._path):
                # with 关闭句柄：原 open(...).read() 靠 CPython 引用计数兜住，
                # 在别的解释器实现上会泄漏 fd（SIM115）
                with open(self._path, encoding="utf-8") as f:
                    data = json.loads(f.read())
                self._engines = data.get("engines") or {}
                # 负缓存仅进程内有效，不从磁盘恢复（避免长期脏状态）
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
            st = self._engines.get(engine) or {}
            state = st.get("state", "closed")
            opened_at = float(st.get("opened_at") or 0)
            return {
                "state": state,
                "failures": int(st.get("failures") or 0),
                "opened_at": opened_at,
                "last_kind": st.get("last_kind"),
                "last_attribution": st.get("last_attribution"),
                "cooldown_remain": max(0, int(OPEN_SECONDS - (time.time() - opened_at)))
                if state == "open" else 0,
            }

    def allow(self, engine: str) -> tuple[bool, str]:
        """是否允许调用该引擎。返回 (allowed, reason)。"""
        with self._lock:
            st = self._engines.get(engine) or {}
            state = st.get("state", "closed")
            opened_at = float(st.get("opened_at") or 0)

            # 自适应禁用：disabled 引擎直接拒绝（不再 half-open 探测，省超时）。
            # 但冷却期（DISABLE_COOLDOWN_SECONDS）过后自动转 half_open 探测，
            # 避免引擎恢复后永久无入口（B4）。disabled_at=0 视为旧数据，直接放行探测。
            if state == "disabled":
                disabled_at = float(st.get("disabled_at") or 0)
                if disabled_at == 0 or \
                        (time.time() - disabled_at) >= DISABLE_COOLDOWN_SECONDS:
                    def _reenable_probe() -> None:
                        st["state"] = "half_open"
                        st["disabled_at"] = time.time()  # 本次探测起点，失败则重新计冷却
                        self._engines[engine] = st
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
                            st["state"] = "disabled"
                            st["disabled_at"] = time.time()
                            self._engines[engine] = st
                        self._mutate_locked(_auto_disable)
                        return False, "auto_disabled"
                    # half-open：允许一次探测
                    def _half_open() -> None:
                        st["state"] = "half_open"
                        self._engines[engine] = st
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
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    self._engines = (json.loads(f.read()).get("engines") or {})
        except Exception:
            pass

    def record_success(self, engine: str) -> None:
        def _m() -> None:
            self._engines[engine] = {
                "state": "closed",
                "failures": 0,
                "opens": 0,          # 重置连续 open 计数
                "last_ok": time.time(),
            }
        self._mutate_locked(_m)

    def record_failure(self, engine: str, kind: str = "error",
                       attribution: dict[str, Any] | None = None) -> None:
        """kind: error | timeout | empty | blocked | rate-limited

        attribution：可选的失败归因（失败现场记录，含 category/reason/detail）。
        kind 是熔断策略用的粗粒度标签，attribution 是给「为什么坏」用的细粒度
        事实——两者维度不同，不能互相推导（把 kind 当响应文本再归类，只能得到
        unknown）。持久化后 `--list-engines --detail` 才能显示真实原因。

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
        self._mutate_locked(lambda: self._mutate_failure(engine, kind, attribution))

    def _mutate_failure(self, engine: str, kind: str,
                        attribution: dict[str, Any] | None) -> None:
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
            # empty 权重低：两次 empty 才算一次 failure 贡献
            if kind == "empty":
                st["empty_streak"] = int(st.get("empty_streak") or 0) + 1
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
            if drives_opens and (st["failures"] >= FAILURE_THRESHOLD
                                 or st.get("state") == "half_open"):
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

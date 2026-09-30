#!/usr/bin/env python3
"""engine_dispatch.py — 一次搜索的引擎编排。

从 search.py 的 execute_search 中外提：该函数原本一千余行，而「把 N 个引擎跑完
并收账」这段独占三分之一——限流收紧、重试与每引擎墙钟预算、缺 env 拦截、
熔断与负缓存、per-engine 缓存、有界并发（wave-1 hedged race / wave-2 / 串行）、
结局分类与配额记账，全挤在一个函数里，与融合/精排/输出无关的读者也得读它。

**依赖显式注入，不在本模块直接 import search。** 三个理由：

1. 测试用 `patch.object(search, "engine_search", ...)` / `"get_engines"` /
   `"_missing_env_for"` / `"get_cost_factor"` 替换这些入口（race 预算、
   时间窗、阶段耗时三套用例都靠它）。若本模块直接引用 search 的全局名，
   补丁打在 search 上而实际调用走本模块的绑定，测试会**静默失效**——
   假引擎不被调用、真网络被打开。
2. 常量 `_FAST_TOTAL_BUDGET_S` / `_PRIMARY_GRACE_S` 同样被测试打补丁，
   必须由调用方在**调用时**读取模块全局再传进来。
3. 避免 search ⇄ engine_dispatch 循环导入。

结论：新增入口时一律走参数，不写模块级 import search。
"""

from __future__ import annotations

import time
import threading
from typing import Any, Callable

from cache_guard import attempt_cache_write, is_state_failure_outcome  # 失败态判据：配置/状态类失败不写负缓存（2026-09-29）
from time_utils import is_time_capable
from query_signals import (
    cumulative_sufficient,
    results_sufficient,
    query_coverage_ok,
)

# 失败原因分类（engine_failure.py 的类别全集）→ 引擎结果 status 的对照表。
# 未列出的类别经 .get(cat, "error") 归为 error：以后新增失败类别时默认可见，
# 不必再回来补白名单。文本里出现超时特征时再细分为 timeout（见 run_one）。
_NOTE_STATUS = {
    "auth": "auth-failed",          # 凭证失效/未登录
    "rate_limited": "rate-limited",  # 源端限流
    "blocked": "blocked",           # 反爬/拦截页
    "dependency": "error",          # 缺后端命令（requires 未满足）
    "upstream": "error",            # 上游改版/页面结构变化
    "network": "error",             # 连接失败/超时（超时再细分）
    "unknown": "error",             # 信息不足（含非 200 状态码）
}

# 配额耗尽错误关键词（唯一来源）：classify_engine_outcome 的 quota-exhausted
# 分类与自适应学习跳过逻辑共用。新增配额错误码（如新的 API 业务码）只改这里。
_QUOTA_ERROR_KEYWORDS = ("quota", "10406")

# 「这次调用对最终答案有贡献」的 outcome 状态（唯一来源）。
# `no-results` / `skipped-*` / `timeout` / `error` 一律不算——它们消耗了时间但
# 没有交出任何可用结果。wasted 记账与 timing 可观测面共用这张表。
_CONTRIBUTING_STATUS = frozenset({"ok", "ok-cached", "partial"})

# 并发上限的兜底值 = 原硬编码的 3。配置缺失 / 非法 / 读不到时用它，
# 保证「接上配置」这件事不会改变任何既有环境下的行为。
_DEFAULT_MAX_PARALLEL = 3
# deep 模式要多铺几个源，值得付更高并发；其余模式沿用默认值。
_DEEP_MAX_PARALLEL = 5


def _max_parallel_engines(*, depth: str | None = None) -> int:
    """一轮编排内最多同时跑几个引擎。

    唯一来源是 config.yaml 的 `execution.max_parallel_engines`——此前这个键
    只在 config.py 的默认值与 config.yaml 里出现，**全仓没有任何读取点**，
    调度用的是 engine_dispatch 里写死的 3：改配置不生效，且没有任何提示。
    调参的人只会得出「改了没用」的结论，不会想到代码没读它。

    deep 模式不受配置限制（深研要证据覆盖，优先墙钟），但仍保底 1。
    """
    if depth == "deep":
        return _DEEP_MAX_PARALLEL
    try:
        from config import get_execution_config
        n = int(get_execution_config().get("max_parallel_engines",
                                           _DEFAULT_MAX_PARALLEL))
    except Exception:
        return _DEFAULT_MAX_PARALLEL
    return n if n >= 1 else _DEFAULT_MAX_PARALLEL

# 拦截页特征词（唯一来源）：error 文本里出现即判 blocked。HTML 引擎的反爬
# 命中没有 error 文本（静默空结果），走 engines_base 的归因寄存器；
# 这张表兜住「error 结果里带拦截页字样」的可见路径。
_BLOCKED_ERROR_KEYWORDS = (
    "just a moment", "checking your browser", "cf-browser-verification",
    "challenge", "ddos-guard", "perimeterx", "access denied",
    "handshake failure", "unable to handshake", "安全验证", "滑动验证",
)


def classify_engine_outcome(eng: str, res: list[dict[str, Any]],
                            latency_ms: int, status_hint: str | None = None
                            ) -> dict[str, Any]:
    """将单引擎结果归类为可观测 outcome。"""
    if status_hint:
        return {
            "engine": eng, "status": status_hint,
            "results_count": 0, "latency_ms": latency_ms,
        }
    if not res:
        return {
            "engine": eng, "status": "no-results",
            "results_count": 0, "latency_ms": latency_ms,
        }
    errors = [r for r in res if isinstance(r, dict) and "error" in r]
    goods = [r for r in res if isinstance(r, dict) and "error" not in r]
    if errors and not goods:
        msg = str(errors[0].get("error", "")).lower()
        if "timeout" in msg:
            st = "timeout"
        elif any(k in msg for k in _QUOTA_ERROR_KEYWORDS):
            st = "quota-exhausted"
        elif "rate" in msg or "429" in msg:
            st = "rate-limited"
        elif any(k in msg for k in _BLOCKED_ERROR_KEYWORDS):
            st = "blocked"
        elif "auth" in msg or "401" in msg or "403" in msg:
            st = "auth-failed"
        else:
            st = "error"
        return {
            "engine": eng, "status": st,
            "results_count": 0, "latency_ms": latency_ms,
            "detail": str(errors[0].get("error", ""))[:200],
        }
    if goods and errors:
        return {
            "engine": eng, "status": "partial",
            "results_count": len(goods), "latency_ms": latency_ms,
        }
    return {
        "engine": eng, "status": "ok",
        "results_count": len(goods), "latency_ms": latency_ms,
    }


class DispatchResult:
    """编排产出 + 两个钩子（融合后仍要补搜的路径用）。

    `run_one` / `ingest` 必须外露：macro_data 域的 D6 证据下限补搜在融合之后，
    它要跑单个引擎并把结果并进同一份账（raw_results / engine_outcomes /
    engine_latency / wasted 计数），另起一份会让 engines_used 与配额记账对不上。

    `budget_used_ms` / `budget_total_ms`：本次编排的实际墙钟消耗与总预算
    （deep 等无预算模式 total 为 None）。搜索输出把它挂进 timing.budget，
    让「这次离预算上限还有多远」可观测，而不必反推 process_ms。

    """

    __slots__ = ("raw_results", "engine_outcomes", "engine_latency",
                 "wasted_ms", "early_stopped", "run_one", "ingest",
                 "budget_used_ms", "budget_total_ms", "useful_ms")

    def __init__(self, raw_results, engine_outcomes, engine_latency,
                 wasted_ms, early_stopped, run_one, ingest,
                 budget_used_ms=None, budget_total_ms=None,
                 useful_ms: int = 0) -> None:
        self.raw_results = raw_results
        self.engine_outcomes = engine_outcomes
        self.engine_latency = engine_latency
        self.wasted_ms = wasted_ms
        self.early_stopped = early_stopped
        self.run_one = run_one
        self.ingest = ingest
        self.budget_used_ms = budget_used_ms
        self.budget_total_ms = budget_total_ms
        self.useful_ms = useful_ms


def run_dispatch(*, query: str, retrieval_query: str, engines: list[str],
                 decision: dict[str, Any], parallel: bool,
                 domain: str, mode: str, depth: str,
                 max_results: int, timeout: int, net_timeout: float,
                 skip_cache: bool, cache: Any, breaker: Any,
                 since_iso: str | None, until_iso: str | None,
                 t0: float,
                 t0_mono: float | None = None,
                 engine_search: Callable,
                 get_engines_fn: Callable,
                 get_execution_config_fn: Callable,
                 missing_env_for: Callable,
                 classify_outcome: Callable,
                 quota_batch: Any,
                 note_quota_exhausted: Callable,
                 per_engine_budget_s: float,
                 fast_budget_s: float,
                 auto_budget_s: float,
                 deep_budget_s: float | None = None,
                 primary_grace_s: float,
                 straggler_grace_s: float = 1.5,
                 serial_stagger_s: float = 0.8,
                 engine_domain: str | None = None,
                 engine_sub_domain: str | None = None) -> DispatchResult:
    """把 engines 跑完并收账：并发/串行调度 → 结局分类 → 熔断与配额记账。

    依赖全部显式注入（原因见模块头）。返回编排产出与两个补搜钩子。
    """
    raw_results: dict[str, list[dict[str, Any]]] = {}
    engine_outcomes: list[dict[str, Any]] = []
    engine_latency: dict[str, int] = {}

    exec_cfg = get_execution_config_fn()
    retry_count = exec_cfg.get("retry_count", 0)
    # 单引擎墙钟预算：config `execution.per_engine_budget_s` 可覆盖。
    # 用 exec_cfg 读取（与本函数其它 execution 项同源），这样用户可在
    # config.yaml 调整而无需改代码；非法值（非正数）回落到常量默认。
    try:
        _budget_cfg = float(exec_cfg.get("per_engine_budget_s",
                                         per_engine_budget_s))
    except (TypeError, ValueError):
        _budget_cfg = per_engine_budget_s
    if _budget_cfg <= 0:
        _budget_cfg = per_engine_budget_s


    # 慢源禁重试：timeout ≥ 8s 的引擎超时即放弃，避免「10s×3 次=30s」线性放大。
    # 超时本质上是源端慢/网络抖，重试不改变结果，只放大尾延迟；快速失败
    # （连接错/4xx）保留重试，重试成本低。
    try:
        _engine_specs = get_engines_fn()
    except Exception as e:
        import sys
        sys.stderr.write(
            f"[dispatch] 引擎规格加载失败（{type(e).__name__}），"
            f"本次按无声明超时/重试\n")
        _engine_specs = {}

    def _declared_timeout(eng: str) -> float | None:
        """引擎声明的超时（秒）；缺失/非数值/非正数一律 None。

        这段解析此前在「重试策略」与「超时收紧」两处各写了一遍，任何一处漏掉
        类型守卫，另一条路径就会拿到 `None >= 8.0` 的 TypeError；把「声明超时
        长什么样」定在一处，两条路径就永远同步。
        """
        spec = (_engine_specs or {}).get(eng) or {}
        if not isinstance(spec, dict):
            return None
        t = spec.get("timeout")
        # bool 是 int 的子类：`timeout: true` 会被 isinstance 放行成 1.0 秒
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            return None
        return float(t) if t > 0 else None

    def _engine_retries(eng: str) -> int:
        eng_timeout = _declared_timeout(eng)
        if eng_timeout is not None and eng_timeout >= 8.0:
            return 0
        return retry_count

    def _engine_domain_kwargs() -> dict[str, str]:
        """CLI --domain / --sub_domain 的引擎入参（未给则不下发空键）。

        刻意**不**并进缓存键：这两个值此前既没进键也没进请求，而任何引擎
        都可能对同 query 返回不同结果集；先让参数真正生效，键的隔离由
        engine_domain_key 单独补齐（见下方 cache key 注释）。
        """
        out: dict[str, str] = {}
        if engine_domain:
            out["domain"] = engine_domain
        if engine_sub_domain:
            out["sub_domain"] = engine_sub_domain
        return out

    def _exec_engine(eng: str, retries: int | None = None,
                     eff_timeout: float | None = None) -> list[dict[str, Any]]:
        # P0-001：用 retrieval_query（clean_query）检索
        if retries is None:
            retries = _engine_retries(eng)
        # 默认超时用网络感知后的 net_timeout（慢网放大），与外层等待预算一致；
        # 非 tight 引擎（anysearch 等）慢网下同样获得放大窗口。
        to = eff_timeout if eff_timeout is not None else net_timeout

        # ── 每引擎墙钟硬预算 ──────────────────────────────────────────
        # 问题（2026-09-10 实测）：重试会**叠乘**。anysearch 曾同时有
        #   引擎级重试 retry_count=1 → 2 次
        #   HTTP 级重试 max_retries=1 → 2 次
        #   8s 超时
        # 最坏 2×2×8 = 32s（实测 31.3s）。用户侧表现是「搜一个查询卡半分钟」，
        # 而这期间既没有切备选源、也没有任何信号说明在等什么。
        #
        # 修法不是逐处调小超时（那会误杀慢网下正常的源），而是给**单个引擎的
        # 总墙钟**设上界：后续尝试的可用超时 = 剩余预算，预算耗尽即停。
        # 这样无论嵌套几层重试，单引擎都不可能超过 cap。
        # 取值优先级：execution.per_engine_budget_s（config）> 常量默认 10.0。
        # fast 模式已有 6s 全局预算，此处取更紧的那个，避免互相打架。
        _eng_budget = _budget_cfg
        if mode == "fast":
            _eng_budget = min(_eng_budget, fast_budget_s)
        _t_eng_start = _now()

        last_result: list[dict[str, Any]] = []
        for _attempt in range(retries + 1):
            _remain = _eng_budget - (_now() - _t_eng_start)
            # 只跳过**后续**尝试。首次必须发出：若因预算小而整段跳过，
            # 引擎的 outcome 会从 timeout 变成 no-results —— 语义从「慢」
            # 变成「没尝试」，会破坏既有 fast 预算测试的契约
            # （实测：patch 预算 0.5s 时首试被跳过，slow_bad_a/b 被标成
            #  no-results 而非 timeout）。
            if _attempt > 0 and _remain <= 0.5:
                break
            # 每次尝试的可用超时 = min(声明超时, 剩余预算)，下限 0.5s：
            # 首试受总预算约束（否则 fast 的 6s 预算会被 8s 首试突破），
            # 后续尝试自动收缩，保证单引擎总耗时不越界。
            attempt_to = min(to, max(0.5, _remain))
            last_result = engine_search(
                retrieval_query, eng, n=max_results, timeout=attempt_to, depth=depth, mode=mode,
                since=since_iso, until=until_iso, skip_cache=skip_cache,
                **_engine_domain_kwargs(),
            )
            if last_result and any("error" not in r for r in last_result):
                return last_result
        # 慢源（retries=0，超时即弃）不再用 balanced 补跑，避免超时场景双倍耗时
        if retries > 0 and depth != "balanced":
            _remain = _eng_budget - (_now() - _t_eng_start)
            if _remain > 0.5:
                last_result = engine_search(
                    retrieval_query, eng, n=max_results,
                    timeout=min(to, max(0.5, _remain)),
                    depth="balanced", mode=mode,
                    since=since_iso, until=until_iso, skip_cache=skip_cache,
                    **_engine_domain_kwargs(),
                )
        return last_result

    def _run_one(eng: str) -> tuple[str, list[dict[str, Any]], dict[str, Any], int]:
        """单引擎：缺 env → 负缓存 → 熔断 → per-engine 缓存 → 网络。"""
        from engines_base import pop_failure_note
        t_eng = _now()

        # 缺环境变量前置拦截：把「静默 no-results」变成可行动的 error。
        # 显式 engine= 覆盖会绕过路由的 env 过滤（zhihu/exa 未配密钥时曾
        # 返回空列表，用户无法区分「没结果」和「没配置」）。
        missing_env = missing_env_for(eng)
        if missing_env:
            lat = int((_now() - t_eng) * 1000)
            outcome = classify_outcome(
                eng, [], lat, status_hint="skipped-missing-env")
            outcome["detail"] = (
                f"缺少环境变量：{' / '.join(missing_env)}（配置后重试）")
            return eng, [], outcome, lat

        # 时间窗只隔离带时间能力引擎的 per-engine 缓存（与 combo 键同语义）
        eng_since = since_iso if is_time_capable(eng) else None
        eng_until = until_iso if is_time_capable(eng) else None

        # 熔断
        if breaker is not None:
            allowed, reason = breaker.allow(eng)
            if not allowed:
                lat = int((_now() - t_eng) * 1000)
                outcome = classify_outcome(eng, [], lat, status_hint="skipped-circuit-open")
                outcome["detail"] = reason
                return eng, [], outcome, lat
            neg = breaker.get_negative(query, eng)
            if neg:
                lat = int((_now() - t_eng) * 1000)
                outcome = classify_outcome(
                    eng, [], lat, status_hint="no-results-cached",
                )
                outcome["detail"] = neg.get("status", "no-results")
                return eng, [], outcome, lat

        # per-engine 缓存
        if not skip_cache:
            eng_hit = cache.get_engine(
                query, eng, max_results, domain=domain, mode=mode, depth=depth,
                since=eng_since, until=eng_until,
                engine_domain=engine_domain, engine_sub_domain=engine_sub_domain,
            )
            if eng_hit is not None:
                lat = int((_now() - t_eng) * 1000)
                # 标记缓存来源
                for r in eng_hit:
                    if isinstance(r, dict):
                        r.setdefault("_engine", eng)
                outcome = classify_outcome(eng, eng_hit, lat)
                outcome["status"] = "ok-cached" if eng_hit else "no-results-cached"
                return eng, eng_hit, outcome, lat

        # 网络调用
        # 答案型域（early_min 存在，1 条快照即可交付）的慢源收紧超时：
        # FRED/Eurostat 这类 timeout=10s 的源一旦挂掉就阻塞整条串行路径，
        # 而快源（worldbank 等 ~150ms）已能交付答案。慢源 5s 内没回就让位。
        # 非答案域（fast/auto/budget 且非 deep）：timeout≥10s 的引擎同样收紧
        # 到 6s——多数正常引擎 <2s，10-15s 的超时只为极端慢源保底，
        # 串行/并行组合里一个慢源就会拖垮整个响应尾部。
        eff_to: float | None = None
        _tighten = (early_min is not None) or (
            mode in ("fast", "auto", "budget") and depth != "deep"
        )
        if _tighten:
            eng_to = _declared_timeout(eng)
            cap = 5.0 if early_min is not None else 6.0
            # half_open 半开探测收紧到 2s：熔断器允许半开探测恢复，但探测应短促，
            # 避免 6s 探测阻塞串行/并行主路径（慢源拖尾主因）。2026-08 修复。
            if breaker is not None:
                try:
                    if breaker.status(eng).get("state") == "half_open":
                        cap = min(cap, 2.0)
                except Exception as _exc:
                    # 读失败按默认 cap 处理（fail-open），但必须留痕：
                    # 静默回退曾让「探测收紧到 2s」的慢源修复失明
                    import logging
                    logging.getLogger("unified_search").debug(
                        f"breaker.status({eng}) 读取失败，探测收紧未生效: {_exc!r}")
            if eng_to is not None and eng_to >= 8.0:
                eff_to = min(float(timeout), cap)
            # 声明值 < 8s 的收紧由 engines.search 分发层统一执行
            # （spec timeout 是硬上限，调用方超时不得覆盖）
        try:
            res = _exec_engine(eng, eff_timeout=eff_to)
        except Exception as e:
            res = [{"error": str(e), "source": eng}]
        lat = int((_now() - t_eng) * 1000)
        for r in res:
            if isinstance(r, dict):
                r.setdefault("_engine", eng)
                r.setdefault("_elapsed", lat / 1000.0)

        outcome = classify_outcome(eng, res, lat)
        # 失败原因合入：引擎内部那些不报错的失败路径（反爬命中/HTTP 状态码/网络异常）
        # 没有 error 文本，结果会落成 no-results；失败原因记录把它们还原成
        # 真实状态，供熔断与 --json 可观测面使用。
        #
        # 改用「类别对照表 + 文本细分」而不是 if-elif 白名单：白名单只认
        # blocked/rate_limited/auth，engine_failure.py 类别全集里的
        # network（超时/连接失败）、upstream（上游改版）、unknown（非 200
        # 状态码）、dependency（缺后端命令）会被当成 no-results——
        # 2026-09-15 实测：`--engine you` 的 SSL 超时上报成
        # `status=completed, count=0, errors=[]`，调用方（Agent）据此判定
        # 「网上没有这个信息」，而引擎实际坏了；熔断还按 empty 记账
        # （不累计 opens、负缓存用 EMPTY_NEGATIVE_TTL 45s 而非 30s）。
        _note = pop_failure_note(eng)
        _attr: dict[str, Any] | None = None
        if _note and outcome["status"] in ("no-results", "error", "auth-failed"):
            _text = f"{_note.get('reason', '')} {_note.get('detail', '')}".strip()
            _mapped = _NOTE_STATUS.get(str(_note.get("category") or ""), "error")
            if _mapped == "error" and (
                    "timeout" in _text.lower() or "timed out" in _text.lower()):
                _mapped = "timeout"
            # auth 只在引擎确实无输出时升级：已有明确 error 时不改写（原语义）
            if not (_mapped == "auth-failed" and outcome["status"] != "no-results"):
                outcome["status"] = _mapped
            outcome["detail"] = _text or outcome.get("detail")
        if _note:
            # 归因随熔断状态一起持久化：「为什么坏」必须在失败现场写下来，
            # 事后只能看到 kind 粗标签（把 kind 当响应文本再归类只会得到 unknown）
            try:
                from engine_failure import from_note
                _attr = from_note(_note, eng)
            except ImportError:
                _attr = None
        goods = [r for r in res if isinstance(r, dict) and "error" not in r]
        quota_batch.add(eng, bool(goods))
        if outcome["status"] == "quota-exhausted":
            # 远端配额耗尽：交由 quota 状态机接管（周期边界自愈），
            # 不计入下面的健康熔断——配额问题不是引擎健康问题
            note_quota_exhausted(eng, outcome.get("detail") or "")

        if breaker is not None:
            if outcome["status"] in _CONTRIBUTING_STATUS:
                # 「交出可用结果」的判据只在这一张表里（同 _ingest 的 wasted 记账）。
                # partial 是「有结果 + 有错误条目」，它同样属于贡献者；此前它落到
                # 最后的 else，被记成 kind=error 的失败——既累计 opens（可能把
                # 一个正常交付的引擎推向 auto-disable），又写进负缓存 30s，于是
                # 同一个查询再搜时直接跳过该引擎，把它自己刚交出来的结果丢掉。
                breaker.record_success(eng)
                breaker.clear_negative(query, eng)
            elif outcome["status"] == "quota-exhausted":
                # 配额问题不是引擎健康问题，停用交给配额状态机（上面已记账）；
                # 但归因必须留下——「为什么不行」正是这一支的可观测缺口。
                breaker.record_note(eng, _attr)
            elif outcome["status"] == "no-results":
                breaker.record_failure(eng, kind="empty", attribution=_attr)
                breaker.set_negative(query, eng, status="no-results")
            elif outcome["status"] == "timeout":
                breaker.record_failure(eng, kind="timeout", attribution=_attr)
                breaker.set_negative(query, eng, status="timeout")
            elif outcome["status"] in ("blocked", "rate-limited"):
                # 被拦截 / 被限流都是源站行为，不是引擎故障：60s 短冷却，
                # 不累计 opens（否则被封引擎会被冤枉 auto-disable）。
                breaker.record_failure(eng, kind=outcome["status"], attribution=_attr)
                breaker.set_negative(query, eng, status=outcome["status"])
            else:
                breaker.record_failure(eng, kind="error", attribution=_attr)
                # 配置/状态类失败（缺密钥/熔断/鉴权/配额）不写负缓存：它们
                # 换个环境就能好，把「此刻没配对」固化成「这个查询没答案」
                # 正是 cache_guard.FailedStateCacheRejected 要防的同一类
                # 事故（issue #12 同构：配好 key 仍回放失败）。熔断计数保留
                # ——引擎确实不健康，只是这个查询不该被负缓存连坐。
                if not is_state_failure_outcome(outcome):
                    breaker.set_negative(query, eng, status=outcome["status"])

        # 写 per-engine 缓存。守卫拒绝（登录态 / 退化 / 失败态）只该跳过这次写入：
        # 异常不接的话会被 _daemon_start 的兜底 except 接走，把一次**成功**的
        # 引擎调用改写成 status=error、结果清空，还按 kind=error 记账进熔断器
        # ——健康引擎被自己刚交出的结果推向 auto-disable（2026-09-27 实锤）。
        # 空结果同样要写（短 TTL 负缓存，配合熔断的 set_negative）；TTL 的
        # 空/非空分支由 cache.set_engine 内部处理，这里只决定写什么。
        # 配置/状态类失败（缺密钥/熔断/鉴权/配额）连空负缓存也不写：配好
        # 密钥后 45s 内仍要回放 {"results": []}，与 combo 层同款事故。
        if not skip_cache and not is_state_failure_outcome(outcome):
            attempt_cache_write(
                lambda: cache.set_engine(
                    query, eng, max_results, goods if goods else [],
                    domain=domain, mode=mode, depth=depth,
                    since=eng_since, until=eng_until,
                    engine_domain=engine_domain, engine_sub_domain=engine_sub_domain,
                ),
                context=f"dispatch.cache_set_engine({eng})",
            )

        return eng, (goods if goods else res), outcome, lat

    def _ingest(eng: str, res: list, outcome: dict, lat: int) -> None:
        raw_results[eng] = res
        engine_outcomes.append(outcome)
        engine_latency[eng] = lat
        # 完成时刻（相对本次编排起点）：wasted 的真值来源，见函数尾部的记账
        engine_done_ms[eng] = (_now() - _budget_base) * 1000.0
        if outcome["status"] in _CONTRIBUTING_STATUS:
            contributed_ms.append(engine_done_ms[eng])

    engine_done_ms: dict[str, float] = {}
    contributed_ms: list[float] = []
    # 被编排主动**弃置**的引擎（早停收工时仍在跑的）已经花掉的墙钟。
    # 这些时间既没换来结果、也没拖住返回（函数当刻就返回了），此前只把
    # lat_ms 记进 engine_latency 就丢掉——「答案就绪后还在等」这个口径在
    # 有界并发下恒为 0（最后完成的引擎就是触发早停的那个，wall==useful），
    # 仪表盘因此永远显示 0，无法据此调 tail_grace / hedge / 并发上限。
    abandoned_ms: list[float] = []
    # 配额批次的构造在调用方：flush 必须发生在**最后一个 _ingest 之后**，
    # 而那个点在融合后的 D6 补搜里（本模块之外）。在这里另建一个实例，
    # 调用方 flush 到的就是空批次——补搜引擎的记账永远落不了盘。
    # 预算与延迟统一走调用方选定的钟：传 t0_mono（单调钟）时整套换
    # time.monotonic——墙钟会被 NTP 跳变拉扯，预算窗随之失真；不传时保持
    # wall _now()，与既有调用方/测试逐位兼容。垫片必须是**整套**换：
    # deadline 基准与所有 now 采样混用两种钟，预算判断就是废纸。
    _now = time.monotonic if t0_mono is not None else time.time
    _budget_base = t0_mono if t0_mono is not None else t0
    early_stopped = False
    to_run = list(engines)
    # deep 模式全量并行；fast/auto/budget 可渐进 early-stop
    allow_early = mode in ("fast", "auto", "budget") and depth != "deep"

    # 总墙钟预算：deadline 之后不再起新引擎、不再等待慢线程。
    # fast 6s（成本优先）/ auto·budget 10s（质量优先但有界）。
    # deep 此前不设预算（研究场景宁可等待），但 2026-09-30 实测它被一个慢源
    # 独占：firecrawl 声明 timeout=20，单查询 dispatch 7338ms 里 6470ms 在等它，
    # 而 wave 判据早已认定「累计够用」——「宁可等待」的本意是**不早停**，
    # 不是「让最慢的那个源定义墙钟」。给 deep 也上预算（search._DEEP_TOTAL_BUDGET_S，
    # 默认 15s，execution.deep_budget_s 可覆盖）：只约束「还等不等/还起不起」，
    # 已完成引擎的证据一条不丢，正常 deep 查询（p50 7s）完全无感。
    budget_s = {"fast": fast_budget_s,
                 "auto": auto_budget_s,
                 "budget": auto_budget_s}.get(mode)
    if budget_s is None and mode == "deep" and deep_budget_s:
        budget_s = deep_budget_s
    _deadline = _budget_base + (budget_s if budget_s is not None else float("inf"))

    early_min = decision.get("early_stop_min_results")
    no_early = bool(decision.get("no_early_stop", False))

    def _cumulative_stop(eng: str, goods: list[dict[str, Any]]) -> bool:
        """wave-2 收工判据：把已完成的引擎合起来看是否已够（跨引擎累计）。"""
        return (not no_early) and cumulative_sufficient(
            raw_results, mode=mode, min_results=early_min, query=query)

    def _daemon_start(eng: str):
        """daemon 线程跑 _run_one：弃置线程不阻塞进程退出。"""
        holder: dict[str, Any] = {"t0": _now()}

        def _work() -> None:
            try:
                holder["r"] = _run_one(eng)
            except Exception as exc:
                holder["r"] = (
                    eng,
                    [{"error": str(exc), "source": eng}],
                    classify_outcome(
                        eng, [{"error": str(exc), "source": eng}], 0),
                    0,
                )
        t = threading.Thread(target=_work, daemon=True)
        t.start()
        return holder, t

    def _ingest_holder(holder: dict[str, Any]) -> None:
        r = holder.get("r")
        if r:
            _ingest(r[0], r[1], r[2], r[3])

    def _holder_goods(holder: dict[str, Any]) -> list[dict[str, Any]]:
        r = holder.get("r")
        if not r:
            return []
        return [x for x in r[1] if isinstance(x, dict) and "error" not in x]

    def _settle_pending(pending: list[tuple[dict[str, Any], Any, str]],
                        *, status: str) -> None:
        """收尾一组待定线程：按弃置原因记账（daemon 自行结束，不阻塞退出），
        恰在末次轮询后完成的照常入账——否则它既不 ingest 也不标状态，
        结果会悄悄丢掉。latency 用真实等待时长（原 timeout 参数×1000 是假值）。

        status 对应两种不同的弃置原因，必须分开：
          - "timeout"：等待预算到期仍活——引擎确实超窗，沿用 timeout 语义
            （error 条目 + 熔断可见的 outcome）；race 双超窗测试依赖此形态。
          - "cancelled"：早停收工（结果已够）主动弃置——引擎没有失败，是
            编排的收工决定。此前一律标 timeout 的实测后果：每次带对冲且主搜
            成功的搜索，errors[] 必出一条假超时（anysearch: timeout @26ms，
            8s 超时上限下 26ms 不可能是真超时）；且 adaptive 学习器把
            {"error": ...} 记成 success=False，健康引擎的 combo 分数被早停
            逐次毒化（2026-09-26 复现并定位）。
        """
        for holder, th, eng in pending:
            lat_ms = int((_now() - holder.get("t0", _now())) * 1000)
            if th.is_alive():
                if status == "cancelled":
                    # 空列表而非 error 条目：error 条目会被学习器当失败、
                    # 被 _collect_errors 当错误文本收进 errors[]
                    # 弃置的引擎白花的墙钟计入 wasted（见 abandoned_ms）：
                    # 它是「调度多花了多少」的量，不是「多等了多久」。
                    abandoned_ms.append(lat_ms)
                    _ingest(eng, [],
                            classify_outcome(eng, [], lat_ms, "cancelled"),
                            lat_ms)
                else:
                    # 走 _ingest 而不是手写两行收账：超时路径此前漏记 engine_latency，
                    # 而 timing.dispatch 的 engines_run / engine_sum_ms / parallel_efficiency
                    # 全是从 engine_latency 推的——一轮里引擎全部超时时，会报出「跑了 0 个
                    # 引擎」，而那恰恰是最该被看见的一轮。归一到同一处记账，以后 _ingest
                    # 再加字段，超时路径自动跟上。
                    res = [{"error": "timeout", "source": eng}]
                    _ingest(eng, res,
                            classify_outcome(eng, res, lat_ms, "timeout"), lat_ms)
            else:
                _ingest_holder(holder)

    def _run_engines_bounded(engs: list[str], wait_s: float,
                             stop: Callable[[str, list[dict[str, Any]]], bool] | None = None,
                             stagger_s: float = 0.0,
                             start_before_s: float | None = None,
                             tail_grace_s: float = 0.0) -> bool:
        """并发跑一组引擎（≤3 并发），等待上限 wait_s 秒；返回是否已「够用」。

        `stop(eng, goods)` 是某引擎完成后的收工判据，返回真即立刻收尾返回。
        判据由调用方给：串行路径是「逐引擎充分性」，wave-2 是「跨引擎累计
        充分性」——两种语义留成参数，调度本身只有一套。

        `stagger_s` 起步间隔（秒），单调：0 = 不节流（立即填满并发位，wave-2
        语义）；值越大越接近严格串行（大于引擎超时上限即完全退回串行）。
        串行路径靠它获得对冲：happy path（首个引擎很快给出合格结果）仍然只发
        一次调用，只有首个引擎「异常地慢」时才提前补发备选源。实测意义见本
        函数调用点的注释。

        `start_before_s` 之后不再起新引擎（已起的不受影响，让它跑完自己的
        单引擎预算）。与 wait_s 分开是刻意的：串行路径要「预算耗尽就不要再
        发新引擎」，但不能因此把已经发出去的那一枪的成果丢掉。

        `tail_grace_s`（>0 时生效）：一旦**已经有引擎交出了非空结果**（哪怕
        判据认为不够），剩余等待收窄到这么久。理由是「有东西可交付」和
        「一个能交付的东西都没有」是两种处境——后者值得等，前者已经可以
        收工。与 wave-1 竞速分支的 `_STRAGGLER_GRACE_S` 同一语义、同一开关
        （`ARGO_STRAGGLER_GRACE_S=0` 可整体关闭做消融对照）。

        为什么不用 ThreadPoolExecutor：它的 `with` 退出会
        `shutdown(wait=True)` 并 join 所有已提交任务，把「超时即返回」的语义
        架空——上面每个预算判断都以为自己已经止损，进程却还在等一个卡住的
        HTTP 读（models.dev 全量 API 超时 15s，实测单查询被拖到 76s，而
        w2_wait / deadline 早已到期）。daemon 线程 + 轮询才真正有界：
        早停后弃置线程既不阻塞函数返回，也不阻塞进程退出——与上面的
        hedged 分支共用同一套并发执行方式。
        """
        if not engs:
            return False
        queue = list(engs)
        pending: list[tuple[dict[str, Any], Any, str]] = []
        deadline = _now() + max(0.0, wait_s)
        gate = None if start_before_s is None else _now() + max(0.0, start_before_s)
        last_start: float | None = None
        while (queue or pending) and _now() < deadline:
            started = False
            # 并发上限。此前是写死的 3，而 config.yaml 的
            # execution.max_parallel_engines **从来没被任何代码读过**——
            # 改配置等于没改，是「看着生效、其实没生效」最难查的一类。
            # 兜底 3 是原硬编码值：配置缺失或非法时行为与从前逐位一致。
            max_concurrency = _max_parallel_engines(depth=depth)
            while queue and len(pending) < max_concurrency:
                if gate is not None and _now() >= gate:
                    break  # 预算耗尽：不再起新引擎（既有 fast 契约）
                if (pending and stagger_s > 0.0 and last_start is not None
                        and (_now() - last_start) < stagger_s):
                    break  # 起步节流：等满 stagger 再补发（串行路径的对冲）
                eng = queue.pop(0)
                holder, th = _daemon_start(eng)
                pending.append((holder, th, eng))
                last_start = _now()
                started = True
            progressed = False
            for item in list(pending):
                holder, th, eng = item
                if th.is_alive():
                    continue
                _ingest_holder(holder)
                pending.remove(item)
                progressed = True
                goods = _holder_goods(holder)
                if stop is not None and stop(eng, goods):
                    _settle_pending(pending, status="cancelled")
                    return True
                if goods and tail_grace_s > 0.0:
                    # 有东西可交付了：剩余等待收窄到宽限窗（只收紧一次，不回扩）
                    deadline = min(deadline, _now() + tail_grace_s)
            if not progressed and not started:
                if not pending:
                    break  # 队列里还有引擎，但起步闸门已关：没有下一步了
                # 自适应轮询间隔：剩余时间充裕时多睡，快到期时少睡。
                # 固定 20ms 在尾部会浪费时间（实测最多浪费 20ms），
                # 自适应后平均等待时长降至 5-8ms。
                remain = deadline - _now()
                time.sleep(min(0.02, remain / 10) if remain > 0 else 0.005)
        _settle_pending(pending, status="timeout")
        return False

    if parallel and to_run and allow_early and len(to_run) > 1:
        # Wave-1 race（2026-09-06）：primary 与次引擎并行起跑，先完成且结果
        # 合格者赢——原「primary 先行」串行等待下，primary 慢则整体慢（实测
        # github 引擎 8.5s 拖尾而次引擎 2.4s 就绪）；race 后墙钟由最先合格
        # 者决定。双成员都不合格则落 wave-2 并行补全（语义不变，且 wave-2
        # 的累计充分性判定天然包含 race 已收入的结果）。
        # 成本语义：hedge 只在主引擎跑过宽限窗还没回来时才多发一次调用——
        # 主引擎在窗内交付就仍然只付 1 次（与串行路径同一套语义、同一个值，
        # 见 search._PRIMARY_GRACE_S 的实测依据）。结果质量仍由充分性判定 +
        # 覆盖守卫把关，先到不等于放行。
        #
        # 实现形态：wave-1 就是「起步间隔 = grace 的有界并发」——primary 先跑，
        # grace 内合格则只付一次调用（成本回退），未完成才补发 backup 并行竞速。
        # 此前这里另有一份手写的赛车循环，与 `_run_engines_bounded` 是同一段
        # 逻辑的两个拷贝（收尸、宽限窗、轮询间隔全都要各维护一遍）。现在两者
        # 是同一函数的两次配置，改一处即改两处。
        grace = max(0.3, min(primary_grace_s, net_timeout * 0.25))
        if _now() + grace > _deadline:
            grace = max(0.0, _deadline - _now())

        def _wave1_stop(eng: str, goods: list[dict[str, Any]]) -> bool:
            """先合格者赢：判据看单个引擎自己的结果，不看跨引擎累计。"""
            if no_early or not goods:
                return False
            return results_sufficient(goods, mode=mode, min_results=early_min,
                                      query=query)

        # 竞速窗口：grace 之后还剩 net_timeout+2 的观察窗（不越过总预算）。
        # 窗口按「从起步算起」折算，所以是 grace + 剩余额度——等价于原来的
        # 「先 join(grace)，再从当下算 min(net_timeout+2, deadline-now)」，
        # 但不必把 grace 白白耗在 join 上。
        #
        # 质量守卫拒绝早停后的等待上限由 tail_grace_s 表达：它防的是「对冲的
        # 备份先回来但零词面交集（实测「上海 地铁 线路图」拿到纽约共享单车
        # 站点），主引擎却已被证明答不了这类查询」时把墙钟拖到窗口耗尽。
        # 守卫没错，错的是拒绝之后没有等待上限。
        race_wait = grace + min(
            net_timeout + 2, max(0.1, _deadline - _now() - grace))
        if _run_engines_bounded(
                to_run[:2], race_wait,
                stop=None if no_early else _wave1_stop,
                stagger_s=grace,
                tail_grace_s=straggler_grace_s):
            early_stopped = True
        # 第 3 个及之后的引擎归 wave-2（竞速是 2 匹马的比赛，再多的交给
        # 累计充分性判定批量补全）
        rest = to_run[2:]
        if (not early_stopped and rest
                and not (budget_s is not None and _now() >= _deadline)):
            # 预算检查（与串行路径 `_now() >= _deadline` 同语义）：
            # deadline 已过不再起新引擎；等待窗口也不越过 deadline——
            # 「总墙钟预算」对并行路径同样成立
            w2_wait = min(net_timeout + 2,
                          max(0.1, _deadline - _now()))
            if _run_engines_bounded(rest, w2_wait, stop=_cumulative_stop):
                early_stopped = True
    elif parallel and to_run:
        # 全量并行（depth=deep，或 mode 不在 fast/auto/budget 内）：没有早停判据，
        # 但**总墙钟预算照样成立**——与上面 wave-2、下面串行两条路径同语义。
        # 此前这里直接等 net_timeout+2，而 budget_s 是按 mode 定的（--depth deep
        # 时 mode 仍是 auto，10s），于是会报出 used_ms > total_ms 的自相矛盾读数。
        _wait = net_timeout + 2
        if budget_s is not None:
            _wait = min(_wait, max(0.1, _deadline - _now()))
        _run_engines_bounded(to_run, _wait)
    else:
        # 串行路径（`parallel=False` 的垂直域）。
        #
        # 它此前是**严格串行**：前一个引擎必须耗满自己的超时上限（收紧后
        # 5-8s）才轮到下一个。实测（2026-09-17，24 条代表性查询）：dispatch
        # 超出「首个可用引擎完成时刻」的部分 p50 39ms、p90 2436ms、合计
        # 22.4s（平均 0.93s/查询，占平均墙钟 36%）——尾部全在这里，而
        # wave-1/wave-2 早就有对冲，只有这条路径没有。
        #
        # 改法不是把垂直域改成并行（那会连 happy path 都多发调用），而是
        # **错开起步**：首个引擎跑过 serial_stagger_s 还没回来才补发备选源。
        # 首个引擎正常完成时仍然只付一次调用（成本语义不变）；它异常地慢时，
        # 备选源不再等到它超时才起跑。垂直域的源（行情/宏观/天气/赛事/地理）
        # 都是免密钥的，增量调用没有成本含义。
        def _serial_stop(eng: str, goods: list[dict[str, Any]]) -> bool:
            if not allow_early or no_early or not goods:
                return False
            # 答案型域 min_results=1：1 条快照即 early-stop
            if results_sufficient(goods, mode=mode, min_results=early_min,
                                  query=query):
                return True
            # 默认串行：任一引擎有结果即停（历史行为）；答案型不够用则继续补源。
            # 词面覆盖守卫同语义：结果与查询几乎无交集 → 试下一引擎（救援线）
            return early_min is None and query_coverage_ok(goods, query)

        stagger = max(0.0, serial_stagger_s)
        # 等待上限：给「最晚起步的那个引擎」留足它自己的单引擎预算。
        # 旧语义等价于 n × 单引擎预算；对冲把引擎错开起步，所以是
        # (n-1) × stagger + 单引擎预算——差的正是要消灭的那段串行拖尾。
        _serial_wait = (len(to_run) - 1) * stagger + _budget_cfg
        if budget_s is not None:
            # 总预算只管「还起不起新引擎」，不没收已经发出去的枪：已起的
            # 引擎可以跑完自己的单引擎预算（与旧串行行为一致，不比它更差）
            _start_before = max(0.0, _deadline - _now())
        else:
            _start_before = None
        if _run_engines_bounded(to_run, _serial_wait,
                                stop=_serial_stop if allow_early else None,
                                stagger_s=stagger,
                                start_before_s=_start_before,
                                tail_grace_s=straggler_grace_s):
            early_stopped = True



    budget_total_ms = int(budget_s * 1000) if budget_s is not None else None
    # 墙钟账：`useful_ms` = 最后一个有效贡献引擎完成的时刻（答案从这一刻起
    # 就已经在手里了）。`wasted_ms` 见下方口径说明。
    #
    # 此前的 wasted 是「所有非 ok 引擎的 latency 之和」——它根本不是时间量：
    # 并行时必然大于墙钟（实测「世界杯 2026 主办国」wasted 2847ms > dispatch
    # 2313ms），而且把「引擎如实返回了 0 条」也算成浪费。两个已知代价：
    # ① 上一轮的优化盘点据此写下「早停与 hedge race 记账诚实、调度不用动」，
    #    而实测调度是最大的单一浪费源；② 读者无法从它推出任何行动。
    #
    # 再往后改成 `wall - useful`（useful = 最后一个**贡献结果**的引擎的完成
    # 时刻），并宣称口径是「答案就绪后还在等」。但有界并发下这仍是恒等式：
    # 触发早停的引擎自己就是最后完成的那个，返回当刻 wall == useful，于是
    # wasted 永远是 0——实测每轮皆 0。空转的仪表比没有仪表更糟：它让人以为
    # 调度无浪费可调，于是谁都不会去调 tail_grace / hedge / 并发上限。
    #
    # 现在的口径：**被编排主动弃置的引擎已经花掉的墙钟之和**。
    # 这些时间确实白花（没换来结果、也没拖住返回），是唯一既能算清、
    # 又能指向具体动作的量；与 wall 无关（弃置不阻塞返回），故不再保证
    # useful + wasted == wall。仍被等待的超时引擎不算浪费——那是引擎不健康，
    # 在 engine_latency / 熔断里已可见。
    wall_ms = int((_now() - _budget_base) * 1000)
    useful_ms = int(min(max(contributed_ms), wall_ms)) if contributed_ms else 0
    return DispatchResult(
        raw_results, engine_outcomes, engine_latency,
        int(sum(abandoned_ms)), early_stopped, _run_one, _ingest,
        budget_used_ms=wall_ms,
        budget_total_ms=budget_total_ms,
        useful_ms=useful_ms,
    )

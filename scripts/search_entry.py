#!/usr/bin/env python3
"""search_entry.py — 搜索的入口两段：准备（prepare）与调度（dispatch）。

与 search_pipeline 的分工（四个阶段合起来就是一次搜索的全过程）：

  prepare   请求侧整理 + 缓存查询（命中即返回存档响应，不再打网络）
  dispatch  引擎编排（并发/串行、重试、熔断、结局分类）——实现在 engine_dispatch
  postprocess  融合 + 13 个加工阶段（search_pipeline）
  finalize     缓存写 + 记账 + 对外响应（search_pipeline）

## 为什么把 prepare/dispatch 单独成模块

它们是**唯一**会打网络、也是唯一依赖「可打桩入口」的两段：测试靠
`patch.object(search, "engine_search" / "get_engines" / "_missing_env_for" /
"_FAST_TOTAL_BUDGET_S" / "_PRIMARY_GRACE_S")` 换掉引擎与预算。这些入口与常量
**按值传入**（`_SearchHooks`），不让本模块反向 import search——直连绑定会让补丁
静默失效（假引擎不被调用、真网络被打开）。

execute_search 因此只剩编排：prepare → （命中则返回）→ dispatch → postprocess →
finalize，五行。

## 边界

本模块不 import http_client / engines_base；网络只经由传入的 hooks 与
engine_dispatch。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from engine_dispatch import run_dispatch
from usage_log import emit as _emit_usage
from search_output import _collect_errors
from search_pipeline import _SearchRequest, _SearchRun
from search_types import Stage
from stage_timing import StageTiming, tick as _tick, tock as _tock
from time_utils import (
    is_time_capable as _is_time_capable,
    normalize_time_window as _normalize_time_window,
    sort_results_by_time as _sort_results,
)


def _log(message: str) -> None:
    """默认静默的调试出口；仅在真正需要记录时才引入 logging。

    与 search_output._log 同一范式。本模块的 prepare 曾有两处**函数内**裸
    `import logging`：CPython 在编译期就把 `logging` 定为 prepare 的局部名，
    任何早于它们的分支再加一处 `logging.getLogger(...)`，另外两处立刻编译成
    LOAD_FAST → UnboundLocalError，而它们全在 except 处理器内，处理器再抛会
    顶替掉原本被 fail-open 吞掉的错误。模块级不留绑定：既无局部遮蔽，也不把
    logging→traceback→dataclasses→inspect（实测 21 ms）拖进 import search
    的必经之路。
    """
    import logging
    logging.getLogger("unified_search").debug(message)


@dataclass(frozen=True)
class _SearchHooks:
    """可打桩的入口与常量（按值传入，见模块 docstring）。"""

    engine_search: Any
    available_engines: Any
    get_cost_factor: Any
    get_engines: Any
    get_execution_config: Any
    missing_env_for: Any
    classify_outcome: Any
    note_quota_exhausted: Any
    per_engine_budget_s: float
    fast_budget_s: float
    auto_budget_s: float
    primary_grace_s: float
    straggler_grace_s: float
    serial_stagger_s: float


@dataclass(frozen=True)
class _Prepared:
    """准备阶段的产出——两种结局显式分开，不用哨兵值：

      - 命中缓存：`cached` 非空（调用方直接返回它，不再打网络），req/run 为空；
      - 未命中：`req`/`run` 就位，交给 dispatch。
    """

    cached: dict[str, Any] | None = None
    req: _SearchRequest | None = None
    run: _SearchRun | None = None



def prepare(query: str, decision: dict[str, Any], max_results: int,
            timeout: int, depth: str, cache: Any, skip_cache: bool,
            *, mode: str = "auto", since: str | None = None,
            until: str | None = None, sort: str = "relevance",
            on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None,
            engine_domain: str | None = None,
            engine_sub_domain: str | None = None,
            timing: StageTiming | None = None,
            hooks: _SearchHooks) -> _Prepared:
    """请求侧整理 + 缓存查询；命中时 `cached` 非空（调用方直接返回它）。

    段内顺序即契约：查询理解 → 词形规范化 → 进度事件 → 网络感知超时 →
    时间窗归一化 → 缓存键 → 缓存查询。
    """
    domain = decision.get("domain") or "general"
    engine_label = decision.get("engine", "auto")
    engines_combo = decision.get("engines_combo", decision.get("engines", [engine_label]))
    # 防御：过滤空引擎名（空串会在 registry 查无 → 空结果 → 熔断空键 ''）。
    # 全空时保底 anysearch，防止下方 engines[0] IndexError（route 层已保证
    # combo 非空，此处仅防畸形 decision 直接调用 execute_search）。
    engines = [e for e in engines_combo if e] or ["anysearch"]
    parallel = decision.get("parallel", False) and len(engines) > 1

    # P0-001：查询理解 — clean_query 用于检索，exclude_terms 用于融合后过滤
    exclude_terms: list[str] = []
    retrieval_query = query
    qu = None
    try:
        from query_understanding import _understand_cached as understand
        qu = understand(query)
        exclude_terms = qu.exclude_terms
        # 仅当去否定片段后仍有实义内容时才替换检索词，避免空检索
        if qu.clean_query and qu.clean_query.strip():
            retrieval_query = qu.clean_query
    except ImportError:
        pass  # query_understanding 不可用
    except Exception as e:
        _log(f"查询理解跳过: {type(e).__name__}")

    # 词形规范化：全角→半角、拆斜杠、压多余空格（提升精确源命中，治型号/日期分隔符）
    try:
        from query_enhance import normalize_query
        retrieval_query = normalize_query(retrieval_query)
    except ImportError:
        pass

    if on_progress:
        on_progress(Stage.START, {"query": query})

    # 网络环境感知：慢网放大超时预算（避免误杀），快网收紧（更快响应）
    _eff_timeout = timeout
    try:
        from network_aware import adjusted_timeout, network_profile
        _eff_timeout = adjusted_timeout(timeout, engines)
        if _eff_timeout != timeout:
            _log(
                f"网络感知超时: {timeout}s → {_eff_timeout}s "
                f"({network_profile(engines).get('network')})",
            )
    except ImportError:
        pass

    # 时间窗归一化：下推/缓存键用归一化 ISO（相对值转绝对日期），
    # 后过滤用 epoch 秒；非法输入保持原样下推、不参与后过滤。
    since_iso, until_iso, since_ts, until_ts = _normalize_time_window(since, until)

    # 缓存键的引擎维度取**请求侧身份**（用户点名的引擎，或 auto），不取
    # 路由出来的 engines_combo。
    #
    # 为什么（2026-09-19 实测）：combo 是决策结果，会被 adaptive 学习器按上一次
    # 搜索的成败逐次改写。拿结果当键就是「用缓存让缓存失效」——同一查询连跑
    # 两次，进键的引擎串从 `anysearch+octen` 变成 `exa+octen`、从 `byted+...`
    # 变成 `local_bing+...`，20 条样本里 6 条重复查询（30%）因此白跑一遍网络，
    # 而查询、域、档位全都没变。
    #
    # 语义边界：用户点名 `--engine pypi` 时键里就是 pypi（显式约束必须隔离，
    # 这正是 v2.4.2 那条修复要保的东西）；`auto` 时键里是 auto——「用自动路由
    # 搜这个查询」本身就是请求，具体挑了哪几个引擎是实现细节，由 TTL 兜住
    # 时效，并原样保留在缓存载荷里供追溯。
    cache_engine_key = decision.get("engine_request") or "auto"
    # 引擎级垂直域并入缓存键：--domain/--sub_domain 改变了发给引擎的请求，
    # 同 query 在不同 domain 下的结果集不同。不隔离就会把「不限域」的结果
    # 当成「限定金融域」的答案发回去——而这两个开关此前连请求都没带上，
    # 静默退化成通用搜索（同一个坑的另一半）。
    if engine_domain:
        cache_engine_key += f"|ed={engine_domain}"
    if engine_sub_domain:
        cache_engine_key += f"|esd={engine_sub_domain}"
    # 时间窗并入缓存键：同一 query 不同 since/until 不串缓存；
    # 用归一化 ISO（7d 与等价绝对日期共享缓存；相对窗跨天自然过期不串旧数据）。
    # 仅当组合内含带时间能力引擎时隔离：无时间字段引擎忽略时间窗、结果相同，
    # 隔离只会降低命中率（7d/30d 查 octen/anysearch 命中同一缓存）。
    time_aware = any(_is_time_capable(e) for e in engines)
    if since_iso and time_aware:
        cache_engine_key += f"|since={since_iso}"
    if until_iso and time_aware:
        cache_engine_key += f"|until={until_iso}"

    if on_progress:
        on_progress(Stage.ROUTING, {"domain": domain, "engine": engine_label, "engines": engines})

    # combo 缓存命中（含 depth + 柔性命中）
    if not skip_cache:
        t_cache_start = time.time()
        _tk_cache = _tick(timing)
        hit = cache.get(query, cache_engine_key, max_results, domain=domain,
                        mode=mode, depth=depth)
        _tock(timing, "cache_lookup", _tk_cache)
        if hit:
            cache_elapsed = int((time.time() - t_cache_start) * 1000)
            if on_progress:
                on_progress(Stage.CACHE_HIT, {"cache_level": hit.get("_cache_level", "L?")})
            tfidf_scores = decision.get("tfidf_scores", [])
            if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
                tfidf_scores = []
            # 排序在缓存读出后、返回前：缓存内容保持 score 序，sort 只改展示顺序
            hit_results = _sort_results(hit.get("results", []), sort)
            # 命中时一律报**产出这批结果的那次运行**的组合与理由，而不是本次
            # 路由的。此前两者恒等（组合就在缓存键里），这条区分不存在；键改成
            # 请求身份后，同一查询两次运行可以路由到不同组合，而本次路由根本
            # 没执行——报它等于报一个没跑过的计划，还会与同样来自缓存的
            # engines_used / engine_outcomes 自相矛盾。
            cached_combo = (hit.get("engines_combo") or hit.get("engines")
                            or engines)
            _hit = {
                "query": query, "engine": (cached_combo or engines)[0],
                "engines": cached_combo,
                "engines_combo": cached_combo, "cached": True,
                "cache_level": hit.get("_cache_level", "L?"),
                "domain": domain, "elapsed_ms": cache_elapsed,
                "tfidf_scores": tfidf_scores,
                "route_reason": hit.get("route_reason") or decision.get("reason"),
                "login_hint": decision.get("login_hint"),
                "results": hit_results,
                "count": len(hit_results),
                "engines_used": hit.get("engines_used") or engines,
                "mode": mode, "depth": depth,
                "reranker": "skipped_cache",
                "engine_outcomes": hit.get("engine_outcomes") or [],
                "errors": _collect_errors({}, hit.get("engine_outcomes") or []),
                "time_filtered": 0,
            }
            # 软命中披露：L2 语义命中返回的是**另一条查询**的载荷，不标出来
            # 就与精确命中无法区分——调用方会以为这就是本查询的缓存。同样遵循
            # 「不适用就整个键缺席」（见下方漏斗注释）：精确命中下这三个键不
            # 存在，不写成 null。
            if hit.get("_semantic_hit"):
                _hit["semantic_hit"] = True
                _hit["semantic_query"] = hit.get("_semantic_query")
                _hit["semantic_similarity"] = hit.get("_semantic_similarity")
            # 缓存命中时漏斗记账沿用存档值（它描述的是上一次真实抓取）。
            # 存档里没有（该条写入于引入漏斗之前）就**整个键缺席**，不写成
            # null——null 会被读成「漏斗算出来是空」，而缺席只表示「这次没有
            # 这个数据」。两种档位（默认/agent）必须同一形态，否则同一件事
            # 有两种表述。
            if hit.get("funnel") is not None:
                _hit["funnel"] = hit["funnel"]
            if timing is not None:
                _hit["timing"] = timing.summary()
            return _Prepared(cached=_hit)
    try:
        from circuit_breaker import get_breaker
        breaker = get_breaker()
    except ImportError:
        breaker = None

    return _Prepared(req=_SearchRequest(
        query=query, decision=decision, engines=engines,
        engines_combo=engines_combo, domain=domain, mode=mode, depth=depth,
        timeout=timeout, max_results=max_results, retrieval_query=retrieval_query,
        parallel=parallel, eff_timeout=_eff_timeout,
        exclude_terms=exclude_terms, qu=qu, since_iso=since_iso,
        until_iso=until_iso, since_ts=since_ts, until_ts=until_ts,
        time_aware=time_aware, skip_cache=skip_cache, timing=timing,
        on_progress=on_progress, sort=sort, cache=cache,
        engine_label=engine_label, cache_engine_key=cache_engine_key,
        emit_usage_log=_emit_usage, breaker=breaker,
        engine_domain=engine_domain, engine_sub_domain=engine_sub_domain,
    ), run=_SearchRun(raw_results={}, engine_outcomes=[], merged=[]))


def dispatch(req: _SearchRequest, run: _SearchRun, hooks: _SearchHooks) -> _SearchRun:
    """引擎编排：打网络、记账、返回填好 dispatch 字段的运行状态。

    run_dispatch 的入口与常量按值传入（见模块 docstring）。
    """
    query = req.query
    decision = req.decision
    engines = req.engines
    domain = req.domain
    mode, depth = req.mode, req.depth
    max_results, timeout = req.max_results, req.timeout
    skip_cache, cache = req.skip_cache, req.cache
    since_iso, until_iso = req.since_iso, req.until_iso
    timing, on_progress = req.timing, req.on_progress
    retrieval_query = req.retrieval_query
    parallel = req.parallel
    _eff_timeout = req.eff_timeout
    if on_progress:
        on_progress(Stage.SEARCHING, {"engines": engines})

    t0 = time.time()
    # 单调钟基准：预算窗不随 NTP 跳变失真（engine_dispatch 整套换钟，见其垫片注释）
    t0_mono = time.monotonic()
    _tk_dispatch = _tick(timing)
    # 配额批次在这里建、在融合后的 D6 补搜之后才 flush：中间所有 _ingest
    # （含补搜）都要记进同一批，提前 flush 会让补搜引擎的记账落不了盘。
    #
    # import 放在函数内：quota 模块在 import 期就 ensure_state_dir()（mkdir）。
    # 放模块级意味着每次 argo 调用——含纯缓存命中、根本不建批次的路径——
    # 都要碰一次文件系统。批量对象只可能在本函数被创建，没理由让不建批次的
    # 路径付这笔钱。
    from quota import _QuotaBatch
    quota_batch = _QuotaBatch()
    # 引擎编排（并发/串行调度、重试、熔断、结局分类）整段在 engine_dispatch。
    # 入口与常量**按值传入**而非让那边直接 import search：测试靠
    # patch.object(search, "engine_search" / "get_engines" / "_missing_env_for" /
    # "_FAST_TOTAL_BUDGET_S" / "_PRIMARY_GRACE_S") 换掉它们，直连绑定会让补丁
    # 静默失效（假引擎不被调用、真网络被打开）。
    _dispatch = run_dispatch(
        query=query, retrieval_query=retrieval_query, engines=engines,
        decision=decision, parallel=parallel,
        domain=domain, mode=mode, depth=depth,
        max_results=max_results, timeout=timeout, net_timeout=_eff_timeout,
        skip_cache=skip_cache, cache=cache, breaker=req.breaker,
        since_iso=since_iso, until_iso=until_iso, t0=t0, t0_mono=t0_mono,
        engine_search=hooks.engine_search,
        get_engines_fn=hooks.get_engines,
        get_execution_config_fn=hooks.get_execution_config,
        missing_env_for=hooks.missing_env_for,
        classify_outcome=hooks.classify_outcome,
        quota_batch=quota_batch,
        note_quota_exhausted=hooks.note_quota_exhausted,
        per_engine_budget_s=hooks.per_engine_budget_s,
        fast_budget_s=hooks.fast_budget_s,
        auto_budget_s=hooks.auto_budget_s,
        primary_grace_s=hooks.primary_grace_s,
        straggler_grace_s=hooks.straggler_grace_s,
        serial_stagger_s=hooks.serial_stagger_s,
        engine_domain=req.engine_domain,
        engine_sub_domain=req.engine_sub_domain,
    )
    raw_results = _dispatch.raw_results
    engine_outcomes = _dispatch.engine_outcomes
    engine_latency = _dispatch.engine_latency
    wasted_ms = _dispatch.wasted_ms
    useful_ms = _dispatch.useful_ms
    early_stopped = _dispatch.early_stopped
    budget_used_ms = _dispatch.budget_used_ms
    budget_total_ms = _dispatch.budget_total_ms
    # 融合后的 D6 补搜要用同一套「跑单引擎 + 入账」，钩子由此取回
    _run_one = _dispatch.run_one
    _ingest = _dispatch.ingest

    elapsed = int((time.time() - t0) * 1000)
    _tock(timing, "dispatch", _tk_dispatch)
    _tk_fusion = _tick(timing)
    return replace(
        run,
        raw_results=raw_results,
        engine_outcomes=engine_outcomes,
        engine_latency=engine_latency,
        wasted_ms=wasted_ms,
        useful_ms=useful_ms,
        early_stopped=early_stopped,
        budget_used_ms=budget_used_ms,
        budget_total_ms=budget_total_ms,
        elapsed=elapsed,
        tk_fusion=_tk_fusion,
        quota_batch=quota_batch,
        run_one=_run_one,
        ingest=_ingest,
    )

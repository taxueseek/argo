#!/usr/bin/env python3
"""search_pipeline.py — 融合后加工：把「多引擎结果」变成「可交付结果」。

## 为什么单独成模块

execute_search 里，dispatch（打网络）与「融合后加工」（纯本地，13 个阶段）是两种
性质完全不同的工作：前者关心并发/超时/熔断，后者关心过滤、去重、排序、标注。
后者有 354 行、9 个计时段、6 个漏斗计数，混在网络编排里时，读一次「为什么这条
结果被剔掉了」要穿过整条调度链。

## 两概念的状态模型（这是本模块存在的理由）

加工阶段的输入输出曾经散成 28 个外层局部量 + 21 个写回量。现在只有两个概念：

  - `_SearchRequest`：**请求侧**，进入加工后不再改变（查询、档位、时间窗、
    钩子、计时句柄）；
  - `_SearchRun`：**运行侧**，被阶段逐步改写的累加量（结果集、各类剔除计数、
    精排状态、漏斗、缓存载荷）。

阶段函数签名统一为 `(req, run) -> run`。要加一个阶段时，只需在 run 上加一个
字段，不必再往 execute_search 的局部量里塞东西——那条路径正是当初把
execute_search 撑到 724 行的原因。

## 边界

本模块**不打网络**（唯一例外是 D6 macro 证据下限与空结果恢复树，它们通过
`req.run_one` / `req.ingest` 钩子回到调度层执行）。判据：本文件不 import
http_client / engines_base / engine_dispatch。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from engine_dispatch import _QUOTA_ERROR_KEYWORDS
from engine_env import env_flag
from except_sets import OPT_IMPORT, SHAPE_BENIGN
from search_output import _collect_errors, _slow_query_ttl, build_funnel
from search_rank import (
    _RERANK_DEGRADED_STATUSES,
    _align_facts_safe,
    _apply_consensus_and_sort,
    _attach_selection_signals,
    _lang_prefer_rerank,
    _rerank_pool_limit,
    deduplicate_by_url,
    local_five_dim_rerank,
    minhash_dedupe,
    rerank_results,
    rrf_merge,
)
from search_types import Stage
from stage_timing import tick as _tick, tock as _tock
from time_utils import apply_time_window as _apply_time_window
from time_utils import sort_results_by_time as _sort_results


@dataclass(frozen=True)
class _SearchRequest:
    """请求侧输入：进入加工后不再改变（请求参数、派生查询、钩子、计时句柄）。"""

    query: str
    decision: dict[str, Any]
    engines: list[str]
    engines_combo: list[str]
    domain: str
    mode: str
    depth: str
    timeout: int
    max_results: int
    retrieval_query: str
    parallel: bool
    eff_timeout: float
    exclude_terms: list[str]
    qu: Any
    since_iso: str | None
    until_iso: str | None
    since_ts: float | None
    until_ts: float | None
    time_aware: bool
    skip_cache: bool
    timing: Any
    on_progress: Any
    sort: str
    cache: Any
    engine_label: str
    cache_engine_key: Any
    emit_telemetry: Any
    breaker: Any


@dataclass(frozen=True)
class _SearchRun:
    """运行侧状态：被阶段逐步改写（`replace` 返回新实例）。

    字段分三组：结果集（raw_results / engine_outcomes / merged）、加工累加量
    （各类剔除计数、精排状态、漏斗、缓存载荷）、调度产出（engine_latency /
    预算与墙钟账 / 计时句柄与钩子）。
    """

    raw_results: dict[str, list[dict[str, Any]]]
    engine_outcomes: list[dict[str, Any]]
    merged: list[dict[str, Any]]
    minhash_removed: int = 0
    excluded_count: int = 0
    time_filtered: int = 0
    time_filter_warning: str | None = None
    recovery_info: dict[str, Any] | None = None
    rank_method: str = "none"
    reranker_status: str = "skipped_short"
    local_rerank_on: bool = False
    fact_alignment: dict[str, Any] | None = None
    noise_dropped: list[dict[str, Any]] = field(default_factory=list)
    funnel: dict[str, Any] | None = None
    result_payload: dict[str, Any] | None = None
    quota_batch: Any = None
    tk_fusion: float = 0.0
    run_one: Any = None
    ingest: Any = None
    engine_latency: dict[str, float] = field(default_factory=dict)
    wasted_ms: int = 0
    useful_ms: int = 0
    early_stopped: bool = False
    budget_used_ms: int | None = None
    budget_total_ms: int | None = None
    elapsed: int = 0


def postprocess(req: _SearchRequest, run: _SearchRun, hooks: Any) -> _SearchRun:
    """融合后加工：13 个阶段，顺序即契约（过滤 → 去重 → 软排 → 精排 → 标注）。

    hooks 只用于两处需要回到调度层的动作（D6 macro 补搜、空结果恢复树）——
    入口按值传入，见 search_entry 的 _SearchHooks 说明。
    """
    engine_search = hooks.engine_search
    available_engines = hooks.available_engines
    query = req.query
    decision = req.decision
    engines = req.engines
    engines_combo = req.engines_combo
    domain = req.domain
    mode = req.mode
    depth = req.depth
    timeout = req.timeout
    max_results = req.max_results
    exclude_terms = req.exclude_terms
    qu = req.qu
    since_iso = req.since_iso
    until_iso = req.until_iso
    since_ts = req.since_ts
    until_ts = req.until_ts
    time_aware = req.time_aware
    skip_cache = req.skip_cache
    timing = req.timing
    on_progress = req.on_progress
    _tk_fusion = run.tk_fusion
    _run_one = run.run_one
    _ingest = run.ingest
    _emit_telemetry = req.emit_telemetry
    quota_batch = run.quota_batch
    breaker = req.breaker
    raw_results = run.raw_results
    engine_outcomes = run.engine_outcomes
    engine_outcomes = run.engine_outcomes
    merged = run.merged

    # 融合输入派生（原先在 execute_search 里，属于加工的第一步）
    valid_lists = [
        res for res in raw_results.values()
        if res and any(isinstance(r, dict) and "error" not in r for r in res)
    ]
    # 去掉 error-only 列表中的 error 条目
    clean_lists = []
    for res in valid_lists:
        clean = [r for r in res if isinstance(r, dict) and "error" not in r]
        if clean:
            clean_lists.append(clean)

    # 查询主语言：噪声门与语言能力加权共用同一个判定（唯一来源，
    # 避免两处各自算导致行为漂移）。
    _q_lang_for_fusion = (
        ((decision or {}).get("features") or {}).get("primary_lang") or None
    )



    # ── 多语言噪声门（result_lang）─────────────────────────────────────
    # 实测问题：引擎在非支持语言下会返回「成功但不相关」的结果——
    # juejin 在阿拉伯语查询下返回 10 条、相关度 0.00（全是通用热帖），
    # 却报告 status=ok。这类噪声混进 RRF 融合会污染最终结果。
    #
    # 判定不依赖「猜测查询语言」：用结果自身的书写系统（Unicode 码位，
    # 确定性）+ 查询词元命中率。语言不符且相关度低 → 判为噪声并剔除。
    #
    # 只在「查询语言可判定且非中英」时启用：中英是 argo 的主战场，
    # 现有引擎面足够宽，过早过滤会误伤（如英文查询命中中文优质内容）。
    _noise_dropped: list[dict[str, Any]] = []
    try:
        from result_lang import assess_results as _assess_lang
        _q_lang = _q_lang_for_fusion
        if _q_lang and _q_lang not in ("zh", "en", "mixed", "other", ""):
            _kept = []
            for _lst in clean_lists:
                if not _lst:
                    continue
                _eng = _lst[0].get("_engine") or _lst[0].get("source") or "?"
                _a = _assess_lang(query, _lst, expected_lang=_q_lang)
                if _a["verdict"] == "noise":
                    _noise_dropped.append({
                        "engine": _eng, "lang": _a["lang"],
                        "relevance": _a["relevance"],
                        "reason": (_a["reasons"][0] if _a.get("reasons")
                                   else "low relevance"),
                    })
                    continue
                _kept.append(_lst)
            clean_lists = _kept
    except Exception as _e:  # 噪声门是质量增强，失败不得拖垮主流程
        import logging as _lg
        _lg.getLogger("unified_search.search").debug(f"噪声门跳过: {_e}")

    if len(clean_lists) > 1:
        merged = rrf_merge(clean_lists, lang=_q_lang_for_fusion)
    elif clean_lists:
        merged = deduplicate_by_url(clean_lists[0])
        # 单引擎也补 consensus
        for r in merged:
            eng = r.get("_engine") or r.get("source") or ""
            if eng:
                r.setdefault("consensus_engines", [eng])
    else:
        merged = []

    # ── D6：macro_data 域证据下限（事实核查防单源）─────────────────────
    # deep 研究场景下结果 <2 条说明结构化源未覆盖该查询：追加通用保底引擎
    # 补证据，避免「单引擎单结果」被事实核查 / 融合阶段当作答案；补搜结果
    # 一并进 RRF，consensus 维度天然加权。
    if (domain == "macro_data" and merged and len(merged) < 2
            and depth in ("deep", "research")):
        _done = set(raw_results.keys())
        _cands = [
            e for e in ("anysearch", "duckduckgo", "local_bing")
            if e not in _done
            and e in set(available_engines())
            and (breaker is None or breaker.allow(e)[0])
        ]
        _extra_lists = []
        for _eng in _cands[:2]:
            _e, _res, _out, _lat = _run_one(_eng)
            _ingest(_e, _res, _out, _lat)
            _goods = [r for r in _res if isinstance(r, dict) and "error" not in r]
            if _goods:
                _extra_lists.append(_goods)
        if _extra_lists:
            merged = rrf_merge([merged] + _extra_lists)

    # 配额记账：整批一次写入文件（同一次搜索的 N 个引擎合并为一次写）。
    # 必须放在 D6 补搜之后：那是最后一个 _ingest 调用点，flush 提前会让
    # 补搜引擎的记账永远落不了盘（2026-09-13 审查实锤）。
    quota_batch.flush()
    _tock(timing, "fusion", _tk_fusion)
    _tk_dedupe = _tick(timing)

    # ── P0：过滤 SERP/跳转 URL（搜索结果页、baidu.com/link 等不可当信源正文）──
    if merged:
        try:
            from evidence import is_serp_or_jump_url as _is_serp
            merged = [r for r in merged if not _is_serp(r.get("url", ""))]
        except ImportError:
            pass  # evidence 不可用时跳过（本地五维 rerank 已对 SERP 降权）

    # ── minhash 近重复去重（结果级，RRF 后 / SERP 后）─────────────────────
    # max_keep 与下方放宽截断同源：去重只需要保证「前 N 条非重复」正确，
    # 因为多出来的条目下一行就被截掉了。不设上限时是 O(结果数²) 的两两比较。
    _pool_limit = _rerank_pool_limit(max_results)
    minhash_removed = 0
    if merged and len(merged) > 1:
        try:
            deduped, minhash_removed = minhash_dedupe(merged, max_keep=_pool_limit)
            merged = deduped
        except Exception as _e:
            logging.getLogger("unified_search").debug(f"minhash 去重跳过: {type(_e).__name__}")
    _tock(timing, "dedupe", _tk_dedupe)
    # 漏斗第 4 格：跨引擎合并 + 近重复去重之后还剩多少（见 build_funnel）
    _funnel_deduped = len(merged)
    # 三个子段各自计时，**不能共用一个 tick**：
    # `filter`（否定词 + 时间窗过滤）是纯本地遍历；`recovery` 是**网络调用**
    # （域路由零结果时的救援链，见下）；`rerank` 是纯本地重排。此前三者共用
    # 一个 tick 且整个挂在 "rerank" 名下，实测「python 怎么读csv」的 rerank
    # 阶段报 706ms（占全查询 40%），而真正的重排只用 3.6ms——那 700ms 是恢复
    # 链打的一次 anysearch。`--explain-timing` 的全部价值就是回答「瓶颈在哪」，
    # 报错桶等于把人指向错误的优化对象。
    _tk_filter = _tick(timing)

    # ── P2：多语言语言偏好软排序（ja/ko 前置含目标语言字符结果，软排不删除）──
    try:
        _p_lang = (decision or {}).get("features", {}).get("primary_lang")
        if _p_lang in ("ja", "ko"):
            merged = _lang_prefer_rerank(merged, _p_lang)
    except OPT_IMPORT + SHAPE_BENIGN:  # 软排序增强，任何失败按「不排序」处理
        pass

    # 放宽截断：rerank 阶段看到 _pool_limit 条，最终输出再截断 max_results
    merged = merged[:_pool_limit]

    # ── P0-001：按 exclude_terms 过滤（否定约束）──
    excluded_count = 0
    if merged and exclude_terms:
        kept = []
        low_terms = [t.lower() for t in exclude_terms if t]
        for r in merged:
            hay = f"{r.get('title', '')} {r.get('snippet', '')} {r.get('url', '')}".lower()
            if any(t in hay for t in low_terms):
                excluded_count += 1
                continue
            kept.append(r)
        merged = kept

    # ── 时间窗结果后过滤保底 ──
    # 仅当组合内含带时间能力引擎时执行：无时间字段引擎的结果没有可滤对象，
    # 跳过遍历省开销；语义上与缓存键隔离保持一致（7d/30d 共享同一缓存内容）。
    time_filtered = 0
    if time_aware and (since_ts is not None or until_ts is not None) and merged:
        merged, time_filtered = _apply_time_window(merged, since_ts, until_ts)
        if time_filtered:
            logging.getLogger("unified_search").debug(
                f"时间窗后过滤剔除 {time_filtered} 条（since={since_iso}, until={until_iso}）")
    # 漏斗第 5 格：否定词过滤 + 时间窗过滤之后（见 build_funnel）
    _funnel_filtered = len(merged)
    _tock(timing, "filter", _tk_filter)

    # D5：时间窗空操作告警——用户指定了时间窗，组合内含时间能力引擎，
    # 但结果没有任何 published_at（下推缺失/源端未返回）：
    # `--since/--until` 实际未生效（宽松策略保留无时间字段条目，
    # time_filtered 恒 0）。透传 warning 而非静默降级。
    # 组合内不含时间能力引擎时是已知常态，不重复告警。
    time_filter_warning: str | None = None
    if time_aware and (since_ts is not None or until_ts is not None) and merged:
        if not any(r.get("published_at") for r in merged):
            time_filter_warning = (
                f"引擎未返回 published_at，时间窗 {since_iso or '任意'} ~ "
                f"{until_iso or '任意'} 未实际过滤"
            )

    # ── P0-002：空结果错误恢复决策树 ──
    recovery_info: dict[str, Any] | None = None
    # 复杂度门控：低复杂度查询只允许低成本放宽（L1/L2），
    # 禁用高价多源/跨语言（L3/L4）——简单问题不搞多轮，省 token。
    _max_rec_level: str | None = None
    try:
        from query_enhance import complexity_gate
        if qu is not None and complexity_gate(query, qu) == "low":
            _max_rec_level = "L2"
    except OPT_IMPORT + SHAPE_BENIGN:  # 门控判定失败按「不限制放宽档位」处理
        pass
    _tk_recovery = _tick(timing)
    _recovery_engines: set[str] = set()
    if not merged:
        try:
            from recovery import run_recovery
            tried = list(raw_results.keys()) or list(engines)
            fallback_engines = decision.get("engines_fallback") or []
            # 域路由零结果：恢复链放行 L3 换引擎。复杂度门此前把简单查询
            # 压到 L2——L3 被禁 + 全域零结果 = 域命中查询无解（实测
            # macro_data「中国GDP」零结果、恢复链空转）。engines_fallback
            # 里是路由的定向保底声明（域未试成员优先），代价可控。
            rec_level = _max_rec_level
            if fallback_engines and decision.get("domain") not in (
                    None, "", "general", "general_search") \
                    and (rec_level is None or rec_level < "L3"):
                rec_level = "L3"
            try:
                enabled_set = set(available_engines())
            except Exception:
                enabled_set = None

            def _recovery_executor(rq: str, rengines: list[str]) -> list[dict[str, Any]]:
                """恢复执行器：串行跑候选引擎，取首个非空。跳过缓存避免污染。"""
                out: list[dict[str, Any]] = []
                for eng in rengines:
                    # 记下真正发出的调用：恢复段走的不是 dispatch，若不单独记账，
                    # 漏斗会算出「routed 2 → called 2 → returned 5」这种自相矛盾的
                    # 账（实测「python 怎么读csv」），而这正是最需要被看见的路径。
                    _recovery_engines.add(eng)
                    try:
                        # 恢复路径同样携带时间窗，避免恢复时丢弃用户约束；
                        # --no-cache 也要透传：本函数的契约是「跳过缓存避免污染」，
                        # 不透传会让恢复段既读到用户明确拒绝的旧缓存，又把自己的
                        # 临时查询写进缓存（dispatch 那条路径是透传的，两处不一致）。
                        res = engine_search(rq, eng, n=max_results,
                                            timeout=timeout, depth=depth, mode=mode,
                                            since=since_iso, until=until_iso,
                                            skip_cache=skip_cache)
                    except Exception:
                        res = []
                    goods = [r for r in (res or [])
                             if isinstance(r, dict) and "error" not in r]
                    if goods:
                        for r in goods:
                            r.setdefault("_engine", eng)
                            r.setdefault("_recovered", True)
                        out.extend(goods)
                        break
                return out

            rec_results, rec_result = run_recovery(
                query, tried, _recovery_executor,
                engines_fallback=fallback_engines, enabled=enabled_set, mode=mode,
                max_level=rec_level)
            recovery_info = rec_result.to_dict()
            # P2-6：恢复遥测——query 截断脱敏，只记概览不记明细
            if _emit_telemetry is not None:
                try:
                    _emit_telemetry("recovery", {
                        "query": (query[:60] if query else query),
                        "triggered": recovery_info.get("triggered"),
                        "recovered": recovery_info.get("recovered"),
                        "level_used": recovery_info.get("level_used"),
                        "strategy_used": recovery_info.get("strategy_used"),
                        "steps_tried": len(recovery_info.get("steps_tried") or []),
                        "final_query": (recovery_info.get("final_query") or "")[:60],
                        "note": recovery_info.get("note", ""),
                    })
                except SHAPE_BENIGN:  # 遥测记录失败不影响恢复结果本身
                    pass
            if rec_results:
                merged = deduplicate_by_url(rec_results)[:max_results]
                # 恢复引擎按引擎分组记回 raw_results：engines_used 此前不含
                # 救援引擎（provenance 断链，实测恢复成功后 engines_used 仍
                # 只列原 combo），自适应学习也看不到恢复成功信号。
                _rec_by_eng: dict[str, list] = {}
                for r in merged:
                    eng = r.get("_engine") or r.get("source") or ""
                    if eng:
                        r.setdefault("consensus_engines", [eng])
                        _rec_by_eng.setdefault(eng, []).append(r)
                for _eng, _lst in _rec_by_eng.items():
                    raw_results.setdefault(_eng, _lst)
        except ImportError:
            pass  # recovery 模块不可用
        except Exception as e:
            logging.getLogger("unified_search").debug(
                f"错误恢复跳过: {type(e).__name__}")

    # Reranker：ARGO_LOCAL_RERANK 开关（0 关闭本地五维保底；默认 1 开启）
    _tock(timing, "recovery", _tk_recovery)
    _tk_rerank = _tick(timing)
    local_rerank_on = env_flag("ARGO_LOCAL_RERANK")
    reranker_status = "skipped_short"
    rank_method = "none"
    if mode == "fast" or depth == "fast":
        reranker_status = "skipped_fast"
    elif merged and len(merged) > 1:
        # 全量重排（top_n=len），由最终输出统一截断 max_results
        merged, reranker_status = rerank_results(query, merged, top_n=len(merged))
        if reranker_status == "ok":
            rank_method = "bocha"

    # 本地五维 rerank 保底：受 ARGO_LOCAL_RERANK 开关控制（可观测 rank_method）。
    # 触发计算方式走 _RERANK_DEGRADED_STATUSES 唯一来源：新增「bocha 未出排序」的
    # 状态时只改那一处，不会漏掉这里的保底（漏了就是既不精排也不保底）。
    if local_rerank_on and merged and len(merged) > 1 and \
            reranker_status in _RERANK_DEGRADED_STATUSES:
        try:
            # top_n=max_results（而非 len(merged)）：下游 _apply_consensus_and_sort
            # 紧接着就按 max_results 截断，尾部不会有人用。传真实 K 才能启用
            # 函数内的 K 相关剪枝（可证明前 K 个选序不变），n=200 时把 O(n²)
            # 贪心从 ~33ms 压到毫秒级。
            merged = local_five_dim_rerank(query, merged, domain=domain,
                                           top_n=max_results)
            rank_method = "local_five_dim"
        except Exception as e:
            logging.getLogger("unified_search").debug(
                f"本地五维 rerank 跳过: {type(e).__name__}")
    elif not local_rerank_on and reranker_status in _RERANK_DEGRADED_STATUSES:
        rank_method = "none"

    _tock(timing, "rerank", _tk_rerank)
    _tk_signals = _tick(timing)

    if merged:
        merged = _apply_consensus_and_sort(merged, max_results)

    _attach_selection_signals(merged, mode, depth)

    # ── P0-004：关键事实交叉标记（仅 deep/auto 且结果 ≥3；fast 跳过）──
    fact_alignment: dict[str, Any] | None = _align_facts_safe(merged, mode, depth)

    _tock(timing, "signals", _tk_signals)
    _tk_cache_write = _tick(timing)

    if on_progress:
        on_progress(Stage.MERGING, {"count": len(merged)})

    # 阶段漏斗账（见 build_funnel）。在写缓存之前算一次、两处消费：
    # 既进 result_payload（缓存命中时它随存档一起返回，否则命中路径的漏斗会
    # 缺席——而「缺席」比数字更容易被误读成「没数据」），也进本次输出。
    # 此处 len(merged) 已等于最终条数：_apply_consensus_and_sort 在上一步已按
    # max_results 截断，而 _sort_results 只重排、不改长度。
    funnel = build_funnel(
        len(engines),
        # 「实际发起调用」= 编排层跑过的引擎 ∪ 恢复链补调的引擎。
        # 此前只数 engine_outcomes/raw_results，恢复段走的不是 dispatch、
        # 两条都不进 → 实测「python 怎么读csv」报出 routed 2 → called 2 →
        # returned 5：两个引擎交出了 5 条结果，而真凶（恢复链打的 anysearch）
        # 在漏斗里根本不存在。漏斗的全部用途就是定位塌陷点，一个会漏掉真凶的
        # 漏斗比没有更危险。
        len({o.get("engine") for o in engine_outcomes if o.get("engine")}
            | set(raw_results) | _recovery_engines),
        sum(1 for items in raw_results.values() if isinstance(items, list)
            for it in items
            if isinstance(it, dict) and "error" not in it),
        _funnel_deduped, _funnel_filtered, len(merged),
    )

    result_payload = {
        "results": merged,
        "engines_used": list(raw_results.keys()),
        # 产出这批结果的**那次运行**的组合与理由，随载荷一起存。
        #
        # 缓存键改成请求身份后，同一查询的两次运行可以路由到不同组合，而命中
        # 时本次路由根本没执行。不存这两个字段的话，命中响应只能报本次路由，
        # 那是一个没跑过的计划，还会与同样来自缓存的 engines_used /
        # engine_outcomes 自相矛盾（2026-09-19）。约 100 字节/条，换来缓存
        # 条目自描述。旧条目没有这两个键，命中路径回退到本次路由（原行为）。
        "engines": list(engines),
        "engines_combo": list(engines_combo),
        "route_reason": decision.get("reason"),
        "domain": domain,
        "engine_outcomes": engine_outcomes,
        "time_filtered": time_filtered,
        "time_filter_warning": time_filter_warning,
        "noise_dropped": _noise_dropped,
        "funnel": funnel,
    }

    return replace(
        run,
        merged=merged,
        minhash_removed=minhash_removed,
        excluded_count=excluded_count,
        time_filtered=time_filtered,
        time_filter_warning=time_filter_warning,
        recovery_info=recovery_info,
        rank_method=rank_method,
        reranker_status=reranker_status,
        local_rerank_on=local_rerank_on,
        fact_alignment=fact_alignment,
        noise_dropped=_noise_dropped,
        funnel=funnel,
        result_payload=result_payload,
    )

def finalize(req: _SearchRequest, run: _SearchRun, hooks: Any) -> dict[str, Any]:
    """收尾：写缓存 → 自适应记账 → 语言偏好 → 装配对外响应。

    与 postprocess 的分工：那里决定「结果是什么」，这里决定「怎么对外说」
    （缓存写入、学习器记账、漏斗/计时/信源的字段形态）。签名同样只吃两个
    状态对象——execute_search 里不再有一串需要手工搬运的局部量。

    hooks 只用于 `get_cost_factor`：测试靠 patch 它把成本系数钉成常数，
    按值传入才能让补丁生效（见 search_entry._SearchHooks）。
    """
    get_cost_factor = hooks.get_cost_factor
    query = req.query
    decision = req.decision
    engines = req.engines
    engines_combo = req.engines_combo
    domain = req.domain
    mode, depth = req.mode, req.depth
    max_results = req.max_results
    timing, on_progress = req.timing, req.on_progress
    sort, cache = req.sort, req.cache
    engine_label, cache_engine_key = req.engine_label, req.cache_engine_key
    skip_cache = req.skip_cache
    merged = run.merged
    raw_results = run.raw_results
    engine_outcomes = run.engine_outcomes
    engine_latency = run.engine_latency
    minhash_removed = run.minhash_removed
    excluded_count = run.excluded_count
    time_filtered = run.time_filtered
    time_filter_warning = run.time_filter_warning
    recovery_info = run.recovery_info
    rank_method = run.rank_method
    reranker_status = run.reranker_status
    local_rerank_on = run.local_rerank_on
    fact_alignment = run.fact_alignment
    _noise_dropped = run.noise_dropped
    funnel = run.funnel
    result_payload = run.result_payload
    wasted_ms, useful_ms = run.wasted_ms, run.useful_ms
    early_stopped = run.early_stopped
    budget_used_ms, budget_total_ms = run.budget_used_ms, run.budget_total_ms
    elapsed = run.elapsed
    exclude_terms = req.exclude_terms
    _tk_cache_write = _tick(timing)
    # 写 combo 缓存：空结果短 TTL / 时效 cap 由 cache.set 处理
    if not skip_cache:
        effective_ttl = None
        if merged and elapsed > 2000:
            # 慢查询略延长缓存：省的是「同一查询再付一次慢网」的钱。
            effective_ttl = _slow_query_ttl(cache.resolve_ttl(domain, query=query),
                                            elapsed)
        cache.set(
            query, cache_engine_key, max_results, result_payload,
            domain=domain, ttl=effective_ttl, mode=mode, depth=depth,
        )
    _tock(timing, "cache_write", _tk_cache_write)

    # 自适应学习
    try:
        from adaptive import get_learner
        learner = get_learner()
        # 引擎结局映射：下面要区分「空结果」与「真失败」，需要它
        _status_of = {o.get("engine"): o.get("status")
                      for o in engine_outcomes if isinstance(o, dict)}
        for eng, res in raw_results.items():
            errors = [str(r.get("error", "")) for r in res if isinstance(r, dict) and "error" in r]
            # 配额/鉴权类是配置态故障，不是引擎质量信号：计入会把恢复后的
            # 引擎分数毒化在历史失败里（byted 配额期 38 连败 → 分数 0.072，
            # 配额自愈后无流量刷正分，死锁）。此类错误不计入，保持中性。
            # 配额关键词走 _QUOTA_ERROR_KEYWORDS 唯一来源；鉴权类仅此处有。
            if errors and all(
                any(k in msg.lower() for k in
                    (*_QUOTA_ERROR_KEYWORDS, "unauthorized", "api key",
                     "forbidden", "401", "403"))
                for msg in errors
            ):
                continue
            success = bool(res and any(isinstance(r, dict) and "error" not in r for r in res))
            # 「引擎正常、但这次查询它没有结果」与「引擎失败」是两回事：
            # 前者说明的是查询与源不匹配，不是源坏了。混在一起会误杀——实测
            # github 窗口内 63 次调用的失败归因全是 empty，它却在 local_code /
            # package_search 这些**声明要用它**的域里因分数低于 0.3 被整个剔除
            # （同类还有 wikipedia 0.17 / openalex 0.29 / hackernews 0.10 /
            # twitter 0.17 / open_library 0.17，共 16 个域受影响）。
            # 空结果只单独统计，不参与成功率计算（见 adaptive.get_score）。
            empty = (not success) and _status_of.get(eng) in (
                "no-results", "no-results-cached")
            latency = engine_latency.get(eng, elapsed / max(len(raw_results), 1))
            cost = get_cost_factor(eng)
            learner.record(eng, success=success, latency_ms=latency,
                           cost=0.0 if cost >= 0.85 else 0.001, empty=empty)
    except ImportError:
        pass
    except Exception as e:
        logging.getLogger("unified_search").debug(f"自适应学习记录跳过: {type(e).__name__}")

    # 语言偏好：记录本轮查询语 + 输出观测快照（默认中英 + 系统 + 习惯）
    lang_pref_info: dict[str, Any] | None = None
    try:
        from lang_pref import record_query_lang, lang_pref_snapshot
        feats = decision.get("features") or {}
        q_lang = feats.get("primary_lang") or ""
        if not q_lang:
            try:
                from lang_detect import detect_language
                q_lang = detect_language(query)
            except ImportError:
                q_lang = ""
        if q_lang:
            record_query_lang(q_lang)
        lang_pref_info = lang_pref_snapshot(query_lang=q_lang)
    except ImportError:
        pass
    except Exception as e:
        logging.getLogger("unified_search").debug(
            f"语言偏好记录跳过: {type(e).__name__}")

    if on_progress:
        on_progress(Stage.DONE, {"count": len(merged), "elapsed_ms": elapsed})

    tfidf_scores = decision.get("tfidf_scores", [])
    if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
        tfidf_scores = []

    # 排序在返回前、写缓存后：缓存内容保持 score 序（缓存键/内容不受 sort 影响），
    # sort 只改变本次展示顺序；缓存命中路径在 return 前同样处理，两路径行为一致。
    out_results = _sort_results(merged, sort)

    out: dict[str, Any] = {
        "query": query, "engine": engine_label, "engines": engines,
        "engines_combo": engines_combo, "cached": False,
        "domain": domain, "elapsed_ms": elapsed,
        "tfidf_scores": tfidf_scores,
        "route_reason": decision.get("reason"),
        "results": out_results,
        "count": len(out_results), "engines_used": list(raw_results.keys()),
        "errors": _collect_errors(raw_results, engine_outcomes),
        "engine_outcomes": engine_outcomes,
        # 阶段漏斗账：0 结果时用它定位塌在哪一层（见 build_funnel）
        "funnel": funnel,
        # 多语言噪声门：被剔除的引擎及原因（可观测，便于定位「为什么少了几个源」）
        "noise_dropped": _noise_dropped,
        "wasted_engine_ms": wasted_ms,
        "early_stopped": early_stopped,
        "reranker": reranker_status,
        "rank_method": rank_method,
        "minhash_removed": minhash_removed,
        "local_rerank_on": local_rerank_on,
        "recovery": recovery_info,
        "fact_alignment": fact_alignment,
        "exclude_terms": exclude_terms,
        "excluded_count": excluded_count,
        "time_filtered": time_filtered,
        "time_filter_warning": time_filter_warning,
        "mode": mode, "depth": depth,
        "login_hint": decision.get("login_hint"),
    }
    if lang_pref_info is not None:
        out["lang_pref"] = lang_pref_info
    if timing is not None:
        # 并发效率：dispatch 是墙钟，engine_latency 之和是各引擎各自耗时。
        # 比值远小于引擎数说明并发没排满（或某个慢源独占尾部），是判断
        # 「该加并发还是该摘慢源」的直接依据。
        eng_sum = sum(engine_latency.values())
        out["timing"] = timing.summary()
        out["timing"]["elapsed_ms"] = elapsed
        out["timing"]["dispatch"] = {
            # 用 dispatch 自己那口单调钟量出来的墙钟（budget_used_ms），不用外层
            # 这笔 time.time() 差值：useful/wasted 都是单调钟算的，混用两种钟会
            # 让「useful + wasted ≡ wall」只在毫秒取整恰好对齐时成立——而这条
            # 恒等式正是测试与文档承诺的「唯一自洽的墙钟分解」。
            "wall_ms": budget_used_ms,
            "engines_run": len(engine_latency),
            "engine_sum_ms": eng_sum,
            "parallel_efficiency": (round(eng_sum / budget_used_ms, 2)
                                    if budget_used_ms else None),
            # useful_ms + wasted_ms ≡ wall_ms（唯一自洽的墙钟分解）。
            # useful = 最后一个有效贡献引擎完成的时刻，wasted = 此后还在等。
            "useful_ms": useful_ms,
            "wasted_ms": wasted_ms,
            "early_stopped": early_stopped,
        }
        if budget_total_ms is not None:
            # 预算消耗额：fast/auto 有总预算，deep 无（键缺席即「无预算」）。
            # timing 在 agent 档保留，预算可见性随答案一起到达。
            out["timing"]["budget"] = {"used_ms": budget_used_ms,
                                       "total_ms": budget_total_ms}
    return out
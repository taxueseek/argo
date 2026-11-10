#!/usr/bin/env python3
"""recovery_l0 — 恢复链 L0：early-stop 误判时补跑被跳过的同域引擎。

early-stop 触发后 merged 归零（壳/噪声被下游过滤清零），combo 里
routed 但未调用的引擎是最小成本恢复对象——它们与已跑引擎同域，
比 L3 换跨域引擎更可能交付本域结果（2026-10-11 热榜实测：
wave-1 首引擎交壳骗过早停、同域真榜单引擎被 cancelled，merged
归零触发 2.3s 跨域恢复链）。

L0 是恢复决策树的第 0 级：补跑本 combo 的 skipped 引擎，非空
即采用；失败才落 L3 换引擎。执行器由调用方注入（search_pipeline
的 _run_one/_ingest 闭包），本模块不依赖编排状态，与 recovery.py
的 L1–L4 决策树职责分离。

两条硬约束（2026-10-11 甲轮收口）：

  - **壳不算「交付」**：补跑结果与充分性判定（query_signals）、
    漏斗壳过滤共用 serp_guard.drop_shell_urls 同一口径——被跳过的
    引擎若也交壳，不算恢复成功，落 L3。否则壳经 L0 绕过主链壳过滤
    直送最终输出（主链过滤跑在恢复链之前）。
  - **死线**：deadline（monotonic 秒）之后不再点火新引擎；剩余不足
    _MIN_USEFUL_SLICE_S（口径：近 72h 真实 P50=1430ms，低于它新起
    的引擎大概率白烧一次调用）也不点火。补跑是串行的，无此约束时
    最坏 |skipped| × 单引擎超时，把已失败的查询再拖出几十秒。
"""

from __future__ import annotations

import engine_states

import time
from typing import Any, Callable

from search_rank import deduplicate_by_url, rrf_merge

# 点火下限：剩余窗口低于一个真实 P50 时，新引擎大概率来不及交付。
_MIN_USEFUL_SLICE_S = 1.5


def _deliverable(res: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """无 error 且非壳的结果才算「交付」（壳口径唯一出口：serp_guard）。"""
    goods = [r for r in res if isinstance(r, dict) and "error" not in r]
    try:
        from serp_guard import drop_shell_urls
    except ImportError:
        return goods  # 守卫不可用 → 壳不过滤（fail-open，与模块容错一致）
    return drop_shell_urls(goods)


def finalize_l3_results(rec_results: list[dict[str, Any]],
                        max_results: int) -> list[dict[str, Any]]:
    """L3 恢复结果收口：去重 → 壳过滤 → 截断。

    主链的壳过滤跑在恢复链之前，恢复产出（L0/L3）是唯一可能把壳带回
    最终输出的入口——L0 在 _deliverable 判定处过滤，L3 在此统一收口。
    """
    merged = deduplicate_by_url(rec_results)
    try:
        from serp_guard import drop_shell_urls
    except ImportError:
        pass
    else:
        merged = drop_shell_urls(merged)
    return merged[:max_results]


def maybe_retry_skipped(
    early_stopped: bool,
    engines_combo: list[str],
    raw_results: dict[str, list[dict[str, Any]]],
    available_engines: Callable[[], list[str] | set[str]],
    query: str,
    run_one: Callable[..., tuple[str, list[dict[str, Any]], Any, float]],
    ingest: Callable[..., None],
    max_results: int,
    emit_usage: Callable[[str, dict[str, Any]], bool] | None,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """early-stop 误判后的 L0 恢复：补跑被跳过的同域引擎。

    返回 (结果, 恢复结论)。结果非空时调用方将其作为 merged、
    结论随顶层 recovery 字段输出；为空返回 ([], None)，调用方
    落 L3 换引擎。补跑结果按引擎记回 raw_results（provenance
    与自适应学习可见），并按恢复流发射使用日志。

    deadline：monotonic 秒（与 time.monotonic() 同钟）；None = 不限
    （仅测试用，生产调用方必须给——见模块 docstring 死线约束）。
    """
    if not early_stopped:
        return [], None
    skipped = [e for e in engines_combo if e not in raw_results]
    if not skipped:
        return [], None
    try:
        available = set(available_engines())
    except Exception:
        available = None
    lists: list[list[dict[str, Any]]] = []
    for eng in skipped:
        if deadline is not None:
            remain = deadline - time.monotonic()
            if remain <= 0 or remain < _MIN_USEFUL_SLICE_S:
                break
        if available is not None and eng not in available:
            continue
        try:
            _e, res, _out, _lat = run_one(eng)
            ingest(_e, res, _out, _lat)
            goods = _deliverable(res)
            if goods:
                # _engine 戳记与恢复执行器同约定：provenance 记回
                # raw_results（by_eng 分组）依赖它，缺失即静默丢账。
                for r in goods:
                    r.setdefault("_engine", eng)
                lists.append(goods)
        except Exception:
            continue
    if not lists:
        return [], None
    merged = (rrf_merge(lists) if len(lists) > 1
              else deduplicate_by_url(lists[0]))
    merged = merged[:max_results]
    if not merged:
        return [], None
    by_eng: dict[str, list[dict[str, Any]]] = {}
    for r in merged:
        eng = r.get("_engine") or r.get("source") or ""
        if eng:
            r.setdefault("consensus_engines", [eng])
            by_eng.setdefault(eng, []).append(r)
    for eng, lst in by_eng.items():
        raw_results.setdefault(eng, lst)
    info = {
        "triggered": True,
        "recovered": True,
        "level_used": engine_states.RECOVERY_LEVEL_L0,
        "strategy_used": "retry_skipped",
        "steps_tried": [{
            "level": engine_states.RECOVERY_LEVEL_L0,
            "strategy": "retry_skipped",
            "query": query,
            "engines": skipped,
            "outcome": f"{len(merged)} results",
        }],
        "final_query": query,
        "note": "early-stop 误判后补跑 skipped 引擎成功",
    }
    if emit_usage is not None:
        try:
            emit_usage("recovery", {
                "query": (query[:60] if query else query),
                "triggered": True,
                "recovered": True,
                "level_used": engine_states.RECOVERY_LEVEL_L0,
                "strategy_used": "retry_skipped",
                "steps_tried": 1,
                "final_query": (query[:60] if query else query),
                "note": info["note"],
            })
        except Exception:
            pass
    return merged, info

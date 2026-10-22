#!/usr/bin/env python3
"""recovery_runner.py — 搜索主管线错误恢复执行器与 L3 策略。

拆分自 search_pipeline.py：将零结果/异常退化恢复链与有界并发对冲执行器从主管线剥离。
"""

from __future__ import annotations

import sys
from typing import Any

from bounded_run import run_bounded
from except_sets import OPT_IMPORT, SHAPE_BENIGN
from search_rank import deduplicate_by_url


def run_pipeline_recovery(
    *,
    query: str,
    merged: list[dict[str, Any]],
    raw_results: dict[str, list],
    decision: dict[str, Any],
    engines: list[str],
    available_engines_fn: Any,
    max_results: int,
    max_rec_level: str | None,
    echo_triggered: bool,
    primary_grace_s: float,
    timeout: float,
    depth: str,
    mode: str,
    since_iso: str | None,
    until_iso: str | None,
    skip_cache: bool,
    quota_batch: Any,
    engine_search_fn: Any,
    emit_usage_fn: Any = None,
    recovery_engines_set: set[str] | None = None,
    engine_domain_extra: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """执行恢复链（L1 放宽 / L2 变体 / L3 换引擎）：有界并发对冲跑候选引擎并复检壳。"""
    recovery_info = None
    try:
        from recovery import run_recovery
        tried = list(raw_results.keys()) or list(engines)
        fallback_engines = decision.get("engines_fallback") or []

        rec_level = max_rec_level
        if echo_triggered:
            rec_level = "L3"
        elif fallback_engines and decision.get("domain") not in (
                None, "", "general", "general_search") \
                and (rec_level is None or rec_level < "L3"):
            rec_level = "L3"

        try:
            enabled_set = set(available_engines_fn())
        except Exception:
            enabled_set = None

        dk = engine_domain_extra or {}

        def _fire(eng: str) -> list[dict[str, Any]]:
            if recovery_engines_set is not None:
                recovery_engines_set.add(eng)
            try:
                res = engine_search_fn(
                    query, eng, n=max_results,
                    timeout=timeout, depth=depth, mode=mode,
                    since=since_iso, until=until_iso,
                    skip_cache=skip_cache, **dk,
                )
            except Exception as e:
                sys.stderr.write(f"[recovery] 恢复链异常（{type(e).__name__}）: {e}\n")
                res = []
            goods = [r for r in (res or []) if isinstance(r, dict) and "error" not in r]
            quota_batch.add(eng, bool(goods))
            if goods:
                for r in goods:
                    r.setdefault("_engine", eng)
                    r.setdefault("_recovered", True)
                try:
                    from postprocess_signals import drop_echo_items as _drop_echo
                    goods = _drop_echo(goods, query)
                except OPT_IMPORT + SHAPE_BENIGN:
                    pass
            return goods

        def _recovery_executor(rq: str, rengines: list[str]) -> list[dict[str, Any]]:
            if len(rengines) <= 1:
                return _fire(rengines[0]) if rengines else []
            grace = max(0.0, float(primary_grace_s or 0.0))
            wait_s = grace * (len(rengines) - 1) + max(1.0, float(timeout)) + 1.0

            def _enough(finished: list[tuple[str, Any]]) -> bool:
                return any(isinstance(v, list) and v for _i, v in finished)

            finished, _unfinished = run_bounded(
                rengines, _fire, wait_s, max_workers=3,
                enough=_enough, stagger_s=grace,
            )
            for _item, value in finished:
                if isinstance(value, list) and value:
                    return value
            return []

        rec_results, rec_result = run_recovery(
            query, tried, _recovery_executor,
            engines_fallback=fallback_engines, enabled=enabled_set, mode=mode,
            max_level=rec_level,
        )
        recovery_info = rec_result.to_dict()

        if emit_usage_fn is not None:
            try:
                emit_usage_fn("recovery", {
                    "query": (query[:60] if query else query),
                    "triggered": recovery_info.get("triggered"),
                    "recovered": recovery_info.get("recovered"),
                    "level_used": recovery_info.get("level_used"),
                    "strategy_used": recovery_info.get("strategy_used"),
                    "steps_tried": len(recovery_info.get("steps_tried") or []),
                    "final_query": (recovery_info.get("final_query") or "")[:60],
                    "note": recovery_info.get("note", ""),
                })
            except SHAPE_BENIGN:
                pass

        if rec_results:
            try:
                from recovery_l0 import finalize_l3_results
                merged = finalize_l3_results(rec_results, max_results)
            except ImportError:
                merged = deduplicate_by_url(rec_results)[:max_results]

            rec_by_eng: dict[str, list] = {}
            for r in merged:
                eng = r.get("_engine") or r.get("source") or ""
                if eng:
                    r.setdefault("consensus_engines", [eng])
                    rec_by_eng.setdefault(eng, []).append(r)
            for eng_name, lst in rec_by_eng.items():
                raw_results.setdefault(eng_name, lst)

    except ImportError:
        pass
    except Exception as e:
        sys.stderr.write(f"[recovery] 错误恢复跳过: {type(e).__name__}\n")

    return merged, recovery_info

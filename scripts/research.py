#!/usr/bin/env python3
"""
research.py — 深度研究取证机（含社交舆情模式）

取证：扩词或工作包 → 多源采集 → dossier。
判断稿由 Agent 按 references/research-protocol.md 写。
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from search import super_search, rrf_merge, deduplicate_by_url
from research_strategy import resolve_route_strategy, should_use_local_first
from research_expand import expand_query, decompose_query, _deduplicate_sub_queries
from research_dossier import (
    detect_cross_references,
    build_dossier,
    synthesize_report,
)
from research_work_packages import (
    parse_work_packages,
    stage_work_packages,
    packages_to_sub_queries,
)
from research_gates import evaluate_dossier_gates


def collect_sources(sub_queries: list[dict[str, str]], max_results: int = 5,
                    timeout: int = 15, depth: str = "balanced",
                    mode: str = "auto",
                    engines_priority: list[str] | None = None,
                    profile: dict[str, Any] | None = None,
                    budget: int | None = None,
                    route_strategy: str | None = None) -> dict[str, Any]:
    """对每个子查询并行执行搜索，返回聚合结果。"""
    all_results = []
    engines_used = set()
    sub_results = []
    t0 = time.time()
    try:
        from cache import SearchCache
        shared_cache = SearchCache()
    except Exception:
        shared_cache = None

    try:
        from engine_policy import research_engine_hints
    except ImportError:
        research_engine_hints = None  # type: ignore

    prio = [e for e in (engines_priority or []) if e]

    def _boost_for(idx: int, sq: dict[str, str]) -> list[str]:
        boosts: list[str] = []
        if research_engine_hints and profile:
            boosts = list(research_engine_hints(profile, idx) or [])
        elif prio:
            if idx <= 0:
                boosts = prio[:3]
            else:
                n = len(prio)
                start = (idx - 1) % n
                boosts = [prio[(start + i) % n] for i in range(min(2, n))]
        prefs_all = sq.get("preferred_engines") or []
        if not prefs_all:
            pref = sq.get("preferred_engine")
            if pref and pref != "auto":
                prefs_all = [pref]
        if prefs_all:
            boosts = list(prefs_all) + [e for e in boosts if e not in prefs_all]
        return boosts

    def _search_one(sq: dict[str, str], idx: int = 0) -> dict[str, Any]:
        boosts = _boost_for(idx, sq)
        strategy = resolve_route_strategy(route_strategy, mode)
        use_local_first = should_use_local_first(strategy)

        def _run(engine_override: str | None, boost_override: list[str] | None) -> dict:
            return super_search(
                sq["query"], n=max_results, timeout=timeout,
                depth=depth, mode=mode, skip_cache=False,
                cache=shared_cache,
                engine=engine_override or "auto",
                context="research",
                local_first=engine_override == "local_search",
                engines_boost=boost_override or boosts,
                envelope=False,
            )

        result = None
        upgraded = False
        if use_local_first:
            result = _run("local_search", None)
            if len(result.get("results", [])) < min(3, max_results):
                result = _run(None, None)
                upgraded = True
        if result is None:
            result = _run(None, None)
        out = {
            "sub_query": sq["query"],
            "intent": sq["intent"],
            "strategy": sq["strategy"],
            "engine_hint": "auto",
            "engines_boost": boosts,
            "results": result.get("results", []),
            "engines_used": result.get("engines_used", []),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "cached": result.get("cached", False),
            "upgraded_to_full": upgraded,
            "route_strategy": strategy,
        }
        if sq.get("package_id"):
            out["package_id"] = sq["package_id"]
        return out

    active_queries = sub_queries
    budget_exhausted = False
    if budget is not None and budget > 0 and len(sub_queries) > budget:
        active_queries = sub_queries[:budget]
        budget_exhausted = True

    if not active_queries:
        return {
            "merged_results": [],
            "sub_results": [],
            "engines_used": [],
            "total_results": 0,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "budget_exhausted": budget_exhausted,
            "budget_limit": budget,
        }

    with ThreadPoolExecutor(max_workers=min(len(active_queries), 4)) as ex:
        futures = {
            ex.submit(_search_one, sq, i): sq
            for i, sq in enumerate(active_queries)
        }
        all_futures = list(futures.keys())
        try:
            for fut in as_completed(futures, timeout=timeout * 2 + 5):
                try:
                    sr = fut.result()
                    sub_results.append(sr)
                    all_results.extend(sr["results"])
                    engines_used.update(sr["engines_used"])
                except Exception as e:
                    sq = futures[fut]
                    fail = {
                        "sub_query": sq["query"],
                        "intent": sq["intent"],
                        "strategy": sq["strategy"],
                        "results": [],
                        "engines_used": [],
                        "error": str(e),
                        "elapsed_ms": 0,
                    }
                    if sq.get("package_id"):
                        fail["package_id"] = sq["package_id"]
                    sub_results.append(fail)
        except Exception:
            for fut in all_futures:
                if fut.done() and not fut.cancelled():
                    try:
                        sr = fut.result()
                        if sr not in sub_results:
                            sub_results.append(sr)
                            all_results.extend(sr["results"])
                            engines_used.update(sr["engines_used"])
                    except Exception:
                        pass

    result_lists = [sr["results"] for sr in sub_results if sr["results"]]
    if len(result_lists) > 1:
        merged = rrf_merge(result_lists)
    elif result_lists:
        merged = deduplicate_by_url(result_lists[0])
    else:
        merged = []

    return {
        "merged_results": merged[:max_results * 3],
        "sub_results": sub_results,
        "engines_used": sorted(engines_used),
        "total_results": len(merged),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "budget_exhausted": budget_exhausted,
        "budget_limit": budget,
    }


def identify_gaps(sub_results: list[dict[str, Any]], query: str) -> list[str]:
    """识别搜索结果中的知识缺口。"""
    import re

    gaps = []
    for sr in sub_results:
        if not sr["results"]:
            gaps.append(f"子查询「{sr['intent']}」无结果：{sr['sub_query'][:40]}")
        elif len(sr["results"]) < 2:
            gaps.append(f"子查询「{sr['intent']}」结果稀少（仅 {len(sr['results'])} 条）")

    all_sources = set()
    for sr in sub_results:
        for r in sr["results"]:
            src = r.get("source", "")
            if src:
                all_sources.add(src)
    if len(all_sources) < 3:
        gaps.append(
            f"来源多样性不足：仅 {len(all_sources)} 个引擎有结果"
            f"（{', '.join(all_sources)}）"
        )

    year_match = re.search(r"20\d{2}", query)
    if year_match:
        target_year = year_match.group()
        has_recent = any(
            target_year in (r.get("title", "") + r.get("snippet", ""))
            for sr in sub_results
            for r in sr["results"]
        )
        if not has_recent:
            gaps.append(f"未找到 {target_year} 年的直接相关内容")
    return gaps


def merge_collections(parts: list[dict[str, Any]], max_results: int) -> dict[str, Any]:
    """合并分阶段采集结果。"""
    sub_results: list[dict[str, Any]] = []
    engines: set[str] = set()
    elapsed = 0
    budget_exhausted = False
    budget_limit = None
    lists: list[list[dict[str, Any]]] = []
    for p in parts:
        sub_results.extend(p.get("sub_results") or [])
        engines.update(p.get("engines_used") or [])
        elapsed += int(p.get("elapsed_ms") or 0)
        budget_exhausted = budget_exhausted or bool(p.get("budget_exhausted"))
        if p.get("budget_limit") is not None:
            budget_limit = p.get("budget_limit")
        if p.get("merged_results"):
            lists.append(p["merged_results"])
    if len(lists) > 1:
        merged = rrf_merge(lists)
    elif lists:
        merged = deduplicate_by_url(lists[0])
    else:
        merged = []
    return {
        "merged_results": merged[: max_results * 3],
        "sub_results": sub_results,
        "engines_used": sorted(engines),
        "total_results": len(merged),
        "elapsed_ms": elapsed,
        "budget_exhausted": budget_exhausted,
        "budget_limit": budget_limit,
    }


def _expand_sub_queries(
    query: str,
    num_sub_queries: int,
    profile: dict[str, Any] | None,
) -> list[dict[str, str]]:
    sub_queries: list[dict[str, str]] = []
    if profile:
        try:
            from topic_research_profiles import build_profile_sub_queries
            sub_queries = build_profile_sub_queries(query, profile, num_sub_queries)
        except Exception:
            sub_queries = []
    heuristic = expand_query(query, num_sub_queries)
    seen_q = {sq["query"] for sq in sub_queries}
    for sq in heuristic:
        if len(sub_queries) >= num_sub_queries:
            break
        if sq["query"] not in seen_q:
            sub_queries.append(sq)
            seen_q.add(sq["query"])
    if not sub_queries:
        sub_queries = heuristic
    try:
        from query_variants import generate_query_variations
        for v in generate_query_variations(query):
            if len(sub_queries) >= num_sub_queries:
                break
            if v not in seen_q:
                sub_queries.append({
                    "query": v,
                    "intent": "变体召回",
                    "strategy": "query_variant",
                })
                seen_q.add(v)
    except Exception:
        pass
    return _deduplicate_sub_queries(sub_queries[:num_sub_queries])


def _collect_work_packages(
    packages: list[dict[str, Any]],
    max_results: int,
    timeout: int,
    depth: str,
    mode: str,
    engines_priority: list[str] | None,
    profile: dict[str, Any] | None,
    budget: int | None,
    route_strategy: str | None,
) -> tuple[dict[str, Any], list[list[str]], list[str]]:
    stages, warnings = stage_work_packages(packages)
    remaining = budget
    parts: list[dict[str, Any]] = []
    exhausted = False
    stage_ids: list[list[str]] = []
    for stage in stages:
        if remaining is not None and remaining <= 0:
            exhausted = True
            break
        take = stage if remaining is None else stage[:remaining]
        if remaining is not None:
            remaining -= len(take)
            if len(take) < len(stage):
                exhausted = True
        stage_ids.append([p["id"] for p in take])
        part = collect_sources(
            packages_to_sub_queries(take),
            max_results, timeout, depth, mode,
            engines_priority=engines_priority,
            profile=profile,
            budget=None,
            route_strategy=route_strategy,
        )
        parts.append(part)
        if exhausted:
            break
    collection = merge_collections(parts, max_results) if parts else {
        "merged_results": [], "sub_results": [], "engines_used": [],
        "total_results": 0, "elapsed_ms": 0,
        "budget_exhausted": True, "budget_limit": budget,
    }
    if exhausted:
        collection["budget_exhausted"] = True
        collection["budget_limit"] = budget
    return collection, stage_ids, warnings


def _attach_profile_and_plan(
    report: dict[str, Any],
    profile: dict[str, Any] | None,
    engines_priority: list[str],
    vertical_engines: list[str],
    plan_info: dict[str, Any] | None,
    rewrite_result: dict[str, Any] | None,
) -> None:
    if profile:
        try:
            from topic_research_profiles import profile_meta
            meta = profile_meta(profile)
        except Exception:
            meta = {
                "name": profile.get("name"),
                "discipline": profile.get("discipline"),
                "quality_gates": profile.get("quality_gates") or [],
                "report_sections": profile.get("report_sections") or [],
                "source_grades": profile.get("source_grades") or {},
                "engines_priority": engines_priority,
                "vertical_engines": vertical_engines,
            }
        report["topic_profile"] = meta.get("name")
        report["topic_profile_key"] = None
        report["discipline"] = meta.get("discipline")
        report["quality_gates"] = meta.get("quality_gates") or []
        report["report_sections"] = meta.get("report_sections") or []
        report["source_grades"] = meta.get("source_grades") or {}
        report["engines_priority"] = meta.get("engines_priority") or engines_priority
        report["vertical_engines"] = meta.get("vertical_engines") or vertical_engines
        if meta.get("discipline") == "finance":
            report["disclaimer"] = (
                "本输出基于公开检索结果整理，供研究线索与信息核验，"
                "不构成投资建议；关键数据请回源核对。"
            )
        if meta.get("discipline") == "academic":
            report["academic_discipline"] = {
                "no_fabricated_doi": True,
                "separate_evidence_layers": True,
                "note": "公开摘要≠已审稿结论；DOI/期刊要求须回官方核验",
            }
    if plan_info is not None:
        report["plan"] = plan_info
        plan_lims = plan_info.get("limitations") or []
        if plan_lims:
            report.setdefault("plan_limitations", plan_lims)
    if rewrite_result and rewrite_result.get("rewritten"):
        report["rewritten_query"] = {
            "original": rewrite_result["original"],
            "rewritten": rewrite_result["rewritten"],
            "confidence": rewrite_result["confidence"],
            "reason": rewrite_result["reason"],
        }


def _attach_evidence_loop(report: dict[str, Any], collection: dict[str, Any]) -> None:
    try:
        from evidence_loop import gate_results
        all_results: list[dict[str, Any]] = []
        sub_results = collection.get("sub_results") or []
        domain_counts: dict[str, int] = {}
        for sr in sub_results:
            for r in (sr.get("results") or [])[:3]:
                if isinstance(r, dict):
                    all_results.append(r)
                    d = r.get("domain") or ""
                    if d:
                        domain_counts[d] = domain_counts.get(d, 0) + 1
        domain = max(domain_counts, key=domain_counts.get) if domain_counts else ""
        gate = gate_results(all_results, domain or None)
        report["fetch_required"] = gate["fetch_required"]
        pending = []
        for sr in sub_results:
            for r in (sr.get("results") or [])[:3]:
                if isinstance(r, dict) and r.get("fetch_suggested"):
                    pending.append({
                        "sub_query": sr.get("sub_query") or sr.get("query") or sr.get("intent") or "",
                        "title": (r.get("title") or "")[:120],
                        "url": r.get("url"),
                        "snippet": (r.get("snippet") or "")[:160],
                    })
        report["evidence_loop"] = {
            "high_consequence_domain": gate["high_consequence_domain"],
            "pending_fetch": pending,
            "verified_count": gate["verified_count"],
            "pending_count": gate["pending_count"],
            "note": (
                "高后果研究建议先核验 pending_fetch 中的信源再下结论；"
                "可用 --verify 对本报告 top 结果执行 fetch 回填。"
                if gate["fetch_required"] and pending else
                "日常研究：结果已含证据分，关键主张仍建议回源核对。"
            ),
        }
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(
            f"研究证据门控跳过: {type(e).__name__}"
        )


def deep_research(query: str, num_sub_queries: int = 4, max_results: int = 5,
                  timeout: int = 15, depth: str = "balanced",
                  mode: str = "auto",
                  profile: dict[str, Any] | None = None,
                  budget: int | None = None,
                  route_strategy: str | None = None,
                  work_packages: Any = None,
                  allow_recompute: bool = False) -> dict[str, Any]:
    """执行取证。有工作包则按依赖分阶段；否则扩词检索。产出 dossier。"""
    original_query = query
    engines_priority = list((profile or {}).get("engines_priority") or [])
    vertical_engines = list((profile or {}).get("vertical_engines") or [])

    plan_info: dict[str, Any] | None = None
    try:
        from plan import build_plan
        plan_info = build_plan(
            query, mode=mode, depth=depth, max_results=max_results,
            context="research",
        )
    except Exception:
        plan_info = None

    rewrite_result = None
    try:
        from query_rewriter import rewrite_query as do_rewrite
        rewrite_result = do_rewrite(query)
        if rewrite_result["rewritten"] and rewrite_result["confidence"] >= 0.7:
            query = rewrite_result["rewritten"]
    except Exception:
        pass

    packages: list[dict[str, Any]] = []
    if work_packages:
        packages = parse_work_packages(work_packages)

    stage_ids: list[list[str]] = []
    package_warnings: list[str] = []
    if packages:
        collection, stage_ids, package_warnings = _collect_work_packages(
            packages, max_results, timeout, depth, mode,
            engines_priority or None, profile, budget, route_strategy,
        )
        sub_queries = packages_to_sub_queries(packages)
    else:
        sub_queries = _expand_sub_queries(query, num_sub_queries, profile)
        collection = collect_sources(
            sub_queries, max_results, timeout, depth, mode,
            engines_priority=engines_priority or None,
            profile=profile,
            budget=budget,
            route_strategy=route_strategy,
        )

    gaps = identify_gaps(collection["sub_results"], original_query)
    source_grades = (profile or {}).get("source_grades") if profile else None
    all_file_inputs: list[dict[str, Any]] = []
    recompute_results: list[dict[str, Any]] = []
    recompute_expected = False
    if packages:
        for p in packages:
            all_file_inputs.extend(p.get("file_inputs") or [])
            rec = p.get("recompute")
            if rec:
                recompute_expected = True
                try:
                    from recompute import run_recompute
                    res = run_recompute(
                        rec["script"],
                        p.get("file_inputs") or [],
                        timeout_s=rec["budget"]["timeout_s"],
                        max_mem_mb=rec["budget"]["max_mem_mb"],
                        allow_exec=allow_recompute,
                    )
                except Exception as e:  # 执行器自身异常不入账为失败
                    res = {"ok": False, "skipped_reason": f"执行器异常: {e}"}
                res["package_id"] = p.get("id") or ""
                recompute_results.append(res)
    report = build_dossier(
        original_query, collection, gaps,
        source_grades=source_grades, mode=mode, depth=depth,
        file_inputs=all_file_inputs or None,
        recompute_results=recompute_results or None,
    )
    report["recompute_expected"] = recompute_expected

    report["execution_tier"] = "deep_research"
    report["requires_confirmation"] = False
    report["query_original"] = original_query
    report["sub_queries"] = [
        {"query": sq["query"], "intent": sq["intent"], "strategy": sq["strategy"]}
        for sq in sub_queries
    ]
    if packages:
        report["work_packages"] = packages
        report["work_package_stages"] = stage_ids
        if package_warnings:
            report["work_package_warnings"] = package_warnings
    else:
        report["query_expansion"] = report["sub_queries"]

    if collection.get("budget_exhausted"):
        report["budget"] = {
            "limit": collection.get("budget_limit"),
            "exhausted": True,
            "note": (
                f"工具预算 {collection.get('budget_limit')} 已用尽，"
                "仅覆盖部分子查询，以下为基于已完成查询的最佳部分答案，"
                "剩余维度见 blind_spots / gaps"
            ),
        }
    elif budget is not None:
        report["budget"] = {"limit": budget, "exhausted": False}
    if route_strategy:
        report["route_strategy"] = route_strategy

    _attach_profile_and_plan(
        report, profile, engines_priority, vertical_engines,
        plan_info, rewrite_result,
    )
    _attach_evidence_loop(report, collection)
    report["quality_gate_results"] = evaluate_dossier_gates(report)
    report["conclusion_cap"] = report["quality_gate_results"]["conclusion_cap"]
    return report


from social_research import (  # noqa: E402
    social_sentiment_research,
    aggregate_social_sentiment,
    _extract_topics,
    _print_social_report,
)


if __name__ == "__main__":
    from research_cli import main
    main()

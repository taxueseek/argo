#!/usr/bin/env python3
"""mcp_compact.py — MCP 结果载荷裁剪与紧凑压缩协议。

拆分自 mcp_handlers.py：纯数据处理与结构级截断，无网络 IO 与外部状态。
供 MCP 工具执行层（mcp_handlers）、CLI 与外部测试独立复用。
"""

from __future__ import annotations

import json
from typing import Any


def _trim_snippet(text: Any, n: int = 120) -> str | None:
    """截断摘要文本，防止超长字符膨胀。"""
    if not text:
        return None
    s = str(text)
    return s if len(s) <= n else s[:n]


def _compact_search_result(result: dict[str, Any], summary: bool = False) -> dict[str, Any]:
    """去掉 MCP 热路径用不到的重字段，缩小首包。"""
    snip_n = 80 if summary else 160
    out: dict[str, Any] = {
        "query": result.get("query"),
        "engine": result.get("engine"),
        "engines_used": result.get("engines_used") or result.get("engines"),
        "count": result.get("count"),
        "elapsed_ms": result.get("elapsed_ms"),
        "cached": result.get("cached"),
        "mode": result.get("mode"),
        "depth": result.get("depth"),
        "execution_tier": result.get("execution_tier"),
        "input_kind": result.get("input_kind"),
        "login_hint": result.get("login_hint"),
    }
    results = []
    for r in result.get("results") or []:
        if not isinstance(r, dict):
            continue
        item = {
            "title": r.get("title"),
            "url": r.get("url"),
            "snippet": _trim_snippet(r.get("snippet"), snip_n),
            "source": r.get("source"),
            "score": r.get("score"),
        }
        # 保留发布时间维度（仅引擎输出该字段时，避免空字段噪音）
        if r.get("published_at"):
            item["published_at"] = r["published_at"]
        # 顶包标记：recovery 替换了引擎原结果时透传，供上层识别结果已被代答
        if r.get("_recovered"):
            item["recovered"] = r["_recovered"]
        # 保留快评，丢掉大块 meta
        for k in ("selection", "absorption", "credibility_fast", "evidence_flags"):
            if k in r:
                item[k] = r[k]
        # 证据完整链路 P0：保留核验信号（仅存在时，避免空字段噪音）
        for k in ("fetch_suggested", "has_fetched_evidence", "post_fetch_absorption"):
            if r.get(k) is not None:
                item[k] = r[k]
        results.append(item)
    out["results"] = results
    # 证据完整链路 P0：顶层门控信号（轻量 4 字段，供 Agent 判断「能否下结论」）
    if result.get("fetch_required") is not None:
        out["fetch_required"] = result["fetch_required"]
    el = result.get("evidence_loop")
    if isinstance(el, dict):
        out["evidence_loop"] = {
            "suggested": el.get("suggested") or [],
            "verified_count": el.get("verified_count") or 0,
            "pending_count": el.get("pending_count") or 0,
        }
    # 引擎可观测性：精简版 engine_outcomes（engine/status/latency，去掉 detail）
    # 让 Agent 能看到「哪个引擎失败、为什么类别」，不占大体积
    outcomes = result.get("engine_outcomes") or []
    if outcomes:
        out["engine_outcomes"] = [
            {
                "engine": o.get("engine"),
                "status": o.get("status"),
                "latency_ms": o.get("latency_ms"),
                "results_count": o.get("results_count"),
            }
            for o in outcomes
            if isinstance(o, dict)
        ][:12]
        wasted = result.get("wasted_engine_ms")
        if wasted:
            out["wasted_engine_ms"] = wasted
    if result.get("early_stopped"):
        out["early_stopped"] = True
    if result.get("minhash_removed"):
        out["minhash_removed"] = result.get("minhash_removed")
    from search_output import decorate_compact_payload as _decorate
    _decorate(out, result)
    if result.get("domain_filter"):
        out["domain_filter"] = result["domain_filter"]
    if result.get("rank_method"):
        out["rank_method"] = result.get("rank_method")
    if result.get("reranker"):
        out["reranker"] = result.get("reranker")
    lp = result.get("lang_pref")
    if isinstance(lp, dict):
        out["lang_pref"] = {
            "query_lang": lp.get("query_lang"),
            "system_lang": lp.get("system_lang"),
            "habit_lang": lp.get("habit_lang"),
            "prefer_langs": lp.get("prefer_langs"),
            "engine_lang": lp.get("engine_lang"),
        }
    rec = result.get("recovery")
    if isinstance(rec, dict) and rec.get("triggered"):
        out["recovery"] = {
            "recovered": rec.get("recovered"),
            "level_used": rec.get("level_used"),
            "strategy_used": rec.get("strategy_used"),
            "note": rec.get("note"),
        }
    return out


def _compact_research_result(report: dict[str, Any], summary: bool = False) -> dict[str, Any]:
    """研究包：只保留 Agent 写作需要的字段，砍掉子查询原始 dumps。"""
    snip_n = 100 if summary else 160
    keys = (
        "query", "query_original", "execution_tier", "topic_profile", "topic_profile_key",
        "discipline", "quality_gates", "report_sections", "source_grades", "disclaimer",
        "academic_discipline", "engines_used", "source_distribution", "elapsed_ms",
        "sub_query_count", "total_sources", "gaps", "mode", "sub_queries",
        "engines_priority", "vertical_engines", "fetch_required",
        "kind", "conclusion_cap", "quality_gate_results", "work_packages",
        "work_package_stages", "protocol", "cross_lingual_trace",
    )
    out: dict[str, Any] = {k: report[k] for k in keys if k in report and report[k] is not None}

    # 证据完整链路 P0：研究包门控（pending_fetch 截断到 5 条，保留可编程核验信号）
    el = report.get("evidence_loop")
    if isinstance(el, dict):
        out["evidence_loop"] = {
            "high_consequence_domain": el.get("high_consequence_domain"),
            "pending_fetch": (el.get("pending_fetch") or [])[:5],
            "verified_count": el.get("verified_count") or 0,
            "pending_count": el.get("pending_count") or 0,
        }
    if isinstance(report.get("verify"), dict):
        v = report["verify"]
        out["verify"] = {
            "revision_summary": v.get("revision_summary") or {},
            "pending": (v.get("pending") or [])[:5],
        }

    findings = []
    for kf in report.get("key_findings") or []:
        if not isinstance(kf, dict):
            continue
        top = dict(kf.get("top_result") or {})
        if top.get("snippet"):
            top["snippet"] = _trim_snippet(top.get("snippet"), snip_n)
        findings.append({
            "aspect": kf.get("aspect"),
            "strategy": kf.get("strategy"),
            "top_result": top,
            "result_count": kf.get("result_count"),
            "citation_refs": kf.get("citation_refs"),
        })
    out["key_findings"] = findings

    sources = []
    for s in report.get("sources") or report.get("citations") or []:
        if not isinstance(s, dict) or not s.get("url"):
            continue
        sources.append({
            "ref": s.get("ref") or s.get("id"),
            "title": s.get("title"),
            "url": s.get("url"),
            "engine": s.get("engine") or s.get("source"),
            "snippet": _trim_snippet(s.get("snippet"), snip_n),
        })
        if summary and len(sources) >= 10:
            break
    out["sources"] = sources
    if not summary:
        out["citations"] = report.get("citations") or sources
        # 交叉引用截断
        crs = report.get("cross_references") or []
        out["cross_references"] = crs[:5]
    # 覆盖度 / 验证记录 / 盲区（科研方法论增强）
    if report.get("coverage_map"):
        out["coverage_map"] = report["coverage_map"]
    if report.get("verification_records"):
        out["verification_records"] = report["verification_records"][:12]
    if report.get("blind_spots"):
        out["blind_spots"] = report["blind_spots"][:8]
    return out


def _cap_extract_output(output: dict[str, Any]) -> dict[str, Any]:
    """argo_fetch mode=extract 的输出上限（防大页表格撑爆上下文）。

    结构级裁剪保持 JSON 可解析：表格数量/单元格、metadata 值、jsonld
    条目体积分别设上限，超限截断并打标记。
    """
    tables = output.get("tables")
    if isinstance(tables, list):
        if len(tables) > 12:
            output["tables"] = tables[:12]
            output["tables_truncated"] = True
        for t in output["tables"]:
            if isinstance(t, list):
                for i, row in enumerate(t):
                    if isinstance(row, list):
                        t[i] = [str(c)[:300] if c is not None else "" for c in row]
    meta = output.get("metadata")
    if isinstance(meta, dict):
        output["metadata"] = {k: str(v)[:500] for k, v in meta.items()}
    jsonld = output.get("jsonld")
    if isinstance(jsonld, list):
        capped = []
        for entry in jsonld:
            try:
                text = json.dumps(entry, ensure_ascii=False)
            except (TypeError, ValueError):
                continue
            if len(text) > 4000:
                capped.append({"_clipped": True, "preview": text[:4000]})
            else:
                capped.append(entry)
        if len(capped) > 8:
            capped = capped[:8]
            output["jsonld_truncated"] = True
        output["jsonld"] = capped
    return output

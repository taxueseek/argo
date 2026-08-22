#!/usr/bin/env python3
"""research_gates.py — dossier 可判定门禁。

topic profile 的 quality_gates 字符串是给 Agent 的自检提示。
这里的谓词决定 conclusion_cap，过不了就降级，不打印空勾选充数。
"""

from __future__ import annotations

from typing import Any


def evaluate_dossier_gates(dossier: dict[str, Any]) -> dict[str, Any]:
    """对取证包跑可判定谓词。返回 passed / conclusion_cap / failures / warnings。"""
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    sources = dossier.get("sources") or dossier.get("citations") or []
    url_count = sum(
        1 for s in sources if isinstance(s, dict) and s.get("url")
    )
    if url_count == 0:
        failures.append({
            "id": "no_sources",
            "detail": "可用 URL 为 0",
        })

    uncovered = [
        cm for cm in (dossier.get("coverage_map") or [])
        if isinstance(cm, dict) and cm.get("status") == "NOT_COVERED"
    ]
    if uncovered:
        failures.append({
            "id": "uncovered_dimensions",
            "detail": "、".join(
                str(cm.get("dimension") or cm.get("sub_query") or "?")
                for cm in uncovered[:8]
            ),
            "count": len(uncovered),
        })

    fetch_required = bool(dossier.get("fetch_required"))
    verified = bool(dossier.get("verify"))
    if not verified:
        el = dossier.get("evidence_loop") or {}
        verified = int(el.get("verified_count") or 0) > 0
    if fetch_required and not verified:
        failures.append({
            "id": "fetch_required_unverified",
            "detail": "高后果取证尚未 --verify / fetch",
        })

    fa = dossier.get("fact_alignment") or {}
    conflicts = fa.get("fact_conflicts") or []
    if conflicts:
        warnings.append({
            "id": "fact_conflicts",
            "detail": f"{len(conflicts)} 组事实冲突未校准",
            "count": len(conflicts),
        })

    grades = dossier.get("source_grades") or {}
    if grades:
        leads = dossier.get("source_leads") or dossier.get("verification_records") or []
        has_primary = any(
            isinstance(r, dict) and r.get("evidence_tier") == "primary"
            for r in leads
        )
        if not has_primary:
            warnings.append({
                "id": "no_primary_sources",
                "detail": "有 source_grades 但零一手命中",
            })

    if failures:
        cap = "low"
    elif warnings:
        cap = "medium"
    else:
        cap = "high"

    return {
        "passed": not failures,
        "conclusion_cap": cap,
        "failures": failures,
        "warnings": warnings,
    }

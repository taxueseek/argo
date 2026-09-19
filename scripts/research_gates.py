#!/usr/bin/env python3
"""research_gates.py — dossier 可判定检查。

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
    # 「跑过 --verify」≠「核验成功」。dossier["verify"] 是 verify_results 的返回
    # dict，而 research_cli 在 --verify 分支里**无条件**赋值它——全部 fetch 失败
    # 时它仍是 {verified: [], revision_summary: {...}, ...}，真值判据为真。
    # 于是「高后果取证尚未核验」这道门在「核验全失败」时反而放行，正是它要拦的
    # 场景。判据取 verified 列表是否非空（那是真正拿到正文的那些 URL）。
    verify_report = dossier.get("verify") or {}
    verified = bool(verify_report.get("verified")) if isinstance(verify_report, dict) \
        else bool(verify_report)
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

    # ── recompute 检查（P0-2）：可复算完整链路 ──
    rec = dossier.get("recomputed_values") or []
    # 1) 声明可复算但未执行（授权检查默认拒绝拦下）→ 结论上限 medium
    if dossier.get("recompute_expected") and not rec:
        warnings.append({
            "id": "recompute_skipped",
            "detail": "工作包声明 recompute 但未运行（未授权或执行失败）",
        })
    # 2) 重算值与检索数字无交集 → 提示冲突（以重算为准，需人工核对）
    elif rec:
        snippet_nums: set[float] = set()
        try:
            from recompute import extract_values as _extract
        except ImportError:
            _extract = None
        for s in (dossier.get("sources") or []):
            if not isinstance(s, dict):
                continue
            text = f"{s.get('title') or ''} {s.get('snippet') or ''}"
            if _extract is not None:
                snippet_nums.update(_extract(text))
        for rv in rec:
            if not rv.get("ok") or not rv.get("values"):
                continue
            if snippet_nums and not (
                set(rv["values"]) & snippet_nums
            ):
                warnings.append({
                    "id": "recompute_conflict",
                    "detail": (
                        f"重算值 {rv['values']} 与检索来源数字无交集"
                        f"（包 {rv.get('package_id') or '?'}），以重算为准"
                    ),
                })

    grades = dossier.get("source_grades") or {}
    if grades:
        leads = dossier.get("source_leads") or dossier.get("verification_records") or []
        has_primary = any(
            isinstance(r, dict) and r.get("evidence_tier") == "primary"
            for r in leads
        )
        # 本地一手数据文件（file_inputs 入账）计为一手命中：用户提供原始
        # 数据时，「零一手来源」不成立（原始数据即一手），避免假阴性。
        local_primary = bool(dossier.get("local_sources"))
        if not has_primary and not local_primary:
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

#!/usr/bin/env python3
"""research_gates.py — dossier 可判定检查。

topic profile 的 quality_gates 字符串是给 Agent 的自检提示。
这里的谓词决定 conclusion_cap，过不了就降级，不打印空勾选充数。

2026-09-27 新增三类门控（方案 C）：
  - 来源多样性：单一域名占比过高时降级（仿 CDQ 的站点级信号）
  - 时效性：过时内容占比过高时降级（仿 Google E-E-A-T 的时效性信号）
  - 事实一致性：多来源事实冲突时降级（仿 Fact-Check-X 的核验流程）
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse


def _host_of(url: str) -> str:
    """取规范化 host（www 折叠为裸域）。"""
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower().split(":", 1)[0]
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _check_source_diversity(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """来源多样性门控：单一域名占比过高时降级。

    仿 CDQ（Content Quality Score）的站点级信号：深度研究可能从同一域名
    获取多条来源（如知乎专栏的多篇文章），这些来源看似独立，实则同源。

    判据：单一域名占比 > 60% 且总来源数 >= 3 时触发。
    """
    hosts = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        host = _host_of(s.get("url") or "")
        if host:
            hosts.append(host)
    if len(hosts) < 3:
        return {"triggered": False, "max_share": 0.0, "max_host": ""}
    counts = Counter(hosts)
    max_host, max_count = counts.most_common(1)[0]
    share = max_count / len(hosts)
    return {
        "triggered": share > 0.6,
        "max_share": round(share, 3),
        "max_host": max_host,
    }


def _check_freshness(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """时效性门控：过时内容占比过高时降级。

    仿 Google E-E-A-T 的时效性信号：深度研究可能引用过时内容（如 2020 年的
    「最新推荐」），影响结论可靠性。

    判据：过时内容（> 365 天）占比 > 50% 且总来源数 >= 2 时触发。
    """
    now_ts = __import__("time").time()
    stale_count = 0
    total = 0
    for s in sources:
        if not isinstance(s, dict):
            continue
        total += 1
        # 尝试从多个字段获取时间
        date_str = (s.get("published_time") or s.get("modified_time")
                    or s.get("date") or "")
        if not date_str:
            continue
        try:
            from datetime import datetime, timezone
            # 尝试 ISO 格式
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).days
            if age_days > 365:
                stale_count += 1
        except Exception:
            continue
    if total < 2:
        return {"triggered": False, "stale_ratio": 0.0, "stale_count": 0}
    ratio = stale_count / total
    return {
        "triggered": ratio > 0.5,
        "stale_ratio": round(ratio, 3),
        "stale_count": stale_count,
    }


def _check_fact_consistency(dossier: dict[str, Any]) -> dict[str, Any]:
    """事实一致性门控：多来源事实冲突时降级。

    仿 Fact-Check-X 的核验流程：各方答案汇总 → 聚合 → 权威核验 → 最终答案。
    这里只做粗粒度检测：fact_conflicts 非空时降级。

    判据：fact_conflicts 非空且冲突数 >= 2 时触发。
    """
    fa = dossier.get("fact_alignment") or {}
    conflicts = fa.get("fact_conflicts") or []
    return {
        "triggered": len(conflicts) >= 2,
        "conflict_count": len(conflicts),
    }


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

    # ── 方案 C：三类新门控 ──────────────────────────────────────────────
    # 1) 来源多样性门控（仿 CDQ 站点级信号）
    diversity = _check_source_diversity(sources)
    if diversity["triggered"]:
        warnings.append({
            "id": "low_source_diversity",
            "detail": (
                f"单一域名 {diversity['max_host']} 占比 "
                f"{diversity['max_share']:.0%}，来源多样性不足"
            ),
            "max_host": diversity["max_host"],
            "max_share": diversity["max_share"],
        })

    # 2) 时效性门控（仿 Google E-E-A-T 时效性信号）
    freshness = _check_freshness(sources)
    if freshness["triggered"]:
        warnings.append({
            "id": "stale_content_heavy",
            "detail": (
                f"过时内容占比 {freshness['stale_ratio']:.0%}"
                f"（{freshness['stale_count']} 条），时效性不足"
            ),
            "stale_ratio": freshness["stale_ratio"],
            "stale_count": freshness["stale_count"],
        })

    # 3) 事实一致性门控（仿 Fact-Check-X 核验流程）
    consistency = _check_fact_consistency(dossier)
    if consistency["triggered"]:
        warnings.append({
            "id": "fact_consistency_low",
            "detail": (
                f"{consistency['conflict_count']} 组事实冲突未校准，"
                f"结论可靠性不足"
            ),
            "conflict_count": consistency["conflict_count"],
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

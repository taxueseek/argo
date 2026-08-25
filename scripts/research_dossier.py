#!/usr/bin/env python3
"""research_dossier.py — 取证包合成。

产出 kind=dossier。key_findings 是检索头条，不是研究结论。
SERP snippet 不得标成 verifiable。
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse, parse_qsl, urlunparse

try:
    from evidence import compute_credibility
except ImportError:  # pragma: no cover
    compute_credibility = None  # type: ignore

try:
    from fact_align import align_facts
except ImportError:  # pragma: no cover
    align_facts = None  # type: ignore


_COVERAGE_OK_MIN = 3
_COVERAGE_PARTIAL_MIN = 1
_TRACKING_KEYS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "ref", "fbclid", "gclid",
})


def detect_cross_references(
    results: list[dict[str, Any]],
    min_sources: int = 2,
    min_ngram_len: int = 3,
) -> list[dict[str, Any]]:
    """检测多个来源的交叉引用（n-gram 重叠）。"""
    ngram_sources: dict[str, set[str]] = {}
    for r in results:
        url = r.get("url", "")
        domain = urlparse(url).netloc.lower() if url else "unknown"
        if domain.startswith("www."):
            domain = domain[4:]
        text = f"{r.get('title', '')} {r.get('snippet', '')}"
        en_tokens = re.findall(r"[a-zA-Z]+", text.lower())
        cn_chars = re.findall(r"[\u4e00-\u9fff]+", text)
        cn_tokens = []
        for seg in cn_chars:
            for i in range(len(seg) - min_ngram_len + 1):
                cn_tokens.append(seg[i:i + min_ngram_len])
        all_tokens = en_tokens + cn_tokens
        for n in range(min_ngram_len, min(min_ngram_len + 1, len(all_tokens) + 1)):
            for i in range(len(all_tokens) - n + 1):
                ngram = " ".join(all_tokens[i:i + n])
                if len(ngram) >= 4:
                    ngram_sources.setdefault(ngram, set()).add(domain)

    cross_refs = []
    for ngram, domains in ngram_sources.items():
        if len(domains) >= min_sources:
            cross_refs.append({
                "ngram": ngram,
                "source_count": len(domains),
                "domains": sorted(domains),
            })
    cross_refs.sort(key=lambda x: x["source_count"], reverse=True)
    return cross_refs[:10]


def coverage_status(sr: dict[str, Any]) -> str:
    results = sr.get("results") or []
    n = len(results)
    if n >= _COVERAGE_OK_MIN:
        return "COVERED"
    if n >= _COVERAGE_PARTIAL_MIN:
        return "PARTIAL"
    return "NOT_COVERED"


def evidence_tier(source: str, source_grades: dict[str, Any] | None) -> str:
    if not source:
        return "unknown"
    grades = source_grades or {}
    for tier, items in (
        ("primary", grades.get("primary") or grades.get("一手") or []),
        ("secondary", grades.get("secondary") or grades.get("权威") or []),
        ("tertiary", grades.get("tertiary") or grades.get("参考") or []),
    ):
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, str):
                continue
            if source == it or source.startswith(it) or it in source:
                return tier
    s = source.lower()
    if any(k in s for k in (
        "arxiv", "openalex", "crossref", "semantic_scholar",
        "pubmed", "europepmc", "github", "cninfo", "fred",
        "worldbank", "nbs_stats", "sina_quote", "tencent_quote",
    )):
        return "primary"
    if any(k in s for k in (
        "zhihu", "eastmoney", "byted", "bocha", "octen",
        "cls_telegraph", "em_global_news", "jin10",
    )):
        return "secondary"
    if any(k in s for k in (
        "twitter", "reddit", "weibo", "bilibili", "xiaohongshu",
        "douban", "v2ex", "hackernews", "local_sogou", "local_baidu",
    )):
        return "tertiary"
    return "unknown"


def canonical_url(url: str) -> str:
    """去跟踪参数、fragment、www、尾斜杠，供同源去重。"""
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
    except Exception:
        return url
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    qs = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_KEYS
    ]
    path = p.path or ""
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((p.scheme.lower(), host, path, "", "&".join(
        f"{k}={v}" for k, v in qs
    ), ""))


def build_local_sources(file_inputs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """本地一手数据文件入账（血缘登记，内容不入账）。

    每条：ref（[L1] 前缀与 URLs 的 [1] 区分）、规范 path、sha256、size、
    mtime、kind、role。读取失败的文件跳过（不入账、不中断取证）。
    """
    if not file_inputs:
        return []
    out: list[dict[str, Any]] = []
    for i, fi in enumerate(file_inputs, 1):
        path = str(fi.get("path") or "")
        try:
            digest = _sha256_file(path)
            mtime = int(os.path.getmtime(path))
        except OSError:
            continue
        out.append({
            "ref": f"[L{i}]",
            "type": "file",
            "path": path,
            "sha256": digest,
            "size": int(fi.get("size") or os.path.getsize(path)),
            "mtime": mtime,
            "kind": fi.get("kind") or "",
            "role": fi.get("role") or "data",
            "note": "本地一手文件：已登记哈希与血缘，内容未入库；"
                    "引用时标注文件路径与行号",
        })
    return out


def _sha256_file(path: str, chunk: int = 256 * 1024) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def build_recomputed_values(
    recompute_results: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """recompute 产物入账：运行记录（含脚本摘要、输出尾部、提取值）。

    数值提取失败/空输出的条目仍入账（ok=False 时是审计痕迹）。
    """
    if not recompute_results:
        return []
    out = []
    for i, r in enumerate(recompute_results, 1):
        try:
            from recompute import extract_values
            values = extract_values(str(r.get("stdout") or ""))
        except Exception:
            values = []
        out.append({
            "ref": f"[R{i}]",
            "package_id": r.get("package_id") or "",
            "ok": bool(r.get("ok")),
            "skipped_reason": r.get("skipped_reason"),
            "timed_out": bool(r.get("timed_out")),
            "values": values,
            "stdout_tail": str(r.get("stdout") or "")[-300:],
            "stderr_tail": str(r.get("stderr") or "")[-200:],
            "elapsed_ms": r.get("elapsed_ms"),
            "note": "数值由本地数据重算得出；与检索数字冲突时以重算为准并标注",
        })
    return out


def _build_source_leads(
    sub_results: list[dict[str, Any]],
    source_grades: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """检索线索。snippet 不是主张，一律 unverified_snippet。"""
    records = []
    for sr in sub_results:
        results = sr.get("results") or []
        if not results:
            records.append({
                "claim": sr.get("intent", ""),
                "evidence_tier": "unknown",
                "verification_method": "no_results",
                "result": "unverifiable",
                "reason": "no_results",
            })
            continue
        # 取每包 top-3 建线索，避免一手/高后果源排位靠后时漏判
        for best in results[:3]:
            url = best.get("url") or ""
            source = best.get("source") or ""
            snippet = (best.get("snippet") or best.get("title") or "")[:120]
            tier = evidence_tier(source, source_grades)
            method_parts = [f"evidence_tier={tier}"]
            if len(results) >= 2:
                method_parts.append("multi_source")
            if any(c.isdigit() for c in (snippet or "")):
                method_parts.append("has_numbers")
            records.append({
                "claim": snippet or best.get("title") or sr.get("intent", ""),
                "source": source,
                "url": url,
                "evidence_tier": tier,
                "verification_method": "+".join(method_parts),
                "result": "unverified_snippet",
                "reason": "serp_snippet_is_not_a_claim",
            })
    return records


def _build_cross_verification(
    merged: list[dict[str, Any]],
    query: str,
    sub_results: list[dict[str, Any]],
) -> dict[str, Any]:
    base = {
        "credibility_available": False,
        "corroboration_level": "unknown",
        "cross_score": 0.0,
        "conflicts": [],
        "unverified_count": 0,
    }
    try:
        if compute_credibility is None:
            return base
        scored = compute_credibility(merged, query)
        cross = scored.get("cross_validation") or {}
        scored_results = scored.get("results") or []
        if not scored_results:
            return base
        conflicts: list[dict[str, Any]] = []
        unverified = 0
        for sr in sub_results:
            results = sr.get("results") or []
            tiers = [
                r.get("credibility", {}).get("authority", {}).get("source_type")
                for r in results[:5]
                if r.get("credibility")
            ]
            tiers = [t for t in tiers if t]
            if len(tiers) >= 2 and any(t in ("blog", "forum", "social") for t in tiers):
                conflicts.append({
                    "dimension": sr.get("intent", ""),
                    "sub_query": sr.get("sub_query", ""),
                    "detail": "同一维度内混入低证据层级来源，需人工复核",
                })
            if not results:
                unverified += 1
        return {
            "credibility_available": True,
            "corroboration_level": cross.get("corroboration_level", "unknown"),
            "cross_score": float(cross.get("score", 0.0)),
            "agreement_count": cross.get("agreement_count", 0),
            "unique_domains": cross.get("unique_domains", 0),
            "content_domains": cross.get("content_domains", 0),
            "detail": cross.get("detail", ""),
            "top_sources": [
                {
                    "url": r.get("url", ""),
                    "source": r.get("source", ""),
                    "final": r.get("credibility", {}).get("final", 0.0),
                }
                for r in scored_results[:5]
                if r.get("url")
            ],
            "conflicts": conflicts,
            "unverified_count": unverified,
        }
    except Exception:
        return base


def build_dossier(
    query: str,
    collection: dict[str, Any],
    gaps: list[str],
    source_grades: dict[str, Any] | None = None,
    mode: str = "auto",
    depth: str = "balanced",
    file_inputs: list[dict[str, Any]] | None = None,
    recompute_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """合成取证包。兼容旧名 synthesize_report。"""
    merged = collection["merged_results"]
    sub_results = collection["sub_results"]

    key_findings = []
    for sr in sub_results:
        if sr["results"]:
            best = sr["results"][0]
            key_findings.append({
                "aspect": sr["intent"],
                "strategy": sr["strategy"],
                "top_result": {
                    "title": best.get("title", ""),
                    "url": best.get("url", ""),
                    "snippet": (best.get("snippet", "") or "")[:200],
                    "score": best.get("score", 0),
                    "source": best.get("source", ""),
                },
                "result_count": len(sr["results"]),
            })

    source_counts: dict[str, int] = {}
    for r in merged:
        src = r.get("source", "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1

    citations = []
    sources = []
    seen_canon: set[str] = set()
    ref = 0
    for r in merged:
        url = r.get("url") or ""
        if not url:
            continue
        canon = canonical_url(url) or url
        if canon in seen_canon:
            continue
        seen_canon.add(canon)
        ref += 1
        title = r.get("title") or ""
        eng = r.get("source") or ""
        citations.append({
            "id": f"[{ref}]",
            "ref": ref,
            "title": title,
            "url": url,
            "canonical_url": canon,
            "source": eng,
            "score": r.get("score", 0),
            "snippet": (r.get("snippet") or "")[:160] or None,
        })
        sources.append({
            "ref": ref,
            "title": title[:160],
            "url": url,
            "engine": eng,
            "score": r.get("score"),
            "snippet": (r.get("snippet") or "")[:160] or None,
        })
        if ref >= 15:
            break

    url_to_ref = {c["url"]: c["ref"] for c in citations if c.get("url")}
    for kf in key_findings:
        top = kf.get("top_result") or {}
        u = top.get("url") or ""
        if u and u in url_to_ref:
            top["ref"] = url_to_ref[u]
            kf["citation_refs"] = [url_to_ref[u]]
        else:
            kf["citation_refs"] = []

    all_results = []
    for sr in sub_results:
        all_results.extend(sr["results"])
    cross_refs = detect_cross_references(all_results)

    coverage_map = []
    for sr in sub_results:
        results = sr.get("results") or []
        engines = {r.get("source") for r in results if r.get("source")}
        coverage_map.append({
            "dimension": sr.get("intent", ""),
            "sub_query": sr.get("sub_query") or sr.get("query") or "",
            "status": coverage_status(sr),
            "result_count": len(results),
            "engine_count": len(engines),
            "package_id": sr.get("package_id"),
        })

    blind_spots = [
        {"dimension": cm["dimension"], "reason": "无结果"}
        for cm in coverage_map if cm["status"] == "NOT_COVERED"
    ]
    blind_spots += [
        {"dimension": cm["dimension"], "reason": "单来源覆盖"}
        for cm in coverage_map
        if cm["status"] == "PARTIAL" and cm["engine_count"] <= 1
    ]
    if not blind_spots and coverage_map:
        blind_spots.append({"dimension": "全局", "reason": "无显式未覆盖维度"})

    source_leads = _build_source_leads(sub_results, source_grades)
    cross_verification = _build_cross_verification(merged, query, sub_results)

    fact_alignment = None
    if align_facts is not None:
        try:
            fact_alignment = align_facts(
                merged, min_results=3, mode=mode, depth=depth
            )
        except Exception:
            fact_alignment = None

    return {
        "kind": "dossier",
        "query": query,
        "key_findings": key_findings,
        "total_sources": collection["total_results"],
        "engines_used": collection["engines_used"],
        "source_distribution": source_counts,
        "citations": citations,
        "sources": sources,
        "local_sources": build_local_sources(file_inputs),
        "recomputed_values": build_recomputed_values(recompute_results),
        "cross_references": cross_refs,
        "coverage_map": coverage_map,
        "source_leads": source_leads,
        "verification_records": source_leads,
        "cross_verification": cross_verification,
        "fact_alignment": fact_alignment,
        "blind_spots": blind_spots,
        "gaps": gaps,
        "elapsed_ms": collection["elapsed_ms"],
        "sub_query_count": len(sub_results),
        "protocol": "references/research-protocol.md",
    }


# 旧名：测试与 MCP 仍 import synthesize_report
synthesize_report = build_dossier

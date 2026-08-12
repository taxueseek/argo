#!/usr/bin/env python3
"""
evidence_loop.py — 证据闭环（P0）：fetch 后正文吸收分回写 + URL→证据分缓存 + 高后果门控

问题重定义（第一性）：
  Argo 搜索输出的 snippet 级证据分（credibility_fast）是「候选」分，
  Agent 高后果下结论前必须 fetch 正文复核。此前 fetch 结果不回填搜索结果、
  不缓存 URL→证据分、无「该核验哪些」的可编程信号，证据闭环是开环。

MECE 分工（互不重叠）：
  A. fetch 证据提取  ：从 fetch_v3 结果提取正文级吸收分（extract_fetch_evidence）
  B. URL 证据缓存    ：URL → 正文级证据分，独立于 fetch 正文缓存（get/set_evidence）
  C. 回填            ：搜索结果若已有证据缓存则回填 post_fetch_absorption（backfill_results）
  D. 高后果门控      ：finance/health/legal 等域标记 fetch_required + fetch_suggested（gate_results）
  E. 核验模式        ：显式对 top-k 未核验结果 fetch 并产出 evidence_revision 分布（verify_results）

闭环：search 输出（建议核验） → Agent fetch → fetch_v3 写证据缓存 → 下次 search 自动回填。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("unified_search.evidence_loop")

# ── 高后果域（finance / health / legal / 事实与安全）──────────────────────────
# 命中这些域时，Agent 在把搜索结果当答案前必须先核验正文。
HIGH_CONSEQUENCE_DOMAINS: frozenset[str] = frozenset({
    # 金融
    "stock_query", "us_stock", "fund_query", "financial_news", "macro_data",
    "crypto_search", "jin10_flash", "cls_telegraph_search", "company_search",
    # 健康
    "medical", "chem_search",
    # 法律
    "legal", "us_legal", "wenshu_query",
    # 事实核查与安全关键
    "fact_check", "aviation_weather",
})

# 证据缓存 TTL：正文级证据分是稳定派生数据，但需跟随正文更新，默认与 fetch 一致
EVIDENCE_DEFAULT_TTL = 3600
EVIDENCE_DOC_TTL = 86400      # docs/reference 长 TTL（正文稳定）
EVIDENCE_NEWS_TTL = 600       # news/realtime 短 TTL（正文常更新）


def is_high_consequence_domain(domain: str | None) -> bool:
    """域是否高后果（finance/health/legal/事实安全）。"""
    return bool(domain and domain in HIGH_CONSEQUENCE_DOMAINS)


# ── A. fetch 证据提取 ──────────────────────────────────────────────────────────

def extract_fetch_evidence(fetch_result: dict[str, Any]) -> Optional[dict[str, Any]]:
    """从 fetch_v3 结果提取正文级证据分。

    返回 None 表示抓取失败或无正文，不产生证据记录（不污染缓存）。
    """
    if not fetch_result:
        return None
    if not fetch_result.get("success"):
        return None
    content = fetch_result.get("content") or ""
    if not content.strip():
        return None
    title = fetch_result.get("title") or ""
    url = fetch_result.get("url") or ""
    if not url:
        return None

    evidence: dict[str, Any] = {}
    try:
        from content_signals import compute_content_quality
        qual = compute_content_quality(content, title)
        evidence = dict(qual)
    except Exception as e:  # pragma: no cover - 防御降级
        logger.debug(f"compute_content_quality 失败: {type(e).__name__}")

    return {
        "url": url,
        "absorption": evidence.get("absorption_score"),
        "quality_score": evidence.get("quality_score", fetch_result.get("quality_score")),
        "content_ok": evidence.get("content_ok", fetch_result.get("content_ok")),
        "word_count": evidence.get("word_count", len(content)),
        "evidence_flags": {
            k: bool(evidence.get(k))
            for k in ("has_numbers", "has_definition", "has_comparison",
                      "has_howto", "has_disclose", "is_qa_format")
        },
        "page_type": fetch_result.get("page_type"),
        "source_type": fetch_result.get("source_type"),
        "fetch_method": fetch_result.get("fetch_method"),
        "cached": bool(fetch_result.get("cached", False)),
    }


def ttl_for_fetch_result(fetch_result: dict[str, Any]) -> int:
    """按页面类型选证据缓存 TTL（与 fetch_v3 写正文缓存的策略对齐）。"""
    st = fetch_result.get("source_type") or fetch_result.get("page_type") or ""
    if st in ("news", "realtime"):
        return EVIDENCE_NEWS_TTL
    if st in ("docs", "documentation", "reference"):
        return EVIDENCE_DOC_TTL
    return EVIDENCE_DEFAULT_TTL


# ── B. URL 证据缓存 ────────────────────────────────────────────────────────────

def store_fetch_evidence(url: str, evidence: dict[str, Any],
                         cache: Any | None = None,
                         ttl: int | None = None) -> None:
    """写 URL → 正文级证据分缓存（独立 kind，与 fetch 正文缓存隔离）。"""
    if not evidence:
        return
    try:
        from cache import SearchCache
        c = cache if cache is not None else SearchCache()
        c.set_evidence(url, evidence, ttl=ttl if ttl is not None else EVIDENCE_DEFAULT_TTL)
    except Exception as e:  # pragma: no cover
        logger.debug(f"store_fetch_evidence 失败: {type(e).__name__}")


def lookup_fetch_evidence(url: str, cache: Any | None = None) -> Optional[dict[str, Any]]:
    """读 URL → 正文级证据分缓存。未命中返回 None。"""
    try:
        from cache import SearchCache
        c = cache if cache is not None else SearchCache()
        hit = c.get_evidence(url)
        if hit:
            out = {k: v for k, v in hit.items() if not str(k).startswith("_")}
            return out
    except Exception as e:  # pragma: no cover
        logger.debug(f"lookup_fetch_evidence 失败: {type(e).__name__}")
    return None


# ── C. 回填 ────────────────────────────────────────────────────────────────────

def backfill_results(results: list[dict[str, Any]],
                     cache: Any | None = None) -> list[dict[str, Any]]:
    """对搜索结果回填已核验证据（若 URL 有证据缓存）。

    原地标记（不改排序）：
      - has_fetched_evidence: bool
      - post_fetch_absorption: float | None（正文级吸收分）
      - fetched_evidence: dict（完整证据记录，含 word_count/quality/content_ok）
    """
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        if not url:
            continue
        ev = lookup_fetch_evidence(url, cache)
        if ev and ev.get("absorption") is not None:
            r["has_fetched_evidence"] = True
            r["post_fetch_absorption"] = ev.get("absorption")
            r["fetched_evidence"] = {
                k: ev.get(k) for k in (
                    "quality_score", "word_count", "content_ok",
                    "page_type", "fetch_method", "evidence_flags",
                )
            }
    return results


# ── D. 高后果门控 ──────────────────────────────────────────────────────────────

def gate_results(results: list[dict[str, Any]],
                 domain: str | None,
                 cache: Any | None = None) -> dict[str, Any]:
    """证据门控：标记哪些结果建议核验 + 是否高后果域。

    返回 gate 元数据（不修改 results 排序）：
      - fetch_required: bool（高后果域）
      - high_consequence_domain: str | None
      - suggested: 建议核验的 URL 列表
      - verified_count: 已有正文证据的结果数
      - pending_count: 建议核验但尚未核验的结果数
    每条结果原地标记 fetch_suggested（SERP/跳转链与已核验不标记）。
    """
    backfill_results(results, cache)
    verified_count = 0
    suggested: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        serp = bool(r.get("authority_tier") == "serp") or bool(
            r.get("evidence_flags", {}).get("is_serp"))
        if r.get("has_fetched_evidence"):
            verified_count += 1
            r["fetch_suggested"] = False
            continue
        is_serp_url = False
        if url:
            try:
                from evidence import is_serp_or_jump_url
                is_serp_url = is_serp_or_jump_url(url)
            except Exception:  # pragma: no cover
                is_serp_url = False
        if serp or is_serp_url or not url:
            r["fetch_suggested"] = False
            continue
        r["fetch_suggested"] = True
        suggested.append(url)

    hc = is_high_consequence_domain(domain)
    return {
        "fetch_required": hc,
        "high_consequence_domain": domain if hc else None,
        "suggested": suggested,
        "verified_count": verified_count,
        "pending_count": len(suggested),
    }


# ── E. 核验模式（显式，不阻塞热路径）────────────────────────────────────────────

def verify_results(results: list[dict[str, Any]],
                   query: str,
                   cache: Any | None = None,
                   fetch_fn: Callable[..., dict[str, Any]] | None = None,
                   top_k: int = 3,
                   max_chars: int = 8000,
                   timeout: float = 8.0) -> dict[str, Any]:
    """对 top_k 未核验结果显式 fetch，回填证据分并产出 evidence_revision 分布。

    不进热路径：调用方（CLI --verify / research --verify）显式触发。
    已核验（有证据缓存）的结果跳过，不重复打网。

    返回：
      - verified: [{url, title, pre_absorption, post_absorption, delta, content_ok, fetch_method}]
      - revision_summary: {n, improved, unchanged, degraded, mean_delta, median_delta}
      - pending: 仍然未核验的 URL（fetch 失败/SERP）
      - skipped_cached: 命中有证据缓存而跳过的 URL 数
    """
    if fetch_fn is None:
        try:
            from fetch_v3 import fetch_v3
            fetch_fn = fetch_v3
        except ImportError as e:  # pragma: no cover
            return {"error": f"fetch_v3 不可用: {e}", "verified": [],
                    "revision_summary": {}, "pending": [], "skipped_cached": 0}

    verified: list[dict[str, Any]] = []
    pending: list[str] = []
    skipped_cached = 0
    revisions: list[float] = []

    for r in results[:top_k]:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        if not url:
            continue
        # 已核验：跳过，不重复 fetch
        if lookup_fetch_evidence(url, cache) is not None:
            skipped_cached += 1
            continue
        pre = float(r.get("post_fetch_absorption")
                    if r.get("post_fetch_absorption") is not None
                    else r.get("absorption") or 0.0)
        try:
            fr = fetch_fn(url, max_chars=max_chars, timeout=timeout)
        except Exception as e:  # pragma: no cover
            logger.debug(f"verify fetch 异常 {url}: {type(e).__name__}")
            pending.append(url)
            continue
        ev = extract_fetch_evidence(fr)
        if ev is None:
            pending.append(url)
            continue
        store_fetch_evidence(url, ev, cache, ttl=ttl_for_fetch_result(fr))
        post = float(ev["absorption"] or 0.0)
        delta = round(post - pre, 3)
        revisions.append(delta)
        verified.append({
            "url": url,
            "title": (r.get("title") or "")[:120],
            "pre_absorption": round(pre, 3),
            "post_absorption": round(post, 3),
            "delta": delta,
            "content_ok": ev.get("content_ok"),
            "fetch_method": ev.get("fetch_method"),
            "word_count": ev.get("word_count"),
        })
        # 回填当前结果（内存内即时生效，便于调用方直接使用）
        r["has_fetched_evidence"] = True
        r["post_fetch_absorption"] = round(post, 3)
        r["fetch_suggested"] = False

    summary: dict[str, Any] = {"n": len(revisions)}
    if revisions:
        summary.update({
            "improved": sum(1 for d in revisions if d > 0.02),
            "unchanged": sum(1 for d in revisions if abs(d) <= 0.02),
            "degraded": sum(1 for d in revisions if d < -0.02),
            "mean_delta": round(sum(revisions) / len(revisions), 3),
            "median_delta": round(sorted(revisions)[len(revisions) // 2], 3),
            "max_delta": round(max(revisions), 3),
            "min_delta": round(min(revisions), 3),
        })

    return {
        "query": query,
        "verified": verified,
        "revision_summary": summary,
        "pending": pending,
        "skipped_cached": skipped_cached,
    }

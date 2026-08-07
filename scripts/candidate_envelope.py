#!/usr/bin/env python3
"""
candidate_envelope.py — 候选交接包（统一候选交接 schema）

在保留 Argo 原有 results[] 的前提下，附加：
  - candidates[]  统一字段 + verification/provenance
  - coverage[]    每后端返回/截断/局限
  - limitations[] 全局局限
  - input_kind / schema_version

设计原则：
  - 纯后处理，不改检索结果排序
  - metrics 缺失写 null，不用 0 伪造
  - snippet 仅作线索（verification.status 默认 candidate）
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "fbclid", "gclid", "mc_cid", "mc_eid",
}

_TZ_CN = timezone(timedelta(hours=8))


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in _TRACKING]
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), ""))
    except Exception:
        return url


def _candidate_id(platform: str, url: str, source_id: str | None = None) -> str:
    if source_id:
        return f"{platform}:{source_id}"
    raw = canonicalize_url(url) or url or ""
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{platform}:{h}"


def _platform_of(url: str, source: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "github.com" in host:
        return "github"
    if "zhihu.com" in host:
        return "zhihu"
    if "xiaohongshu.com" in host or "xhslink.com" in host:
        return "xiaohongshu"
    if "bilibili.com" in host:
        return "bilibili"
    if "weibo.com" in host:
        return "weibo"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if host in {"x.com", "twitter.com"} or host.endswith(".x.com"):
        return "x"
    if "mp.weixin.qq.com" in host:
        return "wechat"
    if source and source.startswith("local_"):
        return "web"
    return "web"


def _login_state_of(item: dict[str, Any], source: str = "") -> bool:
    """结果是否使用了登录态（ego-browser / 显式 provenance）。"""
    if item.get("login_state_used") is True:
        return True
    if item.get("cache_eligible") is False:
        return True
    auth = item.get("auth_partition")
    if isinstance(auth, str) and auth.lower().startswith("login"):
        return True
    blob = " ".join(
        str(x) for x in (
            source,
            item.get("source"),
            item.get("_engine"),
            item.get("engine"),
            item.get("backend"),
            item.get("fetch_method"),
        )
        if x
    ).lower()
    return "ego-browser" in blob or "ego_browser" in blob or "browser_api" in blob


def result_to_candidate(
    item: dict[str, Any],
    query: str,
    rank: int,
    route_reason: str | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    url = item.get("url") or ""
    source = item.get("source") or item.get("_engine") or ""
    platform = _platform_of(url, source)
    canon = canonicalize_url(url) or url
    social = item.get("social_meta") if isinstance(item.get("social_meta"), dict) else {}
    metrics = {
        "likes": social.get("likes") if social else None,
        "comments": social.get("comments") if social else None,
        "collects": social.get("collects") if social else None,
        "shares": social.get("shares") or social.get("retweets"),
        "views": social.get("views") or social.get("play"),
    }
    # 明确后端给了 0 才保留 0；否则 null
    for k, v in list(metrics.items()):
        if v is None:
            metrics[k] = None
        elif v == "":
            metrics[k] = None

    retrieved = retrieved_at or datetime.now(_TZ_CN).isoformat()
    login_used = _login_state_of(item, source)
    limitations = ["snippet is a discovery clue, not verified body text"]
    if login_used:
        limitations.append("login_state_used: not eligible for public SearchCache")
    return {
        "candidate_id": _candidate_id(platform, url),
        "query": query,
        "platform": platform,
        "backend": source or "unknown",
        "rank": rank,
        "title": item.get("title") or "",
        "url": url,
        "canonical_url": canon,
        "snippet": (item.get("snippet") or "")[:300],
        "author": social.get("author") if social else None,
        "published_at": item.get("published_at") or social.get("published_at"),
        "content_type": "social_post" if social else "web_page",
        "language": None,
        "metrics": metrics,
        "access": {
            "visibility": "authenticated" if login_used else "public",
            "login_state_used": login_used,
        },
        "verification": {
            "status": "candidate",  # snippet 线索，未打开原文
            "opened_original": False,
            "checked_at": None,
        },
        "provenance": {
            "source_id": social.get("id") if social else None,
            "retrieved_at": retrieved,
            "route_reason": route_reason,
            "score": item.get("score"),
            "credibility_fast": item.get("credibility_fast"),
            "selection": item.get("selection"),
            "absorption": item.get("absorption"),
            "consensus_engines": item.get("consensus_engines"),
        },
        "limitations": limitations,
    }


def build_coverage(engine_outcomes: list[dict[str, Any]] | None, max_results: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for o in engine_outcomes or []:
        status = o.get("status") or "unknown"
        n = int(o.get("results_count") or 0)
        eng = o.get("engine") or ""
        login_used = bool(o.get("login_state_used")) or _login_state_of(
            o if isinstance(o, dict) else {}, str(eng)
        )
        out.append({
            "backend": o.get("engine"),
            "status": status,
            "returned": n,
            "truncated": n >= max_results if n else False,
            "latency_ms": o.get("latency_ms"),
            "login_state_used": login_used,
            "detail": o.get("detail"),
            "limitations": (
                ["empty or failed backend"]
                if status not in ("ok", "ok-cached", "partial")
                else []
            ),
        })
    return out


def attach_envelope(
    search_result: dict[str, Any],
    *,
    query: str | None = None,
    input_kind: str = "keyword",
    route_reason: str | None = None,
    extra_limitations: list[str] | None = None,
) -> dict[str, Any]:
    """在原 search 结果上附加 envelope 字段（原地扩展并返回）。"""
    q = query or search_result.get("query") or ""
    results = search_result.get("results") or []
    retrieved = datetime.now(_TZ_CN).isoformat()
    candidates = [
        result_to_candidate(
            r, q, rank=i + 1,
            route_reason=route_reason or search_result.get("domain"),
            retrieved_at=retrieved,
        )
        for i, r in enumerate(results)
        if isinstance(r, dict) and "error" not in r
    ]
    max_results = int(search_result.get("count") or len(results) or 5)
    coverage = build_coverage(search_result.get("engine_outcomes"), max_results=max_results)

    limitations = list(extra_limitations or [])
    limitations.append("Do not treat engagement metrics as factual correctness.")
    if search_result.get("early_stopped"):
        limitations.append("early_stopped: later engines in combo may not have run.")
    if search_result.get("recovery"):
        limitations.append("recovery path used; results may come from fallback engines.")
    if search_result.get("cached"):
        limitations.append(f"served from cache level={search_result.get('cache_level')}")

    route_login = (
        search_result.get("login_state_used") is True
        or search_result.get("cache_eligible") is False
        or _login_state_of(search_result, str(search_result.get("engine") or ""))
        or any(c.get("access", {}).get("login_state_used") for c in candidates)
    )
    if route_login:
        limitations.append("login_state_used: do not write to public SearchCache")

    # 去重：canonical_url
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for c in candidates:
        key = c.get("canonical_url") or c.get("url") or c.get("candidate_id")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    search_result["schema_version"] = "1.0"
    search_result["input_kind"] = input_kind
    search_result["candidates"] = deduped
    search_result["coverage"] = coverage
    search_result["limitations"] = limitations
    # 兼容候选交接最小交付
    search_result.setdefault("routes", [{
        "platform": "web",
        "backend": search_result.get("engine"),
        "engines": search_result.get("engines_combo") or search_result.get("engines"),
        "mode": search_result.get("mode"),
        "login_state_used": bool(route_login),
        "status": "completed" if not search_result.get("errors") else "partial",
        "limitations": limitations[:3],
    }])
    return search_result

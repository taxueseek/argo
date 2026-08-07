#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""public 常规检索 + login 专业检索 的分析层融合。

纪律：
  - 不写公共 SearchCache
  - 保留各自 search_partition / runtime / source
  - 去重以 canonical URL 为主，login 与 public 冲突时并列，不静默覆盖
"""
from __future__ import annotations

import hashlib
import json
import sys
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "fbclid", "gclid",
}


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in _TRACKING]
        return urlunparse((p.scheme, p.netloc.lower(), p.path, p.params, urlencode(q), ""))
    except Exception:
        return url


def _items_from_payload(payload: dict[str, Any], *, partition: str) -> list[dict[str, Any]]:
    """从 search 类或 fetch 类 payload 抽出可融合条目。"""
    out: list[dict[str, Any]] = []
    runtime = payload.get("runtime") or ("login" if partition == "login" else "public")
    source = payload.get("source") or payload.get("engine") or "unknown"
    base_meta = {
        "search_partition": payload.get("search_partition") or partition,
        "runtime": runtime,
        "source": source,
        "login_state_used": bool(payload.get("login_state_used") or partition == "login"),
        "cache_eligible": payload.get("cache_eligible", partition != "login"),
        "merge_with_public_ok": payload.get("merge_with_public_ok", True),
    }

    results = payload.get("results")
    if isinstance(results, list):
        for i, r in enumerate(results):
            if not isinstance(r, dict):
                continue
            url = r.get("url") or ""
            out.append({
                **base_meta,
                "rank_in_source": i + 1,
                "title": r.get("title") or "",
                "url": url,
                "canonical_url": canonicalize_url(url),
                "snippet": (r.get("snippet") or "")[:500],
                "kind": "serp",
            })

    # fetch / detail 正文
    if payload.get("content") or payload.get("fetch_method") == "browser":
        url = payload.get("url") or ""
        out.append({
            **base_meta,
            "rank_in_source": 0,
            "title": payload.get("title") or "",
            "url": url,
            "canonical_url": canonicalize_url(url),
            "snippet": (payload.get("content") or "")[:800],
            "word_count": payload.get("word_count"),
            "kind": "body",
            "quality": payload.get("quality"),
        })

    detail = payload.get("detail")
    if isinstance(detail, dict) and (detail.get("content") or detail.get("url")):
        url = detail.get("url") or ""
        out.append({
            **base_meta,
            "rank_in_source": 0,
            "title": detail.get("title") or "",
            "url": url,
            "canonical_url": canonicalize_url(url),
            "snippet": (detail.get("content") or "")[:800],
            "kind": "body",
            "quality": detail.get("quality"),
        })

    return out


def merge_payloads(
    public: dict[str, Any] | None,
    login: dict[str, Any] | None,
    *,
    query: str | None = None,
) -> dict[str, Any]:
    """融合两路结果，供 evidence / 报告使用。"""
    public = public if isinstance(public, dict) else {}
    login = login if isinstance(login, dict) else {}
    q = query or public.get("query") or login.get("query") or ""

    public_items = _items_from_payload(public, partition="public")
    login_items = _items_from_payload(login, partition="login")

    # 去重：同 canonical 保留两边各一条，标记 conflict 若标题/摘要明显不同
    by_url: dict[str, list[dict]] = {}
    for it in public_items + login_items:
        key = it.get("canonical_url") or it.get("url") or hashlib.sha256(
            json.dumps(it, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:12]
        by_url.setdefault(key, []).append(it)

    merged: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for key, group in by_url.items():
        parts = {g.get("search_partition") for g in group}
        if len(parts) > 1 and len(group) >= 2:
            # public + login 同 URL
            titles = { (g.get("title") or "").strip() for g in group }
            if len(titles) > 1:
                conflicts.append({
                    "canonical_url": key,
                    "partitions": sorted(parts),
                    "titles": list(titles),
                })
            merged.append({
                "canonical_url": key,
                "url": group[0].get("url"),
                "partitions": sorted(parts),
                "variants": group,
                "dual_sourced": True,
            })
        else:
            g = group[0]
            merged.append({
                "canonical_url": key,
                "url": g.get("url"),
                "partitions": [g.get("search_partition")],
                "variants": group,
                "dual_sourced": False,
                "title": g.get("title"),
                "snippet": g.get("snippet"),
                "kind": g.get("kind"),
                "source": g.get("source"),
                "runtime": g.get("runtime"),
                "login_state_used": g.get("login_state_used"),
            })

    return {
        "schema": "ego_search_merge_v1",
        "query": q,
        "public_count": len(public_items),
        "login_count": len(login_items),
        "merged_count": len(merged),
        "dual_sourced_count": sum(1 for m in merged if m.get("dual_sourced")),
        "conflicts": conflicts,
        "merged": merged,
        "isolation": {
            "public_cache_eligible": True,
            "login_cache_eligible": False,
            "note": "login 结果禁止写入 public SearchCache；本对象仅供分析",
        },
        "sources": {
            "public": {
                "engine": public.get("engine"),
                "source": public.get("source"),
                "count": public.get("count") or len(public_items),
            },
            "login": {
                "engine": login.get("engine"),
                "source": login.get("source"),
                "runtime": login.get("runtime"),
                "count": login.get("count") or len(login_items),
            },
        },
    }


def load_json_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"expected object JSON: {path}")
    return data


def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="融合 public + login 搜索结果")
    ap.add_argument("--public", required=True, help="常规检索 JSON 文件")
    ap.add_argument("--login", required=True, help="ego-search 登录态 JSON 文件")
    ap.add_argument("--query", default=None)
    args = ap.parse_args(argv)
    out = merge_payloads(
        load_json_file(args.public),
        load_json_file(args.login),
        query=args.query,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

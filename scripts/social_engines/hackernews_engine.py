#!/usr/bin/env python3
"""Hacker News 搜索引擎（HN Algolia 公开 API，零密钥）

对齐 social_engines 统一 schema（title/url/snippet/source/score/social_meta），
sentiment 聚合（aggregate_social_sentiment）按 social_meta 互动字段直接可用。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

HN_API = "https://hn.algolia.com/api/v1/search"


def _http_get(url: str, timeout: int = 10) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "argo-search/1.0 (+https://github.com/taxueseek/argo)", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def search(query: str, n: int = 5) -> list[dict[str, Any]]:
    """通过 HN Algolia API 搜索帖子（story 类型）。"""
    if not query or not query.strip():
        return []
    params = urllib.parse.urlencode({
        "query": query.strip(),
        "tags": "story",
        "hitsPerPage": max(1, min(int(n), 20)),
    })
    url = f"{HN_API}?{params}"
    try:
        data = json.loads(_http_get(url).decode("utf-8"))
    except Exception:
        return []

    results: list[dict[str, Any]] = []
    for h in (data.get("hits") or []):
        if not isinstance(h, dict) or not h.get("title"):
            continue
        object_id = str(h.get("objectID") or "")
        item_url = h.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
        points = h.get("points") or 0
        comments = h.get("num_comments") or 0
        author = h.get("author") or ""
        created = h.get("created_at") or None
        text = h.get("story_text") or (h.get("title") or "")
        snippet = (text[:300] if isinstance(text, str) else "")
        results.append({
            "title": h["title"],
            "url": item_url,
            "snippet": snippet,
            "source": "hackernews",
            "score": max(1.0 - len(results) * 0.1, 0.1),
            "published_at": created,
            "social_meta": {
                "platform": "hackernews",
                "content_type": "story",
                "id": object_id,
                "author": author,
                "likes": points,
                "comments": comments,
                "provider": "hn_algolia",
            },
        })
        if len(results) >= n:
            break
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hacker News search engine (HN Algolia)")
    parser.add_argument("action", nargs="?", default="search")
    parser.add_argument("query", nargs="?")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.query:
        print("[]")
        return
    results = search(args.query, args.n)
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        for i, r in enumerate(results, 1):
            meta = r.get("social_meta") or {}
            print(f"### {i}. {r['title']}")
            print(f"- **URL**: {r['url']}")
            print(f"- {r['snippet'][:200]}")
            print(f"- points={meta.get('likes')} comments={meta.get('comments')} @{meta.get('author', '')}")
            print()


if __name__ == "__main__":
    main()

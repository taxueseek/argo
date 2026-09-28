#!/usr/bin/env python3
"""Hacker News 搜索引擎（HN Algolia 公开 API，零密钥）

保持一致 social_engines 统一 schema（title/url/snippet/source/score/social_meta），
sentiment 聚合（aggregate_social_sentiment）按 social_meta 互动字段直接可用。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

# 出口调度唯一入口（issue #13 同类修复）：urlopen 不认 config.yaml 的
# network.proxy，裸用会在「必须经代理才能出网」的环境里整源连不上。
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from net_proxy import open_url  # noqa: E402

HN_API = "https://hn.algolia.com/api/v1/search"


def _http_get(url: str, timeout: int = 10) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "argo-search/1.0 (+https://github.com/taxueseek/argo)", "Accept": "application/json"},
    )
    with open_url(req, timeout=timeout) as resp:
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
    except Exception as e:
        # 吞异常返空会让 MCP 社交搜索把网络故障静默报成「平台成功、0 结果」
        # （mcp_handlers 的 err=None 设计依赖引擎抛异常或返 error 占位，
        # 见 tests/test_regression_p0p1.py「零结果应以 errors 提示」契约）。
        # 对齐 zhihu_engine 范式：失败必须带着 error 字段浮上来。
        return [{"error": f"hackernews {type(e).__name__}: {e}", "source": "hackernews"}]

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

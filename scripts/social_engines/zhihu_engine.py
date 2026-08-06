#!/usr/bin/env python3
"""知乎搜索引擎（知乎开放平台 zhihu_search API）

密钥：环境变量 ZHIHU_ACCESS_SECRET（或 ARGO_ZHIHU_ACCESS_SECRET），
未配置时返回空列表（不抛错、不静默假成功——error 由调用侧显示）。
对齐 social_engines 统一 schema，sentiment 聚合按 social_meta 直接可用。
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

ZHIHU_API = "https://developer.zhihu.com/api/v1/content/zhihu_search"


def _secret() -> str:
    return os.environ.get("ZHIHU_ACCESS_SECRET") or os.environ.get("ARGO_ZHIHU_ACCESS_SECRET", "")


def search(query: str, n: int = 5) -> list[dict[str, Any]]:
    """通过知乎开放平台 zhihu_search 搜索站内内容。"""
    if not query or not query.strip():
        return []
    secret = _secret()
    if not secret:
        return []

    params = urllib.parse.urlencode({
        "Query": query.strip(),
        "Count": str(max(1, min(int(n), 20))),
        "SearchDB": "zhihu",
    })
    url = f"{ZHIHU_API}?{params}"
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "X-Request-Timestamp": str(int(time.time())),
        "User-Agent": "argo-search/2.6 (unified-search@local)",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []

    if data.get("Code") not in (0, None):
        return []

    results: list[dict[str, Any]] = []
    items = (data.get("Data") or {}).get("Items") or []
    for item in items[:n]:
        if not isinstance(item, dict):
            continue
        title = (item.get("Title") or "").strip()
        url_ = item.get("Url") or ""
        snippet = (item.get("ContentText") or title or "")[:300]
        if not title and not url_:
            continue
        content_id = str(item.get("ContentID") or "")
        author = item.get("AuthorName") or ""
        likes = item.get("VoteUpCount") or 0
        comments = item.get("CommentCount") or 0
        edit_ts = item.get("EditTime")
        published = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(edit_ts)) if edit_ts else None

        results.append({
            "title": title[:100] + ("..." if len(title) > 100 else ""),
            "url": url_,
            "snippet": snippet,
            "source": "zhihu",
            "score": max(1.0 - len(results) * 0.1, 0.1),
            "published_at": published,
            "social_meta": {
                "platform": "zhihu",
                "content_type": (item.get("ContentType") or "answer").lower(),
                "id": content_id,
                "author": author,
                "likes": likes,
                "comments": comments,
                "provider": "zhihu_openapi",
            },
        })
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Zhihu search engine (zhihu_openapi)")
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
            print(f"- likes={meta.get('likes')} comments={meta.get('comments')} @{meta.get('author', '')}")
            print()


if __name__ == "__main__":
    main()

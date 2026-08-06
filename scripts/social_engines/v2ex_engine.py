#!/usr/bin/env python3
"""V2EX 社区搜索引擎（公开网页，零密钥）

复用 argo route 库 v2ex builder 的 HTML 解析逻辑，对齐 social_engines schema。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any


def _http_get(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def search(query: str, n: int = 5) -> list[dict[str, Any]]:
    """V2EX 站内搜索（q 参数 + 结果页解析）。"""
    if not query or not query.strip():
        return []
    url = f"https://www.v2ex.com/search?q={urllib.parse.quote(query.strip())}"
    try:
        html = _http_get(url)
    except Exception:
        return []

    results: list[dict[str, Any]] = []
    # 主题列表：<a href="/t/xxxx"> 标题 </a>
    topic_re = re.compile(r'<a[^>]*href="(/t/\d+)[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
    seen = set()
    for m in topic_re.finditer(html):
        path, raw_title = m.group(1), m.group(2)
        if path in seen:
            continue
        seen.add(path)
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        if not title or len(title) < 2:
            continue
        results.append({
            "title": title[:100] + ("..." if len(title) > 100 else ""),
            "url": f"https://www.v2ex.com{path}",
            "snippet": title[:300],
            "source": "v2ex",
            "score": max(1.0 - len(results) * 0.1, 0.1),
            "social_meta": {
                "platform": "v2ex",
                "content_type": "topic",
                "id": path.lstrip("/t/"),
                "provider": "v2ex_html",
            },
        })
        if len(results) >= n:
            break
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="V2EX search engine")
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
            print(f"### {i}. {r['title']}")
            print(f"- **URL**: {r['url']}")
            print()


if __name__ == "__main__":
    main()

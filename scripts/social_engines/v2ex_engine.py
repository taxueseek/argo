#!/usr/bin/env python3
"""V2EX 社区搜索引擎（公开网页，零密钥）

复用 argo route 库 v2ex builder 的 HTML 解析逻辑，保持一致 social_engines schema。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any

# 出口调度唯一入口（issue #13 同类修复）：urlopen 不认 config.yaml 的
# network.proxy，裸用会在「必须经代理才能出网」的环境里整源连不上。
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from net_proxy import open_url  # noqa: E402


def _http_get(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    )
    with open_url(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def search(query: str, n: int = 5) -> list[dict[str, Any]]:
    """V2EX 站内搜索（q 参数 + 结果页解析）。"""
    if not query or not query.strip():
        return []
    url = f"https://www.v2ex.com/search?q={urllib.parse.quote(query.strip())}"
    try:
        html = _http_get(url)
    except Exception as e:
        # 吞异常返空会让 MCP 社交搜索把网络故障静默报成「平台成功、0 结果」
        # （mcp_handlers 的 err=None 设计依赖引擎抛异常或返 error 占位，
        # 见 tests/test_regression_p0p1.py「零结果应以 errors 提示」契约）。
        # 对齐 zhihu_engine 范式：失败必须带着 error 字段浮上来。
        return [{"error": f"v2ex {type(e).__name__}: {e}", "source": "v2ex"}]

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
                # 剥的是**前缀**，不是字符集：lstrip("/t/") 会连 topic id 里
                # 出现的 't' 一起吃掉（当前 href 正则限定 \d+ 才没暴露）。
                "id": path.removeprefix("/t/"),
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

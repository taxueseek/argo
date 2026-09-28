#!/usr/bin/env python3
"""Reddit 搜索引擎

使用 Reddit JSON API（无需认证即可搜索公开内容）。
也可通过 rdt CLI 或 praw 扩展。
"""

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

# 出口调度唯一入口（issue #13 同类修复）：urlopen 原生只认标准 HTTP(S)_PROXY
# 环境变量，**不认** config.yaml 的 network.proxy —— 裸用会在「必须经代理才能
# 出网」的环境里整源连不上。本文件在 scripts/ 子目录，父目录不在 sys.path 上。
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from net_proxy import open_url  # noqa: E402


def _http_get_with_retry(url: str, headers: dict, timeout: int = 10, max_retries: int = 2):
    """带重试的 HTTP GET，尊重 429 + Retry-After。

    仅使用 stdlib，不引入第三方依赖。
    返回 (body_bytes, status_code)。失败时抛出最后一次异常。
    """
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with open_url(req, timeout=timeout) as resp:
                return resp.read(), resp.status
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                retry_after = int(e.headers.get("Retry-After", "5"))
                time.sleep(min(retry_after, 30))
                continue
            raise
        except (urllib.error.URLError, OSError):
            if attempt < max_retries:
                time.sleep(2 ** attempt + 0.5)
                continue
            raise
    return b"", 0


def search_reddit_api(query: str, n: int = 5) -> list[dict]:
    """通过 Reddit JSON API 搜索"""
    encoded = urllib.parse.quote(query)
    url = f"https://www.reddit.com/search.json?q={encoded}&limit={n}&sort=relevance"
    headers = {"User-Agent": "argo-search/1.0 (by taxueseek)"}
    try:
        body, _ = _http_get_with_retry(url, headers, timeout=10, max_retries=2)
        data = json.loads(body.decode("utf-8"))
        return _parse_reddit_response(data, n)
    except Exception as e:
        # 吞异常返空会让 MCP 社交搜索把网络故障静默报成「平台成功、0 结果」
        # （mcp_handlers 的 err=None 设计依赖引擎抛异常或返 error 占位，
        # 见 tests/test_regression_p0p1.py「零结果应以 errors 提示」契约）。
        # 对齐 zhihu_engine 范式：失败必须带着 error 字段浮上来。
        return [{"error": f"reddit {type(e).__name__}: {e}", "source": "reddit"}]


def _parse_reddit_response(data: dict, n: int) -> list[dict]:
    """解析 Reddit API 响应"""
    results = []
    for child in data.get("data", {}).get("children", [])[:n]:
        post = child.get("data", {})
        if not post:
            continue
        title = post.get("title", "")
        selftext = post.get("selftext", "")
        results.append({
            "title": title[:100] + ("..." if len(title) > 100 else ""),
            "url": f"https://reddit.com{post.get('permalink', '')}",
            "snippet": (selftext or title)[:300],
            "source": "reddit",
            "score": max(1.0 - len(results) * 0.1, 0.1),
            "social_meta": {
                "platform": "reddit",
                "content_type": "post",
                "subreddit": post.get("subreddit", ""),
                "author": post.get("author", ""),
                "upvotes": post.get("ups", 0),
                "comments": post.get("num_comments", 0),
                "awards": post.get("total_awards_received", 0),
            }
        })
    return results


def search(query: str, n: int = 5) -> list[dict]:
    """主搜索入口"""
    # 优先尝试 rdt CLI
    try:
        result = subprocess.run(
            ["rdt", "search", query, "--limit", str(n), "--json"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",  # Windows GBK locale 下 UTF-8 输出会解码崩
        )
        if result.returncode == 0 and result.stdout.strip():
            return _parse_rdt_json(result.stdout, n)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # fallback: Reddit JSON API
    return search_reddit_api(query, n)


def _parse_rdt_json(raw: str, n: int) -> list[dict]:
    """解析 rdt CLI JSON 输出"""
    results = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("results", data.get("posts", []))
        else:
            items = []
        for post in items[:n]:
            if not isinstance(post, dict):
                continue
            title = post.get("title", "")
            results.append({
                "title": title,
                "url": post.get("url", post.get("permalink", "")),
                "snippet": (post.get("selftext", "") or title)[:300],
                "source": "reddit",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "reddit",
                    "content_type": "post",
                    "subreddit": post.get("subreddit", ""),
                    "author": post.get("author", ""),
                    "upvotes": post.get("ups", post.get("upvotes", 0)),
                    "comments": post.get("num_comments", 0),
                }
            })
    except (json.JSONDecodeError, ValueError):
        pass
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Reddit search engine")
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
            print(f"- {r['snippet'][:200]}")
            print()


if __name__ == "__main__":
    main()

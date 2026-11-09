#!/usr/bin/env python3
"""Reddit 搜索引擎

通过 Reddit RSS（Atom）搜索公开内容，无需认证。也可通过 rdt CLI 扩展。

端点史：无 key 的 search.json 自 2026-10 起对直连请求返回 Cloudflare HTML
挑战页（bot 与浏览器 UA 无差别），RSS 是仅存的 keyless 通道；RSS 不带
互动数（upvotes/comments），social_meta 相应字段恒 0。
"""

import html as _html
import json
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET

# 出口调度唯一入口（issue #13 同类修复）：urlopen 原生只认标准 HTTP(S)_PROXY
# 环境变量，**不认** config.yaml 的 network.proxy —— 裸用会在「必须经代理才能
# 出网」的环境里整源连不上。本文件在 scripts/ 子目录，父目录不在 sys.path 上。
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from net_proxy import http_get  # noqa: E402


_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return _html.unescape(re.sub(r"\s+", " ", text)).strip()


def search_reddit_api(query: str, n: int = 5) -> list[dict]:
    """通过 Reddit RSS（Atom）搜索"""
    url = (f"https://www.reddit.com/search.rss?q={urllib.parse.quote(query)}"
           f"&sort=relevance&limit={n}")
    try:
        # 单次 GET（stdlib，不重试）：无 key 端点按 IP「一次一发」，2026-10 实测
        # 首次 200 后即连续 429、Retry-After 达分钟级，短间隔重试必然再 429。
        body, _ = http_get(url, {"User-Agent": "argo-search/1.0 (by taxueseek)"}, timeout=10)
        return _parse_reddit_rss(body, n)
    except Exception as e:
        # 吞异常返空会让 MCP 社交搜索把网络故障静默报成「平台成功、0 结果」
        # （mcp_handlers 的 err=None 设计依赖引擎抛异常或返 error 占位，
        # 见 tests/test_regression_p0p1.py「零结果应以 errors 提示」契约）。
        # 对齐 zhihu_engine 范式：失败必须带着 error 字段浮上来。
        return [{"error": f"reddit {type(e).__name__}: {e}", "source": "reddit"}]


def _parse_reddit_rss(body: bytes, n: int) -> list[dict]:
    """解析 search.rss 的 Atom 响应。link 命中 /comments/ 的是帖子，
    其余（如 /r/<sub>/ 子版块页）按 listing 收录。"""
    root = ET.fromstring(body.decode("utf-8", errors="replace"))
    results = []
    for entry in root.findall("atom:entry", _ATOM_NS)[:n]:
        link_el = entry.find("atom:link", _ATOM_NS)
        url = link_el.get("href", "") if link_el is not None else ""
        m = re.match(r"https://(?:www\.)?reddit\.com/r/([^/]+)/", url or "")
        title = (entry.findtext("atom:title", "", _ATOM_NS) or "").strip()
        author = entry.findtext("atom:author/atom:name", "", _ATOM_NS) or ""
        if author.startswith("/u/"):
            author = author[3:]
        snippet = _strip_html(entry.findtext("atom:content", "", _ATOM_NS))
        results.append({
            "title": title[:100] + ("..." if len(title) > 100 else ""),
            "url": url,
            "snippet": snippet[:300] or title,
            "source": "reddit",
            "score": max(1.0 - len(results) * 0.1, 0.1),
            "social_meta": {
                "platform": "reddit",
                "content_type": "post" if "/comments/" in url else "listing",
                "subreddit": m.group(1) if m else "",
                "author": author,
                # RSS 不带互动数，字段保留供 aggregate_social_sentiment 消费
                "upvotes": 0,
                "comments": 0,
                "awards": 0,
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

    # fallback: Reddit RSS（search.json 已被 Cloudflare 挑战页取代）
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

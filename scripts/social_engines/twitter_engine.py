#!/usr/bin/env python3
"""Twitter/X 搜索引擎

后端优先级：
  1. FxTwitter API v2（api.fxtwitter.com，零 Key）— 主路径
  2. twitter CLI（tw）
  3. nitter 公开实例 HTML

FxTwitter 能力：
  - GET /2/search?q=...&count=N  推文搜索
  - GET /2/status/{id}           单条推文
  - 旧版 GET /:user?/status/:id  兼容

文档：https://docs.fxembed.com/api/twitter/
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

FXTWITTER_BASE = "https://api.fxtwitter.com"
USER_AGENT = "argo-search/1.0 (+https://github.com/taxueseek/argo; fxtwitter)"

# 推文 URL / snowflake id
# URL 中允许短历史 ID（如 jack/status/20）；纯数字查询要求 snowflake 长度以免误伤
_TWEET_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com|fxtwitter\.com|fixupx\.com)"
    r"/(?:i/web/status|[^/\s]+/status)/(\d{1,25})",
    re.I,
)
_STATUS_ID_RE = re.compile(r"^\s*(\d{10,25})\s*$")


def _http_get_with_retry(
    url: str,
    headers: dict | None = None,
    timeout: int = 10,
    max_retries: int = 2,
) -> tuple[bytes, int]:
    """带重试的 HTTP GET，尊重 429 + Retry-After。仅 stdlib。"""
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if headers:
        hdrs.update(headers)

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def _extract_status_id(query: str) -> str | None:
    """从查询中提取推文 ID（URL 或纯数字）。"""
    if not query:
        return None
    m = _TWEET_URL_RE.search(query)
    if m:
        return m.group(1)
    m = _STATUS_ID_RE.match(query)
    if m:
        return m.group(1)
    return None


def _status_to_result(item: dict[str, Any], rank: int = 0) -> dict[str, Any] | None:
    """将 FxTwitter status 对象转为统一 schema。"""
    if not isinstance(item, dict):
        return None
    # v2 status 可能包在 status 键下，或直接是 status 对象
    if "text" not in item and isinstance(item.get("status"), dict):
        item = item["status"]
    text = (item.get("text") or "").strip()
    url = item.get("url") or ""
    sid = str(item.get("id") or "")
    if not url and sid:
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        handle = author.get("screen_name") or "i"
        url = f"https://x.com/{handle}/status/{sid}"
    if not text and not url:
        return None

    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    likes = item.get("likes") or 0
    reposts = item.get("reposts") if item.get("reposts") is not None else item.get("retweets") or 0
    replies = item.get("replies") or 0
    views = item.get("views")
    quotes = item.get("quotes") or 0
    bookmarks = item.get("bookmarks") or 0

    title = text[:100] + ("..." if len(text) > 100 else "")
    if not title:
        title = f"Tweet {sid}" if sid else url

    # 互动分：简单归一化，便于排序参考
    engagement = int(likes or 0) + int(reposts or 0) * 2 + int(replies or 0)
    score = max(1.0 - rank * 0.05, 0.1)
    if engagement > 0:
        score = min(1.0, score + min(engagement / 10000.0, 0.3))

    return {
        "title": title,
        "url": url,
        "snippet": text[:300],
        "source": "twitter",
        "score": round(score, 3),
        "published_at": item.get("created_at") or None,
        "social_meta": {
            "platform": "twitter",
            "content_type": "tweet",
            "id": sid,
            "author": author.get("screen_name") or "",
            "author_name": author.get("name") or "",
            "likes": likes,
            "retweets": reposts,
            "reposts": reposts,
            "replies": replies,
            "views": views,
            "quotes": quotes,
            "bookmarks": bookmarks,
            "lang": item.get("lang"),
            "provider": "fxtwitter",
        },
    }


def fetch_status(status_id: str, timeout: int = 10) -> list[dict[str, Any]]:
    """按推文 ID 拉取单条（v2 优先，旧端点兜底）。"""
    endpoints = [
        f"{FXTWITTER_BASE}/2/status/{status_id}",
        f"{FXTWITTER_BASE}/status/{status_id}",
    ]
    for url in endpoints:
        try:
            body, _ = _http_get_with_retry(url, timeout=timeout, max_retries=2)
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict):
                continue
            code = data.get("code", 200)
            if code not in (200, None, "200"):
                continue
            # v2: {status: {...}} ; v1: {tweet: {...}}
            item = data.get("status") or data.get("tweet") or data
            result = _status_to_result(item if isinstance(item, dict) else {}, rank=0)
            if result:
                return [result]
        except Exception:
            continue
    return []


def search_fxtwitter(query: str, n: int = 5, timeout: int = 10) -> list[dict[str, Any]]:
    """通过 FxTwitter /2/search 搜索推文。"""
    if not query or not query.strip():
        return []

    # URL / ID 查询 → 单条拉取
    status_id = _extract_status_id(query)
    if status_id:
        return fetch_status(status_id, timeout=timeout)[:n]

    params = urllib.parse.urlencode({"q": query.strip(), "count": max(1, min(int(n), 20))})
    url = f"{FXTWITTER_BASE}/2/search?{params}"
    try:
        body, _ = _http_get_with_retry(url, timeout=timeout, max_retries=2)
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return []

    if not isinstance(data, dict):
        return []
    code = data.get("code", 200)
    if code not in (200, None, "200"):
        return []

    results: list[dict[str, Any]] = []
    for i, item in enumerate(data.get("results") or []):
        if not isinstance(item, dict):
            continue
        # 仅处理 status 类型；忽略用户卡等
        if item.get("type") and item.get("type") != "status":
            continue
        parsed = _status_to_result(item, rank=i)
        if parsed:
            results.append(parsed)
        if len(results) >= n:
            break
    return results


def search_nitter(query: str, n: int = 5) -> list[dict]:
    """通过 nitter 公开实例搜索推文（零认证，兜底）。"""
    nitter_instances = [
        "https://nitter.net",
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
    ]
    encoded_query = urllib.parse.quote(query)

    for base in nitter_instances:
        try:
            url = f"{base}/search?f=tweets&q={encoded_query}&since=2025-01-01"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
            body, _ = _http_get_with_retry(url, headers=headers, timeout=8, max_retries=1)
            html = body.decode("utf-8", errors="replace")
            return _parse_nitter_html(html, n)
        except Exception:
            continue
    return []


def _parse_nitter_html(html: str, n: int) -> list[dict]:
    """解析 nitter 搜索结果 HTML。"""
    results = []
    tweet_pattern = re.compile(
        r'<div class="tweet-content[^"]*".*?'
        r'<a class="tweet-link" href="([^"]+)".*?'
        r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>',
        re.DOTALL,
    )
    for match in tweet_pattern.finditer(html):
        url = match.group(1)
        content = match.group(2)
        content = re.sub(r"<[^>]+>", " ", content).strip()
        content = re.sub(r"\s+", " ", content)
        if len(content) > 10:
            results.append({
                "title": content[:100] + ("..." if len(content) > 100 else ""),
                "url": url if url.startswith("http") else f"https://twitter.com{url}",
                "snippet": content[:300],
                "source": "twitter",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "twitter",
                    "content_type": "tweet",
                    "provider": "nitter",
                },
            })
        if len(results) >= n:
            break
    return results


def search(query: str, n: int = 5) -> list[dict]:
    """主搜索入口：FxTwitter → tw CLI → nitter。"""
    # 1) FxTwitter（主路径）
    try:
        results = search_fxtwitter(query, n=n, timeout=10)
        if results:
            return results
    except Exception:
        pass

    # 2) twitter CLI
    try:
        result = subprocess.run(
            ["tw", "search", query, "--limit", str(n), "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            parsed = _parse_tw_json(result.stdout, n)
            if parsed:
                return parsed
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 3) nitter 兜底
    return search_nitter(query, n)


def _parse_tw_json(raw: str, n: int) -> list[dict]:
    """解析 twitter CLI JSON 输出。"""
    results = []
    for line in raw.strip().split("\n"):
        try:
            tweet = json.loads(line)
            if not isinstance(tweet, dict):
                continue
            results.append({
                "title": (tweet.get("text", "") or tweet.get("full_text", ""))[:100],
                "url": tweet.get("url", f"https://twitter.com/i/status/{tweet.get('id', '')}"),
                "snippet": tweet.get("text", "") or tweet.get("full_text", ""),
                "source": "twitter",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "twitter",
                    "content_type": "tweet",
                    "author": tweet.get("user", {}).get("screen_name", ""),
                    "likes": tweet.get("favorite_count", 0),
                    "retweets": tweet.get("retweet_count", 0),
                    "replies": tweet.get("reply_count", 0),
                    "provider": "tw_cli",
                },
            })
        except (json.JSONDecodeError, ValueError):
            continue
        if len(results) >= n:
            break
    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Twitter search engine (FxTwitter)")
    parser.add_argument("action", nargs="?", default="search")
    parser.add_argument("query", nargs="?")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.query:
        print("[]")
        return

    action = (args.action or "search").lower()
    if action == "status" and args.query:
        results = fetch_status(args.query)
    elif action in ("search", "s") and args.query:
        results = search(args.query, args.n)
    elif args.query:
        # 未知 action 时把 query 当搜索词
        results = search(args.query, args.n)
    else:
        # 兼容：`twitter_engine.py "查询词"`
        results = search(args.action, args.n)

    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        for i, r in enumerate(results, 1):
            print(f"### {i}. {r['title']}")
            print(f"- **URL**: {r['url']}")
            print(f"- {r['snippet'][:200]}")
            meta = r.get("social_meta") or {}
            if meta.get("likes") is not None:
                print(
                    f"- likes={meta.get('likes')} reposts={meta.get('retweets') or meta.get('reposts')} "
                    f"replies={meta.get('replies')} @{meta.get('author', '')}"
                )
            print()


if __name__ == "__main__":
    main()

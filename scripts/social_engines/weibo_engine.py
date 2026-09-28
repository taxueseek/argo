#!/usr/bin/env python3
"""微博搜索引擎

使用微博公开搜索 API（无需登录可搜索热门内容）。
"""

import json
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


def search(query: str, n: int = 5) -> list[dict]:
    """通过微博搜索 API 搜索"""
    encoded = urllib.parse.quote(query)
    url = f"https://m.weibo.cn/api/container/getIndex?containerid=100103type%3D1%26q%3D{encoded}&page_type=searchall"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": "https://m.weibo.cn"
    }
    try:
        body, _ = _http_get_with_retry(url, headers, timeout=10, max_retries=2)
        data = json.loads(body.decode("utf-8"))
        return _parse_weibo_response(data, n)
    except Exception as e:
        # 吞异常返空会让 MCP 社交搜索把网络故障静默报成「平台成功、0 结果」
        # （mcp_handlers 的 err=None 设计依赖引擎抛异常或返 error 占位，
        # 见 tests/test_regression_p0p1.py「零结果应以 errors 提示」契约）。
        # 对齐 zhihu_engine 范式：失败必须带着 error 字段浮上来。
        return [{"error": f"weibo {type(e).__name__}: {e}", "source": "weibo"}]


def _parse_weibo_response(data: dict, n: int) -> list[dict]:
    """解析微博 API 响应"""
    results = []
    for card in data.get("data", {}).get("cards", []):
        for card_group in card.get("card_group", []):
            mblog = card_group.get("mblog", {})
            if not mblog:
                continue
            text = mblog.get("text", "")
            import re
            text = re.sub(r'<[^>]+>', '', text).strip()
            if len(text) < 10:
                continue
            results.append({
                "title": text[:100] + ("..." if len(text) > 100 else ""),
                "url": f"https://weibo.com/{mblog.get('user', {}).get('id', '')}/{mblog.get('bid', '')}",
                "snippet": text[:300],
                "source": "weibo",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "weibo",
                    "content_type": "post",
                    "author": mblog.get("user", {}).get("screen_name", ""),
                    "likes": mblog.get("attitudes_count", 0),
                    "reposts": mblog.get("reposts_count", 0),
                    "comments": mblog.get("comments_count", 0),
                    "verified": mblog.get("user", {}).get("verified", False),
                }
            })
            if len(results) >= n:
                break
        if len(results) >= n:
            break
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Weibo search engine")
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

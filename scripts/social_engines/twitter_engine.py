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
import urllib.parse
from typing import Any

# 出口调度唯一入口（issue #13 同类修复）：urlopen 不认 config.yaml 的
# network.proxy，裸用会在「必须经代理才能出网」的环境里整源连不上。
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fxtwitter_api import extract_status_id as _fx_extract_status_id  # noqa: E402
from fxtwitter_api import http_get as _fx_http_get  # noqa: E402

FXTWITTER_BASE = "https://api.fxtwitter.com"
USER_AGENT = "argo-search/1.0 (+https://github.com/taxueseek/argo; fxtwitter)"

# 推文 URL / snowflake id 的解析与 HTTP 出口统一收在 fxtwitter_api（打包器
# tweet.py 用的是同一份）；本模块只保留「引擎侧阈值」——纯数字要求 ≥10 位，
# 见 _extract_status_id。此前这里自带一份正则，与打包器那份迟早漂移。


def _http_get_with_retry(
    url: str,
    headers: dict | None = None,
    timeout: int = 10,
    max_retries: int = 2,
) -> tuple[bytes, int]:
    """带重试的 HTTP GET（实现唯一在 fxtwitter_api.http_get）。

    保留本名与签名：既有测试 patch 的正是 `tw._http_get_with_retry`，薄壳让
    patch 语义不变；同时引擎与打包器共用同一份「重试 + 代理 + SSRF 守卫」纪律，
    不再各写一遍。
    """
    return _fx_http_get(url, headers=headers, timeout=timeout,
                        max_retries=max_retries)
    return b"", 0


def _extract_status_id(query: str) -> str | None:
    """从查询中提取推文 ID（URL 或纯数字）。

    解析器唯一实现在 fxtwitter_api.extract_status_id；这里只固定搜索侧的阈值
    min_digits=10——`argo search "20"` 里的 20 可能是页码或数量，不该当推文 ID；
    而 `argo tweet "20"` 是用户显式给的 ID（那边传 1）。
    """
    return _fx_extract_status_id(query or "", min_digits=10)


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
    """按推文 ID 拉取单条（v2 优先，旧端点保底）。"""
    endpoints = [
        f"{FXTWITTER_BASE}/2/status/{status_id}",
        f"{FXTWITTER_BASE}/status/{status_id}",
    ]
    last_err: Exception | None = None
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
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        # 全部端点异常 ≠ 「没有这条推文」：带 error 浮上来（同 search_fxtwitter）
        return [{"error": f"twitter {type(last_err).__name__}: {last_err}",
                 "source": "twitter"}]
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
    except Exception as e:
        # 吞异常返空会让 MCP 社交搜索把网络故障静默报成「平台成功、0 结果」
        # （mcp_handlers 的 err=None 设计依赖引擎抛异常或返 error 占位，
        # 见 tests/test_regression_p0p1.py「零结果应以 errors 提示」契约）。
        # 对齐 zhihu_engine 范式：失败必须带着 error 字段浮上来。
        return [{"error": f"twitter {type(e).__name__}: {e}", "source": "twitter"}]

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


# Nitter 实例健康状态（进程内记忆）：记录每个实例的连续失败次数和
# 最后健康时间，优先尝试最近健康的实例。实例不可达时不永久封禁，
# 而是逐步降权——Nitter 实例可用性波动大，单次失败不等于永久不可用。
_nitter_health: dict[str, dict] = {}
_NITTER_MAX_FAILURES = 3


def _nitter_healthy_instances() -> list[str]:
    """按健康状态排序的 Nitter 实例列表，最近健康的优先。"""
    instances = [
        "https://nitter.net",
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.privacyredirect.com",
        "https://nitter.tiekoetter.com",
    ]
    def _sort_key(base: str) -> tuple:
        h = _nitter_health.get(base)
        if h is None:
            return (0, 0.0)  # 未记录过，优先尝试
        fails = h.get("fails", 0)
        last_ok = h.get("last_ok", 0.0)
        return (fails, -last_ok)  # 失败少的优先，最近健康的优先
    return sorted(instances, key=_sort_key)


def search_nitter(query: str, n: int = 5) -> list[dict]:
    """通过 nitter 公开实例搜索推文（零认证，保底）。

    多实例故障转移：按健康状态排序，优先尝试最近健康的实例。
    实例失败时降权（不永久封禁），成功时恢复权重。
    """
    encoded_query = urllib.parse.quote(query)

    for base in _nitter_healthy_instances():
        try:
            url = f"{base}/search?f=tweets&q={encoded_query}&since=2025-01-01"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
            body, _ = _http_get_with_retry(url, headers=headers, timeout=8, max_retries=1)
            html = body.decode("utf-8", errors="replace")
            # 更新健康状态
            _nitter_health[base] = {"fails": 0, "last_ok": time.time()}
            results = _parse_nitter_html(html, n)
            # 空结果也算实例健康（只是没有匹配内容），不降权
            return results
        except Exception:
            # 失败时增加失败计数
            h = _nitter_health.get(base) or {}
            h["fails"] = h.get("fails", 0) + 1
            _nitter_health[base] = h
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
    """主搜索入口：FxTwitter → twitter/tw CLI → nitter。"""
    # 1) FxTwitter（主路径）
    # 注意：search_fxtwitter 在异常时返回带 error 字段的占位（不是空列表），
    # 那是给 MCP 的「失败可见」契约用的；但在这里它**不能阻断后续通道**——
    # 否则一次网络抖动就让整条兜底链失效（2026-11-03 issue #36 实测）。
    fx_errors: list[dict] = []
    try:
        results = search_fxtwitter(query, n=n, timeout=3, max_retries=0)
        valid = [r for r in results
                 if isinstance(r, dict) and not r.get("error")]
        if valid:
            return valid
        fx_errors = [r for r in results
                     if isinstance(r, dict) and r.get("error")]
    except Exception:
        pass

    # 2) twitter-cli（`twitter`）/ `tw` CLI，两者参数形态不同，逐个试
    # CLI 只认自己的变量：Twitter 凭据从 engine_env 读，代理从 TWITTER_PROXY 读，
    # 这里把它们收拢成一份子进程环境，避免 CLI 在 Windows 上按 GBK 读输出崩。
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    for k in ("NO_PROXY", "no_proxy"):
        if k in env and "[::1]" in env[k]:
            env[k] = ",".join(x for x in env[k].split(",") if x.strip() != "[::1]")
    proxy_val = (
        env.get("TWITTER_PROXY")
        or env.get("ARGO_PROXY")
        or env.get("HTTPS_PROXY")
        or env.get("https_proxy")
        or env.get("HTTP_PROXY")
        or env.get("http_proxy")
    )
    if proxy_val and proxy_val.lower() != "direct":
        env.setdefault("TWITTER_PROXY", proxy_val)
    try:
        from engine_env import get_env

        for k in ("TWITTER_AUTH_TOKEN", "TWITTER_CT0", "TWITTER_PROXY"):
            val = get_env([k])
            if val:
                env.setdefault(k, val)
        if not env.get("TWITTER_PROXY"):
            argo_proxy = get_env(["ARGO_PROXY"])
            if argo_proxy and argo_proxy.lower() != "direct":
                env["TWITTER_PROXY"] = argo_proxy
    except Exception:
        pass

    for cmd in (
        ["twitter", "search", query, "-n", str(n), "--json"],
        ["tw", "search", query, "--limit", str(n), "--json"],
    ):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",  # Windows GBK locale 下 UTF-8 输出会解码崩
                errors="replace",
                timeout=12,
                env=env,
            )
            if result.returncode == 0 and result.stdout.strip():
                parsed = _parse_tw_json(result.stdout, n)
                if parsed:
                    return parsed
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    # 3) nitter 保底
    nitter_res = search_nitter(query, n)
    if nitter_res:
        return nitter_res
    # 全部通道不可用：把 FxTwitter 的 error 占位带回去，保持「失败可见」契约
    return fx_errors


def _parse_tw_json(raw: str, n: int) -> list[dict]:
    """解析 twitter-cli / tw CLI 的 JSON 输出。

    三种形态都要认（不同版本/不同 CLI 的输出并不统一）：

    - JSONL：每行一个 tweet 对象（`tw` 的默认形态）
    - 数组：`[{...}, {...}]`
    - envelope：`{"ok": true, "data": [{...}]}`（`twitter` CLI 的形态）
    - 单对象：`{...}`（单条结果时）

    错误字典（如 `{"error": "No tweets found for ..."}`）与非 tweet 对象
    一律跳过——issue #36 的根因之一就是它曾被当成有效条目产出。
    """
    results: list[dict] = []
    stripped = raw.strip()
    if not stripped:
        return results

    items: list[dict] = []
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        payload = None
        # JSONL 兜底：整体解析失败时逐行试
        for line in stripped.split("\n"):
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                items.append(obj)
            elif isinstance(obj, list):
                items.extend(x for x in obj if isinstance(x, dict))

    if payload is not None:
        if isinstance(payload, list):
            items.extend(x for x in payload if isinstance(x, dict))
        elif isinstance(payload, dict):
            # 错误/空结果信封：ok 为假，或带 error 字段
            if payload.get("ok") in (False, 0, "false", "False"):
                return results
            if payload.get("error"):
                return results
            data = payload.get("data")
            if isinstance(data, list):
                items.extend(x for x in data if isinstance(x, dict))
            elif isinstance(data, dict):
                items.append(data)
            elif isinstance(payload, dict) and (
                    "text" in payload or "full_text" in payload or "id" in payload):
                # 单条 tweet 对象
                items.append(payload)

    for tweet in items:
        if not isinstance(tweet, dict):
            continue
        text = (tweet.get("text") or tweet.get("full_text") or "").strip()
        tid = str(tweet.get("id") or "")
        if not text and not tid:
            continue
        author_obj = tweet.get("author") or tweet.get("user") or {}
        if not isinstance(author_obj, dict):
            author_obj = {}
        handle = (author_obj.get("screenName") or author_obj.get("screen_name") or "")
        metrics = tweet.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        likes = metrics.get("likes") if "likes" in metrics else tweet.get("favorite_count", 0)
        retweets = (metrics.get("retweets") if "retweets" in metrics
                    else tweet.get("retweet_count", 0))
        replies = metrics.get("replies") if "replies" in metrics else tweet.get("reply_count", 0)
        views = metrics.get("views") if "views" in metrics else tweet.get("views")
        url = tweet.get("url") or (f"https://x.com/{handle}/status/{tid}" if tid else "")
        results.append({
            "title": text[:100] + ("..." if len(text) > 100 else ""),
            "url": url,
            "snippet": text[:300],
            "source": "twitter",
            "published_at": tweet.get("createdAtISO") or tweet.get("createdAt") or None,
            "score": max(1.0 - len(results) * 0.1, 0.1),
            "social_meta": {
                "platform": "twitter",
                "content_type": "tweet",
                "id": tid,
                "author": handle,
                "author_name": author_obj.get("name") or "",
                "likes": likes or 0,
                "retweets": retweets or 0,
                "reposts": retweets or 0,
                "replies": replies or 0,
                "views": views,
                "provider": "twitter_cli",
            },
        })
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

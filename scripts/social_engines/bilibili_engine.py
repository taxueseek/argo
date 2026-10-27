#!/usr/bin/env python3
"""B站搜索引擎

使用 B站公开搜索 API（无需登录可搜索视频）。

2026-10-27 起补三件事（此前搜到视频但读不出「这是视频、多长、谁发的」）：
  - 时长筛选：把 --min-duration/--max-duration 映射成 B 站 API 的 duration
    档位（1=<10min、2=10-30、3=30-60、4=>60min），档位表达不了的范围
    再按解析出的秒数后过滤；
  - 元数据完整化：发布时间转 ISO、收藏/投币/分享一并带出（envelope 只映射
    它认识的键，但引擎层不该先丢）；
  - snippet 前置「UP主 · 时长 · 播放量」：否则五维排序和下游模型只看到标题，
    视频结果无法按「长视频/高播放」判断。
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# 出口调度唯一入口（issue #13 同类修复）：urlopen 原生只认标准 HTTP(S)_PROXY
# 环境变量，**不认** config.yaml 的 network.proxy —— 裸用会在「必须经代理才能
# 出网」的环境里整源连不上。本文件在 scripts/ 子目录，父目录不在 sys.path 上。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from net_proxy import open_url  # noqa: E402
from video_meta import parse_duration_seconds  # noqa: E402

_TZ_CN = timezone(timedelta(hours=8))


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


def _duration_bucket(min_seconds, max_seconds):
    """把秒级范围映射成 B 站搜索的 duration 档位；跨档返回 None（只后过滤）。

    档位语义来自 B 站 search/all/v2：1=<10min、2=10-30min、3=30-60min、
    4=>60min。档位只做**粗召回**，精确范围仍由 _parse_bilibili_response
    里的后过滤保证；两者都做，才不会为了精确而丢掉「档位内但不在区间」的
    大量候选。
    """
    if min_seconds is None and max_seconds is None:
        return None
    if min_seconds is None:
        # 只有上限时唯一安全的档位是「≤10 分钟」（档位 1 正好覆盖全集）。
        # 上限 30/60 分钟不能用档位 2/3：那会把更短的视频一起排除，而用户
        # 只要求「不超过」，没有要求「至少 10/30 分钟」。
        return 1 if max_seconds <= 600 else None
    if max_seconds is None:
        # 只有下限时同理：档位 4（>60 分钟）是下限 ≥60 分钟的安全超集；
        # 下限 10/30 分钟不能用档位 2/3，否则会丢掉更长（>30/>60）的视频。
        return 4 if min_seconds >= 3600 else None
    if min_seconds >= 3600:
        return 4
    if 600 <= min_seconds and max_seconds <= 1800:
        return 2
    if 1800 <= min_seconds and max_seconds <= 3600:
        return 3
    if max_seconds <= 600:
        return 1
    return None


def _format_play(count) -> str:
    try:
        n = int(count)
    except (TypeError, ValueError):
        return ""
    if n >= 10000:
        text = f"{n / 10000:.1f}万"
        return text.replace(".0万", "万")
    return str(n)


def _published_iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=_TZ_CN).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _clean_title(raw: str) -> str:
    """B 站搜索标题带 <em class="keyword"> 高亮和 HTML 实体。"""
    text = re.sub(r"<[^>]+>", "", raw or "")
    return html.unescape(text).strip()


def _snippet_of(item: dict, title: str) -> str:
    """把视频元数据前置到摘要：排序器与模型都需要「多长、谁发的、多少人看」。"""
    meta = []
    author = (item.get("author") or "").strip()
    duration = (item.get("duration") or "").strip()
    play = _format_play(item.get("play"))
    if author:
        meta.append(f"UP主 {author}")
    if duration:
        meta.append(f"时长 {duration}")
    if play:
        meta.append(f"播放 {play}")
    desc = (item.get("description") or "").strip()
    parts = meta + ([desc] if desc else [])
    return " · ".join(parts)[:300] or title[:300]


def search(query: str, n: int = 5, min_duration=None, max_duration=None) -> list[dict]:
    """通过 B站搜索 API 搜索。

    min_duration/max_duration 单位是秒；None 表示不限制。带时长条件时多取
    一些候选（档位粗召回后还要后过滤），避免筛完不足 n 条。

    B 站 search API 单页上限实测 20 条（pagesize=50 仍只回 20），所以
    「档位表达不了、只能后过滤」时按页取 3 页再按 bvid 去重，而不是把
    pagesize 调大（调大无效）。
    """
    bucket = _duration_bucket(min_duration, max_duration)
    filtered = min_duration is not None or max_duration is not None
    pages = 3 if filtered and bucket is None else 1
    # 翻页场景取满单页（20 条），否则 3 页只有 3n 条、长视频更容易筛空。
    page_size = 20 if pages > 1 else min(20, max(n, 1))
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com"
    }
    results: list[dict] = []
    seen: set[str] = set()
    last_error: Exception | None = None
    for page in range(1, pages + 1):
        params = {"keyword": query, "page": page, "pagesize": page_size}
        if bucket is not None:
            params["duration"] = bucket
        url = ("https://api.bilibili.com/x/web-interface/search/all/v2?"
               + urllib.parse.urlencode(params))
        try:
            body, _ = _http_get_with_retry(url, headers, timeout=10, max_retries=2)
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            last_error = e
            break
        for item in _parse_bilibili_response(
                data, n, min_duration=min_duration, max_duration=max_duration):
            url_key = item.get("url") or ""
            if url_key in seen:
                continue
            seen.add(url_key)
            results.append(item)
        if len(results) >= n:
            break
    if results:
        return results[:n]
    if last_error is not None:
        # 吞异常返空会让 MCP 社交搜索把网络故障静默报成「平台成功、0 结果」
        # （mcp_handlers 的 err=None 设计依赖引擎抛异常或返 error 占位，
        # 见 tests/test_regression_p0p1.py「零结果应以 errors 提示」契约）。
        # 对齐 zhihu_engine 范式：失败必须带着 error 字段浮上来。
        return [{"error": f"bilibili {type(last_error).__name__}: {last_error}",
                 "source": "bilibili"}]
    return []


def _parse_bilibili_response(data: dict, n: int,
                             min_duration=None, max_duration=None) -> list[dict]:
    """解析 B站 API 响应；时长条件在这里做精确后过滤。"""
    results = []
    data_data = data.get("data", {})
    for result_type in data_data.get("result", []):
        if result_type.get("result_type") != "video":
            continue
        for item in result_type.get("data", []):
            duration_text = item.get("duration", "")
            seconds = parse_duration_seconds(duration_text)
            if seconds is not None:
                if min_duration is not None and seconds < min_duration:
                    continue
                if max_duration is not None and seconds > max_duration:
                    continue
            title = _clean_title(item.get("title", ""))
            play = item.get("play", 0)
            results.append({
                "title": title[:100] + ("..." if len(title) > 100 else ""),
                "url": f"https://www.bilibili.com/video/{item.get('bvid', '')}",
                "snippet": _snippet_of(item, title),
                "source": "bilibili",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "bilibili",
                    "content_type": "video",
                    "author": item.get("author", ""),
                    "duration": duration_text,
                    "duration_seconds": seconds,
                    "play_count": play,
                    "danmu_count": item.get("video_review", 0),
                    "like_count": item.get("like", 0),
                    "favorite_count": item.get("favorites", 0),
                    "coin_count": item.get("coin", 0),
                    "share_count": item.get("share", 0),
                    "published_at": _published_iso(item.get("pubdate")),
                    "bvid": item.get("bvid", ""),
                }
            })
            if len(results) >= n:
                break
        if len(results) >= n:
            break
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Bilibili search engine")
    parser.add_argument("action", nargs="?", default="search")
    parser.add_argument("query", nargs="?")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--min-duration", type=int, default=None,
                        help="最小时长（秒）")
    parser.add_argument("--max-duration", type=int, default=None,
                        help="最大时长（秒）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.query:
        print("[]")
        return

    results = search(args.query, args.n,
                     min_duration=args.min_duration,
                     max_duration=args.max_duration)
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

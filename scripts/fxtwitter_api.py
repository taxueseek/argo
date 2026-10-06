#!/usr/bin/env python3
"""FxTwitter / FxEmbed 原始客户端（零 Key，只读）。

与 `social_engines/twitter_engine.py` 的分工：那个是**搜索引擎**，把推文压成
统一结果 schema 参与 RRF 融合；本模块是**原始接口层**，原样返回上游 JSON 的
各端点（status / thread / conversation / quotes / reposts），供 `argo tweet`
做完整打包用。两者的公共出口纪律一致（都走 net_proxy + url_safety），但形状
不同——搜索要的是「一行结果」，打包要的是「整份内容」，不做归一。

公共实例：api.fxtwitter.com（备镜像 api.fixupx.com）
文档：https://docs.fxembed.com/api/twitter/
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from net_proxy import open_url  # noqa: E402
from url_safety import check_url  # noqa: E402

DEFAULT_BASES = (
    "https://api.fxtwitter.com",
    "https://api.fixupx.com",
)

USER_AGENT = "argo-tweet/1.0 (+https://github.com/taxueseek/argo; FxTwitter read-only)"

_TWEET_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com|fxtwitter\.com|fixupx\.com)"
    r"/(?:i/web/status|[^/\s]+/status)/(\d{1,25})",
    re.I,
)
_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
_STATUS_ID_RE = re.compile(r"^\d{1,25}$")


class FxTwitterError(Exception):
    """上游/网络错误。code 为 HTTP 状态码（有则填），status 保留原始串。"""

    def __init__(self, message: str, *, code: int | None = None,
                 status: str | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def extract_status_id(text: str, *, min_digits: int = 1) -> str | None:
    """从 URL 或纯 ID 里取 status id；取不到返回 None。

    min_digits 是给**搜索**留的护栏：`argo search "20"` 里的 20 可能是页码或
    数量，不该当推文 ID（引擎侧传 10）；而 `argo tweet "20"` 是用户显式给的
    ID，必须认（默认 1）。两侧共用同一个解析器，只差这一道阈值——不再各写
    一套 URL 正则（那样两处必然漂移）。
    """
    if not text:
        return None
    m = _TWEET_URL_RE.search(text.strip())
    if m:
        return m.group(1)
    s = text.strip()
    if _STATUS_ID_RE.match(s) and len(s) >= min_digits:
        return s
    return None


def extract_handle(text: str) -> str | None:
    """从主页 URL 或 @handle 取用户名（排除 i/home/search 等保留路径）。"""
    s = (text or "").strip()
    m = re.search(
        r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})/?$",
        s, re.I)
    if m and m.group(1).lower() not in {
            "i", "home", "search", "explore", "settings"}:
        return m.group(1)
    m = _HANDLE_RE.match(s)
    return m.group(1) if m else None


def http_get(url: str, *, headers: dict[str, str] | None = None,
             timeout: float = 15,
             max_retries: int = 2) -> tuple[bytes, int]:
    """GET 一个 URL，带 429/网络重试。发请求前过 SSRF 守卫。

    FxTwitter 侧**唯一**的 HTTP 出口：搜索引擎（twitter_engine）与打包器
    （tweet.py）都走这里，不再各写一份重试与代理处理。
    """
    ok, reason = check_url(url)
    if not ok:
        raise FxTwitterError(f"URL 被安全策略拒绝: {reason}")
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with open_url(req, timeout=timeout) as resp:
                return resp.read(), int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 and attempt < max_retries:
                try:
                    wait = int(e.headers.get("Retry-After", "5"))
                except (TypeError, ValueError):
                    wait = 5
                time.sleep(min(wait, 30))
                continue
            body = e.read() if hasattr(e, "read") else b""
            raise FxTwitterError(f"HTTP {e.code}: {body[:200]!r}",
                                 code=e.code, status=str(e.code)) from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(2 ** attempt + 0.3)
                continue
            raise FxTwitterError(f"network: {e}") from e
    raise FxTwitterError(f"failed after retries: {last_err}")


def _get_json(path: str, *, bases: tuple[str, ...] = DEFAULT_BASES,
              timeout: float = 15) -> dict[str, Any]:
    """按镜像依次尝试，返回首个成功响应。

    404/401 视为**确定答案**（帖不存在/私密），立刻抛出、不再试镜像；其余
    错误累积后一起抛——「全部端点异常」与「这条不存在」是两回事，不能混。
    """
    if not path.startswith("/"):
        path = "/" + path
    errors: list[str] = []
    for base in bases:
        url = base.rstrip("/") + path
        try:
            body, _ = http_get(url, timeout=timeout)
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict):
                errors.append(f"{base}: non-object json")
                continue
            code = data.get("code", 200)
            if code not in (200, None, "200"):
                if str(code).isdigit() and int(code) in (401, 404):
                    raise FxTwitterError(str(data.get("message") or code),
                                         code=int(code))
                errors.append(f"{base}: code={code} {data.get('message') or ''}")
                continue
            return data
        except FxTwitterError as e:
            if e.code in (401, 404):
                raise
            errors.append(f"{base}: {e}")
            continue
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            errors.append(f"{base}: bad json {e}")
            continue
    raise FxTwitterError("; ".join(errors) or "all bases failed")


def get_status(status_id: str, *, timeout: float = 15) -> dict[str, Any]:
    """单条。返回 {raw, status, author, thread?}——thread 是响应里带的串。"""
    data = _get_json(f"/2/status/{status_id}", timeout=timeout)
    status = data.get("status") or data.get("tweet") or data
    if not isinstance(status, dict):
        raise FxTwitterError("missing status object")
    return {"raw": data, "status": status,
            "author": data.get("author") or status.get("author"),
            "thread": data.get("thread")}


def get_thread(status_id: str, *, timeout: float = 15) -> dict[str, Any]:
    """作者连发的串。"""
    data = _get_json(f"/2/thread/{status_id}", timeout=timeout)
    thread = data.get("thread")
    return {"raw": data,
            "status": data.get("status") if isinstance(data.get("status"), dict) else {},
            "thread": thread if isinstance(thread, list) else [],
            "author": data.get("author")}


def get_conversation(status_id: str, *, timeout: float = 15) -> dict[str, Any]:
    """对话：主帖 / 串 / 回复分段。"""
    data = _get_json(f"/2/conversation/{status_id}", timeout=timeout)
    return {"raw": data,
            "status": data.get("status") if isinstance(data.get("status"), dict) else {},
            "thread": data.get("thread") if isinstance(data.get("thread"), list) else [],
            "replies": data.get("replies") if isinstance(data.get("replies"), list) else [],
            "author": data.get("author"),
            "cursor": data.get("cursor")}


def _list_page(path: str, *, cursor: str | None = None,
               timeout: float = 15) -> dict[str, Any]:
    """通用 list 端点。上游对空列表常回 404 + results=[]，按空成功处理。"""
    if cursor:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}cursor={urllib.parse.quote(cursor)}"
    try:
        data = _get_json(path, timeout=timeout)
    except FxTwitterError as e:
        if e.code == 404:
            return {"raw": {"code": 404, "results": [], "message": str(e)},
                    "results": [], "cursor": {}, "code": 404}
        raise
    results = data.get("results")
    if not isinstance(results, list):
        tweets = data.get("tweets")
        results = tweets if isinstance(tweets, list) else []
    return {"raw": data, "results": results,
            "cursor": data.get("cursor") if isinstance(data.get("cursor"), dict) else {},
            "code": data.get("code", 200)}


def _paginate(path: str, *, limit: int = 20, timeout: float = 15,
              max_pages: int = 5) -> dict[str, Any]:
    """按 cursor.bottom 翻页直到够 limit 或无更多（含死循环防护）。"""
    limit = max(1, min(int(limit), 100))
    all_results: list[Any] = []
    pages_raw: list[dict[str, Any]] = []
    cursor_bottom: str | None = None
    for _ in range(max_pages):
        page = _list_page(path, cursor=cursor_bottom, timeout=timeout)
        pages_raw.append(page["raw"])
        batch = page["results"] or []
        all_results.extend(batch)
        if len(all_results) >= limit:
            break
        cur = page.get("cursor") or {}
        nxt = cur.get("bottom") if isinstance(cur, dict) else None
        # 无下一页 / 空页 / cursor 原地踏步 → 停（否则上游回同一个 cursor 会死转）
        if not nxt or not batch or str(nxt) == cursor_bottom:
            break
        cursor_bottom = str(nxt)
    return {"results": all_results[:limit], "raw_pages": pages_raw,
            "raw": pages_raw[0] if len(pages_raw) == 1
            else {"pages": pages_raw, "code": 200},
            "cursor": (pages_raw[-1].get("cursor") if pages_raw else {})}


def get_quotes(status_id: str, *, limit: int = 20,
               timeout: float = 15) -> dict[str, Any]:
    """引用该帖的 status 列表（是推文，不是用户）。"""
    page = _paginate(f"/2/status/{status_id}/quotes", limit=limit, timeout=timeout)
    return {"raw": page["raw"],
            "results": [r for r in page["results"] if isinstance(r, dict)],
            "cursor": page.get("cursor"), "kind": "quotes"}


def get_reposts(status_id: str, *, limit: int = 20,
                timeout: float = 15) -> dict[str, Any]:
    """转发该帖的用户 profile 列表（是用户，不是推文）。"""
    page = _paginate(f"/2/status/{status_id}/reposts", limit=limit, timeout=timeout)
    return {"raw": page["raw"],
            "results": [r for r in page["results"] if isinstance(r, dict)],
            "cursor": page.get("cursor"), "kind": "reposts"}


def get_profile(handle: str, *, timeout: float = 15) -> dict[str, Any]:
    handle = handle.lstrip("@")
    data = _get_json(f"/2/profile/{urllib.parse.quote(handle)}", timeout=timeout)
    return {"raw": data, "user": data.get("user") or data.get("profile") or data}


def pick_video_url(video: dict[str, Any],
                   quality: str = "max") -> tuple[str | None, dict[str, Any] | None]:
    """从 video 对象选一个可下载 URL（优先 mp4，跳过 m3u8）。quality: max|min|720。"""
    formats = video.get("formats") if isinstance(video.get("formats"), list) else []
    mp4s: list[dict[str, Any]] = []
    for f in formats:
        if not isinstance(f, dict):
            continue
        url = f.get("url") or ""
        if not url:
            continue
        container = (f.get("container") or "").lower()
        if container == "mp4" or url.endswith(".mp4") or "vid/avc1" in url:
            mp4s.append(f)

    def bitrate(f: dict) -> int:
        try:
            return int(f.get("bitrate") or 0)
        except (TypeError, ValueError):
            return 0

    if mp4s:
        if quality == "min":
            chosen = min(mp4s, key=bitrate)
        elif quality == "720":
            ordered = sorted(mp4s, key=bitrate)
            chosen = ordered[len(ordered) // 2]
        else:
            chosen = max(mp4s, key=bitrate)
        return chosen.get("url"), chosen
    top = video.get("url")
    if top and ".m3u8" not in str(top):
        return str(top), {"url": top, "container": "mp4"}
    return None, None


def iter_photos(status: dict[str, Any]) -> list[dict[str, Any]]:
    """推文里的图片（media.photos 优先，否则从 media.all 里筛）。"""
    media = status.get("media") if isinstance(status.get("media"), dict) else {}
    photos = media.get("photos") or []
    if photos:
        return [p for p in photos if isinstance(p, dict)]
    out = []
    for item in media.get("all") or []:
        if not isinstance(item, dict):
            continue
        if item.get("duration") or item.get("formats"):
            continue  # 视频/gif 也躺在 all 里
        if item.get("url") and "video" not in (item.get("type") or ""):
            out.append(item)
    return out


def iter_videos(status: dict[str, Any]) -> list[dict[str, Any]]:
    """推文里的视频/gif。"""
    media = status.get("media") if isinstance(status.get("media"), dict) else {}
    videos = media.get("videos") or []
    if videos:
        return [v for v in videos if isinstance(v, dict)]
    out = []
    for item in media.get("all") or []:
        if isinstance(item, dict) and (
                item.get("type") in ("video", "gif")
                or item.get("formats") or item.get("duration")):
            out.append(item)
    return out


def status_text(status: dict[str, Any]) -> str:
    return (status.get("text") or "").strip()

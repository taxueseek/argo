#!/usr/bin/env python3
"""video_fetch.py — 视频页正文/元数据提取（B 站优先，YouTube 次之）。

为什么单独成模块：通用 fetch_v3 的降级链是「HTML → 正文提取」，而视频页的
正文不在 HTML 里。实测 B 站视频页只抓到 225 字相关推荐标题，YouTube 只有
134 字页脚——能搜到视频，但读不了，evidence/verify 拿到的是垃圾。

本模块走平台 API，不抓播放器页面：
  - B 站：view 接口取标题/简介/时长/UP 主/统计/cid；player/v2 取字幕
    （有字幕才有，没有则留空）；可选 ARGO_VIDEO_ASR_CMD 兜底转写。
  - YouTube：oEmbed 取标题/作者；timedtext 取字幕（多数视频为空）。
  - 抖音等其它平台不接管：返回 None，让通用抓取链继续（移动 UA 对
    share/video 页仍可能有效，不能在这里判死）。

返回 dict 与 fetch_v3._make_result 的字段对齐；调用方（fetch_v3）再跑一次
质量评估并覆盖 page_type=video。不支持的平台返回 None，让通用抓取链继续。
"""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from net_proxy import open_url
from video_meta import parse_duration_seconds, video_platform

_TZ_CN = timezone(timedelta(hours=8))
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with open_url(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(url: str, timeout: float) -> dict:
    return json.loads(_get(url, timeout).decode("utf-8", "replace"))


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=_TZ_CN).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _bvid_of(url: str) -> str:
    m = re.search(r"(BV[0-9A-Za-z]+)", url or "")
    return m.group(1) if m else ""


def _youtube_id(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return ""
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0]
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("v"):
        return query["v"][0]
    m = re.search(r"/(?:shorts|embed|live)/([0-9A-Za-z_-]{6,})", parsed.path or "")
    return m.group(1) if m else ""


def _limit(text: str, max_chars: int) -> tuple[str, bool, int]:
    full = text or ""
    if max_chars and max_chars > 0 and len(full) > max_chars:
        return full[:max_chars], True, len(full)
    return full, False, len(full)


def _result(url: str, title: str, content: str, method: str, *,
            max_chars: int = 8000, success: bool = True,
            error: str | None = None, content_ok: bool | None = None,
            video_meta: dict | None = None) -> dict:
    delivered, truncated, full_length = _limit(content, max_chars)
    if content_ok is None:
        content_ok = len(delivered.strip()) >= 40
    return {
        "url": url,
        "title": title or "",
        "content": delivered,
        "html": "",
        "length": len(delivered),
        "full_length": full_length,
        "truncated": truncated,
        "success": success,
        "error": error,
        "fetch_method": method,
        "content_ok": content_ok,
        "page_type": "video",
        "source_type": "video",
        "is_official": False,
        "video_meta": video_meta or {},
    }


def _bilibili_subtitle(bvid: str, cid, timeout: float) -> str:
    """取 B 站字幕文本；没有字幕（或接口要求登录）返回空串，不报错。"""
    if not bvid or not cid:
        return ""
    try:
        player = _get_json(
            "https://api.bilibili.com/x/player/v2?"
            + urllib.parse.urlencode({"bvid": bvid, "cid": cid}),
            timeout,
        )
    except Exception:
        return ""
    subtitles = ((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    for item in subtitles:
        sub_url = item.get("subtitle_url") or ""
        if not sub_url:
            continue
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        try:
            body = _get_json(sub_url, timeout)
        except Exception:
            continue
        lines = [str(x.get("content") or "").strip()
                 for x in (body.get("body") or []) if isinstance(x, dict)]
        text = "\n".join(x for x in lines if x)
        if text:
            return text
    return ""


def _run_asr(url: str, timeout: float) -> str:
    """可选 ASR 兜底：ARGO_VIDEO_ASR_CMD 是外部命令模板（含 {url} 或自动追加）。

    不内置 whisper/yt-dlp：两者都不是本仓依赖，装不装由使用者决定。命令用
    shlex.split 执行、不走 shell，避免 URL 里的特殊字符被解释。
    """
    template = os.environ.get("ARGO_VIDEO_ASR_CMD", "").strip()
    if not template:
        return ""
    parts = shlex.split(template)
    if any("{url}" in p for p in parts):
        parts = [p.replace("{url}", url) for p in parts]
    else:
        parts.append(url)
    try:
        proc = subprocess.run(
            parts, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            # 抓取超时通常是 8s，ASR 转一条几分钟的视频不够；转写是显式
            # opt-in，给到 30s 下限，超时由外部命令自己负责长视频队列。
            timeout=max(timeout, 30.0))
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _fetch_bilibili(url: str, max_chars: int, timeout: float) -> dict | None:
    bvid = _bvid_of(url)
    if not bvid:
        return None
    try:
        payload = _get_json(
            "https://api.bilibili.com/x/web-interface/view?"
            + urllib.parse.urlencode({"bvid": bvid}),
            timeout,
        )
    except Exception as e:
        return _result(url, "", "", "bilibili_api", max_chars=max_chars,
                       success=False, error=f"B站详情接口失败: {type(e).__name__}: {e}")
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        return _result(url, "", "", "bilibili_api", max_chars=max_chars,
                       success=False,
                       error=f"B站详情接口返回 code={payload.get('code')}")
    data = payload["data"]
    pages = data.get("pages") or []
    cid = (pages[0] or {}).get("cid") if pages else None
    transcript = _bilibili_subtitle(bvid, cid, timeout)
    if not transcript:
        transcript = _run_asr(url, timeout)

    title = html.unescape(str(data.get("title") or ""))
    desc = html.unescape(str(data.get("desc") or "")).strip()
    duration = parse_duration_seconds(data.get("duration"))
    owner = (data.get("owner") or {}).get("name") or ""
    stat = data.get("stat") or {}
    published = _iso(data.get("pubdate"))
    parts = [f"标题：{title}"]
    if owner:
        parts.append(f"UP主：{owner}")
    if duration is not None:
        minutes, seconds = divmod(duration, 60)
        parts.append(f"时长：{minutes}:{seconds:02d}")
    if published:
        parts.append(f"发布：{published}")
    if stat.get("view") is not None:
        parts.append(f"播放：{stat.get('view')}")
    if desc:
        parts.append("简介：\n" + desc)
    if transcript:
        parts.append("字幕/转写：\n" + transcript)
    else:
        parts.append("（该视频没有可用字幕；只提取到标题、简介与元数据，"
                     "需要正文请配置 ARGO_VIDEO_ASR_CMD）")
    content = "\n\n".join(parts)
    return _result(
        url, title, content, "bilibili_api", max_chars=max_chars,
        content_ok=bool(desc or transcript),
        video_meta={
            "platform": "bilibili",
            "bvid": bvid,
            "duration_seconds": duration,
            "author": owner,
            "published_at": published,
            "view_count": stat.get("view"),
            "danmaku_count": stat.get("danmaku"),
            "like_count": stat.get("like"),
            "favorite_count": stat.get("favorite"),
            "transcript_available": bool(transcript),
        },
    )


def _youtube_captions(video_id: str, timeout: float) -> str:
    """取 YouTube 字幕；无字幕/被墙返回空串。"""
    if not video_id:
        return ""
    for lang in ("zh-Hans", "zh-CN", "en"):
        url = ("https://www.youtube.com/api/timedtext?"
               + urllib.parse.urlencode({"v": video_id, "lang": lang}))
        try:
            raw = _get(url, timeout).decode("utf-8", "replace")
        except Exception:
            continue
        if "<text" not in raw:
            continue
        lines = [html.unescape(re.sub(r"<[^>]+>", "", x)).strip()
                 for x in re.findall(r"<text[^>]*>(.*?)</text>", raw, re.S)]
        text = "\n".join(x for x in lines if x)
        if text:
            return text
    return ""


def _fetch_youtube(url: str, max_chars: int, timeout: float) -> dict | None:
    video_id = _youtube_id(url)
    if not video_id:
        return None
    try:
        oembed = _get_json(
            "https://www.youtube.com/oembed?"
            + urllib.parse.urlencode({"url": url, "format": "json"}),
            timeout,
        )
    except Exception as e:
        return _result(url, "", "", "youtube_oembed", max_chars=max_chars,
                       success=False,
                       error=f"YouTube oEmbed 失败: {type(e).__name__}: {e}")
    title = str(oembed.get("title") or "")
    author = str(oembed.get("author_name") or "")
    transcript = _youtube_captions(video_id, timeout)
    parts = [f"标题：{title}"]
    if author:
        parts.append(f"频道：{author}")
    if transcript:
        parts.append("字幕/转写：\n" + transcript)
    else:
        parts.append("（YouTube 未返回可用字幕；当前网络下播放页要求登录，"
                     "只提取到标题与频道）")
    content = "\n\n".join(parts)
    return _result(
        url, title, content, "youtube_oembed", max_chars=max_chars,
        content_ok=bool(transcript),
        video_meta={
            "platform": "youtube",
            "video_id": video_id,
            "author": author,
            "transcript_available": bool(transcript),
        },
    )


def fetch_video(url: str, max_chars: int = 8000,
                timeout: float = 8.0) -> dict | None:
    """视频页提取入口。

    只接管「API 一定比 HTML 好」的两个平台：B 站与 YouTube。抖音等平台
    返回 None，让通用抓取链继续——实测抖音 share/video 页在移动 UA 下仍能
    提取，直接判「不支持」会打断既有降级链（而且会把可读页面说成不可读）。
    """
    platform = video_platform(url)
    if platform == "bilibili":
        return _fetch_bilibili(url, max_chars, timeout)
    if platform == "youtube":
        return _fetch_youtube(url, max_chars, timeout)
    return None

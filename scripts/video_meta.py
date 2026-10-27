#!/usr/bin/env python3
"""video_meta.py — 视频来源识别与时长归一（零依赖纯函数）。

搜索链路里「视频」此前只体现为平台名：B 站引擎抓到了时长/播放量，但
CLI 解析层不透传 social_meta，envelope 又把所有带 social_meta 的结果写成
social_post。本模块只做三件可单独测试的事：

  - URL → 平台/是否视频页（区分 B 站 /video/ 与 /read/ 图文）；
  - 各种时长写法 → 秒（B 站 "8:27"、API 数字、文本 "5分钟"）；
  - 结果集 → 视频过滤/时长过滤（供 --video 与 --min-duration 使用）。

判据刻意按「页面是不是视频」而不是「域名是不是视频站」：B 站专栏与动态
（/read/、/opus/）不是视频，抖音用户主页也不是单条视频。按域名一刀切会
把图文当视频放进来，--video 的结果就不再可信。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


# 域名 → 平台名。匹配按后缀（www./m. 等子域自动命中），路径规则在
# video_platform 里逐平台收紧。
_VIDEO_HOSTS: dict[str, str] = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "bilibili.com": "bilibili",
    "b23.tv": "bilibili",
    "douyin.com": "douyin",
    "iesdouyin.com": "douyin",
    "kuaishou.com": "kuaishou",
    "vimeo.com": "vimeo",
    "v.qq.com": "tencent_video",
    "youku.com": "youku",
    "iqiyi.com": "iqiyi",
    "ixigua.com": "xigua",
    "xigua.com": "xigua",
    "ted.com": "ted",
}

# envelope 里算「视频」的平台名（与 _platform_of 的返回值对齐）。
VIDEO_PLATFORMS = frozenset({
    "youtube", "bilibili", "douyin", "kuaishou", "vimeo",
    "tencent_video", "youku", "iqiyi", "xigua", "ted",
})


def _host_of(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").lower().split("@")[-1]
    except (ValueError, AttributeError):
        return ""
    # 去端口（IPv6 不在视频站范围，按常见 host:port 处理即可）
    return host.split(":")[0]


def video_platform(url: str) -> str | None:
    """URL → 视频平台名；不是单条视频页返回 None。

    路径判据比域名严格，原因见模块头：B 站 /read/ 与抖音用户主页不是视频。
    """
    if not url:
        return None
    host = _host_of(url)
    if not host:
        return None
    platform = None
    for domain, name in _VIDEO_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            platform = name
            break
    if platform is None:
        return None

    try:
        path = urlparse(url).path or ""
    except (ValueError, AttributeError):
        path = ""

    if platform == "bilibili":
        # 短链 b23.tv 任何路径都视为视频；主站只认 /video/ 与 /bangumi/
        if host == "b23.tv" or host.endswith(".b23.tv"):
            return platform
        if path.startswith("/video/") or path.startswith("/bangumi/"):
            return platform
        return None
    if platform == "youtube":
        if host == "youtu.be" or host.endswith(".youtu.be"):
            return platform
        if any(path.startswith(p) for p in
               ("/watch", "/shorts/", "/embed/", "/live/")):
            return platform
        return None
    if platform == "douyin":
        if host.startswith("v.") or host.startswith("v-"):
            return platform
        return platform if "/video/" in path else None
    if platform == "kuaishou":
        return platform if ("/short-video/" in path or "/fw/photo/" in path) else None
    if platform == "vimeo":
        return platform if re.search(r"/\d+", path) else None
    if platform == "tencent_video":
        return platform if ("/x/page/" in path or "/x/cover/" in path) else None
    if platform == "youku":
        return platform if "/v_show/" in path else None
    if platform == "iqiyi":
        return platform if "/v_" in path else None
    if platform == "xigua":
        return platform if "/video/" in path else None
    if platform == "ted":
        return platform if "/talks/" in path else None
    return platform


def is_video_url(url: str) -> bool:
    """是否单条视频页（视频平台 + 视频路径）。"""
    return video_platform(url) is not None


def parse_duration_seconds(value: Any) -> int | None:
    """把视频时长归一成秒。

    支持：整数/浮点秒、"8:27"、"1:02:03"、"5分钟"、"90秒"、"1小时2分"。
    解析不了返回 None（而不是 0）：0 会被下游当成「零时长视频」误过滤。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    text = str(value).strip()
    if not text:
        return None

    # 纯数字秒
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return int(float(text))

    # mm:ss / hh:mm:ss（B 站搜索结果里 "1:0" 表示 1 分 0 秒）
    if re.fullmatch(r"\d+(?::\d+){1,2}", text):
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
        return total

    # 中文时长（"5分钟"、"1小时2分"、"90秒"）
    m = re.search(
        r"(?:(\d+)\s*(?:小时|时))?\s*(?:(\d+)\s*(?:分钟|分))?\s*(?:(\d+)\s*秒)?",
        text,
    )
    if m and any(m.groups()):
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        seconds = int(m.group(3) or 0)
        return hours * 3600 + minutes * 60 + seconds
    return None


def result_duration_seconds(item: dict[str, Any]) -> int | None:
    """从一条搜索结果里取时长（social_meta 优先，其次顶层与 metrics）。"""
    if not isinstance(item, dict):
        return None
    for container in (item.get("social_meta"), item, item.get("metrics")):
        if not isinstance(container, dict):
            continue
        for key in ("duration_seconds", "duration"):
            seconds = parse_duration_seconds(container.get(key))
            if seconds is not None:
                return seconds
    return None


def is_video_result(item: dict[str, Any]) -> bool:
    """一条结果是否是视频（引擎声明的 content_type 优先，其次 URL 判据）。"""
    if not isinstance(item, dict):
        return False
    social = item.get("social_meta")
    if isinstance(social, dict) and social.get("content_type") == "video":
        return True
    return is_video_url(item.get("url") or "")


def filter_video_results(
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """只保留视频结果，返回 (保留, 剔除数)。"""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in results:
        if is_video_result(item):
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


def filter_duration(
    results: list[dict[str, Any]],
    min_seconds: int | None = None,
    max_seconds: int | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """按时长过滤，返回 (保留, 剔除数, 时长未知且保留数)。

    时长未知的条目保留：除 B 站外的引擎大多没有时长字段，硬删会把
    anysearch 的 YouTube 结果全部清空。调用方据第三个数判断这次过滤的
    实际覆盖率，不要把它当成「已严格过滤」。
    """
    kept: list[dict[str, Any]] = []
    filtered = 0
    unknown = 0
    for item in results:
        seconds = result_duration_seconds(item)
        if seconds is None:
            unknown += 1
            kept.append(item)
            continue
        if min_seconds is not None and seconds < min_seconds:
            filtered += 1
            continue
        if max_seconds is not None and seconds > max_seconds:
            filtered += 1
            continue
        kept.append(item)
    return kept, filtered, unknown

#!/usr/bin/env python3
"""time_utils.py — 时间窗归一化与 published_at 解析的纯函数工具箱。

从 search.py 中外提，解决 3351 行问题。本模块只含纯函数与编译好的正则，
零重依赖（仅 datetime + re），可被路由/搜索/抓取/研究等层安全引用而不必
担心循环导入或副作用。

本模块是「时间语义」的唯一来源：
  - published_at 解析 → published_ts
  - 时间窗归一化 → parse_time_value / normalize_time_window
  - 结果后过滤 → apply_time_window
  - 排序 → sort_results_by_time
  - 哪些引擎带时间能力 → TIME_CAPABLE_ENGINES / is_time_capable
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

# published_at 常见形态：YYYY-MM-DD、YYYY-MM-DD HH:MM[:SS]、ISO(YYYY-MM-DDTHH:MM:SS)
_DATE_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)
# 相对时间：Nd / Nh / Nw（不区分大小写）
_REL_TIME_RE = re.compile(r"^(\d+)\s*([dhw])$", re.IGNORECASE)
# 纯日期 YYYY-MM-DD（until 边界含当天；下推保留日期形态）
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")


# 带发布时间能力（published_at）的引擎集合。时间窗的缓存键隔离与后过滤
# 只对含这些引擎的组合生效：不带时间字段的引擎会忽略时间窗、结果相同，
# 隔离缓存键只会白白降低命中率（7d/30d 查同一引擎本可共享缓存）。
# local_search 聚合内部可能选中 news 类子引擎（带日期），保守纳入。
TIME_CAPABLE_ENGINES: frozenset[str] = frozenset({
    "realtime_index", "wayback_cdx", "local_search",
    "local_bing_news", "local_google_news", "local_duckduckgo_news",
    "local_ddgs_news",
    # parallel（after_date 下推）/ you（freshness 动态化 + page_age）
    "parallel", "you",
})


def is_time_capable(eng: str) -> bool:
    """引擎是否可能返回 published_at（决定时间窗是否参与缓存键/后过滤）。"""
    return eng in TIME_CAPABLE_ENGINES


def published_ts(r: dict[str, Any]) -> float | None:
    """解析结果的 published_at → epoch 秒；无法解析返回 None（恒排最后）。

    ISO 优先（fromisoformat 支持 T/空格分隔、Z、±HH:MM 时区），
    带时区正确换算 epoch，无时区按本地时区解释；回退 YYYY-MM-DD 手工解析。
    """
    raw = r.get("published_at")
    if not raw:
        return None
    text = str(raw).strip()
    if text.isdigit():  # 部分引擎给 epoch 秒时间戳
        try:
            return float(text)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        # aware datetime 正确换算 UTC epoch；naive 按本地时区解释
        return dt.timestamp()
    except ValueError:
        pass
    # basic ISO（YYYYMMDDTHHMMSSZ，如 GDELT seendate）：Python 3.10 的
    # fromisoformat 不认，插入分隔符归一化成扩展格式再走同一解析
    if len(text) >= 15 and text[:8].isdigit() and text[8] == "T" and text[9:15].isdigit():
        try:
            dt = datetime.fromisoformat(
                f"{text[:4]}-{text[4:6]}-{text[6:8]}"
                f"T{text[9:11]}:{text[11:13]}:{text[13:15]}+00:00")
            return dt.timestamp()
        except ValueError:
            pass
    m = _DATE_RE.match(text)
    if not m:
        return None
    try:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
        ).timestamp()
    except ValueError:
        return None


def parse_time_value(value: Any) -> tuple[str | None, float | None]:
    """解析单边时间窗 → (归一化 ISO 字符串, epoch 秒)。

    支持：相对量（Nd/Nh/Nw）、YYYY-MM-DD、YYYY-MM-DD HH:MM[:SS]、
    ISO 8601（含 Z / ±HH:MM）、纯数字 epoch 秒。
    相对量归一化为绝对日期（YYYY-MM-DD），语义确定、可入缓存键；
    无法解析返回 (None, None)，调用方保持原样下推、不参与后过滤。
    """
    if value in (None, ""):
        return None, None
    text = str(value).strip()
    # epoch 秒
    if text.isdigit():
        try:
            ts = float(text)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.isoformat(timespec="seconds"), ts
        except (ValueError, OSError):
            return None, None
    # 相对量：Nd / Nh / Nw → 绝对日期（本地时区零点）
    m = _REL_TIME_RE.match(text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        now = datetime.now()
        if unit == "h":
            dt = now - timedelta(hours=amount)
        elif unit == "w":
            dt = now - timedelta(weeks=amount)
        else:
            dt = now - timedelta(days=amount)
        d = dt.date()
        return d.isoformat(), datetime(d.year, d.month, d.day).timestamp()
    # 绝对时间：fromisoformat 优先（T/空格分隔、Z、±HH:MM）
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if dt.tzinfo is not None:
        return dt.isoformat(timespec="seconds"), dt.timestamp()
    # 无时区：按本地时区（与 published_ts 无时区行为一致）；
    # 纯日期（时间为零点）保留 YYYY-MM-DD 形态下推，兼容引擎既有解析
    if (dt.hour, dt.minute, dt.second, dt.microsecond) == (0, 0, 0, 0):
        return dt.date().isoformat(), dt.timestamp()
    return dt.isoformat(timespec="seconds"), dt.timestamp()


#: 时间窗接受的写法（给用户看的唯一一份说明；CLI 的 argparse 与 MCP 的错误行
#: 都从这里取，避免「文档说支持 2w、代码其实只认 d」这类漂移）。
WINDOW_FORMATS_HINT = (
    "支持 7d/24h/2w、2026-09-01、2026-09-01 12:00、"
    "ISO 8601（含 Z / ±HH:MM）或 epoch 秒")


def is_valid_time_window(value: Any) -> bool:
    """入参能否解析成时间窗（判据与 `parse_time_value` 同源，不另写一套正则）。

    why：此前非法值被**静默接受**——`--since garbage` 原样回显在响应里、
    `time_filtered=0`、没有任何说明，调用方以为自己拿到了筛过的结果。
    这是「静默不生效」里最坏的一种：不是少了一层增强，而是**答案被当成
    已经筛过的**（同一条查询用 `--since 2026-09-01` 与 `--since garbage`
    拿到同一批结果，且无从分辨）。
    """
    if value in (None, ""):
        return True  # 没给就是没给，不是坏值
    _iso, ts = parse_time_value(value)
    return ts is not None


def normalize_time_window(
    since: str | None, until: str | None
) -> tuple[str | None, str | None, float | None, float | None]:
    """归一化时间窗 → (since_iso, until_iso, since_ts, until_ts)。

    下推与缓存键使用归一化 ISO（相对值转绝对日期，消除 7d 与绝对日期的
    缓存碎片）；后过滤使用 epoch 秒。非法输入 iso 保留原始字符串、
    ts 为 None：仍会下推原值、缓存键仍区分，但不参与后过滤，不阻断搜索。
    """
    s_iso, s_ts = parse_time_value(since)
    u_iso, u_ts = parse_time_value(until)
    # 纯日期 until 语义为「含当天」：边界取当天最后一刻，
    # 使次日零点及之后的结果被剔除、当天 23:59:59 保留
    if u_iso and _DATE_ONLY_RE.fullmatch(u_iso):
        try:
            d = datetime.fromisoformat(u_iso).date()
            u_ts = datetime(d.year, d.month, d.day, 23, 59, 59, 999999).timestamp()
        except ValueError:
            pass
    s_raw = str(since).strip() if since not in (None, "") else None
    u_raw = str(until).strip() if until not in (None, "") else None
    return (s_iso or s_raw, u_iso or u_raw, s_ts, u_ts)


def apply_time_window(
    results: list[dict[str, Any]], since_ts: float | None, until_ts: float | None
) -> tuple[list[dict[str, Any]], int]:
    """结果后过滤保底：剔除「有 published_at 且明确超窗」的条目。

    宽松策略：无时间字段的结果无法判断、予以保留（避免大多数引擎清空）；
    只有时间明确落在窗口外的才剔除。返回 (保留列表, 剔除数) 供 envelope 上报。
    """
    if since_ts is None and until_ts is None:
        return results, 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    for r in results:
        ts = published_ts(r)
        if ts is not None:
            if since_ts is not None and ts < since_ts:
                dropped += 1
                continue
            if until_ts is not None and ts > until_ts:
                dropped += 1
                continue
        kept.append(r)
    return kept, dropped


def sort_results_by_time(results: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """按时间重排结果集：oldest 升序 / newest 降序 / relevance 原序。

    排序是纯本地展示顺序：不改变结果集、不进入缓存键、不影响缓存内容；
    无日期条目恒排最后；同时间保持原相对顺序（稳定排序，结果可复现）。
    """
    if sort not in ("oldest", "newest"):
        return results
    if len(results) <= 1:
        return results

    def _key(r: dict[str, Any]) -> tuple[int, float]:
        ts = published_ts(r)
        if ts is None:
            return (1, 0.0)  # 无日期恒排最后
        return (0, ts if sort == "oldest" else -ts)

    return sorted(results, key=_key)

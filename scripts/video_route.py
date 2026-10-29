#!/usr/bin/env python3
"""video_route.py — `--video` 的强制路由决策。

为什么单独成模块：route.py 已登记模块体积上限，而这段逻辑只服务一个显式
开关，不需要挤进主路由。决策形状必须与 route_query 的返回一致（prepare
直接读 engines_combo / parallel / engine_request），否则 --video 会在
执行层退化成空 combo。

与自然语言路由的分工：
  - 查询里出现「视频教程 / B站 / 视频(结尾)」等显式意图 → config 的 video
    域自然命中，不需要本模块；
  - 用户敲 `--video` → 不看查询词，直接锁定视频 combo，并由 pipeline 的
    video_only 过滤器保证结果里没有网页。
"""

from __future__ import annotations

from typing import Any

# config 缺 video 域时的兜底：B 站负责中文视频，parallel_free 负责海外/YouTube
# 视频（免 Key 直连），anysearch 补充国内综合索引。
_FALLBACK = ("bilibili", "parallel_free", "anysearch")


def _video_domain() -> dict[str, Any]:
    try:
        from config import get_domains
        for domain in get_domains() or []:
            if isinstance(domain, dict) and domain.get("name") == "video":
                return domain
    except Exception:
        pass
    return {}


def video_decision(min_duration: int | None = None,
                   max_duration: int | None = None) -> dict[str, Any]:
    """构造 `--video` 的路由决策（形状对齐 route_query 的返回值）。"""
    domain = _video_domain()
    combo = [str(e) for e in (domain.get("engines_combo") or _FALLBACK) if e]
    try:
        from config import get_engines
        specs = get_engines() or {}
        combo = [e for e in combo if (specs.get(e) or {}).get("enabled", True)]
    except Exception:
        pass
    combo = combo or list(_FALLBACK)
    primary = domain.get("primary")
    if primary not in combo:
        primary = combo[0]
    return {
        "engine": primary,
        "engines": list(combo),
        "engines_combo": list(combo),
        "domain": "video",
        "parallel": True,
        "no_early_stop": True,
        "reason": "用户指定 --video：视频源优先（B站 + anysearch），只保留视频结果",
        "confidence": 1.0,
        # 请求身份进缓存键：--video 的结果不能与同 query 的 auto 结果互串。
        "engine_request": "--video",
        "video_only": True,
        "min_duration": min_duration,
        "max_duration": max_duration,
    }

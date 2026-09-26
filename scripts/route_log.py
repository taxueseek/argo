#!/usr/bin/env python3
"""route_log.py — 路由决策采样落日志（旁路，失败静默，仅本机）。

按采样率把「features 齐全」的决策点落一条遥测记录（P2-6）。单独成模块的理由：
它是路由模块**唯一**的写副作用（除决策缓存外），且必须永远 fail-open——
遥测写失败绝不能让一次搜索失败。把它隔离出来，route_query 里就只剩编排。

`ARGO_ROUTE_SAMPLE_RATE` 控制采样率（默认 1/20）。
"""

from __future__ import annotations

import os
from typing import Any

_ROUTE_SAMPLE_RATE = max(1, int(os.environ.get("ARGO_ROUTE_SAMPLE_RATE", "20")))
_route_sample_counter = 0


def sample_route(done: dict[str, Any], kw: dict[str, Any]) -> None:
    """按采样率把路由决策落一条遥测记录。"""
    global _route_sample_counter
    if "features" not in kw or not kw.get("features"):
        return  # engine_override 直通等无语义分支不采样
    _route_sample_counter += 1
    if _route_sample_counter % _ROUTE_SAMPLE_RATE != 0:
        return
    f = kw.get("features") or {}
    try:
        from usage_log import emit
        emit("route", {
            "domain": kw.get("domain"),
            "engine": kw.get("engine"),
            "engines": kw.get("engines"),
            "confidence": kw.get("confidence"),
            "mode": kw.get("mode"),
            "lang_override": f.get("lang_override"),
            "primary_lang": f.get("primary_lang"),
            "script": f.get("script"),
            "has_compare": f.get("has_compare"),
            "has_technical": f.get("has_technical"),
            "chinese_ratio": f.get("chinese_ratio"),
            "intents": f.get("intents"),
        })
    except Exception:
        pass

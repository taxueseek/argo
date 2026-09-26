#!/usr/bin/env python3
"""route_log.py — 路由决策采样落日志（旁路，失败静默，仅本机）。

按采样率把「features 齐全」的决策点落一条使用日志记录（P2-6）。单独成模块的理由：
它是路由模块**唯一**的写副作用（除决策缓存外），且必须永远 fail-open——
使用日志写失败绝不能让一次搜索失败。把它隔离出来，route_query 里就只剩编排。

`ARGO_ROUTE_SAMPLE_RATE` 控制采样率（默认 1/20）。
"""

from __future__ import annotations

import os
from typing import Any


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """读整型环境变量，任何异常都退回 default。

    为什么必须有：这一行原本裸写 `int(os.environ.get(...))`，于是
    `ARGO_ROUTE_SAMPLE_RATE=abc` 在**模块导入期**抛 ValueError，经
    route.py 的导入链直接把整个 `argo search` 打挂——与本模块 docstring
    写的「必须永远 fail-open，使用日志写失败绝不能让一次搜索失败」正好相反，
    而且触发它的只是一个拼错的采样率。mcp_handlers 早有同名 _env_int，
    两处各写各的才是这个 bug 的成因。
    """
    try:
        return max(minimum, int(str(os.environ.get(name, "")).strip() or default))
    except (TypeError, ValueError):
        return default


_ROUTE_SAMPLE_RATE = _env_int("ARGO_ROUTE_SAMPLE_RATE", 20)
_route_sample_counter = 0


def sample_route(done: dict[str, Any], kw: dict[str, Any]) -> None:
    """按采样率把路由决策落一条使用日志记录。"""
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

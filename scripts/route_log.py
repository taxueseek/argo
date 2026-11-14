#!/usr/bin/env python3
"""route_log.py — 路由决策采样落日志（旁路，失败静默，仅本机）。

按采样率把「features 齐全」的决策点落一条使用日志记录（P2-6）。单独成模块的理由：
它是路由模块**唯一**的写副作用（除决策缓存外），且必须永远 fail-open——
使用日志写失败绝不能让一次搜索失败。把它隔离出来，route_query 里就只剩编排。

`ARGO_ROUTE_SAMPLE_RATE` 控制采样率（默认 1/20）。
"""

from __future__ import annotations

import os
import random
from typing import Any


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """读整型环境变量，任何异常都退回 default。

    调用期按需解析（不在模块导入期读 os.environ），避免导入链失败或测试改写
    环境变量后仍沿用旧采样率。
    """
    try:
        return max(minimum, int(str(os.environ.get(name, "")).strip() or default))
    except (TypeError, ValueError):
        return default


_DEFAULT_ROUTE_SAMPLE_RATE = 20
_ROUTE_SAMPLE_RATE = _DEFAULT_ROUTE_SAMPLE_RATE
_route_sample_counter = 0


def sample_route(done: dict[str, Any], kw: dict[str, Any]) -> None:
    """按采样率把路由决策落一条使用日志记录。"""
    global _route_sample_counter
    if "features" not in kw or not kw.get("features"):
        return  # engine_override 直通等无语义分支不采样
    rate = (_env_int("ARGO_ROUTE_SAMPLE_RATE", _DEFAULT_ROUTE_SAMPLE_RATE)
            if _ROUTE_SAMPLE_RATE == _DEFAULT_ROUTE_SAMPLE_RATE
            and "ARGO_ROUTE_SAMPLE_RATE" in os.environ
            else _ROUTE_SAMPLE_RATE)
    rate = max(1, int(rate))
    _route_sample_counter += 1
    if rate > 1:
        # 单次 CLI 进程只有第 1 次调用：若只看 `_route_sample_counter % rate == 0`，
        # 每次退出后计数器归零导致 CLI 路径零采样；首次调用用无状态随机采样，
        # 长驻进程（daemon/MCP）第 2 次起继续按固定步长采样。
        should_emit = (random.randrange(rate) == 0
                       if _route_sample_counter == 1
                       else (_route_sample_counter % rate == 0))
        if not should_emit:
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

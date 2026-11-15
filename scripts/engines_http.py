#!/usr/bin/env python3
"""engines_http.py — builder 共用的 URL 编码与 JSON 取数（不含业务逻辑）。

从 engines_builders_intl.py 与 engines_builders_weather.py 的重复实现抽出
（2026-11-11）。两处各有一份 `_UA` / `_URL_SAFE` / `_encode_url` /
`_http_json`，改一处漏另一处就会让中文地名或 gzip 处理只在部分源生效。

依赖方向：engines_http → engines_base（http_open）。builders 只引用本模块，
不再互相转发取数函数；weather 与 intl 之间没有反向调用。
"""

from __future__ import annotations

import gzip
import json
import sys
import urllib.parse
import urllib.request
from typing import Any, Callable

UA = "argo-search/2.4 (unified-search@local)"

# 结构字符保留集：既有 %XX 编码、协议/路径/参数分隔符不重复转义
URL_SAFE = "/:?&=%,._~#+-"

_LOGGERS: dict[str, "LazyLogger"] = {}


class LazyLogger:
    """延迟创建 logger，避免 import logging 的重量级 import 链（traceback→dataclasses→inspect）。"""

    def __init__(self, name: str = "unified_search.engines") -> None:
        self._name = name
        self._logger = None

    def __getattr__(self, attr: str) -> Any:
        if self._logger is None:
            import logging

            lg = logging.getLogger(self._name)
            if not lg.handlers:
                lg.setLevel(logging.WARNING)
                lg.addHandler(logging.StreamHandler(sys.stderr))
            self._logger = lg
        return getattr(self._logger, attr)


def lazy_logger(name: str = "unified_search.engines") -> LazyLogger:
    """按名称返回共享的延迟 logger 实例。"""
    lg = _LOGGERS.get(name)
    if lg is None:
        lg = LazyLogger(name)
        _LOGGERS[name] = lg
    return lg


def encode_url(url: str) -> str:
    """URL 非 ASCII 安全编码：中文地名直接拼接会让 urllib 报 ascii 编码错误。"""
    return urllib.parse.quote(url, safe=URL_SAFE)


def _fetch_bytes(
    url: str,
    timeout: float,
    engine: str = "",
    *,
    headers: dict[str, str] | None = None,
    ua: str = UA,
    accept_partial: bool = False,
    opener: Callable[..., Any] | None = None,
) -> bytes:
    if opener is None:
        from engines_base import http_open as opener
    hdrs = {"User-Agent": ua, "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(encode_url(url), headers=hdrs)
    with opener(req, timeout=timeout, engine=engine) as resp:
        try:
            raw = resp.read()
        except Exception as e:
            partial = getattr(e, "partial", None) if accept_partial else None
            if partial:
                lazy_logger().warning(f"{engine} 响应不完整，使用已读部分 {len(partial)}B")
                return partial
            raise
        if getattr(resp, "headers", None) and resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw


def http_text(
    url: str,
    timeout: float,
    engine: str = "",
    *,
    headers: dict[str, str] | None = None,
    ua: str = UA,
    accept_partial: bool = False,
    opener: Callable[..., Any] | None = None,
) -> str:
    """GET 一个文本端点，透明处理 gzip 与可选的不完整响应容忍。"""
    return _fetch_bytes(
        url,
        timeout,
        engine=engine,
        headers=headers,
        ua=ua,
        accept_partial=accept_partial,
        opener=opener,
    ).decode("utf-8", "replace")


def http_json(
    url: str,
    timeout: float,
    engine: str = "",
    *,
    headers: dict[str, str] | None = None,
    ua: str = UA,
    accept_partial: bool = False,
    opener: Callable[..., Any] | None = None,
) -> Any:
    """GET 一个 JSON 端点，透明处理 gzip。异常照常抛给调用方。"""
    return json.loads(
        http_text(
            url,
            timeout,
            engine=engine,
            headers=headers,
            ua=ua,
            accept_partial=accept_partial,
            opener=opener,
        )
    )

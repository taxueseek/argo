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
import urllib.parse
import urllib.request
from typing import Any

from engines_base import http_open

UA = "argo-search/2.4 (unified-search@local)"

# 结构字符保留集：既有 %XX 编码、协议/路径/参数分隔符不重复转义
URL_SAFE = "/:?&=%,._~#+-"


def encode_url(url: str) -> str:
    """URL 非 ASCII 安全编码：中文地名直接拼接会让 urllib 报 ascii 编码错误。"""
    return urllib.parse.quote(url, safe=URL_SAFE)


def http_json(url: str, timeout: float, engine: str = "") -> Any:
    """GET 一个 JSON 端点，透明处理 gzip。异常照常抛给调用方。"""
    req = urllib.request.Request(
        encode_url(url),
        headers={"User-Agent": UA, "Accept-Encoding": "gzip"},
    )
    with http_open(req, timeout=timeout, engine=engine) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace"))

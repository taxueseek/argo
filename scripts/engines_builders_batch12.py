#!/usr/bin/env python3
"""批次十二构建器：CN 通用搜索补源（2026-09-28，9044621）。

  so      360 搜索（so.com/s?q=，HTML 解析）
  shenma  神马搜索（m.sm.cn/s?q=，移动端 HTML 解析）

为什么放 batch 而不是 engines_builders_cn：cn 是登记上限 2319 的祖父
文件（中文源声明表，逐源一段），新引擎的下一站按既有惯例是 batch 模块
（engines_builders_tech.py 的登记理由明写了「新引擎的下一站是 batch 模块」）。
两源在 chinese_general 域尾部（位次 8/9）观察期，台账见
tests/test_new_source_reachability.py::_DORMANT_ALLOWLIST。
"""

from __future__ import annotations

import logging
import re
import urllib.request
from typing import Any

from engines_base import http_open, safe_search

logger = logging.getLogger("unified_search.engines")


# ── 360 搜索（so.com）───────────────────────────────────────────────────────

def _build_so_engine(spec: dict[str, Any]) -> Any:
    """360 搜索（so.com/s?q=，HTML 解析）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://www.so.com/s?q=" + up.quote(query)
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.so.com/"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                page = resp.read().decode("utf-8", "replace")
            results = []
            # 360 搜索结果块：<li class="res-list">...<h3><a href="...">title</a>
            for m in re.finditer(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
                url_m, title_m = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
                if not title_m or not url_m:
                    continue
                results.append({
                    "title": title_m[:80],
                    "url": url_m,
                    "snippet": "",
                    "source": "so",
                    "score": max(1.0 - len(results) * 0.1, 0.1),
                })
                if len(results) >= n:
                    break
            return results
        except Exception as e:
            logger.warning(f"360 搜索失败: {e}")
            return []
    return _engine


# ── 神马搜索（sm.cn，移动端）────────────────────────────────────────────────

def _build_shenma_engine(spec: dict[str, Any]) -> Any:
    """神马搜索（m.sm.cn/s?q=，移动端 HTML 解析）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://m.sm.cn/s?q=" + up.quote(query)
        headers = {
            "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
            "Referer": "https://m.sm.cn/",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                page = resp.read().decode("utf-8", "replace")
            results = []
            # 神马结果块：<a href="..." class="...">title</a> + snippet
            for m in re.finditer(r'<a[^>]*href="(https?://[^"]+)"[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</a>', page, re.S):
                url_m, title_m = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
                if not title_m or not url_m or "sm.cn" in url_m:
                    continue
                results.append({
                    "title": title_m[:80],
                    "url": url_m,
                    "snippet": "",
                    "source": "shenma",
                    "score": max(1.0 - len(results) * 0.1, 0.1),
                })
                if len(results) >= n:
                    break
            return results
        except Exception as e:
            logger.warning(f"神马搜索失败: {e}")
            return []
    return _engine

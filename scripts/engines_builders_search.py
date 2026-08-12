#!/usr/bin/env python3
"""专用构建器：通用搜索 API（parallel.ai / you.com）——按官方文档对齐

parallel（docs.parallel.ai/search/search-quickstart）：
  - objective（自然语言目标）+ search_queries（数组）为官方推荐组合
  - mode: turbo(~200ms, $1/千次, 仅英日文) / basic(~1s, $5/千次) / advanced(~3s, $5/千次)
    argo 默认 basic，deep 模式 advanced
  - advanced_settings: excerpt_settings.max_chars_per_result 与 argo snippet 截断对齐

you.com（docs.you.com）：
  - 官方环境变量名 YDC_API_KEY
  - freshness（day/week/month/year）按查询时效敏感度动态化（同 bocha 逻辑）
  - language 按查询主语言下推（zh/en）
  - page_age 为 ISO 8601 时间戳 → 提取为 published_at
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any

from engines_base import safe_search

logger = logging.getLogger("unified_search.engines")

# ── Parallel 搜索（api.parallel.ai）────────────────────────────────────────────

_PARALLEL_URL = "https://api.parallel.ai/v1/search"


def _parallel_key() -> str:
    return os.environ.get("PARALLEL_API_KEY", "")


def _parallel_mode(query: str, depth: str) -> str:
    """mode 选择：deep → advanced；中文查询不可用 turbo（官方仅英日文）→ basic；英文 fast/auto → basic。"""
    if depth == "deep":
        return "advanced"
    return "basic"


def _build_parallel_engine(spec: dict[str, Any]) -> Any:
    """Parallel Search：objective + search_queries + mode（官方推荐组合）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        key = _parallel_key()
        if not key:
            return [{"error": "PARALLEL_API_KEY 未设置", "source": "parallel"}]
        limit = max(1, int(n or 5))
        # 多路召回：原查询 + 无 LLM 变体（问句化/概念扩展等），官方最佳实践 2-3 个
        try:
            from query_variants import generate_query_variations
            queries = generate_query_variations(query)[:3] or [query]
        except Exception:
            queries = [query]
        body = {
            "objective": f"Find the latest, most relevant information about: {query}",
            "search_queries": queries,
            "mode": _parallel_mode(query, depth),
            "advanced_settings": {
                "max_results": min(limit, 20),
                "excerpt_settings": {"max_chars_per_result": 300},
            },
        }
        # argo 时间窗 --since → after_date（官方 --after-date）
        since = kwargs.get("since")
        if since:
            date_str = str(since)[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                body["advanced_settings"]["source_policy"] = {"after_date": date_str}
        req = urllib.request.Request(
            _PARALLEL_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"X-Api-Key": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"parallel 请求失败: {e}")
            return []
        results: list[dict[str, Any]] = []
        for item in (data.get("results") or [])[:limit]:
            title = str(item.get("title") or "")[:200]
            url = str(item.get("url") or "")
            if not (title or url):
                continue
            excerpts = item.get("excerpts") or []
            snippet = str(excerpts[0])[:300] if excerpts else ""
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "source": "parallel",
                "published_at": str(item.get("publish_date") or "")[:64],
            })
        return results
    return _engine


# ── You.com 搜索（ydc-index.io）───────────────────────────────────────────────

_YOU_URL = "https://ydc-index.io/v1/search"


def _you_key() -> str:
    return os.environ.get("YDC_API_KEY", "")


def _you_freshness(query: str) -> str:
    """freshness 动态化：时效敏感查询（复用缓存层检测）→ day；否则省略（全量）。"""
    try:
        from cache import is_freshness_sensitive_query
        if is_freshness_sensitive_query(query or ""):
            return "day"
    except Exception:
        pass
    return ""


def _you_language(query: str) -> str:
    """language 下推：含中文 → zh，其余 en。"""
    try:
        from lang_detect import detect_language
        lang = detect_language(query or "")
        if lang:
            return {"zh": "zh", "ja": "ja"}.get(lang, "en")
    except Exception:
        pass
    return "zh" if any("\u4e00" <= c <= "\u9fff" for c in (query or "")) else "en"


def _build_you_engine(spec: dict[str, Any]) -> Any:
    """You.com Web Search：web+news 合并，freshness/language 动态化，page_age → published_at。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        key = _you_key()
        if not key:
            return [{"error": "YDC_API_KEY 未设置", "source": "you"}]
        body: dict[str, Any] = {"query": query, "count": max(1, int(n or 5))}
        freshness = _you_freshness(query)
        if freshness:
            body["freshness"] = freshness
        body["language"] = _you_language(query)
        req = urllib.request.Request(
            _YOU_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"X-API-Key": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"you 请求失败: {e}")
            return []
        results: list[dict[str, Any]] = []
        for kind in ("news", "web"):
            for item in (data.get("results", {}).get(kind) or [])[: max(1, int(n or 5))]:
                title = str(item.get("title") or "")[:200]
                url = str(item.get("url") or "")
                if not (title or url):
                    continue
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": str(item.get("description") or "")[:300],
                    "source": f"you_{kind}",
                    # page_age 是 ISO 8601 时间戳（如 2026-08-11T16:24:41）→ 取日期
                    "published_at": str(item.get("page_age") or "")[:10],
                })
        return results
    return _engine

#!/usr/bin/env python3
"""mcp_payload — MCP 响应紧凑序列化与社交并行检索辅助。"""

from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

_JSON_SEP = (",", ":")

_RESULT_FIELDS = (
    "title", "url", "snippet", "source", "score", "date", "published",
    "consensus_engines",
)
_TOP_FIELDS = (
    "query", "engines_used", "cached", "cache_level", "elapsed_ms", "count",
    "results", "errors", "domain", "mode", "depth", "rewritten_query", "engine",
    "platforms", "platform_breakdown", "total_posts", "engagement_totals",
    "posts", "source",
)


def dumps(obj: Any) -> str:
    """紧凑 JSON 序列化（无 indent）。"""
    return json.dumps(obj, ensure_ascii=False, separators=_JSON_SEP, default=str)


def tool_ok(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": dumps(payload)}]}


def tool_err(payload: Any) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": dumps(payload)}],
        "isError": True,
    }


def compact_item(item: dict[str, Any], *, summary: bool, snippet_max: int) -> dict[str, Any]:
    out = {k: item[k] for k in _RESULT_FIELDS if k in item and item[k] is not None}
    if "social_meta" in item and item["social_meta"] is not None:
        out["social_meta"] = item["social_meta"]
    snip = out.get("snippet")
    if isinstance(snip, str):
        cap = 80 if summary else snippet_max
        if len(snip) > cap:
            out["snippet"] = snip[:cap]
    return out


def compact_for_mcp(
    data: dict[str, Any],
    *,
    summary: bool = False,
    verbose: bool = False,
    snippet_max: int = 300,
) -> dict[str, Any]:
    """裁剪 MCP 返回体：去掉内部字段，控制 snippet 长度。"""
    if verbose or not isinstance(data, dict):
        return data
    out: dict[str, Any] = {k: data[k] for k in _TOP_FIELDS if k in data}
    if "results" in data and isinstance(data["results"], list):
        out["results"] = [
            compact_item(r, summary=summary, snippet_max=snippet_max)
            if isinstance(r, dict) else r
            for r in data["results"]
        ]
    if "posts" in data and isinstance(data["posts"], list):
        out["posts"] = [
            compact_item(r, summary=summary, snippet_max=snippet_max)
            if isinstance(r, dict) else r
            for r in data["posts"]
        ]
        for k in ("platforms", "platform_breakdown", "total_posts", "engagement_totals"):
            if k in data:
                out[k] = data[k]
    if "source" in data and "source" not in out:
        out["source"] = data["source"]
    return out


def search_platforms(
    platforms: list[str], query: str, n: int
) -> tuple[dict[str, list], list[str], list[str]]:
    """并行调用社交引擎，返回 (platform_results, engines_used, errors)。"""
    by_platform: dict[str, list] = {}
    errors: list[str] = []

    def _one(platform: str) -> tuple[str, list | None, str | None]:
        module_name = platform.replace("-", "_") + "_engine"
        try:
            mod = importlib.import_module(f"social_engines.{module_name}")
            return platform, mod.search(query, n=n), None
        except ImportError:
            return platform, None, (
                f"Platform {platform} not available "
                f"(module social_engines.{module_name})"
            )
        except Exception as e:
            return platform, None, f"{platform}: {str(e)[:100]}"

    raw: dict[str, list | None] = {}
    if len(platforms) <= 1:
        for p in platforms:
            name, results, err = _one(p)
            raw[name] = results
            if err:
                errors.append(err)
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(platforms))) as pool:
            futs = [pool.submit(_one, p) for p in platforms]
            for fut in as_completed(futs):
                name, results, err = fut.result()
                raw[name] = results
                if err:
                    errors.append(err)

    engines_used: list[str] = []
    for p in platforms:
        results = raw.get(p)
        if results is not None:
            by_platform[p] = results
            engines_used.append(p)
        else:
            by_platform[p] = []
    return by_platform, engines_used, errors

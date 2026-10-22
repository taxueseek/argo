#!/usr/bin/env python3
"""mcp_handlers.py — MCP 工具执行逻辑与集中分发。

路径引导、延迟导入缓存、参数守卫与 19 个工具的 execute_tool 分派。
架构解耦：
  - mcp_compact.py   结果裁剪与紧凑压缩协议
  - mcp_local.py     本地文件检索、受限预览与可复算沙箱
  - mcp_warm.py      核心后台预热、连接池预建与社交抓取并发编排
  - mcp_handlers_surface.py 表面五工具分发
  - mcp_tools.py     工具 schema 唯一真源
  - mcp_transport.py JSON-RPC 帧协议与 stdio 传输
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import cli_io
from typing import Any

from mcp_compact import (
    _cap_extract_output,
    _compact_research_result,
    _compact_search_result,
    _trim_snippet,
)
from mcp_local import (
    _LOCAL_READ_ALLOWED_EXTS,
    _local_read_preview,
    _local_read_whitelist,
    _seek_py,
    handle_local_read,
    handle_local_search,
    handle_recompute,
)
from mcp_warm import (
    _prewarm_anysearch_connection,
    _resolve_research_profile as _warm_resolve_research_profile,
    _search_social_platforms as _warm_search_social_platforms,
    _warm_core_async as _warm_core_async_impl,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ARGO_DIR = os.path.dirname(SCRIPT_DIR)  # argo 根目录
sys.path.insert(0, SCRIPT_DIR)
SUB_SKILLS_DIR = os.path.join(ARGO_DIR, "sub-skills")
if os.path.isdir(SUB_SKILLS_DIR):
    for sub in os.listdir(SUB_SKILLS_DIR):
        sub_path = os.path.join(SUB_SKILLS_DIR, sub)
        if os.path.isdir(sub_path) and sub_path not in sys.path:
            sys.path.append(sub_path)
os.chdir(ARGO_DIR)

# 启动日志（写入 stderr，不影响 stdio 通信）
sys.stderr.write("[argo-mcp] starting (lazy imports enabled)\n")
sys.stderr.flush()

_cache_instance = None
_module_cache: dict[str, Any] = {}


def _lazy_import(module_name: str):
    """延迟导入模块，首次调用时加载（进程内缓存）。"""
    return importlib.import_module(module_name)


def _lazy_cached(module_name: str):
    """带进程内缓存的延迟导入，避免重复 importlib 开销。"""
    m = _module_cache.get(module_name)
    if m is None:
        m = importlib.import_module(module_name)
        _module_cache[module_name] = m
    return m


def _get_cache():
    global _cache_instance
    if _cache_instance is None:
        cache = _lazy_cached("cache")
        _cache_instance = cache.SearchCache()
    return _cache_instance


def _dumps(obj: Any, pretty: bool = False) -> str:
    """MCP 的 JSON 序列化：复用 CLI stdout 的同一套计算方式（cli_io 是唯一来源）。"""
    return cli_io.dumps_pretty(obj) if pretty else cli_io.dumps(obj)


def _err_is(message: str) -> dict[str, Any]:
    """工具级参数错误（isError 契约，与 _ok 对偶）。"""
    return {"content": [{"type": "text", "text": _dumps({"error": message})}],
            "isError": True}


def _ok(payload: Any, pretty: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": _dumps(payload, pretty=pretty)}]}


def _warm_core_async() -> None:
    """initialize 后后台预热 search+cache，摊平首次 tools/call 延迟。"""
    _warm_core_async_impl(_lazy_cached, _get_cache)


def _resolve_research_profile(arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """解析 topic profile；全部在 argo 内，不加载外部 skill。"""
    return _warm_resolve_research_profile(arguments, _lazy_cached)


def _search_social_platforms(
    platforms: list[str], query: str, n: int, timeout: float = 20.0,
) -> tuple[dict[str, list], list[str], list[str]]:
    """并行抓取多社交平台（MCP 热路径）。"""
    return _warm_search_social_platforms(platforms, query, n, timeout=timeout, lazy_cached_fn=_lazy_cached)


def _env_bool(name: str, default: bool = False) -> bool:
    """读布尔环境变量；空/非法回退默认。"""
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    """读整数环境变量；空/非法回退默认。"""
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """边界夹取（与工具 schema 的 min/max 同计算方式）：非法回默认，越界取边界。"""
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


class _InvalidParam(Exception):
    """工具参数类型/取值非法：外层转 -32602（区别于引擎内部故障的 -32000）。"""


def _int_param(arguments: dict[str, Any], key: str, default: int) -> int:
    """数值参数严格校验：非整数显式 -32602，不静默回默认。"""
    value = arguments.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _InvalidParam(f"{key} 必须是整数，收到 {value!r}")


def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行 MCP 工具，按需导入模块。

    调试/运行配置（pretty / skip_cache / timeout）已从模型可见 schema 移出，
    改为 server 层环境变量：ARGO_MCP_PRETTY / ARGO_MCP_SKIP_CACHE /
    ARGO_MCP_TIMEOUT。arguments 里的旧字段仍兼容（低版本客户端可传）。
    模型上下文不再为这些非决策参数付费，功能与调试能力不丢。
    """
    pretty = _env_bool("ARGO_MCP_PRETTY", bool(arguments.get("pretty", False)))
    try:
        _required = {
            "argo_search": ("query",),
            "argo_research": ("query",),
            "argo_social_search": ("query",),
            "argo_evidence": ("query",),
            "argo_clarify": ("query",),
            "argo_crawl": ("url",),
            "argo_fetch": ("url",),
            "argo_screenshot": ("url",),
            "argo_pdf": ("url",),
            "argo_article": ("url",),
            "argo_job": ("query",),
        }
        if name in _required:
            missing = [p for p in _required[name] if not arguments.get(p)]
            if missing:
                return {
                    "content": [{"type": "text",
                                 "text": _dumps({"error": {
                                     "code": -32602,
                                     "message": f"Missing required parameter(s): {', '.join(missing)}"
                                 }})}],
                    "isError": True,
                }

        if name == "argo_search":
            search_mod = _lazy_cached("search")
            result = search_mod.super_search(
                query=arguments["query"],
                engine=arguments.get("engine", "auto"),
                n=arguments.get("max_results", 5),
                skip_cache=_env_bool("ARGO_MCP_SKIP_CACHE", _env_bool("ARGO_NO_CACHE", False) or bool(arguments.get("skip_cache", False))),
                timeout=_env_int("ARGO_MCP_TIMEOUT", _int_param(arguments, "timeout", 10)),
                depth=arguments.get("depth", "fast"),
                mode=arguments.get("mode", "auto"),
                since=arguments.get("since"),
                until=arguments.get("until"),
                sort=arguments.get("sort", "relevance"),
                include_local=bool(arguments.get("include_local", True)),
                include_domains=arguments.get("include_domains") or None,
                exclude_domains=arguments.get("exclude_domains") or None,
                cache=_get_cache(),
                envelope=False,
                context="search",
            )
            summary = arguments.get("summary", True)
            if summary or not pretty:
                payload = _compact_search_result(result, summary=bool(summary))
            else:
                payload = result
            return _ok(payload, pretty=pretty)

        elif name == "argo_local_search":
            return handle_local_search(
                arguments, pretty=pretty, ok_fn=_ok,
                clamp_int_fn=_clamp_int, lazy_cached_fn=_lazy_cached,
            )

        elif name == "argo_local_read":
            return handle_local_read(
                arguments, pretty=pretty, ok_fn=_ok,
                int_param_fn=_int_param,
            )

        elif name == "argo_recompute":
            return handle_recompute(
                arguments, pretty=pretty, ok_fn=_ok,
                clamp_int_fn=_clamp_int,
            )

        elif name == "argo_research":
            research_mod = _lazy_cached("research")
            mode = arguments.get("mode", "auto")
            if mode == "social-sentiment":
                platforms_str = arguments.get("platforms", "hackernews,zhihu,bilibili")
                platforms = [p.strip() for p in platforms_str.split(",") if p.strip()]
                result = research_mod.social_sentiment_research(
                    query=arguments["query"],
                    platforms=platforms,
                    max_results=arguments.get("max_results", 5),
                )
            else:
                profile, profile_key = _resolve_research_profile(arguments)
                num_sub = arguments.get("num_sub_queries")
                max_results = arguments.get("max_results")
                depth = arguments.get("depth")
                if profile:
                    num_sub = num_sub if num_sub is not None else profile.get("sub_queries", 4)
                    max_results = max_results if max_results is not None else profile.get("max_results", 5)
                    depth = depth if depth is not None else profile.get("depth", "balanced")
                result = research_mod.deep_research(
                    query=arguments["query"],
                    num_sub_queries=int(num_sub or 4),
                    max_results=int(max_results or 5),
                    depth=str(depth or "balanced"),
                    mode=mode,
                    profile=profile,
                    work_packages=arguments.get("work_packages"),
                    allow_recompute=bool(arguments.get("allow_recompute", False)),
                    cross_lingual_trace=bool(arguments.get("cross_lingual_trace", False)),
                )
                if profile and profile_key:
                    result["topic_profile"] = profile.get("name")
                    result["topic_profile_key"] = profile_key
            summary = arguments.get("summary", True)
            if result.get("mode") == "social-sentiment":
                payload = result
            elif summary or not pretty:
                payload = _compact_research_result(result, summary=bool(summary))
            else:
                payload = result
            return _ok(payload, pretty=pretty)

        elif name == "argo_social_search":
            platforms_str = arguments.get("platforms", "hackernews,zhihu,bilibili")
            platforms = [p.strip() for p in platforms_str.split(",") if p.strip()]
            query = arguments["query"]
            n = _clamp_int(arguments.get("max_results", 5), 5, 1, 20)
            platform_results, engines_used, errors = _search_social_platforms(platforms, query, n)
            if arguments.get("mode", "text") == "sentiment":
                research_mod = _lazy_cached("research")
                output = research_mod.aggregate_social_sentiment(
                    query=query, platforms=platforms, platform_results=platform_results,
                )
            else:
                all_results: list = []
                for p in platforms:
                    all_results.extend(platform_results.get(p) or [])
                output = {
                    "query": query,
                    "platforms": platforms,
                    "results": all_results,
                    "count": len(all_results),
                    "engines_used": engines_used,
                }
            if errors:
                output["errors"] = errors
            return _ok(output, pretty=pretty)

        elif name == "argo_evidence":
            results_json_str = arguments.get("results_json", "")
            if not results_json_str or not results_json_str.strip():
                search_mod = _lazy_cached("search")
                search_result = search_mod.super_search(
                    query=arguments["query"],
                    n=arguments.get("max_results", 10),
                    depth="fast",
                    mode="auto",
                    cache=_get_cache(),
                )
                results = search_result.get("results", [])
            else:
                results_data = json.loads(results_json_str)
                results = results_data.get("results", [])
            evidence_mod = _lazy_cached("evidence")
            result = evidence_mod.compute_credibility(results, arguments["query"])
            return _ok(result, pretty=pretty)

        elif name == "argo_clarify":
            clarify_mod = _lazy_cached("clarify")
            analysis = clarify_mod.analyze_query(arguments["query"])
            routing = clarify_mod.recommend_routing(analysis)
            analysis["routing"] = routing
            return _ok(analysis, pretty=pretty)

        elif name == "argo_crawl":
            crawl_mod = _lazy_cached("crawl")
            strategy = arguments.get("strategy", "bfs")
            max_pages = _clamp_int(arguments.get("max_pages", 10), 10, 1, 50)
            max_depth = _clamp_int(arguments.get("max_depth", 2), 2, 1, 5)
            timeout = _env_int("ARGO_MCP_TIMEOUT_CRAWL", _int_param(arguments, "timeout", 8))
            if strategy == "sitemap":
                result = crawl_mod.crawl_sitemap(arguments["url"], max_pages=max_pages, timeout=timeout)
            else:
                result = crawl_mod.crawl_bfs(arguments["url"], max_pages=max_pages, max_depth=max_depth, timeout=timeout)
            return _ok(result, pretty=pretty)

        elif name == "argo_fetch":
            if arguments.get("mode", "text") == "extract":
                extract_mod = _lazy_cached("extract")
                fetch_mod = _lazy_cached("fetch_v3")
                emode = arguments.get("extract_mode", "all")
                max_chars = _clamp_int(_int_param(arguments, "max_chars", 8000), 8000, 500, 50000)
                fetch_result = fetch_mod.fetch_page_v3(
                    arguments["url"], max_chars=max_chars,
                    timeout=_env_int("ARGO_MCP_TIMEOUT_FETCH",
                                     _int_param(arguments, "timeout", 15)), raw=True)
                if not fetch_result["success"]:
                    return {
                        "content": [{"type": "text", "text": _dumps({"error": fetch_result.get("error", "fetch failed")})}],
                        "isError": True,
                    }
                html = fetch_result["html"]
                output = {}
                if emode in ("tables", "all"):
                    output["tables"] = extract_mod.extract_tables(html)
                if emode in ("metadata", "all"):
                    output["metadata"] = extract_mod.extract_metadata(html)
                if emode in ("jsonld", "all"):
                    output["jsonld"] = extract_mod.extract_jsonld(html)
                output["url"] = fetch_result["url"]
                return _ok(_cap_extract_output(output), pretty=pretty)

            focus_query = arguments.get("focus")
            fetch_v3_mod = _lazy_cached("fetch_v3")
            result = fetch_v3_mod.fetch_v3(
                url=arguments["url"],
                max_chars=fetch_v3_mod._focus_fetch_chars(
                    _int_param(arguments, "max_chars", 8000), focus_query),
                timeout=_env_int("ARGO_MCP_TIMEOUT_FETCH", _int_param(arguments, "timeout", 15)),
                use_browser_fallback=True,
                force_browser=arguments.get("use_browser", False),
            )
            if focus_query and result.get("success"):
                focus_mod = _lazy_cached("focus_extract")
                focus_mod.apply_focus(result, focus_query,
                                      max_chars=_int_param(arguments, "max_chars", 8000))
                if not result.get("focus_applied"):
                    result["focus_note"] = "内容未达聚焦阈值，返回全文"
            if arguments.get("summary", True) and isinstance(result.get("content"), str):
                max_c = _int_param(arguments, "max_chars", 8000)
                if len(result["content"]) > max_c:
                    result["content"] = result["content"][:max_c]
                    result["truncated"] = True
            if isinstance(result, dict):
                result.pop("html", None)
                if not arguments.get("include_links", False):
                    result.pop("links", None)
            return _ok(result, pretty=pretty)

        elif name == "argo_screenshot":
            import time as _time
            import tempfile
            output = arguments.get("output_path") or os.path.join(
                tempfile.gettempdir(), f"argo_screenshot_{int(_time.time())}.png")
            full_page = arguments.get("full_page", False)
            try:
                cdp_mod = _lazy_cached("chrome_cdp")
                with cdp_mod.ChromeCDP(auto_start=True) as cdp:
                    cdp.navigate(arguments["url"])
                    path = cdp.screenshot(output, full_page=full_page)
                    if path and os.path.exists(path):
                        return _ok({"success": True, "screenshot": path, "url": arguments["url"]}, pretty=pretty)
                    return {
                        "content": [{"type": "text", "text": _dumps({"success": False, "error": "screenshot failed"})}],
                        "isError": True,
                    }
            except Exception as e:
                return {
                    "content": [{"type": "text", "text": _dumps({"success": False, "error": str(e)[:200]})}],
                    "isError": True,
                }

        elif name == "argo_pdf":
            pdf_mod = _lazy_cached("pdf_extract")
            result = pdf_mod.extract_pdf(
                url_or_path=arguments["url"],
                pages=arguments.get("pages"),
                password=arguments.get("password"),
            )
            return _ok(result, pretty=pretty)

        elif name == "argo_article":
            article_mod = _lazy_cached("article")
            try:
                out = article_mod.fetch_article(
                    arguments["url"],
                    timeout=_env_int("ARGO_MCP_TIMEOUT_FETCH",
                                     _int_param(arguments, "timeout", 30)),
                )
            except ValueError as e:
                return {"content": [{"type": "text", "text": _dumps({"error": str(e)})}],
                        "isError": True}
            if out.get("ok"):
                max_c = _int_param(arguments, "max_chars", 20000)
                if len(out.get("content", "")) > max_c:
                    out["content"] = out["content"][:max_c]
                    out["truncated"] = True
            return _ok(out, pretty=pretty)

        elif name == "argo_job":
            job_mod = _lazy_cached("job")
            try:
                out = job_mod.search(
                    arguments["query"],
                    city=arguments.get("city", ""),
                    num=_clamp_int(arguments.get("num", 5), 5, 1, 20),
                    platforms=arguments.get("platforms", ""),
                    loose=bool(arguments.get("loose", False)),
                    fetch_n=_clamp_int(arguments.get("fetch_detail", 0), 0, 0, 10),
                )
            except ValueError as e:
                return {"content": [{"type": "text", "text": _dumps({"error": str(e)})}],
                        "isError": True}
            return _ok(out, pretty=pretty)

        surface = _lazy_cached("mcp_handlers_surface")
        surface_result = surface.handle_surface_tool(name, arguments, pretty=pretty)
        if surface_result is not None:
            return surface_result

        else:
            return {
                "content": [{"type": "text",
                             "text": _dumps({"error": {"code": -32601,
                                                       "message": f"Unknown tool: {name}"}})}],
                "isError": True,
            }

    except _InvalidParam as e:
        return {
            "content": [{"type": "text",
                         "text": _dumps({"error": {"code": -32602,
                                                   "message": f"Invalid parameter: {e}"}})}],
            "isError": True,
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": _dumps({"error": {"code": -32000, "message": f"{type(e).__name__}: {e}"}})}],
            "isError": True
        }

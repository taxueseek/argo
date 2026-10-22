#!/usr/bin/env python3
"""mcp_warm.py — MCP 核心预热、连接池预建与社交抓取并发编排。

拆分自 mcp_handlers.py：隔离后台异步预热线程模型与社交平台并发调度。
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

_warm_started = False


def _prewarm_anysearch_connection() -> None:
    """在 warm 线程里预建 anysearch 的隧道连接（2026-10-18）。"""
    if os.environ.get("ARGO_NO_PREWARM", "").strip() not in ("", "0"):
        return
    try:
        import urllib.parse
        from conn_pool import give_back, key_for, stats as pool_stats
        from net_proxy import open_connection, resolve_proxy
        if not pool_stats().get("enabled"):
            return
        url = "https://api.anysearch.com/mcp"
        parsed = urllib.parse.urlparse(url)
        proxy_url = resolve_proxy(url)
        conn, _via = open_connection(parsed, 8.0, proxy_url)
        conn.connect()
        give_back(key_for(parsed, proxy_url), conn)
    except Exception as e:
        sys.stderr.write(f"[argo-mcp] prewarm-skip: {type(e).__name__}: {e}\n")


def _warm_core_async(lazy_cached_fn, get_cache_fn) -> None:
    """initialize 后后台预热 search+cache，摊平首次 tools/call 延迟。"""
    global _warm_started
    if _warm_started:
        return
    _warm_started = True

    def _run() -> None:
        t0 = __import__("time").time()
        try:
            lazy_cached_fn("search")
            get_cache_fn()
            try:
                from route_domains import warm_compiled_domains
                warm_compiled_domains()
            except Exception:
                pass
            _prewarm_anysearch_connection()
            ms = int((__import__("time").time() - t0) * 1000)
            sys.stderr.write(f"[argo-mcp] warm-core ok {ms}ms\n")
        except Exception as e:
            sys.stderr.write(f"[argo-mcp] warm-core fail: {type(e).__name__}: {e}\n")
        sys.stderr.flush()

    threading.Thread(target=_run, name="argo-mcp-warm", daemon=True).start()


def _resolve_research_profile(arguments: dict[str, Any], lazy_cached_fn) -> tuple[dict[str, Any] | None, str | None]:
    """解析 topic profile；全部在 argo 内，不加载外部 skill。"""
    try:
        profiles = lazy_cached_fn("topic_research_profiles")
    except Exception:
        return None, None
    if not profiles:
        return None, None
    topic = arguments.get("topic")
    key = None
    if topic:
        prof = profiles.get_profile(str(topic))
        if prof:
            raw = str(topic).strip()
            key = profiles.ALIASES.get(raw) or profiles.ALIASES.get(raw.lower()) or raw.lower()
            return prof, key
        return None, None
    if arguments.get("auto_topic", True):
        key = profiles.detect_topic_from_query(arguments.get("query") or "")
        if key:
            return profiles.get_profile(key), key
    return None, None


def _search_social_platforms(
    platforms: list[str], query: str, n: int, timeout: float = 20.0,
    lazy_cached_fn=None,
) -> tuple[dict[str, list], list[str], list[str]]:
    """并行抓取多社交平台（MCP 热路径）。"""
    platform_results: dict[str, list] = {}
    errors: list[str] = []
    engines_used: list[str] = []

    def _one(platform: str) -> tuple[str, list, str | None]:
        module_name = platform.replace("-", "_") + "_engine"
        try:
            mod = lazy_cached_fn(f"social_engines.{module_name}")
            return platform, mod.search(query, n=n), None
        except ImportError:
            return platform, [], f"Platform {platform} not available (module social_engines.{module_name})"
        except Exception as e:
            return platform, [], f"{platform}: {str(e)[:100]}"

    workers = min(max(len(platforms), 1), 4)
    import concurrent.futures
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(_one, p): p for p in platforms}
    try:
        for fut in concurrent.futures.as_completed(futs, timeout=timeout):
            platform, results, err = fut.result()
            platform_results[platform] = results
            if err:
                errors.append(err)
            elif results is not None:
                engines_used.append(platform)
    except concurrent.futures.TimeoutError:
        for fut, platform in futs.items():
            if not fut.done():
                fut.cancel()
                platform_results[platform] = []
                errors.append(f"{platform}: timeout (>{timeout}s)")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return platform_results, engines_used, errors

#!/usr/bin/env python3
"""
mcp_server.py — Argo MCP 服务层

将 argo_search/argo_research/argo_evidence/argo_clarify/argo_crawl/argo_extract
等工具暴露为 MCP tool，通过 JSON-RPC over stdio 通信。

实现拆分：
  - mcp_tools.py    tool schema
  - mcp_payload.py  紧凑 JSON / 社交并行
  - mcp_server.py   RPC 循环与工具执行（本文件）

用法：
  python3 mcp_server.py                    # 启动 MCP stdio 服务
  python3 mcp_server.py --test             # 本地测试模式
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ARGO_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
SUB_SKILLS_DIR = os.path.join(ARGO_DIR, "sub-skills")
if os.path.isdir(SUB_SKILLS_DIR):
    for sub in os.listdir(SUB_SKILLS_DIR):
        sub_path = os.path.join(SUB_SKILLS_DIR, sub)
        if os.path.isdir(sub_path) and sub_path not in sys.path:
            sys.path.insert(0, sub_path)
os.chdir(ARGO_DIR)

sys.stderr.write("[argo-mcp] starting (lazy imports, deferred config)\n")
sys.stderr.flush()

from mcp_payload import (  # noqa: E402
    compact_for_mcp,
    dumps as _dumps,
    search_platforms as _search_platforms,
    tool_err as _tool_err,
    tool_ok as _tool_ok,
)
from mcp_tools import TOOLS  # noqa: E402

# ── 延迟导入 / 缓存 ──────────────────────────────────────────────────────────

_module_cache: dict[str, Any] = {}
_cache_instance = None
_response_format = "content-length"
_engine_counts_cache: tuple[int, int, int] | None = None


def _lazy_import(module_name: str):
    mod = _module_cache.get(module_name)
    if mod is None:
        mod = importlib.import_module(module_name)
        _module_cache[module_name] = mod
    return mod


def _get_cache():
    global _cache_instance
    if _cache_instance is None:
        cache = _lazy_import("cache")
        _cache_instance = cache.SearchCache()
    return _cache_instance


def _engine_counts() -> tuple[int, int, int]:
    global _engine_counts_cache
    if _engine_counts_cache is not None:
        return _engine_counts_cache
    try:
        cfg = _lazy_import("config")
        engines = cfg.get_engines()
        local = sum(1 for name in engines if name.startswith("local_"))
        _engine_counts_cache = (len(engines), local, len(engines) - local)
    except Exception:
        _engine_counts_cache = (61, 21, 40)
    return _engine_counts_cache


def _warm_hot_path() -> None:
    try:
        _lazy_import("config").load_config()
        _lazy_import("search")
        _get_cache()
        _engine_counts()
        sys.stderr.write("[argo-mcp] warm complete\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"[argo-mcp] warm skipped: {type(e).__name__}: {e}\n")
        sys.stderr.flush()


# ── 工具执行 ──────────────────────────────────────────────────────────────────

def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行 MCP 工具，按需导入模块。"""
    try:
        if name == "argo_search":
            search_mod = _lazy_import("search")
            result = search_mod.super_search(
                query=arguments["query"],
                engine=arguments.get("engine", "auto"),
                n=arguments.get("max_results", 5),
                skip_cache=arguments.get("skip_cache", False),
                timeout=arguments.get("timeout", 10),
                depth=arguments.get("depth", "fast"),
                mode=arguments.get("mode", "auto"),
                cache=_get_cache(),
            )
            payload = compact_for_mcp(
                result,
                summary=bool(arguments.get("summary", False)),
                verbose=bool(arguments.get("verbose", False)),
            )
            return _tool_ok(payload)

        elif name == "argo_research":
            research_mod = _lazy_import("research")
            mode = arguments.get("mode", "auto")
            if mode == "social-sentiment":
                platforms_str = arguments.get("platforms", "twitter,reddit,xiaohongshu")
                platforms = [p.strip() for p in platforms_str.split(",") if p.strip()]
                result = research_mod.social_sentiment_research(
                    query=arguments["query"],
                    platforms=platforms,
                    max_results=arguments.get("max_results", 5),
                )
            else:
                result = research_mod.deep_research(
                    query=arguments["query"],
                    num_sub_queries=arguments.get("num_sub_queries", 4),
                    max_results=arguments.get("max_results", 5),
                    depth=arguments.get("depth", "balanced"),
                    mode=mode,
                )
            return _tool_ok(result)

        elif name == "argo_social_search":
            platforms_str = arguments.get("platforms", "twitter,reddit,xiaohongshu")
            platforms = [p.strip() for p in platforms_str.split(",") if p.strip()]
            query = arguments["query"]
            n = arguments.get("max_results", 5)
            by_platform, engines_used, errors = _search_platforms(platforms, query, n)
            all_results: list = []
            for p in platforms:
                all_results.extend(by_platform.get(p) or [])
            output = {
                "query": query,
                "platforms": platforms,
                "results": all_results,
                "count": len(all_results),
                "engines_used": engines_used,
            }
            if errors:
                output["errors"] = errors
            return _tool_ok(compact_for_mcp(output, summary=bool(arguments.get("summary", False))))

        elif name == "argo_social_sentiment":
            platforms_str = arguments.get("platforms", "twitter,reddit,xiaohongshu")
            platforms = [p.strip() for p in platforms_str.split(",") if p.strip()]
            query = arguments["query"]
            n = arguments.get("max_results", 5)
            by_platform, engines_used, errors = _search_platforms(platforms, query, n)
            all_posts: list = []
            for p in platforms:
                all_posts.extend(by_platform.get(p) or [])
            engagement_totals = {"likes": 0, "comments": 0, "reposts": 0, "shares": 0}
            for post in all_posts:
                meta = post.get("social_meta") or {}
                engagement_totals["likes"] += meta.get("likes", 0) or meta.get("like_count", 0) or 0
                engagement_totals["comments"] += meta.get("comments", 0) or 0
                engagement_totals["reposts"] += meta.get("reposts", 0) or 0
                engagement_totals["shares"] += meta.get("shares", 0) or 0
            platform_breakdown = {p: len(by_platform.get(p) or []) for p in platforms}
            output = {
                "query": query,
                "platforms": platforms,
                "platform_breakdown": platform_breakdown,
                "total_posts": len(all_posts),
                "engagement_totals": engagement_totals,
                "posts": all_posts,
                "engines_used": engines_used,
            }
            if errors:
                output["errors"] = errors
            return _tool_ok(compact_for_mcp(output, summary=bool(arguments.get("summary", False))))

        elif name == "argo_twitter_search":
            from social_engines.twitter_engine import search as twitter_search
            results = twitter_search(arguments["query"], arguments.get("max_results", 5))
            return _tool_ok(compact_for_mcp({"results": results, "source": "twitter", "count": len(results)}))

        elif name == "argo_reddit_search":
            from social_engines.reddit_engine import search as reddit_search
            results = reddit_search(arguments["query"], arguments.get("max_results", 5))
            return _tool_ok(compact_for_mcp({"results": results, "source": "reddit", "count": len(results)}))

        elif name == "argo_xiaohongshu_search":
            from social_engines.xiaohongshu_engine import search as xhs_search
            results = xhs_search(arguments["query"], arguments.get("max_results", 5))
            return _tool_ok(compact_for_mcp({"results": results, "source": "xiaohongshu", "count": len(results)}))

        elif name == "argo_bilibili_search":
            from social_engines.bilibili_engine import search as bilibili_search
            results = bilibili_search(arguments["query"], arguments.get("max_results", 5))
            return _tool_ok(compact_for_mcp({"results": results, "source": "bilibili", "count": len(results)}))

        elif name == "argo_weibo_search":
            from social_engines.weibo_engine import search as weibo_search
            results = weibo_search(arguments["query"], arguments.get("max_results", 5))
            return _tool_ok(compact_for_mcp({"results": results, "source": "weibo", "count": len(results)}))

        elif name == "argo_evidence":
            results_json_str = arguments.get("results_json", "")
            if not results_json_str or not results_json_str.strip():
                search_mod = _lazy_import("search")
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
            evidence_mod = _lazy_import("evidence")
            result = evidence_mod.compute_credibility(results, arguments["query"])
            return _tool_ok(result)

        elif name == "argo_clarify":
            clarify_mod = _lazy_import("clarify")
            analysis = clarify_mod.analyze_query(arguments["query"])
            routing = clarify_mod.recommend_routing(analysis)
            analysis["routing"] = routing
            return _tool_ok(analysis)

        elif name == "argo_crawl":
            crawl_mod = _lazy_import("crawl")
            strategy = arguments.get("strategy", "bfs")
            max_pages = arguments.get("max_pages", 10)
            max_depth = arguments.get("max_depth", 2)
            timeout = arguments.get("timeout", 8)
            if strategy == "sitemap":
                result = crawl_mod.crawl_sitemap(arguments["url"], max_pages=max_pages, timeout=timeout)
            else:
                result = crawl_mod.crawl_bfs(arguments["url"], max_pages=max_pages, max_depth=max_depth, timeout=timeout)
            return _tool_ok(result)

        elif name == "argo_extract":
            extract_mod = _lazy_import("extract")
            fetch_mod = _lazy_import("fetch")
            mode = arguments.get("mode", "all")
            fetch_result = fetch_mod.fetch_page(arguments["url"], max_chars=50000, timeout=15, raw=True)
            if not fetch_result["success"]:
                return _tool_err({"error": fetch_result.get("error", "fetch failed")})
            html = fetch_result["html"]
            output: dict[str, Any] = {}
            if mode in ("tables", "all"):
                output["tables"] = extract_mod.extract_tables(html)
            if mode in ("metadata", "all"):
                output["metadata"] = extract_mod.extract_metadata(html)
            if mode in ("jsonld", "all"):
                output["jsonld"] = extract_mod.extract_jsonld(html)
            output["url"] = fetch_result["url"]
            return _tool_ok(output)

        elif name == "argo_fetch":
            fetch_v3_mod = _lazy_import("fetch_v3")
            result = fetch_v3_mod.fetch_v3(
                url=arguments["url"],
                max_chars=arguments.get("max_chars", 8000),
                timeout=arguments.get("timeout", 15),
                use_browser_fallback=True,
                force_browser=arguments.get("use_browser", False),
                actions=json.loads(arguments["actions"]) if arguments.get("actions") else None,
            )
            focus_query = arguments.get("focus")
            if focus_query and result.get("success"):
                focus_mod = _lazy_import("focus_extract")
                result["content"] = focus_mod.focus_extract(result["content"], focus_query)
                result["length"] = len(result["content"])
                result["focus_applied"] = True
            return _tool_ok(result)

        elif name == "argo_screenshot":
            import time as _time
            output = arguments.get("output_path", f"/tmp/argo_screenshot_{int(_time.time())}.png")
            full_page = arguments.get("full_page", False)
            try:
                cdp_mod = _lazy_import("chrome_cdp")
                cdp = cdp_mod.ChromeCDP(auto_start=True)
                cdp.navigate(arguments["url"])
                path = cdp.screenshot(output, full_page=full_page)
                cdp.stop()
                if path and os.path.exists(path):
                    return _tool_ok({"success": True, "screenshot": path, "url": arguments["url"]})
                return _tool_err({"success": False, "error": "screenshot failed"})
            except Exception as e:
                return _tool_err({"success": False, "error": str(e)[:200]})

        elif name == "argo_pdf":
            pdf_mod = _lazy_import("pdf_extract")
            result = pdf_mod.extract_pdf(
                url_or_path=arguments["url"],
                pages=arguments.get("pages"),
                password=arguments.get("password"),
            )
            return _tool_ok(result)

        else:
            return {"error": {"code": -32601, "message": f"Unknown tool: {name}"}}

    except Exception as e:
        return _tool_err({"error": {"code": -32000, "message": f"{type(e).__name__}: {e}"}})


# ── MCP JSON-RPC 处理 ────────────────────────────────────────────────────────

def handle_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """处理 JSON-RPC 请求。"""
    if method == "initialize":
        eng_total, eng_local, eng_remote = _engine_counts()
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "argo",
                "version": "2.2.0"
            },
            "instructions": (
                f"Argo MCP 提供 16 个工具：argo_search（{eng_total} 引擎：{eng_remote} 远程+{eng_local} 本地）、"
                "argo_research、argo_evidence、argo_clarify、argo_crawl、argo_extract、argo_fetch、"
                "argo_screenshot、argo_pdf、argo_social_search、argo_social_sentiment、"
                "argo_twitter_search、argo_reddit_search、argo_xiaohongshu_search、argo_bilibili_search、"
                "argo_weibo_search。默认紧凑 JSON（无 indent、裁剪内部字段）；argo_search 可用 verbose=true 看全量。"
                "底层：TF-IDF 路由、RRF 融合、Bocha 精排、双层缓存、成本预算。"
            ),
        }

    elif method == "tools/list":
        return {"tools": TOOLS}

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        return execute_tool(tool_name, arguments)

    elif method == "ping":
        return {}

    elif method.startswith("notifications/"):
        # 通知消息无需回复
        return None

    else:
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def run_stdio():
    """运行 MCP stdio 服务。MCP 帧协议：Content-Length: N\\r\\n\\r\\n{json}"""
    global _response_format

    sys.stderr.write("[argo-mcp] ready, waiting for stdin\n")
    sys.stderr.flush()
    # 后台预热 config/search/cache，不阻塞 initialize
    threading.Thread(target=_warm_hot_path, name="argo-mcp-warm", daemon=True).start()

    while True:
        try:
            # 读取 Content-Length 头
            header = sys.stdin.buffer.readline()
            if not header:
                sys.stderr.write("[argo-mcp] EOF on stdin, exiting\n")
                sys.stderr.flush()
                break  # EOF
            header_str = header.decode("utf-8", errors="replace").strip()
            if not header_str:
                continue
            if not header_str.startswith("Content-Length:"):
                # NDJSON 格式（Kimix 等）
                _response_format = "ndjson"
                try:
                    request = json.loads(header_str)
                except json.JSONDecodeError:
                    _send_error(None, -32700, "Parse error")
                    continue
            else:
                _response_format = "content-length"
                length = int(header_str.split(":")[1].strip())
                sys.stdin.buffer.readline()  # skip blank line
                body = sys.stdin.buffer.read(length).decode("utf-8")
                request = json.loads(body)

            method = request.get("method", "")
            params = request.get("params", {})
            request_id = request.get("id")

            response = handle_rpc(method, params)

            # 通知消息无需回复
            if response is None:
                continue

            if request_id is not None:
                _send_response({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": response
                })

        except json.JSONDecodeError:
            _send_error(None, -32700, "Parse error")
        except Exception as e:
            _send_error(None, -32000, f"Internal error: {e}")


def _send_response(response: dict):
    """发送 MCP 响应，根据客户端请求格式自动选择（紧凑 JSON）。"""
    data = _dumps(response)
    if _response_format == "ndjson":
        sys.stdout.write(data + "\n")
    else:
        encoded = data.encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded)
    sys.stdout.flush()


def _send_error(request_id, code: int, message: str):
    """发送 JSON-RPC 错误响应。"""
    _send_response({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    })

def test_mode():
    """本地测试。"""
    print("=== Argo MCP 工具测试 ===\n")

    # 测试 search
    print("--- argo_search 测试（fast模式）---")
    result = execute_tool("argo_search", {
        "query": "Python async best practices",
        "max_results": 3,
        "depth": "fast",
        "mode": "fast",
    })
    print(result["content"][0]["text"][:500])
    print()

    # 测试 clarify
    print("--- clarify 测试 ---")
    result = execute_tool("argo_clarify", {"query": "Python 吞苹果 兼容吗"})
    print(result["content"][0]["text"][:500])
    print()

    # 测试 research（快速模式）
    print("--- research 测试（fast模式）---")
    result = execute_tool("argo_research", {
        "query": "React Server Components 2025 生产环境案例",
        "num_sub_queries": 2,
        "max_results": 3,
        "depth": "fast",
        "mode": "fast",
    })
    text = result["content"][0]["text"]
    # 只打印前 500 字符
    print(text[:500])
    print()

    # 测试 evidence
    print("--- evidence 测试 ---")
    sample_results = json.dumps({
        "results": [
            {"title": "Python docs", "url": "https://docs.python.org", "snippet": "Official Python documentation", "source": "wikipedia", "score": 0.9},
            {"title": "Some blog", "url": "https://random-blog.com/python", "snippet": "Python tutorial", "source": "duckduckgo", "score": 0.6},
        ]
    })
    result = execute_tool("argo_evidence", {"query": "Python tutorial", "results_json": sample_results})
    print(result["content"][0]["text"][:500])


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--test" in sys.argv:
        test_mode()
    else:
        run_stdio()

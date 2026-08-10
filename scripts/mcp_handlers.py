#!/usr/bin/env python3
"""mcp_handlers.py — MCP 工具执行逻辑（P2-4 拆分自 mcp_server.py）。

路径引导、延迟导入缓存、结果压缩、10 个工具的 execute_tool 分派、
后台预热（含 local-seek 模块导入预热，为进程内调用铺路）。
schema 真源在 mcp_tools.py；JSON-RPC 帧处理在 mcp_transport.py。
"""

from __future__ import annotations

import concurrent.futures
import functools
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ARGO_DIR = os.path.dirname(SCRIPT_DIR)  # argo 根目录
sys.path.insert(0, SCRIPT_DIR)
# 子技能目录，供 sub-skills/local-search/ 等模块本地导入。
# append 而非 insert(0)：sub-skills 下的顶层模块（health_check 等）不得
# 劫持 scripts/ 下同名模块的解析（否则 route 的健康检查分支会静默漂移）。
SUB_SKILLS_DIR = os.path.join(ARGO_DIR, "sub-skills")
if os.path.isdir(SUB_SKILLS_DIR):
    for sub in os.listdir(SUB_SKILLS_DIR):
        sub_path = os.path.join(SUB_SKILLS_DIR, sub)
        if os.path.isdir(sub_path) and sub_path not in sys.path:
            sys.path.append(sub_path)
# 切换 CWD 到 argo 根目录，确保相对路径和子进程 work
os.chdir(ARGO_DIR)

# 启动日志（写入 stderr，不影响 stdio 通信）
sys.stderr.write("[argo-mcp] starting (lazy imports enabled)\n")
sys.stderr.flush()

_cache_instance = None
_warm_started = False
_module_cache: dict[str, Any] = {}




def _seek_py() -> str:
    """定位 local-seek 的 seek.py（argo 子技能，位于 sub-skills/ 下）。

    local-seek 已于 2026-08-05 收编为 argo 子技能，标准位置是
    SUB_SKILLS_DIR/local-seek；旧位置（skills 根目录平级）保留为回退，
    兼容尚未迁移的旧部署。结果进程内不变，lru_cache 避免重复探测。"""
    for cand in (os.path.join(SUB_SKILLS_DIR, "local-seek", "scripts", "seek.py"),
                 os.path.expanduser("~/.agents/skills/local-seek/scripts/seek.py"),
                 os.path.expanduser("~/.claude/skills/local-seek/scripts/seek.py")):
        if os.path.exists(cand):
            return cand
    return os.path.join(SUB_SKILLS_DIR, "local-seek", "scripts", "seek.py")


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
    """MCP 默认紧凑 JSON（无 indent），显著降低 token / 传输体积。"""
    if pretty:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _ok(payload: Any, pretty: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": _dumps(payload, pretty=pretty)}]}


def _trim_snippet(text: Any, n: int = 120) -> str | None:
    if not text:
        return None
    s = str(text)
    return s if len(s) <= n else s[:n]


def _compact_search_result(result: dict[str, Any], summary: bool = False) -> dict[str, Any]:
    """去掉 MCP 热路径用不到的重字段，缩小首包。"""
    snip_n = 80 if summary else 160
    out: dict[str, Any] = {
        "query": result.get("query"),
        "engine": result.get("engine"),
        "engines_used": result.get("engines_used") or result.get("engines"),
        "count": result.get("count"),
        "elapsed_ms": result.get("elapsed_ms"),
        "cached": result.get("cached"),
        "mode": result.get("mode"),
        "depth": result.get("depth"),
        "execution_tier": result.get("execution_tier"),
        "input_kind": result.get("input_kind"),
        "login_hint": result.get("login_hint"),
    }
    results = []
    for r in result.get("results") or []:
        if not isinstance(r, dict):
            continue
        item = {
            "title": r.get("title"),
            "url": r.get("url"),
            "snippet": _trim_snippet(r.get("snippet"), snip_n),
            "source": r.get("source"),
            "score": r.get("score"),
        }
        # 保留发布时间维度（仅引擎输出该字段时，避免空字段噪音）
        if r.get("published_at"):
            item["published_at"] = r["published_at"]
        # 顶包标记：recovery 替换了引擎原结果时透传，供上层识别结果已被代答
        if r.get("_recovered"):
            item["recovered"] = r["_recovered"]
        # 保留快评，丢掉大块 meta
        for k in ("selection", "absorption", "credibility_fast", "evidence_flags"):
            if k in r:
                item[k] = r[k]
        results.append(item)
    out["results"] = results
    # 引擎可观测性：精简版 engine_outcomes（engine/status/latency，去掉 detail）
    # 让 Agent 能看到「哪个引擎失败、为什么类别」，不占大体积
    outcomes = result.get("engine_outcomes") or []
    if outcomes:
        out["engine_outcomes"] = [
            {
                "engine": o.get("engine"),
                "status": o.get("status"),
                "latency_ms": o.get("latency_ms"),
                "results_count": o.get("results_count"),
            }
            for o in outcomes
            if isinstance(o, dict)
        ][:12]
        wasted = result.get("wasted_engine_ms")
        if wasted:
            out["wasted_engine_ms"] = wasted
    if result.get("early_stopped"):
        out["early_stopped"] = True
    if result.get("minhash_removed"):
        out["minhash_removed"] = result.get("minhash_removed")
    if result.get("rank_method"):
        out["rank_method"] = result.get("rank_method")
    if result.get("reranker"):
        out["reranker"] = result.get("reranker")
    # sources 沉底同构（若已有）
    sources = result.get("sources")
    if sources:
        out["sources"] = [
            {
                "ref": s.get("ref"),
                "title": s.get("title"),
                "url": s.get("url"),
                "engine": s.get("engine") or s.get("source"),
            }
            for s in sources
            if isinstance(s, dict) and s.get("url")
        ][:20]
    if result.get("errors"):
        out["errors"] = result["errors"]
    if result.get("limitations"):
        out["limitations"] = result["limitations"]
    # 语言偏好观测（精简：去掉 habit_counts 明细，保留决策用字段）
    lp = result.get("lang_pref")
    if isinstance(lp, dict):
        out["lang_pref"] = {
            "query_lang": lp.get("query_lang"),
            "system_lang": lp.get("system_lang"),
            "habit_lang": lp.get("habit_lang"),
            "prefer_langs": lp.get("prefer_langs"),
            "engine_lang": lp.get("engine_lang"),
        }
    rec = result.get("recovery")
    if isinstance(rec, dict) and rec.get("triggered"):
        out["recovery"] = {
            "recovered": rec.get("recovered"),
            "level_used": rec.get("level_used"),
            "strategy_used": rec.get("strategy_used"),
            "note": rec.get("note"),
        }
    return out


def _compact_research_result(report: dict[str, Any], summary: bool = False) -> dict[str, Any]:
    """研究包：只保留 Agent 写作需要的字段，砍掉子查询原始 dumps。"""
    snip_n = 100 if summary else 160
    keys = (
        "query", "query_original", "execution_tier", "topic_profile", "topic_profile_key",
        "discipline", "quality_gates", "report_sections", "source_grades", "disclaimer",
        "academic_discipline", "engines_used", "source_distribution", "elapsed_ms",
        "sub_query_count", "total_sources", "gaps", "mode", "sub_queries",
        "engines_priority", "vertical_engines",
    )
    out: dict[str, Any] = {k: report[k] for k in keys if k in report and report[k] is not None}

    findings = []
    for kf in report.get("key_findings") or []:
        if not isinstance(kf, dict):
            continue
        top = dict(kf.get("top_result") or {})
        if top.get("snippet"):
            top["snippet"] = _trim_snippet(top.get("snippet"), snip_n)
        findings.append({
            "aspect": kf.get("aspect"),
            "strategy": kf.get("strategy"),
            "top_result": top,
            "result_count": kf.get("result_count"),
            "citation_refs": kf.get("citation_refs"),
        })
    out["key_findings"] = findings

    sources = []
    for s in report.get("sources") or report.get("citations") or []:
        if not isinstance(s, dict) or not s.get("url"):
            continue
        sources.append({
            "ref": s.get("ref") or s.get("id"),
            "title": s.get("title"),
            "url": s.get("url"),
            "engine": s.get("engine") or s.get("source"),
            "snippet": _trim_snippet(s.get("snippet"), snip_n),
        })
        if summary and len(sources) >= 10:
            break
    out["sources"] = sources
    if not summary:
        out["citations"] = report.get("citations") or sources
        # 交叉引用截断
        crs = report.get("cross_references") or []
        out["cross_references"] = crs[:5]
    # 覆盖度 / 验证记录 / 盲区（科研方法论增强）
    if report.get("coverage_map"):
        out["coverage_map"] = report["coverage_map"]
    if report.get("verification_records"):
        out["verification_records"] = report["verification_records"][:12]
    if report.get("blind_spots"):
        out["blind_spots"] = report["blind_spots"][:8]
    return out


def _warm_local_seek() -> None:
    """预热 local-seek 模块导入（P2-4，为 P2-5 进程内调用铺路）。

    用 importlib 从文件加载 seek.py 为命名模块（模块级代码安全，无副作用），
    缓存进 _module_cache["local_seek"]；失败仅记日志，本地搜索继续走
    subprocess 兜底，不影响核心预热。
    """
    try:
        seek_py = _seek_py()
        if not os.path.exists(seek_py):
            sys.stderr.write("[argo-mcp] warm local-seek: not found\n")
            return
        spec = importlib.util.spec_from_file_location("local_seek", seek_py)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["local_seek"] = mod
        spec.loader.exec_module(mod)
        _module_cache["local_seek"] = mod
        sys.stderr.write("[argo-mcp] warm local-seek ok\n")
    except Exception as e:
        sys.stderr.write(f"[argo-mcp] warm local-seek fail: {type(e).__name__}: {e}\n")

def _warm_core_async() -> None:
    """initialize 后后台预热 search+cache，摊平首次 tools/call 延迟。"""
    global _warm_started
    if _warm_started:
        return
    _warm_started = True

    def _run() -> None:
        t0 = __import__("time").time()
        try:
            _lazy_cached("search")
            _get_cache()
            ms = int((__import__("time").time() - t0) * 1000)
            sys.stderr.write(f"[argo-mcp] warm-core ok {ms}ms\n")
        except Exception as e:
            sys.stderr.write(f"[argo-mcp] warm-core fail: {type(e).__name__}: {e}\n")
        _warm_local_seek()
        sys.stderr.flush()

    threading.Thread(target=_run, name="argo-mcp-warm", daemon=True).start()


def _resolve_research_profile(arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """解析 topic profile；全部在 argo 内，不加载外部 skill。"""
    try:
        profiles = _lazy_cached("topic_research_profiles")
    except Exception:
        return None, None
    topic = arguments.get("topic")
    key = None
    if topic:
        prof = profiles.get_profile(str(topic))
        if prof:
            # 归一化 key
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
) -> tuple[dict[str, list], list[str], list[str]]:
    """并行抓取多社交平台（MCP 热路径）。

    timeout 秒后放弃未完成平台、返回已完成部分（不抛错），
    避免单平台卡死拖垮整次搜索；超时平台以空结果 + errors 提示可观测。
    """
    platform_results: dict[str, list] = {}
    errors: list[str] = []
    engines_used: list[str] = []

    def _one(platform: str) -> tuple[str, list, str | None]:
        module_name = platform.replace("-", "_") + "_engine"
        try:
            # 走 _lazy_cached，复用进程内模块缓存，避免重复 importlib 解析
            mod = _lazy_cached(f"social_engines.{module_name}")
            return platform, mod.search(query, n=n), None
        except ImportError:
            return platform, [], f"Platform {platform} not available (module social_engines.{module_name})"
        except Exception as e:
            return platform, [], f"{platform}: {str(e)[:100]}"

    workers = min(max(len(platforms), 1), 4)
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
        # 不等待未完成平台，让卡住的线程自生自灭（各引擎内部有 10-15s 兜底超时）
        ex.shutdown(wait=False, cancel_futures=True)
    return platform_results, engines_used, errors


def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行 MCP 工具，按需导入模块。"""
    pretty = bool(arguments.get("pretty", False))
    try:
        if name == "argo_search":
            search_mod = _lazy_cached("search")
            # MCP 默认 envelope=False：减 candidates/coverage 构造开销；
            # 紧凑序列化仍由 _compact_search_result 负责
            result = search_mod.super_search(
                query=arguments["query"],
                engine=arguments.get("engine", "auto"),
                n=arguments.get("max_results", 5),
                skip_cache=arguments.get("skip_cache", False),
                timeout=arguments.get("timeout", 10),
                depth=arguments.get("depth", "fast"),
                mode=arguments.get("mode", "auto"),
                since=arguments.get("since"),
                until=arguments.get("until"),
                sort=arguments.get("sort", "relevance"),
                cache=_get_cache(),
                envelope=False,
                context="search",
            )
            # 默认精简；pretty 且 summary=false 时返回接近全量
            summary = arguments.get("summary", True)
            if summary or not pretty:
                payload = _compact_search_result(result, summary=bool(summary))
            else:
                payload = result
            return _ok(payload, pretty=pretty)

        elif name == "argo_local_search":
            # 本地文件搜索：封装 local-seek 的 seek.py（搜本机文件/记录，非联网）
            query = str(arguments.get("query", "")).strip()
            if not query:
                return _ok({"query": "", "engine": "local_files", "count": 0,
                            "results": [], "errors": ["query 不能为空"]}, pretty=pretty)
            path = str(arguments.get("path", "~"))
            max_results = max(1, min(int(arguments.get("max_results", 5) or 5), 20))
            exact = bool(arguments.get("exact", False))
            seek_py = _seek_py()
            cmd = [sys.executable, seek_py, query, "--path", path,
                   "--json", "--max", str(max_results)]
            if exact:
                cmd.append("--exact")
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except Exception as e:
                return _ok({"query": query, "engine": "local_files", "count": 0,
                            "results": [], "errors": [f"本地搜索执行失败: {e}"]}, pretty=pretty)
            if proc.returncode != 0:
                msg = (proc.stdout or proc.stderr or "").strip() or "本地搜索无结果"
                return _ok({"query": query, "engine": "local_files", "count": 0,
                            "results": [], "errors": [msg]}, pretty=pretty)
            try:
                payload = json.loads(proc.stdout)
            except Exception:
                return _ok({"query": query, "engine": "local_files", "count": 0,
                            "results": [], "errors": ["本地搜索输出解析失败"]}, pretty=pretty)
            mode = payload.get("mode", "fast")
            score = 0.9 if mode == "fast" else 0.7  # 精确命中 0.9，扩展召回 0.7
            results = []
            for r in payload.get("results") or []:
                abspath = os.path.abspath(r.get("path", ""))
                line = r.get("line") or 0
                url = f"file://{abspath}" + (f"#L{line}" if line else "")
                results.append({
                    "title": abspath,
                    "url": url,
                    "snippet": _trim_snippet(r.get("snippet"), 120),
                    "source": "local_files",
                    "score": score,
                })
            return _ok({
                "query": query,
                "engine": "local_files",
                "engines_used": ["local_files"],
                "count": len(results),
                "elapsed_ms": payload.get("elapsed_ms"),
                "cached": False,
                "mode": mode,
                "depth": None,
                "results": results,
                "errors": [],
            }, pretty=pretty)

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
                    if num_sub is None:
                        num_sub = profile.get("sub_queries", 4)
                    if max_results is None:
                        max_results = profile.get("max_results", 5)
                    if depth is None:
                        depth = profile.get("depth", "balanced")
                result = research_mod.deep_research(
                    query=arguments["query"],
                    num_sub_queries=int(num_sub or 4),
                    max_results=int(max_results or 5),
                    depth=str(depth or "balanced"),
                    mode=mode,
                    profile=profile,
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
            n = arguments.get("max_results", 5)
            platform_results, engines_used, errors = _search_social_platforms(platforms, query, n)
            if arguments.get("mode", "text") == "sentiment":
                # 舆情聚合逻辑下沉在 research.aggregate_social_sentiment
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
            max_pages = arguments.get("max_pages", 10)
            max_depth = arguments.get("max_depth", 2)
            timeout = arguments.get("timeout", 8)
            if strategy == "sitemap":
                result = crawl_mod.crawl_sitemap(arguments["url"], max_pages=max_pages, timeout=timeout)
            else:
                result = crawl_mod.crawl_bfs(arguments["url"], max_pages=max_pages, max_depth=max_depth, timeout=timeout)
            return _ok(result, pretty=pretty)

        elif name == "argo_fetch":
            if arguments.get("mode", "text") == "extract":
                # 结构化提取（原 argo_extract）：raw fetch + tables/meta/jsonld
                extract_mod = _lazy_cached("extract")
                fetch_mod = _lazy_cached("fetch_v3")
                emode = arguments.get("extract_mode", "all")
                fetch_result = fetch_mod.fetch_page_v3(arguments["url"], max_chars=50000,
                                                       timeout=arguments.get("timeout", 15), raw=True)
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
                return _ok(output, pretty=pretty)

            fetch_v3_mod = _lazy_cached("fetch_v3")
            result = fetch_v3_mod.fetch_v3(
                url=arguments["url"],
                max_chars=arguments.get("max_chars", 8000),
                timeout=arguments.get("timeout", 15),
                use_browser_fallback=True,
                force_browser=arguments.get("use_browser", False),
            )
            focus_query = arguments.get("focus")
            if focus_query and result.get("success"):
                focus_mod = _lazy_cached("focus_extract")
                result["content"] = focus_mod.focus_extract(result["content"], focus_query)
                result["length"] = len(result["content"])
                result["focus_applied"] = True
            # 默认截断 content 以控 token
            if arguments.get("summary", True) and isinstance(result.get("content"), str):
                max_c = int(arguments.get("max_chars", 8000))
                if len(result["content"]) > max_c:
                    result["content"] = result["content"][:max_c]
                    result["truncated"] = True
            return _ok(result, pretty=pretty)

        elif name == "argo_screenshot":
            import time as _time
            output = arguments.get("output_path", f"/tmp/argo_screenshot_{int(_time.time())}.png")
            full_page = arguments.get("full_page", False)
            try:
                cdp_mod = _lazy_cached("chrome_cdp")
                cdp = cdp_mod.ChromeCDP(auto_start=True)
                cdp.navigate(arguments["url"])
                path = cdp.screenshot(output, full_page=full_page)
                cdp.stop()
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

        else:
            return {"error": {"code": -32601, "message": f"Unknown tool: {name}"}}

    except Exception as e:
        return {
            "content": [{"type": "text", "text": _dumps({"error": {"code": -32000, "message": f"{type(e).__name__}: {e}"}})}],
            "isError": True
        }


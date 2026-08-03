#!/usr/bin/env python3
"""
mcp_server.py — Argo MCP 服务层

将 argo_search/argo_research/argo_evidence/argo_clarify/argo_crawl/argo_fetch/
argo_social_search 等 10 个工具暴露为 MCP tool，通过 JSON-RPC over stdio
与 Grok/Claude/Kimi 等客户端通信。

用法：
  python3 mcp_server.py                    # 启动 MCP stdio 服务
  python3 mcp_server.py --test             # 本地测试模式
"""

from __future__ import annotations

import concurrent.futures
import functools
import json
import os
import subprocess
import sys
import traceback
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ARGO_DIR = os.path.dirname(SCRIPT_DIR)  # argo 根目录
sys.path.insert(0, SCRIPT_DIR)
# 子技能目录，供 sub-skills/local-search/ 等模块本地导入
SUB_SKILLS_DIR = os.path.join(ARGO_DIR, "sub-skills")
if os.path.isdir(SUB_SKILLS_DIR):
    for sub in os.listdir(SUB_SKILLS_DIR):
        sub_path = os.path.join(SUB_SKILLS_DIR, sub)
        if os.path.isdir(sub_path) and sub_path not in sys.path:
            sys.path.insert(0, sub_path)
# 切换 CWD 到 argo 根目录，确保相对路径和子进程 work
os.chdir(ARGO_DIR)


@functools.lru_cache(maxsize=1)
def _seek_py() -> str:
    """定位 local-seek 的 seek.py。

    argo 目录本身是符号链接（~/.agents/skills/argo -> ~/argo），
    物理 cwd 与逻辑路径不一致，不能靠 dirname(ARGO_DIR) 推导 skills 根目录，
    按候选根目录逐个探测。结果进程内不变，lru_cache 避免每次调用重复探测。"""
    for root in (os.path.dirname(ARGO_DIR),
                 os.path.expanduser("~/.agents/skills"),
                 os.path.expanduser("~/.claude/skills")):
        cand = os.path.join(root, "local-seek", "scripts", "seek.py")
        if os.path.exists(cand):
            return cand
    return os.path.expanduser("~/.agents/skills/local-seek/scripts/seek.py")

# 启动日志（写入 stderr，不影响 stdio 通信）
sys.stderr.write("[argo-mcp] starting (lazy imports enabled)\n")
sys.stderr.flush()

# 延迟导入：避免启动时加载所有模块导致超时，按需导入
import importlib
import threading

def _lazy_import(module_name: str):
    """延迟导入模块，首次调用时加载（进程内缓存）。"""
    return importlib.import_module(module_name)

_cache_instance = None
_response_format = "content-length"  # 根据客户端请求自动切换
_warm_started = False
_module_cache: dict[str, Any] = {}


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
        sys.stderr.flush()

    threading.Thread(target=_run, name="argo-mcp-warm", daemon=True).start()


# ── 工具定义（MCP schema，唯一真源；改动只在这里）──────────────────────────
TOOLS = [
    {
        "name": "argo_search",
        "description": "统一搜索：约 110 引擎 TF-IDF 语义路由 + RRF 融合 + Bocha 语义精排 + 双层缓存，默认紧凑 JSON。查资料、找答案、搜新闻、学术、代码、中文内容等通用场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词（支持中英文）"},
                "engine": {"type": "string", "description": "指定搜索引擎（默认 auto，可选 octen/anysearch/exa/zhihu/eastmoney/arxiv/duckduckgo/byted/bocha/tavily/github/wikipedia/semantic_scholar/local_search 等）", "default": "auto"},
                "max_results": {"type": "integer", "description": "最大结果数（默认 5）", "default": 5, "minimum": 1, "maximum": 20},
                "depth": {"type": "string", "enum": ["fast", "balanced", "deep"], "description": "搜索深度（默认 fast）", "default": "fast"},
                "mode": {"type": "string", "enum": ["fast", "auto", "deep", "budget"], "description": "预算模式：fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制（默认 auto）", "default": "auto"},
                "skip_cache": {"type": "boolean", "description": "跳过缓存（默认 false）", "default": False},
                "summary": {"type": "boolean", "description": "精简模式：截断 snippet + 去掉重字段（默认 true，省 token）", "default": True},
                "pretty": {"type": "boolean", "description": "美化 JSON（默认 false；调试用）", "default": False},
                "timeout": {"type": "integer", "description": "超时秒数（默认 10）", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "argo_local_search",
        "description": "本地文件搜索（搜本机，非联网）：封装 local-seek，搜代码/笔记/技能库/本地记忆，统一输出（title=路径, url=file:// 带行号, source=local_files）。与 argo_search 互补（网络 vs 本机）。中文自动扩展召回。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词（支持中英文，中文自动扩展召回）"},
                "path": {"type": "string", "description": "搜索目录（默认家目录；建议缩小到 ~/.agents/skills、~/notes、~/Documents 等，更快更准）", "default": "~"},
                "max_results": {"type": "integer", "description": "最大结果数（默认 5）", "default": 5, "minimum": 1, "maximum": 20},
                "exact": {"type": "boolean", "description": "关闭中文扩展，精确匹配（默认 false）", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "argo_research",
        "description": "深度研究（argo 内建子技能，不调用外部 skill）：问题分解+多源并行+质量门禁+底部信源。学术/金融用 topic=academic|finance。日常快问请用 argo_search。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "研究查询（可以是复杂的、多步骤的问题）"},
                "topic": {"type": "string", "description": "选题 profile：ai/investment/finance/academic/tech/tool/internet/social；省略则自动推断"},
                "auto_topic": {"type": "boolean", "description": "无 topic 时是否自动推断（默认 true）", "default": True},
                "num_sub_queries": {"type": "integer", "description": "子查询数量（默认由 topic 决定，通常 4，最大 8）", "minimum": 2, "maximum": 8},
                "max_results": {"type": "integer", "description": "每个子查询最大结果数（默认5）", "default": 5},
                "depth": {"type": "string", "enum": ["fast", "balanced", "deep"], "description": "搜索深度（默认由 topic 决定，通常 balanced）"},
                "mode": {"type": "string", "enum": ["fast", "auto", "deep", "budget", "social-sentiment"], "description": "预算/模式（默认 auto；社交舆情用 social-sentiment）", "default": "auto"},
                "platforms": {"type": "string", "description": "social-sentiment 平台列表，逗号分隔"},
                "summary": {"type": "boolean", "description": "精简研究包（默认 true，省 token）", "default": True},
                "pretty": {"type": "boolean", "description": "美化 JSON（默认 false）", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "argo_evidence",
        "description": "来源可信度评估：对搜索结果进行权威性+时效性+交叉验证的综合评分，输出每个结果的可信度分解。适用于事实核查、高后果决策、学术引用等需要评估来源可靠性的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词（用于交叉验证）"},
                "results_json": {"type": "string", "description": "搜索结果 JSON 字符串（可选；为空时自动调用 super_search 搜索）。格式：{\"results\": [{\"title\": \"...\", \"url\": \"...\", \"snippet\": \"...\", \"source\": \"...\", \"score\": 0.8}]}"},
                "max_results": {"type": "integer", "description": "自动搜索时的最大结果数（默认 10，仅在 results_json 为空时有效）", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "argo_clarify",
        "description": "意图消歧：分析查询中的歧义词、多义实体，给出意图分类和推荐搜索策略。适用于查询含歧义词（如「苹果」=公司/水果、「Python」=语言/蛇）或意图不明确的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "需要消歧的搜索查询"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "argo_crawl",
        "description": "站点级爬取：通过 sitemap.xml 或 BFS 策略批量抓取站点页面，输出页面 URL、正文片段和深度。适用于整站内容审计、站内多页对比、批量抓取等场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标站点根 URL（如 https://docs.python.org/）"},
                "strategy": {"type": "string", "enum": ["sitemap", "bfs"], "description": "爬取策略（默认 bfs）", "default": "bfs"},
                "max_pages": {"type": "integer", "description": "最大抓取页面数（默认 10，sitemap 策略默认 20）", "default": 10, "minimum": 1, "maximum": 50},
                "max_depth": {"type": "integer", "description": "BFS 最大深度（默认 2）", "default": 2, "minimum": 1, "maximum": 5},
                "timeout": {"type": "integer", "description": "单页超时秒数（默认 8）", "default": 8, "minimum": 3, "maximum": 30},
            },
            "required": ["url"],
        },
    },
    {
        "name": "argo_fetch",
        "description": "智能页面抓取：HTTP 优先 + 反检测浏览器降级（Patchright/Cloudflare 绕过）。自动检测 CF 挑战/JS shell 并升级浏览器。支持 BM25 聚焦提取（focus 省 80%+ token）、内容质量信号、结构化提取（mode=extract 抽表格/元数据/JSON-LD）。适用于反爬网站、JS 渲染页、Cloudflare 保护页、页面结构化数据解析。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},
                "mode": {"type": "string", "enum": ["text", "extract"], "description": "抓取模式：text=正文/聚焦文本（默认），extract=结构化提取（表格/meta/JSON-LD）", "default": "text"},
                "extract_mode": {"type": "string", "enum": ["tables", "metadata", "jsonld", "all"], "description": "mode=extract 时的提取子模式（默认 all）", "default": "all"},
                "focus": {"type": "string", "description": "BM25 聚焦查询词（只返回相关段落，省 80%+ token；仅 mode=text）"},
                "max_chars": {"type": "integer", "description": "最大字符数（默认 8000）", "default": 8000, "minimum": 500, "maximum": 50000},
                "timeout": {"type": "integer", "description": "超时秒数（默认 15）", "default": 15, "minimum": 5, "maximum": 60},
                "use_browser": {"type": "boolean", "description": "强制使用反检测浏览器（默认 false，HTTP 失败时自动升级）", "default": False},
            },
            "required": ["url"],
        },
    },
    {
        "name": "argo_screenshot",
        "description": "页面截图：捕获网页为图片（PNG），供多模态 agent 分析页面布局、验证渲染结果、存档网页快照。支持全页截图和视口截图。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},
                "full_page": {"type": "boolean", "description": "全页截图（默认 false，仅当前视口）", "default": False},
                "output_path": {"type": "string", "description": "输出路径（默认 /tmp/argo_screenshot_<timestamp>.png）"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "argo_pdf",
        "description": "PDF 结构化提取：将 PDF 转为 Markdown（含表格、目录、元数据、CID 损坏检测）。支持 URL 下载和本地文件路径。依赖 pdfplumber 或 PyMuPDF（自动选择）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "PDF URL 或本地文件路径"},
                "pages": {"type": "string", "description": "页码范围（如 \"1-5\" 或 \"1,3,5\"，默认全部）"},
                "password": {"type": "string", "description": "加密 PDF 密码（可选）"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "argo_social_search",
        "description": "社交平台搜索：跨平台搜索 Twitter/X、Reddit、小红书、B站、微博等 UGC 内容，返回帖子与互动数据。mode=sentiment 输出舆情聚合（互动汇总+平台分布+代表性帖子）。适用于舆情分析、热门话题、产品口碑、竞品用户反馈等场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "mode": {"type": "string", "enum": ["text", "sentiment"], "description": "text=帖子列表（默认），sentiment=舆情聚合分析", "default": "text"},
                "platforms": {"type": "string", "description": "平台列表，逗号分隔（默认 twitter,reddit,xiaohongshu）。可选：twitter,reddit,xiaohongshu,bilibili,weibo", "default": "twitter,reddit,xiaohongshu"},
                "max_results": {"type": "integer", "description": "每个平台最大结果数（默认 5）", "default": 5, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
]



# ── 工具执行（延迟导入）──────────────────────────────────────────────────────

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
                platforms_str = arguments.get("platforms", "twitter,reddit,xiaohongshu")
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
            platforms_str = arguments.get("platforms", "twitter,reddit,xiaohongshu")
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
                fetch_mod = _lazy_cached("fetch")
                emode = arguments.get("extract_mode", "all")
                fetch_result = fetch_mod.fetch_page(arguments["url"], max_chars=50000,
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


# ── MCP JSON-RPC 处理 ────────────────────────────────────────────────────────

def handle_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """处理 JSON-RPC 请求。"""
    if method == "initialize":
        _warm_core_async()
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "argo",
                "version": "2.5.1"
            },
            # 短指令：降 tools 上下文；细节在 tool schema
            "instructions": (
                "Argo：日常用 argo_search（默认精简 JSON，信源在 sources）；"
                "深度研究只用 argo_research（内建 academic/finance topic，不调用外部 skill）；"
                "核验 argo_evidence；消歧 argo_clarify；正文 argo_fetch（mode=extract 可提取表格/元数据/JSON-LD）。"
                "社交用 argo_social_search（mode=sentiment 做舆情聚合）。缓存+RRF+成本路由已内建。"
                "本地文件/记录搜索用 argo_local_search（搜本机，非联网，与 argo_search 互补）。"
            ),
        }

    elif method == "tools/list":
        _warm_core_async()
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
    import sys, os, time as _time

    global _response_format

    sys.stderr.write("[argo-mcp] ready, waiting for stdin\n")
    sys.stderr.flush()

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
    """发送 MCP 响应，根据客户端请求格式自动选择。紧凑 separators 省传输体积。"""
    data = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    if _response_format == "ndjson":
        sys.stdout.write(data + "\n")
    else:
        encoded = data.encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded)
    sys.stdout.flush()


def _send_error(request_id, code: int, message: str):
    """发送 JSON-RPC 错误响应。"""
    resp = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message}
    }
    _send_response(resp)

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

    # 测试 local search（本地文件）
    print("--- argo_local_search 测试 ---")
    result = execute_tool("argo_local_search", {
        "query": "数据抓取",
        "path": os.path.expanduser("~/.agents/skills"),
        "max_results": 3,
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
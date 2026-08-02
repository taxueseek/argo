#!/usr/bin/env python3
"""
mcp_server.py — Argo MCP 服务层

将 argo_search/argo_research/argo_evidence/argo_clarify/argo_crawl/argo_extract
六个工具暴露为 MCP tool，通过 JSON-RPC over stdio 与 Grok/Claude/Kimi 等客户端通信。

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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# 启动日志（写入 stderr，不影响 stdio 通信）
sys.stderr.write("[argo-mcp] starting (lazy imports, deferred config)\n")
sys.stderr.flush()

# ── 延迟导入 / 缓存 ──────────────────────────────────────────────────────────

_module_cache: dict[str, Any] = {}
_cache_instance = None
_response_format = "content-length"  # 根据客户端请求自动切换
_JSON_SEP = (",", ":")  # 紧凑 JSON，降低序列化与 LLM token 开销

# 结果字段白名单（MCP 默认裁剪内部打分/路由噪声）
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


def _lazy_import(module_name: str):
    """延迟导入模块，进程内缓存。"""
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


def _dumps(obj: Any) -> str:
    """紧凑 JSON 序列化（无 indent）。"""
    return json.dumps(obj, ensure_ascii=False, separators=_JSON_SEP, default=str)


def _tool_ok(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": _dumps(payload)}]}


def _tool_err(payload: Any) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _dumps(payload)}],
        "isError": True,
    }


def _compact_item(item: dict[str, Any], *, summary: bool, snippet_max: int) -> dict[str, Any]:
    out = {k: item[k] for k in _RESULT_FIELDS if k in item and item[k] is not None}
    # 社交字段
    if "social_meta" in item and item["social_meta"] is not None:
        out["social_meta"] = item["social_meta"]
    snip = out.get("snippet")
    if isinstance(snip, str):
        cap = 80 if summary else snippet_max
        if len(snip) > cap:
            out["snippet"] = snip[:cap]
    return out


def _compact_for_mcp(
    data: dict[str, Any],
    *,
    summary: bool = False,
    verbose: bool = False,
    snippet_max: int = 300,
) -> dict[str, Any]:
    """裁剪 MCP 返回体：去掉 tfidf/outcomes 等内部字段，控制 snippet 长度。"""
    if verbose or not isinstance(data, dict):
        return data
    out: dict[str, Any] = {k: data[k] for k in _TOP_FIELDS if k in data}
    if "results" in data and isinstance(data["results"], list):
        out["results"] = [
            _compact_item(r, summary=summary, snippet_max=snippet_max)
            if isinstance(r, dict) else r
            for r in data["results"]
        ]
    # 社交舆情等结构
    if "posts" in data and isinstance(data["posts"], list):
        out["posts"] = [
            _compact_item(r, summary=summary, snippet_max=snippet_max)
            if isinstance(r, dict) else r
            for r in data["posts"]
        ]
        for k in ("platforms", "platform_breakdown", "total_posts", "engagement_totals"):
            if k in data:
                out[k] = data[k]
    if "source" in data and "source" not in out:
        out["source"] = data["source"]
    return out


def _search_platforms(
    platforms: list[str], query: str, n: int
) -> tuple[dict[str, list], list[str], list[str]]:
    """并行调用社交引擎，返回 (platform_results, engines_used, errors)。

    platform_results 按请求 platforms 顺序保留键；engines_used 同序。
    """
    by_platform: dict[str, list] = {}
    errors: list[str] = []

    def _one(platform: str) -> tuple[str, list | None, str | None]:
        module_name = platform.replace("-", "_") + "_engine"
        try:
            mod = importlib.import_module(f"social_engines.{module_name}")
            return platform, mod.search(query, n=n), None
        except ImportError:
            return platform, None, f"Platform {platform} not available (module social_engines.{module_name})"
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


# ── 引擎统计（延迟：不在 import 时读 config.yaml） ─────────────────────────────

_engine_counts_cache: tuple[int, int, int] | None = None


def _engine_counts() -> tuple[int, int, int]:
    """返回 (启用引擎总数, 本地引擎数, 远程引擎数)。首次调用才加载 config。"""
    global _engine_counts_cache
    if _engine_counts_cache is not None:
        return _engine_counts_cache
    try:
        cfg = _lazy_import("config")
        engines = cfg.get_engines()
        local = sum(1 for name in engines if name.startswith("local_"))
        _engine_counts_cache = (len(engines), local, len(engines) - local)
    except Exception:
        _engine_counts_cache = (61, 21, 40)  # 兜底
    return _engine_counts_cache


def _warm_hot_path() -> None:
    """后台预热 search/cache，缩短首次 tools/call 延迟。"""
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


# ── 工具定义（MCP schema；描述不依赖 import 时 config） ───────────────────────

TOOLS = [
    {
        "name": "argo_search",
        "description": "统一搜索引擎：多引擎（远程+本地）统一搜索，支持 TF-IDF 语义路由 + RRF 多引擎融合 + Bocha 语义精排 + 双层缓存。默认返回紧凑字段（title/url/snippet/source）。适用于查资料、找答案、搜新闻、学术检索、代码搜索、中文内容搜索等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询词（支持中英文）"
                },
                "engine": {
                    "type": "string",
                    "description": "指定搜索引擎（默认 auto，可选 anysearch/zhihu/eastmoney/arxiv/duckduckgo/byted/bocha/tavily/github/wikipedia/semantic_scholar/local_search 等）",
                    "default": "auto"
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大结果数（默认 5）",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20
                },
                "depth": {
                    "type": "string",
                    "enum": ["fast", "balanced", "deep"],
                    "description": "搜索深度（默认 fast）",
                    "default": "fast"
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "auto", "deep", "budget"],
                    "description": "预算模式：fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制（默认 auto）",
                    "default": "auto"
                },
                "skip_cache": {
                    "type": "boolean",
                    "description": "跳过缓存（默认 false）",
                    "default": False
                },
                "summary": {
                    "type": "boolean",
                    "description": "精简模式：snippet 截断到 80 字符，进一步节省 LLM token（默认 false）",
                    "default": False
                },
                "verbose": {
                    "type": "boolean",
                    "description": "完整模式：保留路由/打分/可信度等内部字段（默认 false，MCP 已默认紧凑）",
                    "default": False
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_research",
        "description": "深度研究：将复杂查询分解为子问题，多源并行采集，输出综合报告+引用+知识缺口。适用于学术综述、事实核查、竞品分析、技术选型等需要多步搜索的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "研究查询（可以是复杂的、多步骤的问题）"
                },
                "num_sub_queries": {
                    "type": "integer",
                    "description": "子查询数量（默认4，最大8）",
                    "default": 4,
                    "minimum": 2,
                    "maximum": 8
                },
                "max_results": {
                    "type": "integer",
                    "description": "每个子查询最大结果数（默认5）",
                    "default": 5
                },
                "depth": {
                    "type": "string",
                    "enum": ["fast", "balanced", "deep"],
                    "description": "搜索深度（默认balanced）",
                    "default": "balanced"
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "auto", "deep", "budget"],
                    "description": "预算模式（默认auto）",
                    "default": "auto"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_evidence",
        "description": "来源可信度评估：对搜索结果进行权威性+时效性+交叉验证的综合评分，输出每个结果的可信度分解。适用于事实核查、高后果决策、学术引用等需要评估来源可靠性的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询词（用于交叉验证）"
                },
                "results_json": {
                    "type": "string",
                    "description": "搜索结果 JSON 字符串（可选；为空时自动调用 super_search 搜索）。格式：{\"results\": [{\"title\": \"...\", \"url\": \"...\", \"snippet\": \"...\", \"source\": \"...\", \"score\": 0.8}]}"
                },
                "max_results": {
                    "type": "integer",
                    "description": "自动搜索时的最大结果数（默认 10，仅在 results_json 为空时有效）",
                    "default": 10
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_clarify",
        "description": "意图消歧：分析查询中的歧义词、多义实体，给出意图分类和推荐搜索策略。适用于查询含歧义词（如「苹果」=公司/水果、「Python」=语言/蛇）或意图不明确的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "需要消歧的搜索查询"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_crawl",
        "description": "站点级爬取：通过 sitemap.xml 或 BFS 策略批量抓取站点页面，输出页面 URL、正文片段和深度。适用于整站内容审计、站内多页对比、批量抓取等场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "目标站点根 URL（如 https://docs.python.org/）"
                },
                "strategy": {
                    "type": "string",
                    "enum": ["sitemap", "bfs"],
                    "description": "爬取策略（默认 bfs）",
                    "default": "bfs"
                },
                "max_pages": {
                    "type": "integer",
                    "description": "最大抓取页面数（默认 10，sitemap 策略默认 20）",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50
                },
                "max_depth": {
                    "type": "integer",
                    "description": "BFS 最大深度（默认 2）",
                    "default": 2,
                    "minimum": 1,
                    "maximum": 5
                },
                "timeout": {
                    "type": "integer",
                    "description": "单页超时秒数（默认 8）",
                    "default": 8,
                    "minimum": 3,
                    "maximum": 30
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "argo_extract",
        "description": "结构化数据提取：从页面 HTML 中抽取表格、<meta> 元数据、OpenGraph、JSON-LD 等结构化信息。适用于价格表抽取、SEO 元数据分析、富媒体结构化数据解析等场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "目标页面 URL"
                },
                "mode": {
                    "type": "string",
                    "enum": ["tables", "metadata", "jsonld", "all"],
                    "description": "提取模式（默认 all）",
                    "default": "all"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "argo_fetch",
        "description": "智能页面抓取：HTTP 优先 + 反检测浏览器降级（Patchright/Cloudflare 绕过）。自动检测 CF 挑战/JS shell 并升级浏览器。支持 BM25 聚焦提取（focus 参数省 80%+ token）、页面交互（actions）、内容质量信号（content_ok/page_type/quality_score）。适用于反爬网站、JS 渲染页、Cloudflare 保护页。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "目标 URL"
                },
                "focus": {
                    "type": "string",
                    "description": "BM25 聚焦查询词（只返回相关段落，省 80%+ token）"
                },
                "max_chars": {
                    "type": "integer",
                    "description": "最大字符数（默认 8000）",
                    "default": 8000,
                    "minimum": 500,
                    "maximum": 50000
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数（默认 15）",
                    "default": 15,
                    "minimum": 5,
                    "maximum": 60
                },
                "use_browser": {
                    "type": "boolean",
                    "description": "强制使用反检测浏览器（默认 false，HTTP 失败时自动升级）",
                    "default": False
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "argo_screenshot",
        "description": "页面截图：捕获网页为图片（PNG），供多模态 agent 分析页面布局、验证渲染结果、存档网页快照。支持全页截图和视口截图。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "目标 URL"
                },
                "full_page": {
                    "type": "boolean",
                    "description": "全页截图（默认 false，仅当前视口）",
                    "default": False
                },
                "output_path": {
                    "type": "string",
                    "description": "输出路径（默认 /tmp/argo_screenshot_<timestamp>.png）"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "argo_pdf",
        "description": "PDF 结构化提取：将 PDF 转为 Markdown（含表格、目录、元数据、CID 损坏检测）。支持 URL 下载和本地文件路径。依赖 pdfplumber 或 PyMuPDF（自动选择）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "PDF URL 或本地文件路径"
                },
                "pages": {
                    "type": "string",
                    "description": "页码范围（如 \"1-5\" 或 \"1,3,5\"，默认全部）"
                },
                "password": {
                    "type": "string",
                    "description": "加密 PDF 密码（可选）"
                }
            },
            "required": ["url"]
        }
    },
    # ── 社交平台工具 ─────────────────────────────────────────────────────────
    {
        "name": "argo_social_search",
        "description": "社交平台搜索：跨平台搜索 Twitter/X、Reddit、小红书、B站、微博等社交媒体内容。返回 UGC 帖子、评论、互动数据（点赞/转发/收藏）。适用于舆情分析、热门话题、用户讨论等场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "platforms": {
                    "type": "string",
                    "description": "平台列表，逗号分隔（默认 twitter,reddit,xiaohongshu）。可选：twitter,reddit,xiaohongshu,bilibili,weibo",
                    "default": "twitter,reddit,xiaohongshu"
                },
                "max_results": {
                    "type": "integer",
                    "description": "每个平台最大结果数（默认 5）",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_social_sentiment",
        "description": "社交舆情分析：跨平台 UGC 情绪与讨论分析。聚合多平台帖子，输出互动数据汇总、高频话题、代表性内容。适用于产品口碑、事件舆情、竞品用户反馈等场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "研究查询"},
                "platforms": {
                    "type": "string",
                    "description": "平台列表，逗号分隔（默认 twitter,reddit,xiaohongshu）",
                    "default": "twitter,reddit,xiaohongshu"
                },
                "max_results": {
                    "type": "integer",
                    "description": "每个平台最大结果数（默认 5）",
                    "default": 5
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_twitter_search",
        "description": "Twitter/X 搜索：搜索推文、话题、用户。支持 nitter 公开实例（零认证）和 twitter CLI。返回推文内容、互动数据（点赞/转发/回复）、作者信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "max_results": {"type": "integer", "description": "最大结果数（默认 5）", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_reddit_search",
        "description": "Reddit 搜索：搜索帖子、subreddit、评论。使用 Reddit JSON API（无需认证）。返回帖子标题、内容、点赞数、评论数、subreddit 信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "max_results": {"type": "integer", "description": "最大结果数（默认 5）", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_xiaohongshu_search",
        "description": "小红书搜索：搜索笔记、话题、用户。需先通过 xhs login 登录。返回笔记标题、描述、点赞/收藏/评论数、作者信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "max_results": {"type": "integer", "description": "最大结果数（默认 5）", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_bilibili_search",
        "description": "B站搜索：搜索视频、UP主、弹幕。使用 B站公开搜索 API（无需认证）。返回视频标题、描述、播放量、弹幕数、点赞数、UP主信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "max_results": {"type": "integer", "description": "最大结果数（默认 5）", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_weibo_search",
        "description": "微博搜索：搜索帖子、话题、热门内容。使用微博公开搜索 API（无需认证）。返回帖子内容、点赞/转发/评论数、作者信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "max_results": {"type": "integer", "description": "最大结果数（默认 5）", "default": 5}
            },
            "required": ["query"]
        }
    },
]


# ── 工具执行（延迟导入）──────────────────────────────────────────────────────

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
            payload = _compact_for_mcp(
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
            return _tool_ok(_compact_for_mcp(output, summary=bool(arguments.get("summary", False))))

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
            return _tool_ok(_compact_for_mcp(output, summary=bool(arguments.get("summary", False))))

        elif name == "argo_twitter_search":
            from social_engines.twitter_engine import search as twitter_search
            results = twitter_search(arguments["query"], arguments.get("max_results", 5))
            return _tool_ok(_compact_for_mcp({"results": results, "source": "twitter", "count": len(results)}))

        elif name == "argo_reddit_search":
            from social_engines.reddit_engine import search as reddit_search
            results = reddit_search(arguments["query"], arguments.get("max_results", 5))
            return _tool_ok(_compact_for_mcp({"results": results, "source": "reddit", "count": len(results)}))

        elif name == "argo_xiaohongshu_search":
            from social_engines.xiaohongshu_engine import search as xhs_search
            results = xhs_search(arguments["query"], arguments.get("max_results", 5))
            return _tool_ok(_compact_for_mcp({"results": results, "source": "xiaohongshu", "count": len(results)}))

        elif name == "argo_bilibili_search":
            from social_engines.bilibili_engine import search as bilibili_search
            results = bilibili_search(arguments["query"], arguments.get("max_results", 5))
            return _tool_ok(_compact_for_mcp({"results": results, "source": "bilibili", "count": len(results)}))

        elif name == "argo_weibo_search":
            from social_engines.weibo_engine import search as weibo_search
            results = weibo_search(arguments["query"], arguments.get("max_results", 5))
            return _tool_ok(_compact_for_mcp({"results": results, "source": "weibo", "count": len(results)}))

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

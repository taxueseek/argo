#!/usr/bin/env python3
"""mcp_tools.py — MCP 工具 schema 唯一真源（P2-4 拆分自 mcp_server.py）。

10 个工具的 inputSchema 只在本模块维护；mcp_transport 的 tools/list 与
mcp_server 的兼容导出均从本模块取。改 schema 只动这一个文件。
"""

from __future__ import annotations

TOOLS = [
    {
        "name": "argo_search",
        "description": "统一搜索：多语言路由 + 约 120+ 引擎 TF-IDF + RRF 融合 + 垂直域（金融/影视/体育等）+ 双层缓存，默认紧凑 JSON。查资料、找答案、搜新闻、学术、代码、多语言内容等通用场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词（支持中英文）"},
                "engine": {"type": "string", "description": "指定搜索引擎（默认 auto，可选 octen/anysearch/exa/zhihu/eastmoney/arxiv/duckduckgo/byted/bocha/tavily/github/wikipedia/semantic_scholar/local_search 等）", "default": "auto"},
                "max_results": {"type": "integer", "description": "最大结果数（默认 5）", "default": 5, "minimum": 1, "maximum": 20},
                "depth": {"type": "string", "enum": ["fast", "balanced", "deep"], "description": "搜索深度（默认 fast）", "default": "fast"},
                "mode": {"type": "string", "enum": ["fast", "auto", "deep", "budget"], "description": "预算模式：fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制（默认 auto）", "default": "auto"},
                "skip_cache": {"type": "boolean", "description": "跳过缓存（默认 false）", "default": False},
                "since": {"type": "string", "description": "发布时间下限（如 7d / 2026-08-01），下推到支持时间窗的引擎"},
                "until": {"type": "string", "description": "发布时间上限（如 7d / 2026-08-01），下推到支持时间窗的引擎"},
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
        "description": "社交平台搜索：跨平台搜索知乎、Hacker News、B站、V2EX、Twitter/X、Reddit、小红书、微博等 UGC 内容，返回帖子与互动数据。mode=sentiment 输出舆情聚合（互动汇总+平台分布+代表性帖子）。适用于舆情分析、热门话题、产品口碑、竞品用户反馈等场景。注：hackernews/v2ex/zhihu/bilibili 零密钥直连；twitter/reddit/weibo/xiaohongshu 依赖外部登录态或第三方 API，可能返回空。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "mode": {"type": "string", "enum": ["text", "sentiment"], "description": "text=帖子列表（默认），sentiment=舆情聚合分析", "default": "text"},
                "platforms": {"type": "string", "description": "平台列表，逗号分隔（默认 hackernews,zhihu,bilibili，均为零密钥可用）。可选：hackernews,v2ex,zhihu,bilibili,twitter,reddit,xiaohongshu,weibo", "default": "hackernews,zhihu,bilibili"},
                "max_results": {"type": "integer", "description": "每个平台最大结果数（默认 5）", "default": 5, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
]


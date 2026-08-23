#!/usr/bin/env python3
"""mcp_tools.py — MCP 工具 schema 唯一真源（P2-4 拆分自 mcp_server.py）。

10 个工具的 inputSchema 只在本模块维护；mcp_transport 的 tools/list 与
mcp_server 的兼容导出均从本模块取。改 schema 只动这一个文件。

schema 设计原则（质量第一，token 第二）：
- 工具 description：第一句「做什么」，第二句「何时用 / 关键注意」。
  删除内部实现机制（引擎数、TF-IDF/RRF、协议文件引用）——模型不靠这些决策。
- 参数 description：语义 + 何时用。默认值由 schema 的 default 字段表达，
  不在 description 里重复；删除「下推到引擎」类实现细节。
- 调试/运行配置（pretty / skip_cache / timeout）不暴露给模型：
  这些不是模型决策输入，由 server 层环境变量接管（见 mcp_handlers.execute_tool：
  ARGO_MCP_PRETTY / ARGO_MCP_SKIP_CACHE / ARGO_MCP_TIMEOUT[_CRAWL|_FETCH]）。
  功能参数全部保留，质量不受影响。
"""

from __future__ import annotations

TOOLS = [
    {
        "name": "argo_search",
        "description": "统一网络搜索：多引擎融合、支持时间过滤与排序，返回带来源与摘要的结果。查资料、找答案、搜新闻/学术/代码/多语言内容等通用场景的首选工具。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "engine": {"type": "string", "description": "指定搜索引擎（auto 自动选择；可选 octen/anysearch/exa/zhihu/eastmoney/arxiv/duckduckgo/byted/bocha/tavily/github/wikipedia/semantic_scholar/local_search 等）", "default": "auto"},
                "max_results": {"type": "integer", "description": "返回结果条数", "default": 5, "minimum": 1, "maximum": 20},
                "depth": {"type": "string", "enum": ["fast", "balanced", "deep"], "description": "搜索深度：fast 快、deep 全（慢）", "default": "fast"},
                "mode": {"type": "string", "enum": ["fast", "auto", "deep", "budget"], "description": "预算模式：fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制", "default": "auto"},
                "since": {"type": "string", "description": "发布时间下限，如 7d / 2026-08-01"},
                "until": {"type": "string", "description": "发布时间上限，如 7d / 2026-08-01"},
                "sort": {"type": "string", "enum": ["relevance", "oldest", "newest"], "description": "排序：relevance=相关度, oldest=最早在前, newest=最新在前", "default": "relevance"},
                "summary": {"type": "boolean", "description": "精简输出：截断摘要、去掉重字段，省 token", "default": True},
            },
            "required": ["query"],
        },
    },
    {
        "name": "argo_local_search",
        "description": "本机文件/笔记/代码搜索（非联网）：返回文件路径与行号。搜本地资料时用，与网络搜索互补。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词（中文自动扩展召回）"},
                "path": {"type": "string", "description": "搜索目录；缩小范围更快更准", "default": "~"},
                "max_results": {"type": "integer", "description": "返回结果条数", "default": 5, "minimum": 1, "maximum": 20},
                "exact": {"type": "boolean", "description": "精确匹配，关闭中文扩展", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "argo_research",
        "description": "深度研究取证：多子查询并行检索，返回带来源/覆盖/缺口/质量门禁的 dossier。适合复杂、多步骤、需要交叉验证的研究问题；简单快问用 argo_search。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "研究问题（可以是复杂、多步骤的）"},
                "topic": {"type": "string", "description": "选题 profile：ai/investment/finance/academic/tech/tool/internet/social；省略则自动推断"},
                "auto_topic": {"type": "boolean", "description": "无 topic 时是否自动推断", "default": True},
                "num_sub_queries": {"type": "integer", "description": "子查询数量；有 work_packages 时忽略", "minimum": 2, "maximum": 8},
                "max_results": {"type": "integer", "description": "每个子查询的结果条数", "default": 5},
                "depth": {"type": "string", "enum": ["fast", "balanced", "deep"], "description": "搜索深度", "default": "balanced"},
                "mode": {"type": "string", "enum": ["fast", "auto", "deep", "budget", "social-sentiment"], "description": "预算/模式；社交舆情用 social-sentiment", "default": "auto"},
                "work_packages": {"type": "string", "description": "工作包 JSON 数组：id, question, query?, depends_on?。有则跳过扩词，按依赖分阶段取证"},
                "platforms": {"type": "string", "description": "social-sentiment 平台列表，逗号分隔"},
                "summary": {"type": "boolean", "description": "精简研究包，省 token", "default": True},
            },
            "required": ["query"],
        },
    },
    {
        "name": "argo_evidence",
        "description": "来源可信度评估：对搜索结果做权威性+时效性+交叉验证评分，输出逐条可信度分解。用于事实核查、高后果决策、学术引用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词（用于交叉验证）"},
                "results_json": {"type": "string", "description": "待评估的搜索结果 JSON（为空时自动搜索）。格式：{\"results\": [{\"title\":\"...\",\"url\":\"...\",\"snippet\":\"...\",\"source\":\"...\",\"score\":0.8}]}"},
                "max_results": {"type": "integer", "description": "自动搜索时的结果条数", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "argo_clarify",
        "description": "意图消歧：识别查询中的歧义词与多义实体，给出意图分类与推荐搜索策略。查询含歧义词（苹果=公司/水果）或意图不明时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "需要消歧的查询"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "argo_crawl",
        "description": "站点级爬取：按 sitemap 或 BFS 批量抓取整站页面，返回 URL、正文片段与深度。适合整站内容审计、站内多页对比。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "站点根 URL"},
                "strategy": {"type": "string", "enum": ["sitemap", "bfs"], "description": "爬取策略", "default": "bfs"},
                "max_pages": {"type": "integer", "description": "最大抓取页数", "default": 10, "minimum": 1, "maximum": 50},
                "max_depth": {"type": "integer", "description": "BFS 最大深度", "default": 2, "minimum": 1, "maximum": 5},
            },
            "required": ["url"],
        },
    },
    {
        "name": "argo_fetch",
        "description": "智能页面抓取：HTTP 优先，遇 Cloudflare/JS 渲染自动升级反检测浏览器。支持 BM25 聚焦提取（focus 省 80%+ token）与结构化提取（mode=extract 抽表格/元数据/JSON-LD）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},
                "mode": {"type": "string", "enum": ["text", "extract"], "description": "text=正文/聚焦文本, extract=结构化提取", "default": "text"},
                "extract_mode": {"type": "string", "enum": ["tables", "metadata", "jsonld", "all"], "description": "mode=extract 时的提取子模式", "default": "all"},
                "focus": {"type": "string", "description": "BM25 聚焦查询词：只返回相关段落，省 token（仅 mode=text）"},
                "max_chars": {"type": "integer", "description": "返回正文最大字符数", "default": 8000, "minimum": 500, "maximum": 50000},
                "use_browser": {"type": "boolean", "description": "强制用反检测浏览器（HTTP 失败时已自动升级）", "default": False},
            },
            "required": ["url"],
        },
    },
    {
        "name": "argo_screenshot",
        "description": "网页截图（PNG）：分析页面布局、验证渲染结果、存档快照。支持全页与视口截图。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},
                "full_page": {"type": "boolean", "description": "全页截图（默认仅当前视口）", "default": False},
                "output_path": {"type": "string", "description": "输出路径（默认 /tmp/argo_screenshot_<timestamp>.png）"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "argo_pdf",
        "description": "PDF 结构化提取：转为 Markdown（含表格、目录、元数据），支持 URL 下载与本地文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "PDF URL 或本地文件路径"},
                "pages": {"type": "string", "description": "页码范围，如 \"1-5\" 或 \"1,3,5\"（默认全部）"},
                "password": {"type": "string", "description": "加密 PDF 密码"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "argo_social_search",
        "description": "社交平台搜索：知乎、Hacker News、B站、V2EX、Twitter/X、Reddit、小红书、微博等 UGC 内容。mode=sentiment 输出舆情聚合。注意：hackernews/v2ex/zhihu/bilibili 零密钥可用；twitter/reddit/weibo/xiaohongshu 依赖登录态或第三方 API，可能返回空。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "mode": {"type": "string", "enum": ["text", "sentiment"], "description": "text=帖子列表, sentiment=舆情聚合分析", "default": "text"},
                "platforms": {"type": "string", "description": "平台列表，逗号分隔；可选 hackernews,v2ex,zhihu,bilibili,twitter,reddit,xiaohongshu,weibo", "default": "hackernews,zhihu,bilibili"},
                "max_results": {"type": "integer", "description": "每个平台的结果条数", "default": 5, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
]

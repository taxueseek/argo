#!/usr/bin/env python3
"""mcp_tools — Argo MCP tool schema 定义。"""

from __future__ import annotations

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
        "name": "argo_fetch",
        "description": "智能页面抓取：HTTP 优先 + 反检测浏览器降级（Patchright/Cloudflare 绕过）。自动检测 CF 挑战/JS shell 并升级浏览器。支持 BM25 聚焦提取（focus 参数省 80%+ token）、页面交互（actions）、内容质量信号（content_ok/page_type/quality_score）。适用于反爬网站、JS 渲染页、Cloudflare 保护页。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "目标 URL"
                },
                "mode": {
                    "type": "string",
                    "enum": ["text", "extract"],
                    "description": "抓取模式：text=正文/聚焦文本（默认），extract=结构化提取（表格/meta/JSON-LD）",
                    "default": "text"
                },
                "extract_mode": {
                    "type": "string",
                    "enum": ["tables", "metadata", "jsonld", "all"],
                    "description": "mode=extract 时的提取子模式（默认 all）",
                    "default": "all"
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
        "description": "社交平台搜索：跨平台搜索 Twitter/X、Reddit、小红书、B站、微博等 UGC 内容，返回帖子与互动数据。mode=sentiment 输出舆情聚合（互动汇总+平台分布+代表性帖子）。适用于舆情分析、热门话题、产品口碑、竞品用户反馈等场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "mode": {
                    "type": "string",
                    "enum": ["text", "sentiment"],
                    "description": "text=帖子列表（默认），sentiment=舆情聚合分析",
                    "default": "text"
                },
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
]


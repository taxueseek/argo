// 本文件由 scripts/gen_native_tools.py 从 schema 唯一真源
// scripts/mcp_tools.py 生成 —— 勿手改；改 schema 后重新生成，
// tests/test_native_tools_sync.py 负责漂移门禁。
//
// 说明：
//   - description / parameters 与 MCP 模型可见 schema 完全一致
//   - allowed = parameters.properties 键序（原生侧 pickArgs 白名单）
//   - 不含 argo_research：分钟级编排不适合 CLI 单发（默认超时会中断），
//     走 MCP 形态或 wide_research 入口
//
export const NATIVE_TOOLS = {
  "argo_search": {
    "description": "统一网络搜索：多引擎融合、支持时间过滤与域名过滤，返回带来源与摘要的结果。查资料、找答案、搜新闻/学术/代码/多语言内容等通用场景的首选工具。",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "搜索查询词"
        },
        "engine": {
          "type": "string",
          "description": "指定搜索引擎（auto 自动选择；可选 octen/anysearch/exa/zhihu/eastmoney/arxiv/duckduckgo/byted/bocha/tavily/github/wikipedia/semantic_scholar/local_search 等）",
          "default": "auto"
        },
        "max_results": {
          "type": "integer",
          "description": "返回结果条数",
          "default": 5,
          "minimum": 1,
          "maximum": 20
        },
        "depth": {
          "type": "string",
          "enum": [
            "fast",
            "balanced",
            "deep"
          ],
          "description": "搜索深度：fast 快、deep 全（慢）",
          "default": "fast"
        },
        "mode": {
          "type": "string",
          "enum": [
            "fast",
            "auto",
            "deep",
            "budget"
          ],
          "description": "预算模式：fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制",
          "default": "auto"
        },
        "since": {
          "type": "string",
          "description": "发布时间下限，如 7d / 2026-08-01"
        },
        "until": {
          "type": "string",
          "description": "发布时间上限，如 7d / 2026-08-01"
        },
        "sort": {
          "type": "string",
          "enum": [
            "relevance",
            "oldest",
            "newest"
          ],
          "description": "排序：relevance=相关度, oldest=最早在前, newest=最新在前",
          "default": "relevance"
        },
        "include_domains": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "仅保留这些域名（含子域），如 [\"github.com\"]",
          "default": []
        },
        "exclude_domains": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "排除这些域名（含子域），如 [\"pinterest.com\"]",
          "default": []
        },
        "include_local": {
          "type": "boolean",
          "description": "并入本机文件命中（seek 结果尾部，source=local_files，不参与融合评分；默认关）",
          "default": false
        },
        "summary": {
          "type": "boolean",
          "description": "精简输出：截断摘要、去掉重字段，省 token",
          "default": true
        }
      },
      "required": [
        "query"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "query",
      "engine",
      "max_results",
      "depth",
      "mode",
      "since",
      "until",
      "sort",
      "include_domains",
      "exclude_domains",
      "include_local",
      "summary"
    ]
  },
  "argo_local_search": {
    "description": "本机文件/笔记/代码搜索（非联网）：返回文件路径与行号。搜本地资料时用，与网络搜索互补。",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "搜索查询词（中文自动扩展召回）"
        },
        "path": {
          "type": "string",
          "description": "搜索目录；缩小范围更快更准",
          "default": "~"
        },
        "max_results": {
          "type": "integer",
          "description": "返回结果条数",
          "default": 5,
          "minimum": 1,
          "maximum": 20
        },
        "exact": {
          "type": "boolean",
          "description": "精确匹配，关闭中文扩展",
          "default": false
        }
      },
      "required": [
        "query"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "query",
      "path",
      "max_results",
      "exact"
    ]
  },
  "argo_local_read": {
    "description": "读取白名单内的本地文本文件（预览，非全文）：用于分析本地数据/笔记/研究成果。白名单目录由 ARGO_LOCAL_READ_DIRS 配置（逗号分隔），未配置或路径越权时拒绝（fail-closed）。",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "文件路径（必须位于白名单目录内）"
        },
        "max_chars": {
          "type": "integer",
          "description": "预览字符数上限",
          "default": 4000,
          "minimum": 200,
          "maximum": 20000
        },
        "line_start": {
          "type": "integer",
          "description": "起始行号（1 基，可选）",
          "minimum": 1
        },
        "line_end": {
          "type": "integer",
          "description": "结束行号（可选）"
        }
      },
      "required": [
        "path"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "path",
      "max_chars",
      "line_start",
      "line_end"
    ]
  },
  "argo_recompute": {
    "description": "fail-closed 可复算执行器：在受限子进程中运行计算脚本（只读白名单输入、断网、超时硬杀、内存软限），验证本地数据重算出的数值。用于结论承重要重算数字时；默认拒绝，需显式授权。",
    "parameters": {
      "type": "object",
      "properties": {
        "script": {
          "type": "string",
          "description": "要运行的计算代码（Python）"
        },
        "file_inputs": {
          "type": "string",
          "description": "白名单输入文件 JSON 数组：[{\"path\":\"...\",\"role\":\"原始数据\"}]"
        },
        "timeout_s": {
          "type": "integer",
          "description": "超时秒数",
          "default": 30,
          "minimum": 1,
          "maximum": 120
        },
        "max_mem_mb": {
          "type": "integer",
          "description": "内存上限 MB（尽力而为）",
          "default": 512
        },
        "allow_exec": {
          "type": "boolean",
          "description": "是否授权运行（fail-closed，默认拒绝；未授权时返回 skipped_reason）",
          "default": false
        }
      },
      "required": [
        "script"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "script",
      "file_inputs",
      "timeout_s",
      "max_mem_mb",
      "allow_exec"
    ]
  },
  "argo_evidence": {
    "description": "来源可信度评估：对搜索结果做权威性+时效性+交叉验证评分，输出逐条可信度分解。用于事实核查、高后果决策、学术引用。",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "搜索查询词（用于交叉验证）"
        },
        "results_json": {
          "type": "string",
          "description": "待评估的搜索结果 JSON（为空时自动搜索）。格式：{\"results\": [{\"title\":\"...\",\"url\":\"...\",\"snippet\":\"...\",\"source\":\"...\",\"score\":0.8}]}"
        },
        "max_results": {
          "type": "integer",
          "description": "自动搜索时的结果条数",
          "default": 10
        }
      },
      "required": [
        "query"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "query",
      "results_json",
      "max_results"
    ]
  },
  "argo_clarify": {
    "description": "意图消歧：识别查询中的歧义词与多义实体，给出意图分类与推荐搜索策略。查询含歧义词（苹果=公司/水果）或意图不明时用。",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "需要消歧的查询"
        }
      },
      "required": [
        "query"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "query"
    ]
  },
  "argo_crawl": {
    "description": "站点级爬取：按 sitemap 或 BFS 批量抓取整站页面，返回 URL、正文片段与深度。适合整站内容审计、站内多页对比。",
    "parameters": {
      "type": "object",
      "properties": {
        "url": {
          "type": "string",
          "description": "站点根 URL"
        },
        "strategy": {
          "type": "string",
          "enum": [
            "sitemap",
            "bfs"
          ],
          "description": "爬取策略",
          "default": "bfs"
        },
        "max_pages": {
          "type": "integer",
          "description": "最大抓取页数",
          "default": 10,
          "minimum": 1,
          "maximum": 50
        },
        "max_depth": {
          "type": "integer",
          "description": "BFS 最大深度",
          "default": 2,
          "minimum": 1,
          "maximum": 5
        }
      },
      "required": [
        "url"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "url",
      "strategy",
      "max_pages",
      "max_depth"
    ]
  },
  "argo_fetch": {
    "description": "智能页面抓取：HTTP 优先，遇 Cloudflare/JS 渲染自动升级反检测浏览器。支持 BM25 聚焦提取（focus 省 80%+ token）与结构化提取（mode=extract 抽表格/元数据/JSON-LD）。",
    "parameters": {
      "type": "object",
      "properties": {
        "url": {
          "type": "string",
          "description": "目标 URL"
        },
        "mode": {
          "type": "string",
          "enum": [
            "text",
            "extract"
          ],
          "description": "text=正文/聚焦文本, extract=结构化提取",
          "default": "text"
        },
        "extract_mode": {
          "type": "string",
          "enum": [
            "tables",
            "metadata",
            "jsonld",
            "all"
          ],
          "description": "mode=extract 时的提取子模式",
          "default": "all"
        },
        "focus": {
          "type": "string",
          "description": "BM25 聚焦查询词：只返回相关段落，省 token（仅 mode=text）"
        },
        "max_chars": {
          "type": "integer",
          "description": "返回正文最大字符数",
          "default": 8000,
          "minimum": 500,
          "maximum": 50000
        },
        "use_browser": {
          "type": "boolean",
          "description": "强制用反检测浏览器（HTTP 失败时已自动升级）",
          "default": false
        }
      },
      "required": [
        "url"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "url",
      "mode",
      "extract_mode",
      "focus",
      "max_chars",
      "use_browser"
    ]
  },
  "argo_screenshot": {
    "description": "网页截图（PNG）：分析页面布局、验证渲染结果、存档快照。支持全页与视口截图。",
    "parameters": {
      "type": "object",
      "properties": {
        "url": {
          "type": "string",
          "description": "目标 URL"
        },
        "full_page": {
          "type": "boolean",
          "description": "全页截图（默认仅当前视口）",
          "default": false
        },
        "output_path": {
          "type": "string",
          "description": "输出路径（默认写入系统临时目录：<temp>/argo_screenshot_<timestamp>.png）"
        }
      },
      "required": [
        "url"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "url",
      "full_page",
      "output_path"
    ]
  },
  "argo_pdf": {
    "description": "PDF 结构化提取：转为 Markdown（含表格、目录、元数据），支持 URL 下载与本地文件。",
    "parameters": {
      "type": "object",
      "properties": {
        "url": {
          "type": "string",
          "description": "PDF URL 或本地文件路径"
        },
        "pages": {
          "type": "string",
          "description": "页码范围，如 \"1-5\" 或 \"1,3,5\"（默认全部）"
        },
        "password": {
          "type": "string",
          "description": "加密 PDF 密码"
        }
      },
      "required": [
        "url"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "url",
      "pages",
      "password"
    ]
  },
  "argo_social_search": {
    "description": "社交平台搜索：知乎、Hacker News、B站、V2EX、Twitter/X、Reddit、小红书、微博等 UGC 内容。mode=sentiment 输出舆情聚合。注意：hackernews/v2ex/zhihu/bilibili 零密钥可用；twitter/reddit/weibo/xiaohongshu 依赖登录态或第三方 API，可能返回空。",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "搜索查询词"
        },
        "mode": {
          "type": "string",
          "enum": [
            "text",
            "sentiment"
          ],
          "description": "text=帖子列表, sentiment=舆情聚合分析",
          "default": "text"
        },
        "platforms": {
          "type": "string",
          "description": "平台列表，逗号分隔；可选 hackernews,v2ex,zhihu,bilibili,twitter,reddit,xiaohongshu,weibo",
          "default": "hackernews,zhihu,bilibili"
        },
        "max_results": {
          "type": "integer",
          "description": "每个平台的结果条数",
          "default": 5,
          "minimum": 1,
          "maximum": 20
        }
      },
      "required": [
        "query"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "query",
      "mode",
      "platforms",
      "max_results"
    ]
  },
  "argo_article": {
    "description": "微信公众号文章全文抓取：标题/作者/发布时间/正文纯文本/图片列表。仅支持 mp.weixin.qq.com 链接；要读公众号文章全文时用（普通网页走 argo_fetch）。",
    "parameters": {
      "type": "object",
      "properties": {
        "url": {
          "type": "string",
          "description": "mp.weixin.qq.com 文章链接"
        },
        "max_chars": {
          "type": "integer",
          "description": "返回正文最大字符数",
          "default": 20000,
          "minimum": 500,
          "maximum": 50000
        }
      },
      "required": [
        "url"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "url",
      "max_chars"
    ]
  },
  "argo_job": {
    "description": "招聘岗位多平台聚合：BOSS直聘/猎聘/智联/前程无忧/597/今日招聘，职位+地区并发搜索，三级地区命中判定+结构化字段（薪资/学历/经验）+跨平台去重。招聘市场调研时用。",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "职位关键词，如：工艺工程师、会计、电工"
        },
        "city": {
          "type": "string",
          "description": "地区：省/市/县或海外城市（成都、昆山、新加坡…），空=不过滤地区"
        },
        "num": {
          "type": "integer",
          "description": "每后端条数",
          "default": 5,
          "minimum": 1,
          "maximum": 20
        },
        "platforms": {
          "type": "string",
          "description": "逗号分隔平台: zhipin,liepin,zhaopin,51job,597,jrzp；空=全部",
          "default": ""
        },
        "loose": {
          "type": "boolean",
          "description": "宽松：异地岗位也保留；默认严格地区过滤",
          "default": false
        },
        "fetch_detail": {
          "type": "integer",
          "description": "对前 N 条高命中结果抓详情页补全结构化字段（0=仅 snippet 提取）",
          "default": 0,
          "minimum": 0,
          "maximum": 10
        }
      },
      "required": [
        "query"
      ],
      "additionalProperties": false
    },
    "allowed": [
      "query",
      "city",
      "num",
      "platforms",
      "loose",
      "fetch_detail"
    ]
  }
};

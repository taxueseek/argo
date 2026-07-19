---
name: local-search
parent: unified-search
description: unified-search 的本地/零成本兜底子技能。封装基于公开页面/HTML/RSS/JSON 的 24 个本地搜索引擎，不单独响应触发词，仅由 unified-search 通过 --sub-skill local-search 或 --local-first 调用。
version: 1.0.1
---

## Local Search 子技能

Local Search 是 unified-search 的「零成本兜底适配器」，用于：

- 在 `--mode fast` / `--mode budget` 下优先使用本地抓取引擎，避免消耗付费 API 配额。
- 当 SearXNG 不可用时，回退到本地 HTML/JSON 解析。
- 对中文网页、新闻、代码问答、学术、参考百科等垂直域提供补充结果。

### 设计原则

- **不单独响应触发词**：没有独立的 skill trigger，仅作为 unified-search 的子能力。
- **统一 schema**：输出与 unified-search 主 skill 完全一致，包含 `results[]`、`engines_used`、`errors`、`elapsed_ms` 等字段。
- **声明式解析**：HTML 结构变化时只需修改 `parse_maps.yaml`。
- **命名空间隔离**：本地引擎统一使用 `local_` 前缀（如 `local_bing`、`local_google`），避免与 unified-search 已有的 HTTP 引擎（`duckduckgo`、`wikipedia` 等）重名。

### 本地引擎列表（25 个，20 个默认启用）

| unified 名称 | 类型 | 默认启用 | 说明 |
|--------------|------|----------|------|
| local_bing | html | ✅ | Bing 网页结果 |
| local_google | html | ❌ | Google 网页结果（反爬强） |
| local_mojeek | html | ✅ | Mojeek 独立索引 |
| local_yandex | html | ❌ | Yandex 搜索（反爬强） |
| local_startpage | html | ✅ | Startpage 隐私搜索 |
| local_duckduckgo | html | ✅ | DuckDuckGo HTML |
| local_baidu | html | ✅ | 百度搜索 |
| local_sogou | html | ✅ | 搜狗搜索 |
| local_arxiv | xml | ✅ | arXiv API |
| local_pubmed | json | ✅ | PubMed/EUtils |
| local_crossref | json | ✅ | Crossref API |
| local_semantic_scholar | json | ✅ | Semantic Scholar API |
| local_bing_news | rss/html | ✅ | Bing 新闻 |
| local_google_news | rss | ✅ | Google News RSS |
| local_duckduckgo_news | rss/html | ❌ | DuckDuckGo 新闻 |
| local_github | json | ✅ | GitHub Search API |
| local_stackoverflow | html/json | ✅ | StackOverflow 问题 |
| local_gitlab | json | ✅ | GitLab API |
| local_npm | json | ✅ | NPM Registry |
| local_wikipedia | json | ✅ | MediaWiki API |
| local_wiktionary | json | ✅ | Wiktionary API |
| local_wikiquote | json | ✅ | Wikiquote API |
| local_imdb | html | ❌ | IMDb 搜索 |
| local_goodreads | html | ❌ | Goodreads 搜索 |
| local_openstreetmap | json | ✅ | Nominatim API |

### 调用方式

```bash
# 直接调用子技能（单引擎）
python3 sub-skills/local-search/local_search_adapter.py "query" --engine local_bing

# 批量调用多个本地引擎
python3 sub-skills/local-search/local_search_adapter.py "query" \
  --engine local_bing,local_baidu,local_duckduckgo

# 由 unified-search 调用
python3 scripts/search.py "query" --sub-skill local-search
python3 scripts/search.py "query" --local-first --mode fast
```

### 文件结构

```
sub-skills/local-search/
├── SKILL.md                 # 本文件
├── config.yaml              # 引擎基础配置（URL/超时/类型/开关）
├── parse_maps.yaml          # HTML/RSS/JSON 抽取映射
├── engine_registry.py       # 引擎注册中心（唯一真源）
├── health_check.py          # 轻量健康探针
├── smart_router.py          # 查询特征路由
├── search_v3.py             # local-search 主入口
└── local_search_adapter.py  # 兼容入口
```

### 输出 schema

与 unified-search 主 skill 一致：

```json
{
  "query": "string",
  "engine": "local_search",
  "engines": ["local_bing", "local_baidu"],
  "engines_combo": ["local_bing", "local_baidu"],
  "cached": false,
  "cache_level": null,
  "domain": null,
  "elapsed_ms": 1234,
  "tfidf_scores": [],
  "results": [
    {"title": "...", "url": "...", "snippet": "...", "score": 0.8, "source": "local_bing"}
  ],
  "count": 10,
  "engines_used": ["local_bing", "local_baidu"],
  "errors": []
}
```

---
name: local-search
parent: unified-search
description: unified-search 的本地/零成本兜底子技能。封装基于公开页面/HTML/RSS/JSON/CLI 的 33 个本地搜索引擎，不单独响应触发词，仅由 unified-search 通过 --sub-skill local-search 或 --local-first 调用。
version: 1.1.0
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

### 本地引擎列表（33 个，29 个默认启用）

| unified 名称 | 类型 | 默认启用 | 类别 | 说明 |
|--------------|------|----------|------|------|
| local_bing | cli(ddgs) | ✅ | web_general | Bing 网页结果（ddgs -b bing + JSON） |
| local_google | html | ❌ | web_general | Google 网页结果（反爬强；ddgs google backend 不可用） |
| local_mojeek | html | ✅ | web_general | Mojeek 独立索引（ddgs backend 返回导航链接） |
| local_yandex | cli(ddgs) | ✅ | web_general/japanese | Yandex 搜索（ddgs -b yandex） |
| local_startpage | html | ✅ | web_general | Startpage 隐私搜索（ddgs backend 不可用） |
| local_duckduckgo | cli(ddgs) | ✅ | web_general | DuckDuckGo（ddgs 默认） |
| local_brave | cli(ddgs) | ✅ | web_general | Brave 搜索（ddgs -b brave，实测稳定） |
| local_yahoo | cli(ddgs) | ✅ | web_general | Yahoo 搜索（ddgs -b yahoo，实测稳定） |
| local_baidu | html | ✅ | chinese | 百度搜索 |
| local_sogou | html | ✅ | chinese | 搜狗搜索 |
| local_360 | html | ✅ | chinese | 360 搜索 |
| local_jisilu | html | ✅ | finance/chinese | 集思录 |
| local_ddgs_news | cli(ddgs) | ✅ | news | ddgs news 默认后端（带日期） |
| local_bing_news | rss | ✅ | news | Bing 新闻 RSS |
| local_google_news | rss | ✅ | news | Google News RSS |
| local_duckduckgo_news | cli(ddgs) | ✅ | news | ddgs news 备用后端 |
| local_ddgs_images | cli(ddgs) | ✅ | images | ddgs images（bing TLS 偶发，已自动重试） |
| local_ddgs_videos | cli(ddgs) | ✅ | videos | ddgs videos |
| local_arxiv | xml | ✅ | academic | arXiv API |
| local_pubmed | json | ✅ | academic | PubMed/EUtils |
| local_crossref | json | ✅ | academic | Crossref API |
| local_semantic_scholar | json | ✅ | academic | Semantic Scholar API |
| local_github | json | ✅ | code | GitHub Search API |
| local_stackoverflow | json | ✅ | code | StackOverflow 问题 |
| local_gitlab | json | ✅ | code | GitLab API |
| local_npm | json | ✅ | code | NPM Registry |
| local_wikipedia | json | ✅ | reference | MediaWiki API（ddgs wikipedia backend 结果过少） |
| local_wiktionary | json | ✅ | reference | Wiktionary API |
| local_wikiquote | json | ✅ | reference | Wikiquote API |
| local_imdb | html | ❌ | vertical | IMDb 搜索 |
| local_goodreads | html | ❌ | vertical | Goodreads 搜索 |
| local_openstreetmap | json | ✅ | vertical | Nominatim API |

> CLI 引擎（ddgs 9.14.4）统一走 `-o json` 结构化输出 + 失败自动重试 1 次；
> 错误信号（DDGSException/ConnectError 等）在 rc=0 时也会被识别并上报，不静默吞错。

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
├── local_health_check.py    # 轻量健康探针（local_ 前缀避免与 scripts/health_check 同名冲突）
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

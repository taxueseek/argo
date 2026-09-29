---
name: local-search
parent: argo
description: argo 的本地/零成本兜底子技能。封装基于公开页面/HTML/RSS/JSON/CLI 的 29 个本地搜索引擎，不单独响应触发词，仅由 argo 通过 --local-first 调用。
version: 1.1.0
---

## Local Search 子技能

Local Search 是 argo 的「零成本兜底适配器」，用于：

- 在 `--mode fast` / `--mode budget` 下优先使用本地抓取引擎，避免消耗付费 API 配额。
- 当 SearXNG 不可用时，回退到本地 HTML/JSON 解析。
- 对中文网页、新闻、代码问答、学术、参考百科等垂直域提供补充结果。

### 设计原则

- **不单独响应触发词**：仅作为 argo 的子能力，由 `--local-first` 调用。
- **输出与主 skill 同 schema**：`results[]` / `engines_used` / `errors` / `elapsed_ms`，可直接进 evidence 与 RRF。
- **`local_X` 与主清单的 `X` 是别名，不是备份**：`local_arxiv`/`local_crossref`/`local_semantic_scholar`/`local_github`/`local_npm`/`local_wikipedia`/`local_stackoverflow`/`local_google_news` 与主清单同名项**打同一个上游端点**（主清单走官方 API、这里走免密钥直取），不存在「API 挂了抓取版兜底」。同上游同格式由 `tests/test_local_search_registry.py` 锁定。

### 本地引擎列表（29 个，27 个默认启用）

| unified 名称 | 类型 | 默认启用 | 类别 | 说明 |
|--------------|------|----------|------|------|
| local_bing | cli(ddgs) | ✅ | web_general | Bing 网页结果（ddgs -b bing + JSON） |
| local_yandex | cli(ddgs) | ✅ | web_general/japanese | Yandex 搜索（ddgs -b yandex） |
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

# 由 argo 调用
python3 scripts/search.py "query" --local-first --mode fast
```

### 两条不显然的约定

- `engine_registry.py` 是本地引擎注册中心（唯一真源，读本目录 `config.yaml` 与 `parse_maps.yaml`）。
- `local_health_check.py` 用 `local_` 前缀，避免与 `scripts/health_check.py` 同名冲突。


### 输出 schema

与 argo 主 skill 一致：

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

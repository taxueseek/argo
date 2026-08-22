# Argo 架构与原理（v2.8.0）

## 文件结构

```
argo/
├── SKILL.md              # 技能注册文档（路由壳，本文件指回）
├── config.yaml           # 引擎配置 & 路由规则（单一真源，~4000 行）
├── backends/             # 注册表派生（sync_backends.py 生成）
│   ├── domain_profiles.json   # TF-IDF 领域文档
│   ├── engine_registry.yaml   # 引擎注册表
│   └── quota_profiles.json    # 配额配置
├── engines/specs/        # 外置引擎声明（10 个，优先覆盖同名）
├── scripts/
│   ├── search.py         # CLI 入口 & 执行编排
│   ├── route.py          # 三层路由决策
│   ├── tfidf_router.py   # TF-IDF 语义路由
│   ├── engine_families.py # 能力族分类（16 族，MECE）
│   ├── engines.py        # 引擎适配层（_BUILDERS）
│   ├── cache.py          # 双层缓存（L1 LRU + L2 SQLite + gzip）
│   ├── adaptive.py       # 自适应学习（success×latency×cost）
│   ├── quota.py          # 配额管理
│   ├── search_types.py   # 统一类型系统
│   ├── research.py       # 深度研究取证编排（dossier）
│   ├── research_expand.py / research_work_packages.py
│   ├── research_dossier.py / research_gates.py / research_cli.py
│   ├── evidence.py       # 可信度评估工具
│   ├── clarify.py        # 意图消歧工具
│   ├── crawl.py          # 站点爬取工具
│   ├── extract.py / fetch.py / focus_extract.py / pdf_extract.py
│   ├── content_security.py # 内容安全引擎
│   ├── content_signals.py  # 内容质量信号
│   ├── mcp_server.py     # MCP 服务层（10 工具）
│   └── social_engines/   # 社交平台引擎
├── sub-skills/
│   ├── local-search/     # 本地引擎聚合
│   ├── local-seek/       # 本机文件搜索
│   └── ego-search/       # 登录态专业搜索
├── docs/                 # 发布说明（只保留 v2.8.0）+ 指南
├── references/           # 本目录：引擎全景 / 详细用法 / 架构
└── tests/
```

## 证据流水线（MECE）

```
Query
  ├─ Clarify（意图是否可执行）
  ├─ Search Selection（引擎召回 + 域名权威 + SERP 剔除）
  ├─ Absorption（数字/定义/对比/披露密度；fetch 后 quality）
  ├─ Freshness（发布年；忽略「2015年以来」历史对比年）
  └─ Consensus（多可吸收域名佐证；社交仅叙事）
```

四块互不重叠、合起来覆盖「能不能用这条结果」。Argo 的产出是「证据候选 + 可信度分解」，不是「链接清单」。

## 量化公式（evidence v2.2）

```
selection  = authority_score（SERP/跳转链 ≤ 0.15）
absorption = evidence_density（has_numbers/definition/comparison/howto/disclose − qa）
freshness  = 发布年/URL年/完整日期
final      = 0.40·selection + 0.35·absorption + 0.15·freshness + 0.10·engine_score
```

完整交叉验证：`python3 scripts/evidence.py --stdin --json`。

## 成本感知路由

```
score = quality × cost_factor
cost_factor: free=1.0（anysearch/zhihu/eastmoney/arxiv/ddgs/octen/local_*）
             low=0.7（bocha/byted）  api=0.5（exa）  paid=0.3（tavily）
```

## 预算模式

| 模式 | 说明 | 触发条件 |
|------|------|---------|
| fast | 免费引擎优先，禁用付费 | 简单查询 |
| auto | 成本感知评分（默认） | 普通查询 |
| deep | 质量优先，忽略成本 | 深度研究 |
| budget | 配额控制，用完降级 | 配额紧张 |

## 核心能力清单

- **TF-IDF 语义路由**：二元组 + boost_keywords + boost_combos，< 5ms
- **加权 RRF（WG-RRF）**：权威源 1.2-1.4、社交源 0.7-0.8 加权融合
- **语义缓存**：minhash n-gram 近重复软命中（阈值 0.7），自适应 TTL（稳定内容 ×2）
- **自适应引擎禁用**：熔断 open 达 3 次自动 disabled，成功或新环境自动恢复
- **网络环境感知**：慢网超时预算 ×1.8 并偏好本地源，快网 ×0.8
- **Bocha Reranker**：语义精排后处理
- **查询变体生成**：无 LLM 六策略（问句化/概念扩展/反方观点/范围调整/缩写互换）
- **Wayback 快照回退**：HTTP 失败自动查最新快照
- **SSRF 防护**（v2.7.1）：fetch/crawl/http_client 统一拦截内网/私有地址（scheme 白名单 + 主机名黑名单 + DNS 解析 IP 段 + 重定向逐跳校验）；`ARGO_ALLOW_PRIVATE_URLS=1` 显式放行
- **env 占位缺失过滤**：未配置的 `{GITHUB_TOKEN}` 等占位替换为空并过滤残留认证头
- **引擎层 HttpClient**（v2.7.10）：UA 轮换 + Cookie 积累 + 429/503 Retry-After + 指数退避；`ARGO_ENGINE_HTTP_CLIENT=0` 回退 urllib
- **实时索引引擎**（v2.7.3）：`realtime_index` 免 Key 实时索引源，需本机安装 realtime-index CLI

## 输出 JSON Schema

```json
{
  "query": "string", "engine": "string", "engines": ["string"],
  "engines_combo": ["string"], "cached": false,
  "cache_level": "L1 | L2", "domain": "string | null",
  "elapsed_ms": 0, "tfidf_scores": [{"engine": "string", "score": 0.0}],
  "results": [{"title": "string", "url": "string", "snippet": "string", "score": 0.0, "source": "string"}],
  "count": 0, "engines_used": ["string"], "errors": ["string"]
}
```

v2.8 新增证据门控字段：`fetch_required`、`evidence_loop.suggested/verified_count/pending_count`、每条结果 `fetch_suggested/has_fetched_evidence/post_fetch_absorption`。

## MCP 工具契约

| 工具 | 输入要点 |
|------|---------|
| argo_search | query + 可选 engine/mode/since/until/sort |
| argo_local_search | 本机文件搜索（非联网），query + path |
| argo_research | 取证包（扩词或 work_packages），含 social-sentiment |
| argo_evidence | query + results_json + max_results |
| argo_clarify | 歧义查询消歧 |
| argo_crawl | 站点级爬取（sitemap/BFS） |
| argo_fetch | URL + focus（BM25）+ mode=extract |
| argo_screenshot / argo_pdf | URL + 可选参数 |
| argo_social_search | 跨平台舆情，mode=sentiment |

# Argo 架构与原理（v2.8.0 + traceability v2.3）

## 文件结构

```
argo/
├── SKILL.md              # 技能注册文档（路由壳，本文件指回）
├── config.yaml           # 路由规则（cache / domains / execution / output / semantic_evidence / network）
│                         # engines: {} 保留空键仅为兼容顶层键读取；2059 行
├── engines/specs/        # 290 个引擎声明（每引擎一文件，唯一声明源，人工维护）
│                         # config.py 运行时从这里加载，不再合并第二处
├── backends/             # 注册表派生（sync_backends.py 从引擎声明生成，勿手改）
│   ├── domain_profiles.json   # TF-IDF 领域文档（67 条人工语料 + 生成器补的空模板）
│   ├── engine_registry.yaml   # 引擎注册表
│   ├── quota_profiles.json    # 配额配置
│   └── env_passthrough.txt    # mcp_launch.sh 的密钥透传名单
├── scripts/
│   ├── search.py         # 搜索 CLI 入口 & 执行编排（execute_search / super_search）
│   ├── search_rank.py    # 排序/融合层（RRF、minhash 去重、精排、共识信号、域过滤）
│   ├── search_output.py  # 输出成形（信源标准化、漏斗、人读格式）
│   ├── route.py          # 路由编排（三分支：指定引擎 / 域命中 / TF-IDF 与保底）
│   ├── route_domains.py  # 域规则（声明式 patterns/unless/intent_required + 匹配）
│   ├── route_lang.py     # 语言判定与按语言选源/排序
│   ├── route_combo.py    # 引擎组合装配（谁在场、什么顺序）
│   ├── route_policy.py   # 预算截断与保留策略（谁必须留下）
│   ├── route_cache.py    # 路由决策缓存（存储层）
│   ├── route_log.py      # 路由决策采样落本地日志（旁路，失败静默；「遥测」已正名为本地使用日志）
│   ├── tfidf_router.py   # TF-IDF 语义路由
│   ├── engine_families.py # 能力族分类（16 族，MECE）
│   ├── engines.py        # 引擎适配层（_BUILDERS）
│   ├── cache.py          # 双层缓存（L1 LRU + L2 SQLite + gzip）
│   ├── adaptive.py       # 自适应学习（success×latency×cost×终榜边际贡献；(domain, lang) 桶 = 健康度收缩 × 场景召回率）
│   ├── quota.py          # 配额管理
│   ├── search_types.py   # 统一类型系统
│   ├── research.py       # 深度研究取证调度（dossier）
│   ├── research_expand.py / research_work_packages.py
│   ├── research_dossier.py / research_gates.py / research_cli.py
│   ├── evidence.py       # 可信度评估工具
│   ├── clarify.py        # 意图消歧工具
│   ├── crawl.py          # 站点爬取工具
│   ├── extract.py / fetch.py / focus_extract.py / pdf_extract.py
│   ├── content_security.py # 内容安全引擎
│   ├── content_signals.py  # 内容质量信号
│   ├── mcp_server.py     # MCP 服务层（工具清单见 scripts/mcp_tools.py，单一真源）
│   └── social_engines/   # 社交平台引擎
├── sub-skills/
│   ├── local-search/     # 本地引擎聚合
│   ├── local-seek/       # 本机文件搜索
│   └── ego-search/       # 登录态专业搜索
├── docs/                 # 发布说明（只保留 v2.8.0）+ 指南
├── references/           # 本目录：引擎全景 / 详细用法 / 架构
└── tests/
```

## 状态词表（scripts/engine_states.py）

全部状态名、派生集合与转换判据只有一处：`scripts/engine_states.py`。它只放
词表与纯函数，不碰 IO，所以热路径可以直接 import。改状态名、加状态值、调
派生集合，都只改这一个文件；各模块用常量，不写字符串字面量。`tests/
test_engine_states.py` 钉住派生关系与消费面同源，`tests/
test_state_literal_drift.py` 用 AST 扫「已登记状态值又退回裸字面量」，
`tests/test_native_tools_sync.py` 把词表投影对拍到 JS 生成物；三者都进
`scripts/pre-commit` 快门禁。

登记的 27 台状态机（25 台 + 坏例根因 root_cause）：

| 分组 | 状态机 | 关键判据 |
|------|--------|---------|
| 引擎层 | 熔断器 breaker | closed/open/half_open/disabled；`is_breaker_routable` |
| | 配置 config、env 过滤、admission | enabled/disabled、allowed/env_filtered、admitted/blocked/unverified |
| | 运行时健康 verdict | auth_down→suspect→auto_disabled→flapping→watch→unknown（`health_severity_index`） |
| | 调用结局 outcome | ok/ok-cached/partial/no-results/timeout/error/auth-failed/quota-exhausted/rate-limited/blocked/skipped-*/cancelled |
| | 失败归因 category | dependency/auth/rate_limited/quota_exhausted/blocked/upstream/network/unknown |
| | 重试判据 retry | `is_retryable_outcome`（派生自 outcome） |
| | 抓取降级链 fetch method | http_md→http→tls→jina→wayback→browser→chrome_cdp→…；`is_degraded_fetch_method` |
| | 展示态 engine_detail | ready/unknown/disabled/env_filtered/missing_key/missing_dep/quota_exhausted/blocked/admitted |
| 加工层 | 精排 reranker | ok 与 7 个 skipped_*；`RERANK_DEGRADED_STATUSES` = 全集减 ok，漏一个就是静默退化 |
| | 搜索计划 plan | ready/handoff_required/needs_authorization（`plan.main` 退出码也按它） |
| | 数值印证 numeric | confirmed/partial/missing/unfetchable；只有 `NUMERIC_PENALIZED`（missing/partial）降权 |
| | 取证覆盖 coverage | COVERED/PARTIAL/NOT_COVERED（research_dossier 产、research_gates 消） |
| | 候选核验 verification | candidate/opened（snippet 永不得标 opened） |
| | 信封路由 envelope route | completed/partial（整次检索的交付结论，与单引擎 outcome 的 partial 不同层） |
| 预检与观测 | 批处理预检 batch probe | 条目 unsupported/needs_auth/already_have/not_found/unknown；结论 go/go_with_skips/stop |
| | 健康探针 probe | available/degraded/unavailable/unknown；`PROBE_MAX_FAILURES`=2；`is_probe_available` 只认 unavailable 一票否决（fail-open） |
| | 缓存层级 cache level | L1/L2/L?（`L?` 是旧存档缺 `_cache_level` 的兜底） |
| | 观察模式 watch change | none/noise/substantive/unknown |
| | 平台运行时 daemon/plugin | running/stopped/idle/busy；ready/loading/unavailable |
| 跨层结论 | 恢复链等级 recovery level | L0/L1/L2/L3/L4；`RECOVERY_LEVEL_TIER` 成本序，`recovery_level_tier` 未登记值返回 `None`（fail-closed） |
| | 结论上限 conclusion_cap | low/medium/high；Python 三处与 JS `wide-research` 共用生成物 `STATES.conclusionCap` |
| | 验证结论 validation | pass/fail/skipped/error；阶段 health/quality；`engine_validate` 产出、`engine_admission` 落库、`engine_status` 展示共用 |
| 抓取降级链 | fetch method 补值 | http_mobile/tls_spoof/jina_reader/md_variant/llms_txt/robots_blocked/http_direct/full；`FETCH_METHODS_ALL` 是全集，`DEGRADED_FETCH_METHODS` 是其子集 |

词表边界：`outcome.status` 用连字符（`quota-exhausted`），`engine_detail.status`
与失败归因用下划线（`quota_exhausted`）。这是三层各自的接口契约，跨层映射走
显式字典，不做字符串等价合并。

## 证据流水线（MECE）

```
Query
  ├─ Clarify（意图是否可执行）
  ├─ Search Selection（引擎召回 + 域名权威 + SERP 剔除）
  ├─ Absorption（数字/定义/对比/披露密度；fetch 后 quality）
  ├─ Freshness（发布年；忽略「2015年以来」历史对比年）
  ├─ Traceability（可溯源性：一手信源 vs 二手转载）
  └─ Consensus（多可吸收域名佐证；社交仅叙事）
```

五块互不重叠、合起来覆盖「能不能用这条结果」。Argo 的产出是「证据候选 + 可信度分解」，不是「链接清单」。

## 量化公式（evidence v2.3 + traceability）

```
selection    = authority_score（SERP/跳转链 ≤ 0.15）
absorption   = evidence_density（has_numbers/definition/comparison/howto/disclose − qa）
freshness    = 发布年/URL年/完整日期
traceability = 可溯源性（一手信源 0.85-0.95 / 二手转载 0.30-0.45 / 未知 0.50）
final        = 0.35·selection + 0.30·absorption + 0.15·freshness + 0.10·engine_score + 0.10·traceability
```

**Traceability 评分规则**（借鉴 doubao-sentiment-tracker 溯源四场景方法论）：

| 来源类型 | 评分 | 示例 |
|---------|------|------|
| 一手信源（社交原帖） | 0.80-0.95 | 微博原帖、知乎回答、GitHub issue |
| 原始媒体报道 | 0.70-0.95 | 新华社、路透社、财新 |
| 综合报道/评论 | 0.50-0.65 | 引用了原始来源的评论 |
| 二手转述/洗稿 | 0.30-0.45 | 百家号、头条号 |
| 无法判断 | 0.50 | 未知域名 |
| SERP/跳转链 | 0.10 | 百度搜索结果页 |

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

- **TF-IDF 语义路由**：二元组 + boost_keywords + boost_combos，< 5ms（**同进程稳态**；CLI 每次新进程的首调实测 40-80ms，由路由决策缓存兜到 ~1.8ms，2026-09-19 实测）
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

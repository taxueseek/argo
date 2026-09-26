# Argo 详细用法（v2.9.0）

> 本文受门禁保护（tests/test_usage_doc_gates.py）：文中命令必须存在于
> bin/argo 分发表、`ARGO_*` 开关必须代码实存。改命令先改代码，本文随行。

## 命令总览（18 个，唯一事实=bin/argo 分发表）

| 命令 | 一句话 |
|------|--------|
| `argo search` | 统一搜索（多引擎融合；`--list-engines` 看源清单） |
| `argo research` | 深度研究（问题分解→多源采集→综合报告，`--broaden`/`--deep-read`） |
| `argo evidence` | 可信度评估（选拔×吸收两维） |
| `argo clarify` | 意图消歧（歧义检测+意图分类+路由建议） |
| `argo fetch` | 单页正文抓取（六级降级链，`--full` 全文存档） |
| `argo crawl` | 站点爬取（sitemap/BFS 多页） |
| `argo extract` | 结构化提取（表格/Meta/JSON-LD） |
| `argo article` | 公众号文章全文（标题/正文/图片） |
| `argo screenshot` | 网页截图（`--full-page`/`--output`） |
| `argo pdf` | PDF 正文提取（`--pages`/`--password`） |
| `argo answer` | 直答（Seltz 带引用合成答案） |
| `argo watch` | 网页变化监控（add/check/list/remove） |
| `argo job` | 招聘多平台聚合 |
| `argo preflight` | 批量 URL 预检（`--probe` 联网探测） |
| `argo cite` | DOI 引用条目（四格式） |
| `argo mcp` | 多客户端 MCP 注入/诊断/还原 |
| `argo paths` | 路径自省与状态目录自检 |
| `argo stats` | 使用日志与反馈状态（本地使用日志只读出口） |

> SKILL.md 只留核心命令；本页是参数大全与输出字段说明。

## search 完整参数

```bash
python3 scripts/search.py "查询词" \
  [--engine ENGINE]           # 强制引擎（可多个）\
  [--max-results N]           # 每引擎结果数\
  [--depth fast|balanced|deep] # 搜索深度\
  [--mode fast|auto|deep|budget] # 预算模式\
  [--local-first]             # 本地零成本聚合优先\
  [--no-cache] [--explain] [--json]\
  [--timeout S] [--progress]\
  [--since 7d|2026-08-01] [--until 2026-08-01] [--sort relevance|oldest|newest]\
  [--domain DOMAIN] [--sub_domain SUB_DOMAIN]  # 垂直域限定\
  [--input-kind auto|keyword|url-seed|known-url]\
  [--plan-only] [--force-search] [--envelope]\
  [--timing|--no-timing]       # 输出里的阶段耗时，默认开\
  [--archive] [--archive-dir DIR] [--archive-tag TAG] [--archive-note NOTE]\
  [--verify [TOP_K]]          # 核验 top-K 未核验结果并回填证据分
```

**时间窗**：`--since`/`--until` 支持相对值（`7d`）或绝对日期（`2026-08-01`，含当天）；下推到支持时间窗的引擎，任意引擎组合融合后按 `published_at` 保底过滤（`time_filtered: N`）；`--sort newest` 找最新动态、`oldest` 找最早出处。`wayback_cdx` 输出标准 `published_at`（CDX 最早快照）。

**Python 解释器**：脚本最低支持 3.9（与 `bin/argo` 的 `MIN_PYTHON` 一致）。`bin/argo` 自动探测 `ARGO_PYTHON` → python3.14/3.13/3.12/3.11/3.10 → 保底。强制：`ARGO_PYTHON=/opt/homebrew/bin/python3.14 argo ...`。

## search 输出字段与体积纪律

`search --json` **默认只给答案**（实测 5 条：5.6 KB ≈ 1.4k token）。加
`--envelope` 才会附**三个视图**——这三个视图各有用途、不是重复，但它们服务的是
**归档与来源追溯**，日常取答案不需要，故默认关（`--archive` 会自动带上）：

| 视图 | 用途 | 体积（5 条） |
|------|------|-------------|
| `results` | **答案用这个**：融合+精排后的条目（含 `score`/`rerank_dims`/`consensus_engines`/`fetch_suggested`/`image_url`/`full_text_url`） | 2.4 KB |
| `sources` | **引用用这个**：底部相关链接形态的稳定 5 字段投影 | 1.0 KB |
| `candidates` | **归档/整理素材才要**：完整候选记录，带来源追溯字段（`candidate_id`/`canonical_url`/`platform`/`verification`/`metrics`/`limitations`） | 4.9 KB |

- **Agent 消费再加 `--fields agent`**：剥掉本地使用日志字段、只留答案与质量信号，
  实测 `-n 2` 输出 1.3 KB。`fetch_required` 与 `limitations` 在各档都保留。
- **`--envelope` 与 `--fields agent` 不要同给**（2026-09-19 实测）：`_strip_for_agent`
  会把 `candidates`/`coverage` 一并剥掉，envelope 的增量是 **0 字节**——同一查询
  `--json` 7226 B、`--fields agent` 5614 B、`--envelope` 19066 B、
  `--envelope --fields agent` 5614 B。调用方会以为拿到了 provenance、实际没有，
  所以 CLI 在这个组合下会往 stderr 打一行告警。要 provenance 就去掉 `--fields agent`。
- 三视图内部确实有重复（同一段 snippet 在三处各写一遍，合计约占全文档 40%）——
  那是**归档要的冗余**：`candidates.jsonl` 一行一条要能自证来源。所以做法是
  「要用时打开 `--envelope`」，不是把视图削瘦（削了归档就残了）。
- **`funnel`（阶段漏斗账）**：`routed → called → returned → deduped → filtered →
  kept` 六格计数，按管线顺序。相邻两格的差值就是该层损耗——「只有 5 条」和「0 条」
  都能一眼看出塌在哪一层（引擎没抓到 / 被当重复削掉 / 被过滤压没），`limitations`
  会把塌陷点写成一句话。约 70 字节，agent 档也保留。
- 另有一批诊断字段（`tfidf_scores`/`engine_outcomes`/`coverage`/`routes`/
  `lang_pref`）体积不大但通常无用，别把它们当结果读。
- 结果级字段：`fetch_suggested`（是否建议核验）、`has_fetched_evidence`（已核验）、
  `post_fetch_absorption`（正文级吸收分，核验后回填）；`full_text_url` 是源给出的
  **可确定性取正文**端点（如 e-Gov `lawdata`、Gutenberg 纯文本），有时代替 `url` 去 fetch。

### 阶段耗时（`timing`，默认开）

每次搜索的输出里都带一块 `timing`，约 170 字节。它回答的是「这次搜索的时间
花到哪儿去了」，不用再去外面计时。不想要就加 `--no-timing`。

```json
"timing": {
  "stages_ms": 448.9,
  "stages": [                       // 按耗时降序，pct 是占各阶段之和的比例
    {"stage": "dispatch", "ms": 394.5, "pct": 84.2},
    {"stage": "route",    "ms": 46.6,  "pct": 9.9}
  ],
  "elapsed_ms": 449,
  "dispatch": {                     // 引擎调度这一段单独展开
    "wall_ms": 394, "engines_run": 1, "engine_sum_ms": 365,
    "parallel_efficiency": 0.93,    // 引擎各自耗时之和 ÷ 墙钟。多数值大 = 并行有效
    // useful_ms + wasted_ms ≡ wall_ms：前者是最后一个有效引擎完成的时刻（答案
    // 从这一刻起已在手里），后者是此后还在等的那段。分开看才知道「慢」是源真的
    // 慢，还是答案早就有、我们在白等一个不会来的源。
    "useful_ms": 394, "wasted_ms": 0, "early_stopped": true
  },
  "import_ms": 49.4,                // 加载模块占的时间
  "overhead_ms": 61.5,              // 除各阶段外的开销（import + 解析参数 + 收尾）
  "process_ms": 100.4               // 整个进程
}
```

常见阶段名：`route`（选引擎）、`cache_lookup` / `cache_write`（读写缓存）、
`dispatch`（等各引擎返回）、`filter`（否定词 + 时间窗过滤）、`recovery`
（零结果救援链，**含网络调用**）、`fusion`（合并）、`dedupe`（去重）、
`rerank`（重排）、`signals`（算各项质量分）。

两个最常看的数：
- **`stages` 第一行**——慢在等网络（`dispatch`）还是慢在本地算（`fusion`/`rerank`）。
- **`overhead_ms`**——程序启动本身的开销。缓存命中的搜索里它常占六成，
  所以「同一条查询第二次跑」未必快在搜索上。

### `--list-engines` 的体积陷阱

`--list-engines` 列名字约 3 KB（2026-09-19 实测 2697 B，可放心用）；**`--detail` 带 `--engine` 过滤 = 单引擎全量诊断 ~0.9 KB**；**不带过滤的 `--detail --json`** 才是体积陷阱（2026-09-16 实测 ~50 KB，2026-09-19 复测 50302 B；原全量转储 151 KB，runtime/admission 嵌套占三成，已默认投影压缩）
（含每引擎的熔断/配额/准入/依赖运行态），属诊断转储。查单个或几个引擎请**同时
给 `--engine`**（逗号分隔），体积降到 KB 级；未收录的名字会走 stderr 提示。

```bash
python3 scripts/search.py --list-engines --detail --engine egov_law,kor_law   # ≈2 KB
```


## research 输出字段

取证包（`kind=dossier`），不是判断稿。协议：`references/research-protocol.md`。

```bash
python3 scripts/research.py "查询" [--sub-queries N] [--depth deep] [--budget N] [--json] [--verify N] [--route-strategy local_first|cost_aware|full]
python3 scripts/research.py "查询" --work-packages '[{"id":"d","question":"定义"},{"id":"r","question":"风险","depends_on":["d"]}]' --json
```

- `kind=dossier`、`conclusion_cap`（high/medium/low）、`quality_gate_results`（可判定谓词，不是空勾选）
- 有 `--work-packages`：按 `depends_on` 分阶段，写入 `work_packages` / `work_package_stages`
- 无工作包：`query_expansion`（扩词，不是问题树）
- `key_findings`：各维度检索头条，不是结论
- `citations` / `sources`：按 canonical URL 去跟踪参数去重
- `coverage_map`：COVERED/PARTIAL/NOT_COVERED
- `source_leads`（兼 `verification_records`）：SERP snippet 一律 `unverified_snippet`，有 URL 也不算核实
- `blind_spots`：未覆盖或单来源维度
- Verify：`corroboration_level`、`cross_score`、`conflicts`、`unverified_count`
- `fact_alignment`（auto/deep 且结果 ≥3）：`fact_conflicts` / `fact_corroborated`
- `--budget N`：超限标记 `budget.exhausted=true`

**社交舆情**：`--mode social-sentiment --platforms xiaohongshu,reddit,twitter` → `platform_breakdown` / `engagement_totals` / `top_topics` / `cross_platform_posts`。

## evidence 输出字段

```bash
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json [--high-stakes]
```

- `credibility.final` / `selection` / `absorption`
- `authority`（含 `is_serp`）、`freshness`（忽略「YYYY年以来」历史对比年）
- `evidence_density`（has_numbers/has_comparison/…）
- `cross_validation`（可吸收域名数）
- 中文信源覆盖与降权表：`backends/source_types_cn.json`

## clarify 输出字段

```bash
python3 scripts/clarify.py "有歧义的查询" --explain --json
```

`ambiguities`（歧义词+可能含义+置信度）、`intents`（意图分类）、`recommended_strategy`（clarify_first/deep_research/split_search/direct_search）。

## 抓取三工具细节

### argo_fetch

```bash
argo fetch "https://example.com"                      # HTTP 优先，失败自动升级浏览器
argo fetch "https://example.com/long-article" --focus "关键词"   # BM25 聚焦，省 token
argo fetch "https://cloudflare-protected.com" --use-browser     # 强制反检测浏览器
```

- **降级链顺序**：HTTP（带内容协商；桌面/移动 UA，抖音等分流站移动优先）→ `{url}.md` 直出探测（主请求没拿到 Markdown 才回探）→ TLS 指纹 → jina/Parallel 免费云渲染 → Wayback/浏览器 自动降级 + BM25 聚焦提取 + 质量信号 + 内容安全引擎
- **内容协商**：HTTP 那一级带 `Accept: text/markdown`，站点愿意给 Markdown 就直接拿走（`fetch_method=http_md`），既省下整条反爬降级链，也保住了站点自己的标题/表格/代码块结构。文档站实测约四成支持；不支持的站点原样返回 HTML，行为不变，故默认常开（`ARGO_FETCH_MD_NEGOTIATE=0` 关闭）。声称 `text/markdown` 却是占位页/错误页/404 的响应一律按 HTML 处理——实测这类假货约占五分之一
- **截断可见 + 全文可回读**：正文仍按 `--max-chars` 裁剪（token 预算不变），但被裁掉的部分不再丢弃——结果里给 `truncated` / `full_length` / `full_text_path`，完整正文另存一份到状态目录的 `fulltext/`。想复核被裁的那段不必重新联网：
  ```bash
  argo fetch "https://example.com/long" --full              # 读全文（优先用存档）
  argo fetch "https://example.com/long" --offset 20000 --limit 5000   # 翻页读指定区间
  ```
  存档只在**确实发生截断**时写入，超上限（单档 8MB / 共 300 份）自动淘汰最旧的；`ARGO_FULLTEXT=0` 关闭
- **降级触发**：HTTP 失败 / 内容 < 50 字符 / 检测到 CF 挑战 / 检测到 JS shell
- **Wayback 回退**：失败或空内容自动查最新快照（`fetch_method=wayback` + `snapshot_url`/`snapshot_ts`）
- **内容安全引擎**：抓取内容先过注入检测再交给 Agent——70+ 中英日韩俄阿希泰模式（指令覆盖/角色操纵/系统提示泄露/越狱/数据外泄/身份冒充/XSS）+ 编码归一化（零宽字符/RTL/Unicode 同形字/base64/URL 编码）+ 语义意图分析 + 风险评分 + 目标脱敏。输出 `content_security.content_clean / risk_score / threat_count / threat_types / redactions / content_lang`
- 单独调用：`python3 scripts/content_security.py "文本" --json` 或 `--stdin < content.txt`

### argo_screenshot

```bash
argo screenshot "https://example.com" [--full-page] [--output /tmp/page.png]
```

### argo_pdf

```bash
argo pdf "https://example.com/paper.pdf" [--pages "1-5"] [--password "secret"]   # 支持本地路径
```

## 证据流程字段语义（v2.8.0）

搜索输出自带可编程判定开关，回答「现在能不能下结论」：

- `fetch_required`：高后果域（金融/医疗/法律/事实核查）为 true，**下结论前必须核验正文**
- `evidence_loop.suggested` / `verified_count` / `pending_count`：建议核验的 URL 与进度
- 每条结果的 `fetch_suggested` / `has_fetched_evidence` / `post_fetch_absorption`：
  这条要不要抓正文、抓过没有、抓后吸收分变没变

`--verify N` 一键抓正文核验 top-N 并回填证据分：

```bash
python3 scripts/search.py "贵州茅台股价" --verify 3
# [verify] 核验 3 条，improved=2 unchanged=1 degraded=0 mean_delta=0.18
```

以上开关在精简档与 `--fields agent` 档都保留（两档都不会剥掉 `fetch_required`）。

## argo answer（直答）与 Seltz 语料

```bash
argo answer "query" [--scope news|wikipedia|people|companies] [--model seltz-base|seltz-pro]
```

**`scope` 是语料选择，不是可选项**，不传一律落上游默认的 `news`。2026-09-16 实测
（不接 console 直打上游）：

| 语料 | 实测结论 |
|------|----------|
| `companies` | 英文公司名最好：Apple / Microsoft / Tesla 概况答得准且完整（成立于哪年、总部、主营） |
| `wikipedia` | 干净，引用全来自 en.wikipedia.org；内容不在维基时如实拒答 |
| `news` | 英文真新闻查询可用（美联储降息 → channelnewsasia / businesstimes 等）；覆盖窄 |
| `people` | 不可用：10 条引用全是 linkedin.com，人名基本查不到 |

**中文查询各语料均差**——同一 `news` 语料下英文查询 3/3 有答案，中文查询 3/3 拒答且
引用指向时政与播客类条目。故 Seltz 按「**英文主力**」定位配置：`langs: [en]` 限定英文
（`engine_langs` 只认 `spec.langs` / `ENGINE_LANGS` 表，**不看 coverage**；不写这条则
默认 `["*"]` 语言中立，中文查询也会被路由过来，这正是中文查询拿到时政类条目的通路），
`coverage` 收为 `[news, english]`，`family: news_flash`（原归 `web_general` 属误判，
且该族在 `dedupe_by_family` 里 `max_per_family=2`，它会与 octen/anysearch 争槽位被静默挤掉）。

**当前它仍未进入日常自动路由**，原因是结构性的，配置层解不掉：本域 `news_realtime`
声明 7 个源，fast 档 `budget=2` / auto-balanced=3，位次 3+ 本就不跑；而自适应学习器
按**族内分数**排序，新源分数起点低（seltz 实测 0.33，同族 people_daily/google_news 0.5），
laggard 规则（与族内最高分差 ≥0.15 即后置）会把它推到族末——即代码里记录的「饿死循环」。
要它真参与，需按本仓原则「**加槽不顶位**」（见 `route._VERTICAL_NEW_SOURCE` 的注释：
把新源提前会顶掉既有可用源，属横向替换而非净增益）给该域扩预算；而加槽目前只对垂直域
开口，`news_realtime` 是通用域不在其列。加槽是策略层改动，且 fast 档加槽会把延迟乘上去。

未走加槽前的可用入口：`--engine seltz` 单跑，或 `argo answer --scope ...` 直答。

`model` 可选 `seltz-base`（默认，一次 grounding 搜索）与 `seltz-pro`（模型自己发起检索，
更慢更贵）。

> 排查提示：升级或改动检索层后，先跑 `python3 scripts/engine_validate.py --engine seltz
> --stage health`。准入门禁会做相关性判据（结果与查询零词面交集即判负），能挡住
> 「字段齐全但语料接错」这类问题——这正是 Seltz 2026-09-16 之前拿 `quality_score=1.0`
> 通过准入的原因。

## 引用纪律（讲给用户时）

把搜索结果讲给用户时，凡是来自检索的事实都要带 URL 出处——**日常档也要带，不必等深度研究**。
URL 就在 `results[].url` 里（`--fields agent` 也保留），零额外成本。

`sources` 是 `results` 里 URL 的重投影，只在 `--envelope` 下生成（1.0 KB），形态是「底部相关链接」；
要那种整齐样式就加 `--envelope`，不加也不影响能引用。

## 内容质量信号

所有抓取结果自动附带：`content_ok`、`page_type`（article/list/forum/qa/docs/js_shell/auth_wall/paywall）、`source_type`（gov/edu/github/news/blog/forum/qa/docs-site/ecommerce）、`is_official`、`is_stale`（>365 天）、`content_age_days`、`quality_score`（0-1：长度 0.2+密度 0.2+结构 0.2+证据密度 0.3+标题 0.1）、`has_numbers/has_definition/has_comparison/has_howto`、`absorption_score`、`selection/credibility_fast`。

## 子技能细节

### local-search（本地零成本聚合）

- 33 本地引擎、29 默认启用，覆盖 web_general/chinese/academic/news/code/reference/vertical 七大类
- 注册表：`sub-skills/local-search/engine_registry.py`（唯一来源，加载 config.yaml + parse_maps.yaml）
- 健康探针：canary 查询 + 反爬检测，状态缓存 5 分钟；连续 2 次失败或单次 >8s 标记 unavailable
- 智能路由：`sub-skills/local-search/smart_router.py` 按查询特征选引擎组合
- 输出与 argo 同 schema，直接参与 RRF 融合

### 图片检索（网络图与本地图）

**网络图**走 `argo search`，命中 `image_search` 域（`openverse` + `wikimedia_commons`
+ `ddgs_images` 三源并行）。结果里的图片字段：

| 字段 | 含义 |
|------|------|
| `image_url` | 图片直链（可直接嵌入/下载；非详情页） |
| `image_license` | 归一后的协议名（`CC BY-SA 4.0`；上游短码 `by-sa` + 版本号已拼好） |
| `image_license_url` | 条款原文页（能点开核对的那一个） |
| `image_width` / `image_height` | 像素尺寸（int；上游没给则为缺省） |
| `image_commercial_ok` | `true` 可商用 / `false` 明确不可（NC/受版权）/ **缺省=未判定** |

三源分工：`openverse` 与 `wikimedia_commons` 是开放版权图库（**带许可，用于要发布的素材**），
`ddgs_images` 是全网图片（覆盖最广，但多数无许可信息，属「未判定」而非「不可商用」）。
中文查询 Openverse 命中率低，此时结果主要由 `ddgs_images` 承担——**许可字段会普遍缺失，
这是上游数据边界不是缺陷**，用在公开材料前须自行核对。

`image_search` 域内会自动剔除字段层面即可判定不可用的图（过小、极端长宽比）；
**尺寸未知的一律放行**（拿不到尺寸不等于图小）。剔除明细见输出 `image_dropped`，
重复图剔除数见 `image_dup_removed`（同一素材跨 CDN 的两个 URL 会被归一为一张）。

**本地图**走 `argo local-image`，用 macOS 内置 Vision 建索引（无第三方依赖）：

```bash
argo local-image index ~/Pictures ~/Downloads   # 建库；默认增量（(mtime,size) 变化才重算）
                                                # 约 0.12 秒/张（4 路并行），7.7 万张约 2.5 小时
argo local-image search "MCP 配置"              # 按 文件名 / 图中文字(OCR) / 分类标签 三档打分
argo local-image search --similar-to a.png      # 以图找相似（768 维 Vision 特征指纹）
argo local-image search "海报" -n 12 --sheet /tmp/s.png   # 候选拼成联络表
argo local-image stats                          # 索引概况
```

索引三个维度各自的用途：**图中文字**（截图/海报/文档类最有用）、**分类标签**（Vision
提供的场景词，如 stairs/document）、**特征指纹**（以图搜图与相似图判定）。
指纹用 SQLite BLOB 存，检索是矩阵点积（7.7 万张约 2-3ms），未引入向量索引——
这是几十万级规模，FAISS 的收益在千万级。

`--sheet` 是「脚本与多模态模型复合」的关键出口：把候选拼成一张联络表并给出
**编号 → 路径**映射。把这张图交给多模态模型，它能一次看清 12 张并回答「哪几张是用户
要的」——理解否定、关系、审美这些标签与 OCR 表达不了的部分。模型用编号作答，调用方
按映射换回路径（不让模型抄长路径，它会抄错）。

已知边界：纯氛围类查询（「暖光下孤独感的照片」）召回有洞——标签里不会出现「孤独」。
这类需要真正的图像-文本嵌入（CLIP 路线），本机当前未接（要 ~2GB 依赖）；接口留了位，
需要时再挂，不为少数场景让所有安装背上重依赖。

### local-seek（本机文件搜索）

```bash
python3 sub-skills/local-seek/scripts/seek.py "查询词" --path ~/notes --count   # L1 定位
python3 sub-skills/local-seek/scripts/seek.py "查询词" --path ~/notes --context # L2 上下文
python3 sub-skills/local-seek/scripts/seek.py "查询词" --path ~/notes --lines  # L3 精读
```

- 路由：rg（正文）→ fd（文件名）→ mdfind（Spotlight 全盘保底）；中文「精确优先、2-gram 扩展保底」
- 扩展：`--structural`（裸 except/空 catch/装饰函数）、`--git-log`/`--git-blame`、`--outline`、`--domains`
- MCP：`argo_local_search` subprocess 调用 seek.py，包装为 `file://` URL + `source=local_files`

### ego-search（登录态专业搜索）

```bash
python3 sub-skills/ego-search/scripts/ego_search.py status
python3 sub-skills/ego-search/scripts/ego_search.py search "AI 搜索" --runtime auto
python3 sub-skills/ego-search/scripts/ego_search.py fetch "https://www.zhihu.com/..." --site zhihu.com
python3 sub-skills/ego-search/scripts/ego_search.py merge --public /tmp/p.json --login /tmp/l.json
```

- 双运行时：ego lite + Kimi WebBridge，`--runtime auto|ego|webbridge`
- 与常规检索隔离：`search_partition=login`、`cache_eligible=false`；`--site host` 粘性空间
- 专业模式默认关：`enable`/`disable`/`status`


## 功能开关总表（`ARGO_*`，唯一事实=源码扫描）

> 56 个开关按五类 MECE。原则：调试/运行配置不进模型上下文（MCP schema 不暴露）；
> 本表由门禁与源码双向锁定——文档里的开关必须代码实存，代码新增开关必须入表。

### 能力开关（28）

| 变量 | 作用 | 默认/备注 |
|------|------|----------|
| `ARGO_ADMISSION_DIR` | admission dir | 见 references/operations.md 与模块 docstring |
| `ARGO_ALLOW_PRIVATE_URLS` | allow private urls | 见 references/operations.md 与模块 docstring |
| `ARGO_ALLOW_RECOMPUTE` | ('research 可复算脚本执行', '默认拒绝，显式授权') | 见 references/operations.md 与模块 docstring |
| `ARGO_CONFIG_CACHE` | ('配置解析缓存', '默认开') | 见 references/operations.md 与模块 docstring |
| `ARGO_CONFIG_STAMP_TTL_S` | config stamp ttl s | 见 references/operations.md 与模块 docstring |
| `ARGO_DISABLE_ENGINES` | disable engines | 见 references/operations.md 与模块 docstring |
| `ARGO_ENABLE_ENGINES` | enable engines | 见 references/operations.md 与模块 docstring |
| `ARGO_ENGINE_HTTP_CLIENT` | ('HttpClient 渐进增强层（UA轮换/节流/重试）', '默认开；=0 回退 urllib 保底') | 见 references/operations.md 与模块 docstring |
| `ARGO_ENV_FILE` | env file | 见 references/operations.md 与模块 docstring |
| `ARGO_FULLTEXT` | ('抓取全文存档（--full）', '随 --full 启用') | 见 references/operations.md 与模块 docstring |
| `ARGO_IDENTITY_MEMORY` | identity memory | 见 references/operations.md 与模块 docstring |
| `ARGO_LOCAL_RERANK` | ('本地五维重排', '默认开') | 见 references/operations.md 与模块 docstring |
| `ARGO_MINHASH_DEDUPE` | ('近重复结果去重', '默认开') | 见 references/operations.md 与模块 docstring |
| `ARGO_MOBILE_FIRST_HOSTS` | mobile first hosts | 见 references/operations.md 与模块 docstring |
| `ARGO_NO_AUTORELOAD` | no autoreload | 见 references/operations.md 与模块 docstring |
| `ARGO_NO_CACHE` | ('跳过结果缓存读', '默认关；等价 --no-cache') | 见 references/operations.md 与模块 docstring |
| `ARGO_PROXY` | proxy | 见 references/operations.md 与模块 docstring |
| `ARGO_PYTHON` | python | 见 references/operations.md 与模块 docstring |
| `ARGO_REDSKILL_CACHE` | redskill cache | 见 references/operations.md 与模块 docstring |
| `ARGO_RESPECT_ROBOTS` | ('robots.txt 遵守', '默认遵守') | 见 references/operations.md 与模块 docstring |
| `ARGO_ROUTE_CACHE` | ('路由决策跨进程缓存', '默认开；测试环境默认关') | 见 references/operations.md 与模块 docstring |
| `ARGO_RRF_WEIGHTED` | ('RRF 加权融合', '默认关（逃生开关）') | 见 references/operations.md 与模块 docstring |
| `ARGO_SEMANTIC_EVIDENCE` | ('可选语义证据层（classifier.dev）', '默认关，个人可选开') | 见 references/operations.md 与模块 docstring |
| `ARGO_SERP_GUARD` | ('SERP 垃圾结果守卫', '默认开；=0 关闭') | 见 references/operations.md 与模块 docstring |
| `ARGO_USAGE_LOG` | ('本地使用日志（JSONL 流，stats 数据源）', '默认开；=0 整体关闭') | 见 references/operations.md 与模块 docstring |
| `ARGO_UNPAYWALL_EMAIL` | unpaywall email | 见 references/operations.md 与模块 docstring |
| `ARGO_WOLFRAM_APPID` | wolfram appid | 见 references/operations.md 与模块 docstring |
| `ARGO_XHS_TIMEOUT` | xhs timeout | 见 references/operations.md 与模块 docstring |

### API 密钥（值放 ~/.config/argo/env）（17）

| 变量 | 作用 | 默认/备注 |
|------|------|----------|
| `ARGO_ANYSEARCH_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_BOCHA_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_BRAVE_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_BYTED_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_EASTMONEY_APIKEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_EXA_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_FELO_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_FIRECRAWL_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_GITHUB_TOKEN` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_METASO_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_OCTEN_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_QWEATHER_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_TAVILY_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_TINYFISH_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_WEB_SEARCH_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_WEREAD_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_ZHIHU_ACCESS_SECRET` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |

### 行为调参（7）

| 变量 | 作用 | 默认/备注 |
|------|------|----------|
| `ARGO_ADMISSION_TTL_S` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_FETCH_DEADLINE_S` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_MINHASH_THRESHOLD` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_ROUTE_SAMPLE_RATE` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_SERIAL_STAGGER_S` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_STRAGGLER_GRACE_S` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_TOOL_CALL_COALESCE` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |

### 路径与数据位置（10）

| 变量 | 作用 | 默认/备注 |
|------|------|----------|
| `ARGO_ARCHIVE_ROOT` | 数据/状态位置覆盖 | 默认惯例目录（argo paths 查看） |
| `ARGO_CLIENTS_PATH` | 数据/状态位置覆盖 | 默认惯例目录（argo paths 查看） |
| `ARGO_FULLTEXT_DIR` | 数据/状态位置覆盖 | 默认惯例目录（argo paths 查看） |
| `ARGO_HOME_OVERRIDE` | 数据/状态位置覆盖 | 默认惯例目录（argo paths 查看） |
| `ARGO_LINK_TARGETS` | 数据/状态位置覆盖 | 默认惯例目录（argo paths 查看） |
| `ARGO_LOCAL_READ_DIRS` | 数据/状态位置覆盖 | 默认惯例目录（argo paths 查看） |
| `ARGO_LOCAL_SEEK_PATH` | 数据/状态位置覆盖 | 默认惯例目录（argo paths 查看） |
| `ARGO_LOCAL_SEEK_ROOTS` | 数据/状态位置覆盖 | 默认惯例目录（argo paths 查看） |
| `ARGO_STATE_DIR` | 数据/状态位置覆盖 | 默认惯例目录（argo paths 查看） |
| `ARGO_USAGE_LOG_DIR` | 数据/状态位置覆盖 | 默认惯例目录（argo paths 查看） |

### MCP 运行配置（6）

| 变量 | 作用 | 默认/备注 |
|------|------|----------|
| `ARGO_MCP_PRETTY` | MCP 输出缩进美化 | server 层接管，模型不可见 |
| `ARGO_MCP_SKIP_CACHE` | 跳过缓存直连 | server 层接管，模型不可见 |
| `ARGO_MCP_TIMEOUT` | MCP 工具统一超时秒 | server 层接管，模型不可见 |
| `ARGO_MCP_TIMEOUT_CRAWL` | crawl 专用超时 | server 层接管，模型不可见 |
| `ARGO_MCP_TIMEOUT_FETCH` | fetch 专用超时 | server 层接管，模型不可见 |
| `ARGO_MCP_TOOLS` | tools/list 注入范围（core 三件套/all/逗号名单） | server 层接管，模型不可见 |

### 抓取降级链分级开关（7）

| 变量 | 作用 | 默认/备注 |
|------|------|----------|
| `ARGO_FETCH_IMPERSONATE` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_JINA` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_MD_NEGOTIATE` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_MD_VARIANT` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_MOBILE` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_PARALLEL` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_TINYFISH` | fetch 降级链单级启停 | 默认自动降级 |

## 监控与取证命令（v2.9.0 补齐，MCP 同步有 argo_watch/argo_preflight/argo_cite）

### argo watch — 网页变化监控

```bash
argo watch add "url" --note "备注"   # 登记快照（本地状态文件）
argo watch check [url]               # 复查变化（缺省查全部）；cron 可用
argo watch list                      # 看清单
argo watch remove "url"              # 取消关注
```
适合盯价格页/公告页/版本发布页。MCP 工具 `argo_watch` 的 action 参数与此一一对应。

### argo preflight — 批量 URL 预检

```bash
argo preflight "url1" "url2" ...     # 纯本地规则：登录墙/已死/已归档/需确认分类
argo preflight --probe "url1" ...    # 追加联网探测（404/410 判死）
```
引用或抓取一批来源前先过一遍。MCP 工具 `argo_preflight` 同能力。

### argo cite — DOI 引用条目

```bash
argo cite "10.1038/xxx" "10.1000/yyy"          # 默认 GB/T 7714
argo cite "10.1038/xxx" --style apa            # apa | bibtex | numeric | gbt7714
```
Crossref+OpenAlex 免 key。MCP 工具 `argo_cite`（dois 数组 + style）。

### argo job / paths / mcp

- `argo job "query" --city 成都`：BOSS/猎聘/智联/前程无忧/597/今日招聘并发聚合。
- `argo paths [--check] [--migrate]`：状态目录/密钥文件位置自省与本机自检。
- `argo mcp {{status|inject|undo}}`：多客户端 MCP 一键注入/诊断/还原。

## 日志与反馈（本地使用日志 + stats 读出口）

### 数据在哪、有什么

`<状态目录>/usage_log/`（`argo stats` 首行给出实际路径）下四个 append-only JSONL 流，
单流 1MB/2000 行自动回缩，**本地数据不出本机**：

| 流 | 一条 = | 用途 |
|----|--------|------|
| `query` | 一次非缓存搜索的总账：query(截断60字)/count/elapsed_ms/engines_used/errors/recovered | 使用统计与命中率 |
| `recovery` | 救援链触发概览 | 看救援是否过度 |
| `route` | 路由采样（域/引擎/置信/语言） | 路由质量分析 |
| `merge` | 本地数据融合概览 | 数据融合审计 |

### 怎么读

```bash
argo stats          # 汇总：命中率/平均时延/引擎频次/救援率 + 最近 5 条
argo stats -n 200   # 回看窗口加大
ARGO_USAGE_LOG=0    # 整体关闭（写入侧静默失败，关闭零风险）
```

### 反馈闭环（搜索质量的自我修正）

- **失败归因寄存器**：引擎级失败（限流/封锁/网络）写入归因，聚合层区分「引擎坏」与「被挡住」；
- **自适应调度**：adaptive 学习器消费引擎结果质量，坏源自动降权、好源升权（弃置≠超时已分流，不毒化学习器）;
- **熔断**：连续失败引擎熔断跳过，恢复后自动回归；
- **归档**：`argo search --archive` 留完整候选（流量回放/审计用），`ARGO_ARCHIVE_ROOT` 定位置。

隐私纪律：本地使用日志只在本机、query 截断脱敏、**本机用量统计不进任何对外材料**（判据=对方能否在仓库里复现该数字）。

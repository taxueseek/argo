# Argo 详细用法（v2.9.0）

> 本文受门禁保护（tests/test_usage_doc_gates.py）：文中命令必须存在于
> bin/argo 分发表、`ARGO_*` 开关必须代码实存。改命令先改代码，本文随行。

## 命令总览（29 个，唯一事实=bin/argo 分发表）

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
| `argo wx` | 微信文章多级降级抓取（HTTP→CDP→搜狗兜底，仅 mp.weixin.qq.com 链接） |
| `argo screenshot` | 网页截图（`--full-page`/`--output`） |
| `argo pdf` | PDF 正文提取（`--pages`/`--password`，`--outline`/`--section` 分节读取） |
| `argo paper` | arXiv 论文深读（`--outline`/`--section`/`--latex`/`--cited-by`） |
| `argo tweet` | X 帖子完整打包（正文全文+串/引用/转发+媒体，`--out` 落盘） |
| `argo x-search` | X 关键词搜索（原生布尔语法 from:/-is:retweet/lang:；ego-browser 通道） |
| `argo template` | 查询模板（命名检索模式，展开并转发给 search/x-search；`--list`/`--add`） |
| `argo answer` | 直答（Seltz 带引用合成答案） |
| `argo watch` | 网页变化监控（add/check/list/remove） |
| `argo auth` | 浏览器登录态（login/status/logout；登录墙站点抓取自动带会话） |
| `argo job` | 招聘多平台聚合 |
| `argo preflight` | 批量 URL 预检（`--probe` 联网探测） |
| `argo engine` | 新引擎一键接入（spec→探测→派生→语料→验证→准入→同步；`add --from-yaml/--from-url`） |
| `argo cite` | DOI 引用条目（四格式） |
| `argo mcp` | 多客户端 MCP 注入/诊断/还原 |
| `argo paths` | 路径自省与状态目录自检 |
| `argo daemon` | 常驻进程管理（search 自动走 UDS 快路径，见「argo daemon」节） |
| `argo stats` | 使用日志与反馈状态（本地使用日志只读出口） |
| `argo budget` | 按引擎的月度预算（原生币种；list/set/unset） |
| `argo quota` | 知乎开放平台当日额度（官方 API，不消耗业务额度） |
| `argo local-image` | 本地图片检索（Vision 索引；默认关，见「图片检索」节） |

> SKILL.md 只留核心命令；本页是参数大全与输出字段说明。

## argo daemon — 常驻进程与 UDS 快路径（2026-10）

`argo search` 每次冷调用要付「解释器启动 + engines 全家 import」约 85–100ms
固定成本；agent 循环一次研究任务调 15–20 次，这笔成本放大成 1.5–2s。daemon
把 import 付一次，此后每笔 search 走 UDS 快路径（本地零 import 直达常驻进程）。

```bash
argo daemon start     # 后台启动（socket/pid/指针文件落状态根，见 argo paths）
argo daemon status    # 运行状态
argo daemon stop      # 停止并清场（等在途请求收尾，上限 20s）
```

- 快路径生效：daemon 在位时 `argo search ...` 直达常驻进程。**默认不自启**
  （2026-10-18 起改为显式 opt-in）：用 `argo daemon start` 或 `ARGO_DAEMON=1`
  拉起。实测净收益中位 33ms/笔（缓存命中 41 vs 75ms），1–3s 的网络查询下无感，
  故不再默认养 20–27MB 常驻进程。任何失败——不在位、连不上、正忙、源码已
  变更——都静默回退常态执行，CLI 行为不变。`ARGO_NO_DAEMON=1` 强制关闭，
  优先级高于 `ARGO_DAEMON`。
- 第一笔请求多付 ~100ms（daemon 内首次 import search 全家），此后全热路径。
- daemon 空闲 1800s 自杀退出（`start --idle-exit N` 调整，0=常驻）；
  SIGTERM 同样清场。
- 请求串行执行（stdout 重定向是进程级全局态）；前一笔未收尾时新请求立即返回
  忙并静默回退冷路径（不排队等，避免排队把客户端拖到超时）。argo 内部引擎
  并发不受影响。
- **源码变更即退役**：daemon 启动时记录 scripts/*.py 指纹，请求时比对；
  `git pull`/改脚本后旧 daemon 拒绝执行并退出，客户端回退冷路径、后台拉起
  新代码的 daemon——不会静默跑旧代码。
- 按次环境变量（`FOO_KEY=x argo search`、`ARGO_*` 覆盖）随请求透传给 daemon，
  请求结束即还原，与冷路径行为一致。
- socket 路径超 AF_UNIX 104 字节上限时自动落 `/tmp/argo-daemon-<uid>/`
  短路径哈希位，客户端经指针文件 `.daemon_sock-<树指纹>` 发现，无需感知。

## search 完整参数

```bash
python3 scripts/search.py "查询词" \
  [--engine ENGINE]           # 强制引擎（可多个）\
  [--max-results N]           # 每引擎结果数\
  [--depth fast|balanced|deep] # 搜索深度\
  [--mode fast|auto|deep|budget] # 预算模式\
  [--local-first]             # 本地零成本聚合优先\
  [--no-cache] [--explain] [--json]\
  [--timeout S]\
  [--since 7d|2026-08-01] [--until 2026-08-01] [--sort relevance|oldest|newest]\
  [--domain DOMAIN] [--sub_domain SUB_DOMAIN]  # 垂直域限定\
  [--input-kind auto|keyword|url-seed|known-url]\
  [--plan-only] [--force-search] [--envelope]\
  [--timing|--no-timing]       # 输出里的阶段耗时，默认开\
  [--archive] [--archive-dir DIR] [--archive-tag TAG] [--archive-note NOTE]\
  [--verify [TOP_K]]          # 核验 top-K 未核验结果并回填证据分
```

**时间窗**：`--since`/`--until` 支持相对值（`7d`）或绝对日期（`2026-08-01`，含当天）；下推到支持时间窗的引擎，任意引擎组合融合后按 `published_at` 保底过滤（`time_filtered: N`）；`--sort newest` 找最新动态、`oldest` 找最早出处。`wayback_cdx` 输出标准 `published_at`（CDX 最早快照）。

**按场景选引擎**（唯一真源，SKILL.md 只留指针）：时事新闻/实体人物/带时间窗的查询 → `--engine deepseek_search`（默认开、按次计费；结果带 authority 权威分级与发布时间，`--since/--until` 只对它真过滤——另两家无日期字段；多语言与 anysearch 持平，exa 韩语查询会空手）；学术 → `--academic-deep` 或 exa；社交 UGC → anysearch。

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
    // useful_ms：最后一个有效引擎完成的时刻（答案从这一刻起已在手里）。
    // tail_wait_ms：结果已可交付之后仍在等的墙钟——早停时刻成立就用早停时刻算，
    //   no_early 域退回首条结果时刻。它才是「收工早一点能省多少」的量。
    // abandoned_engine_ms：早停时被弃置的引擎已花墙钟之和（并行资源浪费，
    //   不拖住返回，所以不等于用户多等）。wasted_ms 是它的旧别名。
    //   仍被等到超时的引擎**不算**浪费：那是引擎不健康，看 engine_latency 与熔断。
    "useful_ms": 394, "tail_wait_ms": 0, "abandoned_engine_ms": 143,
    "first_sufficient_ms": 394, "wasted_ms": 143, "early_stopped": true
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

`--list-engines` 列名字约 3 KB（2026-09-19 实测 2697 B，可放心用）。

**`--detail` 不带过滤 = 分组摘要 ≈2.5 KB**（2026-09-27 起）：答的是「多少个源
可用、不可用的那些为什么不可用」——计数 + 成因分组 + 只列不可路由项。
要逐条全量（含每引擎的熔断/配额/准入/依赖运行态）加 **`--all`**，约 54 KB，
那是诊断转储、不是日常档（2026-09-16 前是 151 KB，2026-09-19 压到 50 KB）。

**`--detail --engine <名>`（逗号分隔可多个）= 单引擎全量诊断**，~0.9 KB/个；
未收录的名字会走 stderr 提示。

```bash
python3 scripts/search.py --list-engines --detail                            # ≈2.5 KB 摘要
python3 scripts/search.py --list-engines --detail --engine egov_law,kor_law  # ≈2 KB 全量行
python3 scripts/search.py --list-engines --detail --all --json                # ≈54 KB 逐条
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
python3 scripts/clarify.py "有歧义的查询" --json
```

`ambiguities`（歧义词+可能含义+置信度）、`intents`（意图分类）、`recommended_strategy`（clarify_first/deep_research/split_search/direct_search）。

## 抓取三工具细节

### argo_fetch

```bash
argo fetch "https://example.com"                      # HTTP 优先，失败自动升级浏览器
argo fetch "https://example.com/long-article" --focus "关键词"   # BM25 聚焦，省 token
argo fetch "https://cloudflare-protected.com" --use-browser     # 强制反检测浏览器
argo fetch "https://example.com/post" --json --include-links    # 附带正文链接（默认只给 links_count）
```

- **文章级元数据**：同一份 HTML 里顺带提取 `byline` / `excerpt` / `site_name` / `published_at`，不额外联网；`--include-links` 时附带 `<article>/<main>` 内的链接（最多 30 条，相对链接已解析）。没有语义容器时宁可不给，也不把导航链接当正文引用。

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
- **内容安全引擎**：抓取内容先过注入检测再交给 Agent——70+ 中英日韩俄阿希泰模式（指令覆盖/角色操纵/系统提示泄露/越狱/数据外泄/身份冒充/XSS）+ 编码归一化（零宽字符/RTL/Unicode 同形字/base64/URL 编码）+ 语义意图分析 + 风险评分 + 目标脱敏。输出 `content_security.content_clean / risk_score / threat_count / threat_types / redactions / content_lang`。**风险门禁**（v2.9.8，默认开）：`risk_score ≥ ARGO_CONTENT_RISK_THRESHOLD`（默认 0.5）的正文被隔离——用清洗后正文替换原始正文、标 `content_quarantined`、`content_ok=false`，且不进证据分；开关 `ARGO_CONTENT_RISK_GATE=0` 应急放行。
- 单独调用：`python3 scripts/content_security.py "文本" --json` 或 `--stdin < content.txt`

### argo_screenshot

```bash
argo screenshot "https://example.com" [--full-page] [--output /tmp/page.png]
```

### argo_pdf

```bash
argo pdf "https://example.com/paper.pdf" [--pages "1-5"] [--password "secret"]   # 支持本地路径
argo paper "1706.03762" --outline          # 分节大纲（先看 id）
argo paper "1706.03762" --section 2        # 读单节（缓存后秒回）
argo paper "1706.03762" --latex            # LaTeX 源精读（公式级）
argo paper "1706.03762" --cited-by 10      # 引文图（S2，配 key 更稳）
```

### argo_tweet

给定推文 URL 或 ID，把**整份内容**吸收到本地。`argo search` 负责**发现**推文（把推文压成一条结果参与融合），本命令负责**吸收**——同一条推文，搜索给「一行」，打包给「整份」。

```bash
argo tweet "https://x.com/jack/status/20"                  # 正文全文打到 stdout
argo tweet "20" --json                                     # 结构化 JSON（全文+媒体 URL+互动+串/引用）
argo tweet "20" --out ./tweets/20                          # 落盘四件套
argo tweet "20" --out ./tweets/20 --media                  # 再下载图片/视频
argo tweet "20" --mode thread|conversation|quotes|reposts  # 串 / 对话 / 引用 / 转发
argo tweet "20" --with-quotes --with-reposts --limit 50    # 在主 mode 上叠加分区
```

- **按需采用**：默认只拉正文与结构、**不下载媒体**（媒体只记 URL）；`--out DIR` 才落盘，`--media` 才下载。
- **四件套**（`--out`）：`post.md`（可读正文 + 本地媒体相对路径）、`raw.json`（上游原始响应）、`manifest.json`（媒体清单/计数/路径）、`CHECKLIST.md`（PASS/PARTIAL 校验表，媒体部分失败标 PARTIAL 但仍交付）。
- **媒体下载**：图片直链 + 视频按 `--video-quality max|min|720` 选 mp4（跳过 m3u8）；每跳都过 SSRF 守卫（拒内网/保留地址）并走代理配置。
- **零 Key、不碰 Cookie/登录态**；上游 FxTwitter（`api.fxtwitter.com`，备镜像 `api.fixupx.com`）。
- **与 fetch 的分工**：`argo fetch` 是通用网页正文（抓 x.com 页面会经第三方 reader，无结构、无媒体）；X 的完整结构化数据只有本命令拿得到。

### argo x-search — X 关键词搜索

X 原生搜索是唯一吃 X 布尔语法的通道（`from:` / `-is:retweet` / `min_faves:` /
`lang:` / `since:`），通用网页引擎一律忽略这些语法。借 ego-browser 驱动已登录的
浏览器抓 `x.com/search`（同 `x_trending` 引擎的通道思路），需已安装 Ego Lite 且
浏览器已登录 X。

```bash
argo x-search "OpenAI" -n 20                     # 关键词搜索
argo x-search 'from:OpenAI -is:retweet' --json   # 盯人 + 去转发，JSON 输出
argo x-search '(GPT OR Claude) release -is:retweet' --sort top   # 热门榜
```

- **与 `argo tweet` 的分工**：本命令**发现**（关键词 → 一批推文），`argo tweet`
  **吸收**（单条 URL/ID → 整份内容）。
- **与 `--engine fxtwitter` 的关系**：后者是 HTTP 免登录搜索通道，上游公开实例的
  `/2/search` 已停用（恒返回空），关键词搜索请走本命令。

### argo template — 查询模板

命名好的检索模式，一步展开执行：取模板 → 展开成查询串 → 转发给模板的 command。
注册表 `references/query-templates.yaml`（随包）+ `~/.config/argo/query-templates.yaml`
（用户，零代码扩展）。完整说明见 `references/query-templates.md`。

```bash
argo template --list                              # 列出全部模板
argo template ai_model_release --add "GPT-5"      # 模板 + 追加词（空格即 AND）
argo template ai_model_release --dry-run          # 只看展开后的命令与查询串
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

核验有全局墙钟 `ARGO_VERIFY_BUDGET_S`（默认 5s）：到点未回来的 URL 标进
`pending`，已回来的照常回填；`--fields agent` 保留 `verify.revision_summary`、
`pending` 与逐条 `delta`，不必为了看核验结果放弃精简档。

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

**本地图**走 `argo local-image`，用 macOS 内置 Vision 建索引（无第三方依赖）。

> **默认关闭**：需 `ARGO_LOCAL_IMAGE=1` 才可执行（它要 macOS+swiftc+numpy，
> 且先建索引才有数据）。不开启时命令直接拒绝，不建库、不占资源。

```bash
export ARGO_LOCAL_IMAGE=1                        # 或写进 ~/.config/argo/env
argo local-image index ~/Pictures ~/Downloads   # 建库；默认增量（(mtime,size,inode) 变化才重算）
                                                # 约 0.02–0.12 秒/张（4 路并行，随图大小）
argo local-image search "MCP 配置"              # 按 文件名 / 图中文字(OCR) / 分类标签 三档打分
argo local-image search --similar-to a.png      # 以图找相似（768 维 Vision 特征指纹）
argo local-image search "海报" -n 12 --sheet /tmp/s.png   # 候选拼成联络表
argo local-image stats                          # 索引概况
```

索引三个维度各自的用途：**图中文字**（截图/海报/文档类最有用）、**分类标签**（Vision
提供的场景词，如 stairs/document）、**特征指纹**（以图搜图与相似图判定）。

存储分两处：`index.db`（SQLite）放元数据（路径/尺寸/标签/OCR/`fp_slot`），指纹另存
同目录的 `fp.npy`——一个连续 float32 矩阵，检索时 mmap 只读、一次矩阵点积出全部
相似度（**7.7 万张约 4ms**）。指纹写入时即 L2 归一化，点积即余弦。实测索引约
**3.8 KB/张**（指纹 3KB 为主），7.7 万张约 290MB，占原图总量的 0.2%–8%（随原图
大小）。`fp.npy` 是派生件：每次 `index` 结束按当前表重建，删了会自动重生成。

未引入向量索引（FAISS/hnswlib）：连续矩阵点积已 4ms，FAISS 的价值在千万级，这里
是几十万级，引入它是为不存在的规模付费。

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

### js-run（V8 算车道）

不开浏览器跑网页 JS：V8（mini-racer）+ 自研环境垫片执行「环境探测 + 纯计算」型脚本，单次上下文 P50 ≈ 1ms，逻辑时间让 `setTimeout(5000)` 瞬间完成。已接入 fetch 降级链第一级 E（`scripts/fetch_jsrun.py`）：抓页遇到 JS 挑战时自动跑挑战脚本算通行证再重试。

- 入口：无独立命令——fetch 链第一级 E 自动触发；开关 `ARGO_FETCH_JSRUN`（默认开，`=0` 关闭）
- 三类活：反爬挑战通行证、混淆数据解算、JS 内嵌状态提取
- v0 边界：不解析 DOM/布局、一个字节的网络都不取（取数归 HTTP 车道）、无登录态可继承（归 ego-search）、不截图（归 ego-browser）
- 纪律：产物只当线索不当正文——垫片里的「网络响应」没有真源，数据必须 HTTP 侧真取；对第三方站点的挑战绕过默认关闭，启用须单独拍板
- 金标：`python3 sub-skills/js-run/tests/test_jsrun.py`（7 项，含速度门 <100ms）


## 查询操作符（Google 风格语法，2026-10-17 起）

查询词里可带下列指令，解析层（`scripts/query_operators.py`）把它们从路由与检索两侧剥掉、结构化消费——指令不再污染路由（`site:` 今天以前会把路由锁死进错误域）：

| 操作符 | 示例 | 效果 |
|---|---|---|
| `site:` / `-site:` | `DeepSeek site:github.com OR site:huggingface.co` | 站点限定/排除（v1 记入可观测，按引擎下推开发中） |
| `-词` / `-"短语"` | `GPT-4o -gpt-3` | 排除关键词/短语（汇合否定理解，融合后过滤） |
| `"短语"` | `"精确短语"` | 精确匹配（保留在检索串，phrase_filter 照常提取） |
| `lang:` | `模型 lang:zh,en` | 语言覆盖（替代自动检测，走路由语言偏好） |
| `after:` / `before:` | `模型 after:2026-01-01` | 发布时间窗兜底（CLI `--since/--until` 显式值优先） |

## 新增结果字段（2026-10-17）

| 字段 | 含义 |
|---|---|
| `authority_level` | 离散权威等级 0-4（≥3 极权威，对齐 DeepSeek Search 语义；上游原生值优先，否则由连续 authority 分映射） |
| `summary` | Agent 档摘要（≤800 字，核验过的结果才有：`--verify` 当轮产出、之后经证据缓存回填，不重新抓取也有） |
| `upstream_authority` / `upstream_score` | 上游源原生信号透传（deepseek_search 等，不覆盖 Argo 自己的 authority/score） |

## 功能开关总表（`ARGO_*`，唯一事实=源码扫描）

> 120 个开关按六类 MECE。原则：调试/运行配置不进模型上下文（MCP schema 不暴露）；
> 本表由门禁与源码双向锁定——文档里的开关必须代码实存，代码新增开关必须入表。

### 能力开关（41）

| 变量 | 作用 | 默认/备注 |
|------|------|----------|
| `ARGO_ADMISSION_DIR` | admission dir | 见 references/operations.md 与模块 docstring |
| `ARGO_ALLOW_PRIVATE_URLS` | allow private urls | 见 references/operations.md 与模块 docstring |
| `ARGO_ALLOW_RECOMPUTE` | ('research 可复算脚本执行', '默认拒绝，显式授权') | 见 references/operations.md 与模块 docstring |
| `ARGO_CONFIG_CACHE` | ('配置解析缓存', '默认开') | 见 references/operations.md 与模块 docstring |
| `ARGO_CONFIG_STAMP_TTL_S` | config stamp ttl s | 见 references/operations.md 与模块 docstring |
| `ARGO_CONTENT_RISK_GATE` | ('内容风险门禁：高风险正文隔离 + 清洗后替换', '默认开；=0 放行高风险内容') | 见 content_security.py docstring |
| `ARGO_CONTENT_RISK_THRESHOLD` | ('门禁阈值：risk_score ≥ 此值即隔离', '默认 0.5') | 见 content_security.py docstring |
| `ARGO_COMPLETENESS_V2` | ('完整性维度 QSDM 信息/噪声比', '默认开；=0 退回长度启发式') | 见 rank_signals.py docstring |
| `ARGO_CROSS_DOMAIN_HOMOGENEITY` | ('跨域内容同质惩罚（异域发同一篇内容降权，对应 Google scaled content abuse）', '默认开') | 见 rank_signals.py docstring |
| `ARGO_DOMAIN_CONCENTRATION` | ('域级聚合惩罚（同域占比过半降权）', '默认开') | 见 rank_signals.py docstring |
| `ARGO_DISABLE_ENGINES` | disable engines | 见 references/operations.md 与模块 docstring |
| `ARGO_ENABLE_ENGINES` | enable engines | 见 references/operations.md 与模块 docstring |
| `ARGO_ENGINE_HTTP_CLIENT` | ('HttpClient 渐进增强层（UA轮换/节流/重试）', '默认开；=0 回退 urllib 保底') | 见 references/operations.md 与模块 docstring |
| `ARGO_ENV_FILE` | env file | 见 references/operations.md 与模块 docstring |
| `ARGO_FULLTEXT` | ('抓取全文存档（--full）', '随 --full 启用') | 见 references/operations.md 与模块 docstring |
| `ARGO_HTTP_POOL` | ('HTTP keep-alive 连接池', '默认开；=0 退回一次请求一条连接') | 同主机请求复用 TCP+TLS 连接；对拍/应急用 0 |
| `ARGO_IDENTITY_MEMORY` | identity memory | 见 references/operations.md 与模块 docstring |
| `ARGO_IMAGE_DB` | ('本地图片索引库路径', '默认 ~/.cache/unified-search/argo-image/index.db') | 建库与检索共用；指纹矩阵 fp.npy 与它同目录 |
| `ARGO_LOCAL_IMAGE` | ('本地图片检索（local-image 子命令）', '默认关；=1 开启') | 要 macOS+swiftc+numpy 且先建索引，默认不启用 |
| `ARGO_LOCAL_RERANK` | ('本地五维重排', '默认开') | 见 references/operations.md 与模块 docstring |
| `ARGO_MAX_REDIRECTS` | ('最大重定向跳数', '默认 5；合法 1-20，越界回落默认') | 见 http_client.py `_max_redirects` |
| `ARGO_MCPJOBS_DIR` | ('mcp-jobs 安装根', '默认随平台缓存根 argo-mcpjobs') | job.py 安装与启动共用；换位置只改这一处 |
| `ARGO_MINHASH_DEDUPE` | ('近重复结果去重', '默认开') | 见 references/operations.md 与模块 docstring |
| `ARGO_MOBILE_FIRST_HOSTS` | mobile first hosts | 见 references/operations.md 与模块 docstring |
| `ARGO_MULTI_QUERY` | ('multi-query 变体召回（普适性增强）', '默认开；=0 一键回滚/A-B') | 见 variant_recall.py docstring |
| `ARGO_NO_AUTORELOAD` | no autoreload | 见 references/operations.md 与模块 docstring |
| `ARGO_NO_CACHE` | ('测试模式：跳过结果缓存读', '默认关；等价 --no-cache。仅改进 Skill/插件需要实时结果时开') | 见 references/operations.md 与模块 docstring |
| `ARGO_NO_PREWARM` | ('MCP warm 线程里的 anysearch 预连接', '默认开；非空且非 0 时跳过') | 预连接把 TLS 握手藏进 initialize 到首次 tools/call 的空档；对拍/排障时置 1 |
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
| `ARGO_VIDEO_ASR_CMD` | ('视频转写兜底命令模板', '默认空=不转写；命令含 {url} 或自动追加 URL') | 仅 video_fetch.py 使用；shlex.split 执行、不走 shell |
| `ARGO_WOLFRAM_APPID` | wolfram appid | 见 references/operations.md 与模块 docstring |
| `ARGO_XHS_TIMEOUT` | xhs timeout | 见 references/operations.md 与模块 docstring |

### API 密钥（值放 ~/.config/argo/env）（23）

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
| `ARGO_JEV_API_KEY` | TypeSafe JEV key（语义证据层 `semantic_evidence`；官方名 `TYPESAFE_API_KEY` 同样识别） | 缺密钥=语义层静默跳过，核验链路照常 |
| `ARGO_METASO_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_OPENCORPORATES_API_TOKEN` | 对应引擎/服务的凭据（OpenCorporates，2026-10 起匿名请求被上游全面拒 401） | 缺密钥=该源跳过或降级 |
| `ARGO_OCTEN_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_PARALLEL_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_QWEATHER_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_SELTZ_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_TAVILY_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_TINYFISH_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_WEB_SEARCH_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_WEREAD_API_KEY` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |
| `ARGO_YDC_API_KEY` | 对应引擎/服务的凭据（you.com） | 缺密钥=该源跳过或降级 |
| `ARGO_YUANBAO_API_KEY` | 对应引擎/服务的凭据（腾讯元宝搜索，官方名 `TENCENTCLOUD_WSA_APIKEY` 同样识别） | 缺密钥=该源跳过或降级 |
| `ARGO_ZHIHU_ACCESS_SECRET` | 对应引擎/服务的凭据 | 缺密钥=该源跳过或降级 |

### 行为调参（37）

| 变量 | 作用 | 默认/备注 |
|------|------|----------|
| `ARGO_ADAPTIVE_GRACE` | 对冲宽限窗按主引擎历史 P75 自适应收窄（只缩不放） | 默认开；=0 关闭（回固定 base 窗，作消融对照）。v2 冻结期 A/B 曾判未证实收益，c52b7d4 起转默认启用 |
| `ARGO_GENERAL_FALLBACK_STATIC` | 通用兜底链完全回到静态元组顺序（不参与实测排序） | 默认关；=1 开启。用于逐位复现 2026-10-26 之前的行为，或 A/B 固定静态臂 |
| `ARGO_GENERAL_FALLBACK_LEGACY` | 静态顺序并去掉 octen（anysearch → local_bing） | 默认关；=1 开启，且会钉住顺序（不再动态排序）。2026-10-17 交错 A/B：octen 第二顺位在 anysearch 抖动窗口把 p90 3273→1614ms、max 6266→1928ms、错误 9→0 |
| `ARGO_GENERAL_FALLBACK_LEAD` | 通用兜底链领队覆盖（传引擎名，提到首位） | 默认空=按 `source_selection` 实测先验排序；=anysearch 复现旧顺位。会钉住顺序，用于交错 A/B |
| `ARGO_PAID_SEARCH` | 付费搜索对冲开关 | 默认 `auto`：在 english_tech / intl_news_flash / global_event 的付费位插入 DeepSeek（预算用完回落 TinyFish）；`off` 关闭。见 `scripts/paid_policy.py` |
| `ARGO_PAID_HEDGE_DEFER` | english_tech 的付费对冲降到第三位（免费源先跑，不达标才付费） | 默认空=常驻第二位（2026-10-26 四臂实测：p50 打平，均值 967 vs 1246ms、权威覆盖 0.83 vs 0.50）；=1 省 75% DeepSeek 调用，适合预算紧张。新闻域始终第二位 |
| `ARGO_BUDGET_DEEPSEEK_SEARCH` | DeepSeek 月度预算（元） | 默认 5（≈1000 次成功调用）；0=关闭自动使用。也可用 `argo budget set deepseek_search 5` 写共享文件 |
| `ARGO_QUOTA_ANYSEARCH_LIMIT` | 行为阈值/预算调参 | AnySearch 每日次数上限（默认 1000）；0=不限制；空=沿用 config.yaml |
| `ARGO_QUOTA_ZHIHU_LIMIT` | 行为阈值/预算调参 | 知乎每日次数上限（默认 5000）；0=不限制；空=沿用 config.yaml |
| `ARGO_QUOTA_YUANBAO_LIMIT` | 行为阈值/预算调参 | 元宝每月次数上限（默认 2000）；0=不限制；空=沿用 config.yaml |
| `ARGO_BUDGET_EXA` | Exa 月度预算（美元） | 默认 10（≈1000 次）；0=关闭自动使用；按成功调用计费 |
| `ARGO_QUOTA_TAVILY_LIMIT` | 行为阈值/预算调参 | Tavily 每月次数上限（默认 1000）；0=不限制；空=沿用 config.yaml |
| `ARGO_BUDGETS_JSON` | 预算整份 JSON 覆盖 | 高级/测试用；优先级高于预算文件，例 `{"deepseek_search":{"amount":5,"currency":"CNY","period":"month"}}` |
| `ARGO_BUDGET_FILE` | 预算文件路径覆盖 | 默认 `~/.config/argo/budgets.json`；多环境隔离时用 |
| `ARGO_PAID_BUDGET_BYPASS` | 单次绕过月度预算 | =1 时显式/自动调用都不检查预算；用于临时排障，不改变配置 |
| `ARGO_ADAPTIVE_TAIL` | 尾宽按 backup 引擎历史 P75 分层（快源等满/慢源早收工） | 默认关；=1 开启（2026-10-16 三轮交错 A/B 未证实收益，对拍记录在本机 docs-local/调度对拍_尾宽分层与并发_2026-10-16.md，属本地归档不随发布件分发） |
| `ARGO_TAIL_COMPLETE_S` | 早停后台补全的收割宽限（秒）：弃置引擎宽限内完成则记学习器真实成绩并把交付合并进 L2 缓存 | 默认 3；0=关闭（回到纯丢弃）。CLI 由 atexit 等待、输出已先行；MCP/daemon 常驻进程自然收割。证据见 docs/QUALITY_OF_SLOW_SOURCES.md |
| `ARGO_ADMISSION_TTL_S` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_DAEMON` | 允许 search 在 daemon 不在位时自动后台拉起（显式 opt-in） | 默认关；=1 开启。`argo daemon start` 手动启动的实例不受此开关影响；`ARGO_NO_DAEMON=1` 优先 |
| `ARGO_DAEMON_TIMEOUT` | daemon 快路径客户端等待上限（秒） | 默认 120；deep 长查询可调大 |
| `ARGO_FETCH_DEADLINE_S` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_VERIFY_BUDGET_S` | `--verify` 全局墙钟预算（秒） | 默认 5：到点未回的 URL 进 `pending`，已回的照常回填；0=关闭（逐 URL 8s 降级链照旧）。2026-10-22 实测最坏 9.7s→≤5.5s |
| `ARGO_DEDUP_FUZZY` | ('minhash 去重的 rapidfuzz 模糊近重复复核（近似命中带内启用）', '默认开；=0 关；未装 rapidfuzz 时自动回退纯 minhash') | 见 rank_signals.py docstring |
| `ARGO_DEDUP_FUZZY_THR` | 行为阈值/预算调参 | 默认 0.88（token_set_ratio 判定线） |
| `ARGO_DEDUP_FUZZY_BAND` | 行为阈值/预算调参 | 默认 0.55（minhash 近似命中带下限） |
| `ARGO_TFIDF_RERANK` | ('精排降级时的零依赖 TF-IDF 语义抛光（防 SERP 壳链回顶）', '默认开；=0 关；fast/budget 档内建跳过') | 见 rerank_tfidf.py docstring |
| `ARGO_NUMBER_CROSSCHECK` | ('数字交叉验证信号（多源同指标数值分歧标记）', '默认开；仅 deep/auto 且结果 ≥3；=0 关') | 见 fact_crosscheck.py docstring |
| `ARGO_TEXT_LEXICON` | 行为阈值/预算调参 | 数字交叉验证的指标词表 JSON 路径（默认 backends/text_lexicon.json） |
| `ARGO_LOCAL_SEEK_GRACE_S` | 本地命中并入的宽限窗秒数 | 默认 0.25；覆盖 seek 常规 60~140ms |
| `ARGO_MAX_PARALLEL_ENGINES` | 单轮编排并发引擎数上限（config.yaml `execution.max_parallel_engines` 的环境覆盖，供 A/B 对拍/运维应急） | 默认随配置（6）；非法值回落配置不报错 |
| `ARGO_MINHASH_THRESHOLD` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_NO_DAEMON` | 关闭 UDS 快路径，search 恒走常态冷进程 | 默认开快路径；=1 关闭 |
| `ARGO_RELEVANCE_V2` | ('相关性算子：CJK 二元组覆盖率', '默认开；=0 退回单字覆盖率') | 见 rank_signals.py docstring |
| `ARGO_ROUTE_SAMPLE_RATE` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_SERIAL_STAGGER_S` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_STRAGGLER_GRACE_S` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |
| `ARGO_TOOL_CALL_COALESCE` | 行为阈值/预算调参 | 默认经实测校准，勿轻动 |

### 路径与数据位置（11）

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
| `ARGO_TEMPLATES_FILE` | 用户查询模板注册表路径覆盖 | 默认 `~/.config/argo/query-templates.yaml`（见 query-templates.md） |
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

### 抓取降级链分级开关（9）

| 变量 | 作用 | 默认/备注 |
|------|------|----------|
| `ARGO_FETCH_IMPERSONATE` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_JINA` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_JSRUN` | js-run 车道（V8 沙箱跑挑战页 JS）启停 | 默认开启；=0 关闭 |
| `ARGO_FETCH_MD_NEGOTIATE` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_MD_VARIANT` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_MOBILE` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_PARALLEL` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_FETCH_TINYFISH` | fetch 降级链单级启停 | 默认自动降级 |
| `ARGO_AUTH_FETCH`（别名 `ARGO_FETCH_AUTH`） | 登录态车道（站点有 `argo auth` profile 时跳过匿名链直进浏览器）启停 | 默认开启；=0 关闭。不影响 auth 子命令本身 |

### argo auth — 浏览器登录态（A2，2026-10）

```bash
argo auth login "https://example.com"   # 开可见窗口登录/扫码一次，回终端按 Enter 保存
argo auth status                        # 已登录站点清单（--json 可供脚本消费）
argo auth logout "example.com"          # 删除该站 profile（Chrome 占用时拒绝并提示）
```

- profile 是 argo 自持的独立 Chromium profile（状态根 `browser-profiles/<site>/`，**不是你的主 Chrome**）。
- cookie 只存 Chrome 自身加密存储（macOS Keychain），argo 全程不读取、不导出 cookie 值；抓取由浏览器页面内带凭证发请求，argo 只收响应体。
- 登录态抓取结果标 `login_state_used: true` / `cache_eligible: false`：不进公共缓存、envelope 记为 authenticated 可见性。
- 未登录站点零行为变化（不破默认路径）；批量爬取（`use_browser_fallback=False` 调用方）也不走登录态车道。

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

## argo budget — 月度预算（原生币种）

```bash
argo budget list                 # 预算 + 本月已用 + 可用状态
argo budget set deepseek_search 5 --currency CNY --period month
argo budget unset deepseek_search
```

预算存 `~/.config/argo/budgets.json`（`ARGO_BUDGET_FILE` 可覆盖），`quota.py`
启动时读取；只对配置了预算的引擎生效，其他引擎沿用 `quota_profiles.json`
的次数额度。默认只有 DeepSeek ¥5/月（≈1000 次成功调用）。预算用完时自动
路由剔除、显式调用返回可读错误；`ARGO_PAID_BUDGET_BYPASS=1` 单次放行。

## argo quota — 知乎开放平台当日额度

```bash
argo quota            # 人读表：全网/知乎/热榜/直答/知识库/用户数据/创作
argo quota --json     # 机器读
argo quota --refresh  # 跳过 10 分钟缓存
```

数据源 `GET /api/v1/quota`（官方，不消耗业务额度），按北京时间 00:00 重置；
同一账号多个 Access Secret 共用额度。缓存 10 分钟。

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

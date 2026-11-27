---
name: argo
description: Argo 阿尔戈 — 统一搜索、网页抓取与证据核验。当需要搜索/查一下/核实/抓取网页/爬取/深度研究/论文检索/新闻/舆情/公众号文章/招聘聚合时使用；支持影视/体育/金融/宏观/学术等垂直域与多语言查询。
version: 2.19.0
triggers:
  - 搜索
  - 查一下
  - 搜一下
  - 核实
  - 查证
  - 可信度
  - 抓取
  - 爬取
  - 深度研究
  - 论文
  - 舆情
  - 公众号
  - 招聘
  - search for
  - look up
  - fact check
  - fetch
  - crawl
  - research
---

# Argo — 统一搜索与证据核验

> 不止搜到，还要核到：高后果问题标 `fetch_required`、结果标 `fetch_suggested`，`--verify` 核验正文并回填证据分。源数见 docs/ENGINE_CATALOG.md；digup 默认关；deepseek_search 默认开、需 DEEPSEEK_API_KEY（按次计费，无 key 跳过）。

## 快速上手

```bash
argo search "查询词"               # 自动路由（daemon 需显式启动）
argo search "查询词" --yaml        # Agent 紧凑 YAML（省 ~60% Token）
argo search "查询词" --agent       # Agent JSON（--json --fields agent）
argo search --schema               # 离线自描述契约
argo search "查询词" --intent news # 意图路由（news/academic/social/image-reverse/calc）
argo search "查询词" --verify 3    # 核验 top-3 并回填证据分
argo research "复杂问题" --json    # 取证包（扩词或多工作包 → dossier）
```

默认不附 candidates/sources；`--yaml`/`--agent` 只留答案，追溯加 `--envelope`。

深度研究产出**取证包**（来源/覆盖/缺口/达标，不是判断稿）：先读 `references/research-protocol.md` 写工作包再取证，判断分事实/推断/建议。不要另装深度研究 skill。

研究笔记归档（判断稿一等公民化）：`argo note study|explore|list`，`research` 存证据、`note` 存结论。用法见 `references/usage.md`。

## 核心命令

### search — 统一搜索

| 参数 | 说明 |
|------|------|
| `--engine <name>` | 强制引擎（见 `argo list-engines`） |
| `--local-first` | 本地零成本聚合优先（local_search 内部管线） |
| `--include-local` / `--no-local` | 本机文件命中（fast/budget 默认开） |
| `--mode fast|auto|deep|budget` | fast 免费 / auto 成本感知（默认）/ deep 质量 / budget 配额 |
| `--explain` / `--no-cache` | 解释路由决策 / 跳过缓存 |
| `--depth fast|balanced|deep` | 搜索深度 |
| `--academic-deep` | 学术多源：depth=deep + domain=academic，禁用早停 |
| `--since 7d|2026-08-01` `--until` `--sort relevance|newest|oldest` | 时间窗过滤 + 时间排序 |
| `--video` `--min-duration SEC` `--max-duration SEC` | 视频模式（B 站 + YouTube）及时长筛选 |
| `--verify [N]` | 对 top-N 抓正文回填证据分 |
| `--numverify [N]` `--verify-with <引擎>` | 数值印证（对关键数字抓原文确认核对簇）+ 核对配对 |
| `--domain` `--sub_domain` / `ARGO_USAGE_LOG=0` | 垂直域下推（仅 anysearch）+ 缓存键隔离；不参与选源 / 关查询落盘 |

**按场景路由**：优先 `--intent news|academic|social|image-reverse|calc`（缺 key/超预算自动降级），或显式 `--engine deepseek_search` / `--academic-deep` / `anysearch`，见 `references/usage.md`。

**explicit_only 源**：feed、deepseek_search、tracemoe/saucenao/google_lens/animetrace、wolframalpha、brave/ecosia/qwant/gdelt 默认不进自动路由，用 `--intent` 或 `--engine`，全量见 `argo list-engines`。

### 图片检索

网络图走 `search`（`image_search` 域自动命中）；本地素材走 `argo local-image`（Vision 索引/相似图/联络表；默认关，需 `ARGO_LOCAL_IMAGE=1`，见 `references/usage.md`）。反搜图（图→出处）：`argo search "<图片URL>" --engine saucenao|animetrace|google_lens|tracemoe`，不进自动路由，saucenao/google_lens 需 key。

### 增强三工具

```bash
# research — 取证（扩词或 --work-packages → 取证包 + 引用 + 达标检查）
#   工作包可带 file_inputs / recompute（默认拒绝，需显式授权）
#   社交舆情：--mode social-sentiment --platforms xiaohongshu,reddit,twitter
argo research "查询" [--work-packages PATH|JSON] [--depth deep] [--json] [--verify N]

# evidence — 可信度评估（选拔×吸收两维）
echo '{"results": [...]}' | argo evidence "查询词" --high-stakes

# clarify — 意图消歧
argo clarify "有歧义的查询" --json
```


### 抓取族（`bin/argo` 入口）

```bash
argo fetch "https://example.com" [--focus "关键词"] [--use-browser]
# 降级链见 references/usage.md
argo screenshot "https://example.com" [--full-page]
argo pdf "https://example.com/paper.pdf" [--pages "1-5"] [--password "secret"]
argo paper "1706.03762"  # 论文深读
argo tweet "<x-url|id>"  # X 帖子打包（正文+串/引用/转发+媒体）
argo answer "query" [--scope <语料>]   # 直答（语料见 usage.md）
argo watch add|check|list|remove   # 观察模式（--json 供 cron）
argo ego-search status|search|fetch|merge  # 登录态专业搜索（需先 enable）
```

## Agent 执行纪律

1. **高后果问题**（金融/医疗/法律/事实核查）：search → evidence（或看 `credibility_fast`）→ fetch 高分 URL → 再下结论；`fetch_required=true` 时禁止跳过核验
2. **数字**：必须标注算法（全市场/主动/持仓市值 vs 占比）；冲突时并列；关键财务/价格/指标推荐带 `--numverify` 抓原文印证
3. **SERP 链**（baidu/s、sogou/link）：禁止当正文来源
4. **社交帖**：叙事/舆情，不进事实真值
5. **深度研究**：先读 `references/research-protocol.md`；有决策含义就交工作包，不要靠扩词充问题树；`quality_gate_results.passed=false` 必须降级表述
6. **引用**：讲给用户的事实带 URL 出处，日常档也要带（URL 在 `results[].url`，零成本）
7. **上下文纪律**：Agent 用 `--yaml` 或 `--agent`、按需 `-n`；追溯用 `--json --envelope`；查单源用 `argo list-engines --detail --engine <名>`。见 Gotchas

## Gotchas

- **`--envelope` 与 `--yaml`/`--agent` 互斥**：入口报错；要 provenance 用 `--json --envelope`。
- **结果异常少**：看 `funnel` 六格阶段计数，哪格归零即塌陷点。
- **慢查询**：看 `timing.dispatch` 的 `tail_wait_ms` / `abandoned_engine_ms`。
- **引擎列表**：别加 `--all`（~54 KB）。
- **缺密钥**：`argo settings list/set/unset`（set 值走 stdin）。
- `-n` 超 10 无收益。

## 按需读取（低频操作细节）

| 场景 | 读什么 |
|------|--------|
| MCP / DSH 插件 / 配额 / 工程纪律 | `references/operations.md` |
| **使用指南**：全命令、参数、开关总表、输出体积陷阱 | `references/usage.md` |
| 深度研究协议：约定、工作包、取证包 vs 判断稿 | `references/research-protocol.md` |
| 约定 / 工作包 / 判断稿模板 | `references/research-templates.md` |
| X 关键词搜索与查询模板 | `references/query-templates.md` |
| 引擎全景：垂直域/社交/学术/本地引擎表 + 路由规则 | `references/engines.md` |
| 学术检索：查询构造、相关性五因子排序、证据分级 | `references/academic-query.md` |
| 架构：文件结构、证据流水线、输出 JSON Schema | `references/architecture.md` |
| **搜索源使用文档**：全量清单 + 特别能力 | `docs/ENGINE_CATALOG.md`（生成，勿手改） |

> 工程纪律（每个事实只定义一处）见 `references/operations.md` 末尾。

---
name: argo
description: Argo 阿尔戈 — 统一搜索、网页抓取与证据核验。当需要搜索/查一下/核实/抓取网页/爬取/深度研究/论文检索/新闻/舆情/公众号文章/招聘聚合时使用；支持影视/体育/金融/宏观/学术等垂直域与多语言查询。
version: 2.9.2
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

> 不止「帮你搜到」，还要「帮你核到」：高后果问题标 `fetch_required`、结果标
> `fetch_suggested`，`--verify` 核验正文并回填证据分。收录 261 个源、226 个免密钥开箱可用。

## 快速上手

```bash
python3 scripts/search.py "查询词"                      # 自动路由搜索
python3 scripts/search.py "查询词" --json --fields agent  # Agent 消费档
python3 scripts/search.py "查询词" --verify 3            # 核验 top-3 并回填证据分
python3 scripts/research.py "复杂问题" --json            # 取证包（扩词或多工作包 → dossier）
```

默认不附归档用的 candidates/sources；`--fields agent` 剥遥测只留答案（Agent 消费档，~4.3 KB）；
要来源追溯或归档才加 `--envelope`（`--archive` 自动带上）。

深度研究只此一条。机器产出**取证包（dossier）**：来源、覆盖、缺口、是否达标，不是判断稿。Agent 先读 `references/research-protocol.md`（含多轨道「广泛研究」节），写出工作包再取证；判断按事实/推断/建议写。不要另装「专业深度研究」skill。

## 核心命令

### search — 统一搜索

| 参数 | 说明 |
|------|------|
| `--engine <name>` | 强制引擎（anysearch/byted/bocha/exa/tavily/eastmoney/zhihu/arxiv/pypi/mdn/hackernews/v2ex/redskill…，全量见 `--list-engines`） |
| `--local-first` | 本地零成本聚合优先（local_search 29 引擎，27 默认启用） |
| `--include-local` / `--no-local` | 本机文件命中（source=local_files，score 0.9/0.7）：fast/budget 默认开，auto/deep 显式 |
| `--mode fast|auto|deep|budget` | fast 免费优先 / auto 成本感知（默认）/ deep 质量优先 / budget 配额控制 |
| `--explain` | 解释路由决策（含 TF-IDF 分数） |
| `--no-cache` / `--depth fast|balanced|deep` | 跳过缓存 / 搜索深度 |
| `--academic-deep` | 学术多源模式：自动设置 depth=deep + domain=academic，禁用 early-stop 让所有学术源参与（arxiv/openalex/local_pubmed/core 等），适合深度研究场景 |
| `--since 7d|2026-08-01` `--until` `--sort relevance|newest|oldest` | 时间窗过滤 + 时间排序 |
| `--verify [N]` | 对 top-N 未核验结果 fetch 正文，回填证据分（URL→证据分缓存，同 URL 二次搜索自动复用） |
| `--domain` `--sub_domain` | 垂直域 / 子域限定 |

### 图片检索

网络图走 `search`（`image_search` 域自动命中）；本地素材走 `argo local-image`
（Vision 索引 + `--similar-to` 找相似图 + `--sheet` 出联络表交多模态模型判断）。
**本地图默认关闭**，需 `ARGO_LOCAL_IMAGE=1`；详见 `references/usage.md`。

### 增强三工具

```bash
# research — 取证（扩词或 --work-packages → 取证包 + 引用 + 达标检查）
#   工作包可带 file_inputs（本地一手数据入账）+ recompute（可复算脚本，默认拒绝，需显式授权）
#   社交舆情：--mode social-sentiment --platforms xiaohongshu,reddit,twitter
python3 scripts/research.py "查询" [--work-packages PATH|JSON] [--depth deep] [--json] [--verify N]

# evidence — 可信度评估（选拔×吸收两维）
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json [--high-stakes]

# clarify — 意图消歧
python3 scripts/clarify.py "有歧义的查询" --json
```


### 抓取族（`bin/argo` 入口）

```bash
argo fetch "https://example.com" [--focus "关键词"] [--use-browser]
# 降级链顺序与各级条件见 references/usage.md
argo screenshot "https://example.com" [--full-page]
argo pdf "https://example.com/paper.pdf" [--pages "1-5"] [--password "secret"]
argo paper "1706.03762"  # 论文深读
argo answer "query" [--scope <语料>]   # 直答，语料见 references/usage.md
argo watch add|check|list|remove   # 观察模式：快照+变化检测（check --json 供 cron）
```

## Agent 执行纪律

1. **高后果问题**（金融/医疗/法律/事实核查）：search → evidence（或看 `credibility_fast`）→ fetch 高分 URL → 再下结论；`fetch_required=true` 时禁止跳过核验
2. **数字**：必须标注算法（全市场/主动/持仓市值 vs 占比）；冲突时并列，禁止算法未对齐就合并
3. **SERP 链**（baidu/s、sogou/link）：禁止当正文来源
4. **社交帖**：叙事/舆情，不进事实真值
5. **深度研究**：先读 `references/research-protocol.md`；有决策含义就交工作包，不要靠扩词充问题树；`quality_gate_results.passed=false` 必须降级表述
6. **引用**：讲给用户的事实带 URL 出处，日常档也要带（URL 在 `results[].url`，零成本）
7. **上下文纪律**：Agent 搜索用 `--json --fields agent`、按需 `-n`；要来源追溯或归档才加 `--envelope`；查引擎状态用 `--list-engines --detail --engine <名>`（单引擎 ~0.9 KB）。踩坑记录见下方 Gotchas

## Gotchas

踩到新坑加一行，口径见 `references/usage.md`。

- **`--envelope` 与 `--fields agent` 别同给**：后者会把 envelope 的增量剥成 0 字节——以为拿到了 provenance、实际没有。要 provenance 就去掉 `--fields agent`。
- **结果异常少**：看 `funnel` 六格阶段计数，哪格归零即塌陷点。
- **慢查询**：看 `timing.dispatch` 的 `useful_ms`/`wasted_ms`，区分「等答案」与「白等」。
- **`--list-engines` 别直接 `--all`**：全量 ~54 KB 会灌爆版面；默认摘要 ~2.5 KB 够用。
- **`-n` 超 10 无收益**。

## 按需读取（低频操作细节）

以下按需打开；日常搜索/抓取/研究走上面核心命令即可。

| 场景 | 读什么 |
|------|--------|
| MCP 工具全清单 / 多客户端注入 / DSH 插件接入 / 配额·TinyFish / 子技能 / 本地打通 / 工程纪律 | `references/operations.md` |
| **使用指南**：全命令、参数、86 开关总表、输出体积陷阱、日志反馈 | `references/usage.md` |
| 深度研究协议：约定、工作包、取证包 vs 判断稿、达标检查 | `references/research-protocol.md` |
| 约定 / 工作包 / 判断稿模板 | `references/research-templates.md` |
| 引擎全景：垂直域/社交/学术/本地引擎表 + 路由规则 | `references/engines.md` |
| 学术检索：查询构造（arXiv/S2/GS 语法）、相关性五因子排序、引用网络挖掘、学术反模式与证据分级 | `references/academic-query.md` |
| 架构：文件结构、证据流水线、量化公式、输出 JSON Schema、内容质量信号 | `references/architecture.md` |
| MCP 多客户端注入详解 | `docs/MCP_SETUP.md` |
| **搜索源使用文档**：全量清单（费用 / 密钥 / 状态 / 域组合）+ 特别能力 + 打开方式 | `docs/ENGINE_CATALOG.md`（生成，勿手改） |


> 工程纪律（每个事实只定义一处：代码看本仓库、引擎声明看 config.yaml、宿主入口用 link_source.py 建软链、新增源流程）见 `references/operations.md` 末尾。

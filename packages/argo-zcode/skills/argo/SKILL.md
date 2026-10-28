---
name: argo
description: Argo 阿尔戈 — 统一搜索、网页抓取与证据核验（284 源、241 免密钥）。当需要搜索/查一下/搜一下/核实/查证/可信度/抓取/爬取/深度研究/论文/舆情/公众号/招聘/search for/look up/fact check/fetch/crawl/research 时使用；支持影视/体育/金融/宏观/学术等垂直域与多语言查询。依赖本机已装 argo CLI。
---

# Argo — 统一搜索与证据核验

> 不止搜到，还要核到：高后果问题标 `fetch_required`、结果标 `fetch_suggested`，`--verify` 核验正文并回填证据分。收录 284 个源、241 个免密钥可用；deepseek_search 默认开、需 DEEPSEEK_API_KEY（按次计费，无 key 自动跳过）。

前置依赖：本机已装 argo CLI（`which argo` 确认；未装见 https://github.com/taxueseek/argo 的安装说明）。

## 快速上手

```bash
argo search "查询词"                       # 自动路由搜索（daemon 需显式启动）
argo search "查询词" --json --fields agent # Agent 消费档（只留答案，~4.3 KB）
argo search "查询词" --verify 3            # 核验 top-3 并回填证据分
argo research "复杂问题" --json            # 深度研究（分解→多源采集→取证包 dossier）
```

默认不附 candidates/sources；`--fields agent` 只留答案，要追溯或归档才加 `--envelope`（`--archive` 自动带）。

深度研究只此一条，产出**取证包（dossier）**（来源/覆盖/缺口/达标，不是判断稿）：先读本插件 `references/research-protocol.md` 写工作包再取证，判断按事实/推断/建议写。多视角对比、要引用账本的用 wide-research 技能（本插件另一条）。

## 核心命令

```bash
argo search "q" [--engine <名>] [--local-first] [--mode fast|auto|deep|budget]
             [--verify N] [--academic-deep] [--since 7d|--until 2026-08-01]
             [--domain 域] [--explain] [--json --fields agent]
argo fetch "url" [--focus "关键词"] [--use-browser] [--full]   # 多级降级抓取
argo evidence "q" --json            # 可信度评估（选拔×吸收两维；--high-stakes 提门槛）
argo clarify "q" --json             # 意图消歧
argo crawl "url"                    # 站点爬取（sitemap/BFS）
argo extract "url"                  # 结构化提取（表格/Meta/JSON-LD）
argo article "url"                  # 微信公众号文章全文
argo screenshot "url" [--full-page] # 网页截图
argo pdf "url" [--pages "1-5"]      # PDF 正文提取
argo paper "1706.03762"             # 论文深读（--outline/--section/--cited-by）
argo tweet "<x-url|id>"             # X 帖子打包（正文+串/引用/转发+媒体）
argo x-search "q"                   # X 关键词搜索（原生布尔语法）
argo answer "q"                     # 直答（带引用合成，置信口径=引用计数）
argo watch add|check|list|remove    # 观察模式（--json 供 cron）
argo job "q"                        # 招聘多平台聚合
argo preflight "url1 url2"          # 批量 URL 预检（登录墙/已死/已归档）
argo cite "doi"                     # DOI → 引用条目（GB/T 7714/APA/BibTeX）
argo search "q" --list-engines      # 引擎状态（--detail 看单引擎；勿用 --all，~54 KB）
```

**按场景选引擎**：时事/时间窗 → deepseek_search，学术 → `--academic-deep`/exa，社交 → anysearch。全命令、参数与 86 开关总表见 argo 本体仓库的 usage.md 文件（`argo search --help` 可速查）。

## Agent 执行纪律

1. **高后果问题**（金融/医疗/法律/事实核查）：search → evidence（或看 credibility_fast）→ fetch 高分 URL → 再下结论；`fetch_required=true` 时禁止跳过核验
2. **数字**：必须标注算法（全市场/主动/持仓市值 vs 占比）；冲突时并列，禁止算法未对齐就合并
3. **SERP 链**（baidu/s、sogou/link）：禁止当正文来源
4. **社交帖**：叙事/舆情，不进事实真值
5. **深度研究**：先读 `references/research-protocol.md`；有决策含义就交工作包，不要靠扩词充问题树；`quality_gate_results.passed=false` 必须降级表述
6. **引用**：讲给用户的事实带 URL 出处，日常档也要带（URL 在 `results[].url`，零成本）
7. **上下文纪律**：用 `--json --fields agent`、按需 `-n`；追溯/归档才加 `--envelope`；引擎状态用 `--list-engines --detail --engine <名>`（~0.9 KB）

## Gotchas

- `--envelope` 与 `--fields agent` 别同给：后者把 envelope 增量剥成 0 字节；要 provenance 就去掉 `--fields agent`
- 结果异常少：看 funnel 六格阶段计数，哪格归零即塌陷点
- 慢查询：看 `timing.dispatch` 的 `tail_wait_ms`（就绪后还在等）/`abandoned_engine_ms`
- `-n` 超 10 无收益

## 按需读取（插件内自带）

| 场景 | 读什么 |
|---|---|
| 深度研究协议：约定、工作包、取证包 vs 判断稿、达标检查 | `references/research-protocol.md` |
| 工作包 / 判断稿模板 | `references/research-templates.md` |
| 引擎全景：垂直域/社交/学术/本地引擎表 + 路由规则 | `references/engines.md` |
| 全命令与参数细节 | `argo --help`、`argo search --help` |

多客户端 MCP 注入、配额、工程纪律等运维项不在本插件范围，见 argo 本体仓库 docs；全命令参数总表（usage.md）在 argo 本体仓库，不在本插件内。

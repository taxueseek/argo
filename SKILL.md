---
name: argo
description: Argo 阿尔戈 — 统一搜索与证据核验。核心不是「返回链接」，而是「最大化可被 Agent 吸收的可核验证据」。工具：argo_search + research + evidence（Selection×Absorption 两阶段）+ clarify + fetch/crawl/extract + 社交引擎。TF-IDF路由 + RRF + 证据密度 + SERP降权 + 中文信源表。深度研究走 research.py（academic/finance 专业 profile）。
version: 2.4.1
triggers:
  - 搜索
  - 查一下
  - 搜一下
  - 核实
  - 查证
  - 可信度
  - search for
  - look up
  - fact check
  - 投资价值
  - 研报
  - 估值
  - 公司分析
  - 财报
  - 行情
  - research
  - argo
  # ── 深度研究专用（走 research.py，禁止当日常 SERP）──
  - 深度研究
  - 深度调研
  - 深度报告
  - 深度分析
  - 全面调研
  - 系统研究
  - 系统综述
  - 文献综述
  - 科研调研
  - 深入研究
  - deep research
  - deep-research
  - thorough research
  - literature review
  # 主命令 + 子技能斜杠（见 ~/.claude/commands/argo*.md）
  - /argo
  - /argo-search
  - /argo-research
  - /deep-research
  - /argo-research-academic
  - /argo-research-finance
  - /argo-evidence
  - /argo-clarify
  - /argo-fetch
engines:
  - local_search
  - anysearch
  - tavily
  - zhihu
  - eastmoney
  - byted
  - arxiv
  - duckduckgo
  - uapi
  - semantic_scholar
  - felo
  - bocha
  - openalex
  - crossref
  - github
  - wikipedia
  - metaso
  - wolframalpha
  - brave
  - twitter
  - reddit
  - xiaohongshu
  - bilibili
  - weibo
  - exa
  - wechat_sogou
  - hackernews
  - stackoverflow
  - google_scholar
  - v2ex
  - ths_hot
  - cls_telegraph
  - em_global_news
---

## Argo v2.4.1（WorkBuddy 版）

> **安装路径**：`~/.workbuddy/skills/argo/`
> 所有命令请先 `cd ~/.workbuddy/skills/argo/` 后再执行。
> 配置已在 config.yaml 中更新为指向 `~/.workbuddy/skills/argo/`。

### WorkBuddy 快速调用

Argo 已安装为 WorkBuddy 用户级 skill，可在对话中通过以下方式触发：
- 直接说「用 Argo 搜一下...」
- 说「搜索/查一下/核实...」等触发词（Argo 会自动响应）

### 问题重定义（第一性原理）

| 旧问题（错误） | 新问题（正确） |
|---|---|
| 怎么返回更多搜索结果？ | 怎么让 Agent **吸收到可核验的证据**？ |
| 域名权威高 = 可信 | 权威只是 **Selection 门槛**；还要 **Absorption 证据密度** |
| 被引用/被搜到 = 事实 | 被检索到 ≠ 进入答案；口径未对齐前禁止合并数字 |
| 单次泛查询够用 | 事实核查需要 **分层查询**（来源要求 / 对比 / 时间 / 主体） |

**一句话**：Argo 的产出是「证据候选 + 可信度分解」，不是「链接清单」。

### MECE 证据流水线

```
Query
  ├─ Clarify（意图是否可执行）
  ├─ Search Selection（引擎召回 + 域名权威 + SERP 剔除）
  ├─ Absorption（数字/定义/对比/披露密度；fetch 后 quality）
  ├─ Freshness（发布年；忽略「2015年以来」历史对比年）
  └─ Consensus（多可吸收域名佐证；社交仅叙事）
```

四块互不重叠、合起来覆盖「能不能用这条结果」。

### 量化公式（evidence v2.2）

```
selection  = authority_score（SERP/跳转链 ≤ 0.15）
absorption = evidence_density（has_numbers/definition/comparison/howto/disclose − qa）
freshness  = 发布年/URL年/完整日期
final      = 0.40·selection + 0.35·absorption + 0.15·freshness + 0.10·engine_score
```

搜索结果内嵌快评字段：`selection` / `absorption` / `credibility_fast` / `evidence_flags`。
完整交叉验证：`python3 scripts/evidence.py --stdin --json`。

### Agent 执行纪律

1. **日常直搜（默认）**：`mode=auto|fast|budget` 且 `depth≠deep` → **直接 `argo_search`，禁止先确认、禁止强制 plan**  
2. **专业 / 深度**：`mode=deep` 或 `depth=deep` → 直搜同时可读 `plan` 元数据；高后果再 evidence/fetch  
3. **斜杠分层**：主命令 `/argo`；日常 `/argo-search`；深度 `/argo-research`（及 academic/finance）；核验 `/argo-evidence`；消歧 `/argo-clarify`；正文 `/argo-fetch`  
3b. **深度研究**：触发词见 frontmatter（「深度研究」「文献综述」「deep research」等）或 `/argo-research` / `/argo research` → **只**走 `argo_research` / `research.py`；顶层离线 plan **一次**后拆子查询；**不得** plan↔search 循环；**禁止**用日常 `search.py` 冒充深度研究  
4. **专业选题**：`--topic academic|finance|investment|ai|…`；省略时启发式自动推断；输出挂 `quality_gates` / `report_sections` / `source_grades`  
5. **高后果问题**：search → evidence（或看 `credibility_fast`）→ fetch 高分 URL → 再下结论  
6. **数字**：必须标注口径（全市场/主动/持仓市值 vs 占比）；冲突时并列  
7. **SERP 链**（baidu/s、sogou/link）：禁止当正文来源  
8. **社交帖**：叙事/舆情，不进事实真值  
9. **分层查询**：事实类 deep 至少 2–3 条子查询（来源 / 对比数据 / 关键主体）
10. **input_kind**：纯 URL / 已知链接默认 `known-url`，**禁止**多引擎热搜，改 `argo_fetch`；只有「搜索引用/讨论该 URL」才用 `url-seed`（这是工具分流，不是用户确认门）
11. **snippet 是线索**：`verification.status=candidate` 时不得把摘要当已核验事实；引用前 fetch/extract
12. **plan 显式开关**：仅调试或专业路径用 `plan.py` / `--plan-only`；日常热路径 `execution_tier=daily` 且 `requires_confirmation=false`
13. **归档分层**：日常 `search` **默认不归档**（仅显式 `--archive`）；`research` **默认归档**（`--no-archive` 退出）。归档≠已核验  
14. **信源沉底**：人读输出正文用 `[n]` + 标题/摘要；完整 URL 在底部「相关信源」；JSON 带 `sources[]`
15. **学术纪律**：`--topic academic` 禁止编造 DOI/论文/期刊要求；区分共识 vs 开放问题  
16. **金融纪律**：`--topic finance|investment` 标注信源级别与盲区；输出免责声明；**不**给买卖指令

### 离线计划与候选交接

```bash
# 离线计划（不联网）
python3 scripts/plan.py "贵州茅台股价" --json
python3 scripts/search.py "https://example.com/a" --plan-only --json

# 已知 URL：默认 handoff，不烧配额
python3 scripts/search.py "https://docs.python.org/3/" --json

# 把 URL 当发现种子
python3 scripts/search.py "搜索引用 https://example.com/a 的报道" --input-kind url-seed --json
```

搜索 JSON 附加字段（不破坏原 `results[]`）：`schema_version` / `input_kind` / `candidates[]` / `coverage[]` / `limitations[]`。

### 信源显示（日常 SERP）

```bash
# 人读：正文 [n]+标题+摘要；底部「相关信源」给链接
python3 scripts/search.py "查询词"

# 机器：results[] + sources[]（ref/title/url/engine）
python3 scripts/search.py "查询词" --json
```

### 工作区归档（分层）

| 场景 | 默认 | 开关 |
|------|------|------|
| 日常 `search` | **不归档** | 显式 `--archive` |
| 深度 `research` | **归档** | `--no-archive` 退出 |

落盘根：`数据/argo-search-archive/`（`ARGO_ARCHIVE_ROOT` 可覆盖）。**不**抓正文、**不**下媒体、**不**覆盖旧 run。

```bash
# 日常：一般不要 --archive
python3 scripts/search.py "快问" 

# 确需复现的单次搜索
python3 scripts/search.py "专题" --archive --archive-tag adhoc

# 研究：默认归档
python3 scripts/research.py "议题" --json
python3 scripts/research.py "议题" --no-archive   # 轻试

python3 scripts/archive_run.py list --limit 20
python3 scripts/archive_run.py root
```

每个 run：`run-summary.json` · `envelope.json` · `sources.jsonl` · `candidates.jsonl` · `results.jsonl` · `coverage.json` · `INDEX.md`。  
问题重定义：`docs/PROBLEM_REFRAME_SEARCH_UX_v2.4.5.md`；细则：`docs/SEARCH_ARCHIVE.md`。

### 能力清单

- **TF-IDF 语义路由**：二元组 + boost_keywords + boost_combos，< 5ms 延迟
- **Exa 语义搜索引擎**：embedding 匹配 + 内容摘要，中英文开放式调研首选（v2.3 新增）
- **搜狗微信搜索引擎**：weixin.sogou.com 公众号文章搜索，无需登录（v2.3 新增）
- **成本感知评分**：free=1.0 / low=0.7 / api=0.5 / paid=0.3 四档 cost_factor
- **预算模式**：fast / auto / deep / budget 四档配额追踪 + 自动降级
- **渐进式多源**：engines_combo 先快后全 + 并行模式
- **AnySearch 垂直域**：19 个垂直域结构化搜索，匿名兜底
- **双层缓存**：L1 LRU + L2 SQLite + gzip，分级 TTL；**query 归一化键** + 时效 query 硬 cap≤900s（v2.4.1）
- **渐进 early-stop**：auto/fast/budget 下主引擎足够则跳过其余并行波次（v2.4.1）
- **RRF 融合**：多引擎 Reciprocal Rank Fusion 去重合并
- **Bocha Reranker**：语义精排后处理
- **Selection×Absorption**：SERP 降权 + 证据密度 + 中文信源表（v2.2）
- **自适应学习**：success × latency × cost 三维评分，SQLite 持久化
- **社交引擎**：Twitter/Reddit/小红书/B站/微博 5 大平台原生搜索
- **引擎生命周期（v2.4.4）**：外置 YAML 声明 + env 注入 + `engine_validate` 准入 + 自动路由过滤 blocked/缺 Key

### 引擎接入与验证（v2.4.4）

```bash
cd ~/.workbuddy/skills/argo/

# 状态一览（env / admission / routable）
python3 scripts/search.py --list-engines --detail
python3 scripts/search.py --list-engines --detail --routable-only
python3 scripts/engine_status.py --engine tavily --json

# 标准化验证 → 准入
python3 scripts/engine_validate.py --engine hackernews --stage health --admit --write-doc
python3 scripts/engine_validate.py --engine hackernews --stage all --admit
python3 scripts/engine_validate.py --all-free --stage health

# 环境变量（新名优先，旧名兼容）
export ARGO_TAVILY_API_KEY=...
export ARGO_EXA_API_KEY=...
export ARGO_ENABLE_ENGINES="hackernews,eastmoney,tavily"   # 可选白名单
export ARGO_DISABLE_ENGINES="brave"                        # 可选黑名单

# 新引擎 L1：复制模板 → 改 YAML → 配 Key → validate
# 详见 docs/ADDING_NEW_ENGINE.md
# cp engines/_template_http.yaml engines/specs/myengine.yaml
```

自动路由只使用 **routable** 引擎（config enabled ∧ env 就绪 ∧ 未 blocked）。  
`--engine` 强制指定不受 admission 限制（便于调试）。

### 用法（从 `~/.workbuddy/skills/argo/` 目录执行）

```bash
# 先切换到 argo 目录
cd ~/.workbuddy/skills/argo/

# 自动路由（推荐）
python3 scripts/search.py "查询词"

# JSON 输出（供 Agent 消费）
python3 scripts/search.py "查询词" --json

# 解释路由决策（含 TF-IDF 分数）
python3 scripts/search.py "查询词" --explain

# 强制引擎
python3 scripts/search.py "查询词" --engine anysearch
python3 scripts/search.py "查询词" --engine byted
python3 scripts/search.py "查询词" --engine arxiv
python3 scripts/search.py "查询词" --engine eastmoney
python3 scripts/search.py "查询词" --engine zhihu
python3 scripts/search.py "查询词" --engine bocha
python3 scripts/search.py "查询词" --engine exa
python3 scripts/search.py "查询词" --engine wechat_sogou
python3 scripts/search.py "查询词" --engine hackernews
python3 scripts/search.py "查询词" --engine stackoverflow
python3 scripts/search.py "查询词" --engine google_scholar
python3 scripts/search.py "查询词" --engine v2ex

# 本地零成本优先（local_search 聚合）
python3 scripts/search.py "查询词" --local-first
python3 scripts/search.py "查询词" --local-first --mode fast

# 预算模式
python3 scripts/search.py "查询词" --mode fast     # 免费优先，自动前置 local_search
python3 scripts/search.py "查询词" --mode auto     # 成本感知（默认）
python3 scripts/search.py "查询词" --mode deep     # 质量优先
python3 scripts/search.py "查询词" --mode budget   # 配额控制

# 跳过缓存 / 搜索深度 / 列出引擎 / 配额状态
python3 scripts/search.py "查询词" --no-cache
python3 scripts/search.py "查询词" --depth fast|balanced|deep
python3 scripts/search.py --list-engines
python3 scripts/search.py --list-engines --detail
python3 scripts/engine_validate.py --engine hackernews --stage health --admit
python3 scripts/quota.py stats

# AnySearch 垂直域搜索
python3 scripts/search.py "AAPL" --domain finance --sub_domain finance.us_stock

# TF-IDF 路由测试
python3 scripts/tfidf_router.py "查询词"

# ── 深度研究（问题分解→多源采集→综合报告；默认归档）──
# 触发：深度研究触发词，或 /argo · /argo-research · /argo academic|finance
# 斜杠一览：/argo（主） /argo-search /argo-research /argo-research-academic
#           /argo-research-finance /deep-research /argo-evidence /argo-clarify /argo-fetch
python3 scripts/research.py "CRISPR脱靶效应AI预测方法综述"
python3 scripts/research.py "CVE-2024-6387 生产环境影响" --depth deep --json
python3 scripts/research.py "React vs Vue 2025 生产环境对比" --sub-queries 5

# ── 选题研究（模板子查询 + 引擎优先 + 质量门禁 + 报告结构）──
python3 scripts/research.py "Claude Opus 5" --topic ai              # AI/大模型
python3 scripts/research.py "CRISPR 脱靶" --topic academic           # 科研/文献：arxiv 优先 + 学术门禁
python3 scripts/research.py "台积电估值分歧" --topic finance          # 金融深度：IC 风格门禁 + 免责
python3 scripts/research.py "离岸信托新政" --topic investment        # 投资线索（非买卖建议）
python3 scripts/research.py "显卡涨价" --topic tech
python3 scripts/research.py "Obsidian插件" --topic tool
python3 scripts/research.py "SpaceX商业闭环" --topic internet
python3 scripts/research.py "苹果Vision Pro 口碑" --topic social
python3 scripts/research.py "带综述关键词的查询"                      # 无 --topic 时自动推断
python3 scripts/research.py --topic help                             # 选题 + 触发词 + 斜杠命令

# ── 来源可信度评估 ──
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json

# ── 意图消歧 ──
python3 scripts/clarify.py "Python 吞苹果 兼容吗" --explain
python3 scripts/clarify.py "苹果股价" --json
```

### 引擎全景（28 个引擎 + Reranker）

| 引擎 | cost_tier | 特点 | 延迟 |
|------|-----------|------|------|
| anysearch | free | 垂直领域通用 | ~2.7s |
| zhihu | free | 知乎观点 | ~700ms |
| eastmoney | free | 金融数据 | ~400ms |
| arxiv | free | 学术论文 | ~1s |
| duckduckgo | free | 快速事实 | ~500ms |
| uapi | free | 中文网页 | ~800ms |
| semantic_scholar | free | 学术+引用 | ~1s |
| openalex | free | 2.5亿+论文 | ~2s |
| crossref | free | DOI/引用元数据 | ~2s |
| github | free | 代码搜索 | ~1s |
| wikipedia | free | 百科事实 | ~500ms |
| searxng | free | 聚合搜索 | ~1s |
| wolframalpha | free | 计算知识 | ~2s |
| bocha | low | 中文网页(AI友好) | ~1s |
| metaso | low | 中文AI搜索 | ~2s |
| byted | low | 字节搜索 | ~1s |
| brave | low | 隐私搜索 | ~1s |
| tavily | paid | 国际搜索 | ~2s |
| felo | paid | AI综合答案 | ~3s |
| exa | api | 语义搜索(embedding+内容摘要) | ~6s |
| wechat_sogou | free | 搜狗微信搜索(公众号文章) | ~2s |
| hackernews | free | Hacker News(科技新闻+讨论) | ~2s |
| stackoverflow | free | Stack Overflow(编程问答) | ~2s |
| google_scholar | free | Google Scholar(学术论文) | ~3s |
| v2ex | free | V2EX(中文技术社区) | ~2s |
| ths_hot | free | 同花顺热点(强势股+题材归因) | ~2s |
| cls_telegraph | free | 财联社电报(实时财经快讯) | ~2s |
| em_global_news | free | 东财全球资讯(7×24快讯) | ~2s |
| **Bocha Reranker** | low | 语义精排（后处理） | ~500ms |

### Exa 语义搜索引擎（v2.3 新增）

Exa 是基于向量 embedding 的语义搜索引擎，核心能力：
- **语义匹配**：不使用关键词，而是理解查询意图，在 embedding 空间中找到最相关的页面
- **内容摘要**：每条结果自带 `text` 字段（页面正文摘要），减少 fetch 环节
- **中英文均可**：中文搜索质量超预期（embedding 模型对中文友好）

**免费额度**：1000 次/月，超出后需升级付费计划。

### 搜狗微信搜索引擎（v2.3 新增）

通过搜狗微信搜索（weixin.sogou.com）直接抓取公众号文章，无需登录、无需 API key。
返回字段：`title`、`url`、`snippet`、`account`（公众号名）

### Hacker News 搜索（v2.3 新增）

通过 Algolia API 搜索 Hacker News，覆盖科技新闻和讨论。
返回字段：`title`、`url`、`snippet`（score/comments/author）

### Stack Overflow 搜索（v2.3 新增）

通过 Stack Exchange API 搜索编程问答，覆盖技术问题和解决方案。
返回字段：`title`、`url`、`snippet`（score/answers/tags）

### Google Scholar 搜索（v2.3 新增）

通过 HTTP 页面解析搜索 Google Scholar，覆盖学术论文。
返回字段：`title`、`url`、`snippet`（论文摘要）

### V2EX 社区搜索（v2.3 新增）

搜索 V2EX 中文技术社区讨论。
返回字段：`title`、`url`、`snippet`

```bash
# 强制使用某个引擎
python3 scripts/search.py "查询" --engine hackernews
python3 scripts/search.py "查询" --engine stackoverflow
python3 scripts/search.py "查询" --engine google_scholar
python3 scripts/search.py "查询" --engine v2ex

python3 scripts/search.py "查询词" --engine v2ex
python3 scripts/search.py "查询词" --engine ths_hot
python3 scripts/search.py "查询词" --engine cls_telegraph
python3 scripts/search.py "查询词" --engine em_global_news

# 查看配额
python3 scripts/quota.py stats

### 社交平台引擎（v2.1 新增）

| 引擎 | cost_tier | 特点 | 认证 |
|------|-----------|------|------|
| twitter / fxtwitter | free | X/Twitter 推文搜索（FxTwitter 主路径） | 零 Key；nitter/tw 兜底 |
| reddit | free | Reddit 帖子+评论 | 无需认证 |
| xiaohongshu | free | 小红书笔记+评论 | xhs login |
| bilibili | free | B站视频+弹幕 | 无需认证 |
| weibo | free | 微博帖子+话题 | 无需认证 |

社交引擎统一输出 `social_meta` 字段，包含作者、互动数据（点赞/评论/转发）、平台元信息。

### 三大增强工具（v2.0 新增）

| 工具 | 功能 | 适用场景 | Token 开销 |
|------|------|---------|-----------|
| `research` | 问题分解→多源并行采集→综合报告+引用+知识缺口 | 学术综述、事实核查、竞品分析、技术选型 | ~700/次 |
| `evidence` | 权威性+时效性+交叉验证的综合可信度评分 | 高后果决策、学术引用、新闻真伪 | ~300/次 |
| `clarify` | 歧义检测+意图分类+推荐路由策略 | 歧义查询、意图不明确、多语言混合 | ~200/次 |

#### research — 深度研究（专业 profile）

**何时用**：用户明确要深度研究 / 综述 / 研报级信息包；或斜杠 `/argo-research`、`/argo-research-academic`、`/argo-research-finance`、`/deep-research`。  
**何时不用**：日常快问 → 只用 `search.py`，底部给链接即可，**不**归档、**不**展开质量门禁长文。

```bash
# 自动分解问题，多源采集（默认归档；轻试加 --no-archive）
python3 scripts/research.py "你的复杂查询"

# 控制子查询数量和搜索深度
python3 scripts/research.py "查询" --sub-queries 5 --depth deep

# 专业选题（academic / finance 吸收了科研与投研质量纪律）
python3 scripts/research.py "查询" --topic academic --json
python3 scripts/research.py "查询" --topic finance --json

# JSON 输出供 Agent 消费
python3 scripts/research.py "查询" --json
```

输出包含：`key_findings`、`citations` / `sources[]`、`gaps`、`source_distribution`、  
`quality_gates`（交付自检清单）、`report_sections`（建议结构）、`source_grades`（信源级别）、  
`discipline`、`disclaimer`（金融域）。人读时链接仍沉底「相关信源」。

#### evidence — 可信度评估（v2.2 Selection×Absorption）

```bash
# 对搜索结果进行可信度评分
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json

# 高后果模式（共识微调）
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json --high-stakes
```

输出包含：
- `credibility.final` / `selection` / `absorption`
- `authority`（含 `is_serp`）
- `freshness`（忽略「YYYY年以来」历史对比年）
- `evidence_density`（has_numbers / has_comparison / …）
- `cross_validation`（可吸收域名数）

中文信源覆盖与降权表：`backends/source_types_cn.json`。

#### clarify — 意图消歧

```bash
# 分析查询歧义和意图
python3 scripts/clarify.py "有歧义的查询" --explain --json
```

输出包含：`ambiguities`（歧义词+可能含义+置信度）、`intents`（意图分类）、`recommended_strategy`（推荐策略：clarify_first/deep_research/split_search/direct_search）。

#### social-sentiment — 社交舆情研究（v2.1 新增）

```bash
# 跨平台舆情分析
python3 scripts/research.py "iPhone 16 用户评价" --mode social-sentiment --platforms xiaohongshu,reddit,twitter

# JSON 输出
python3 scripts/research.py "AI Agent 产品口碑" --mode social-sentiment --json
```

输出包含：`platform_breakdown`（各平台帖子数）、`engagement_totals`（互动数据汇总）、`top_topics`（高频讨论话题）、`cross_platform_posts`（代表性内容）。

### MCP 服务

十六个工具同时暴露为 MCP server（JSON-RPC over stdio），可被 Grok/Claude/Kimi 等客户端直接调用：

```bash
# 启动 MCP 服务
python3 scripts/mcp_server.py

# 本地测试
python3 scripts/mcp_server.py --test
```

MCP 工具名：`argo_search`、`argo_research`（含 social-sentiment / topic）、`argo_evidence`、`argo_clarify`、`argo_crawl`、`argo_extract`、`argo_fetch`、`argo_screenshot`、`argo_pdf`、`argo_social_search`、`argo_social_sentiment`、`argo_twitter_search`、`argo_reddit_search`、`argo_xiaohongshu_search`、`argo_bilibili_search`、`argo_weibo_search`。

**深度研究边界**：`argo_research` **只**用 argo 内 `research.py` + `topic_research_profiles.py`。academic/finance 的方法（门禁/结构/信源级别）已内化进 profile，**运行时不**调用 super-research / invest-analyst / rw-research-router 等外部 skill。需要整份 IC 研报写作或科研投稿工作流时，再由 Agent 另开对应 skill——那是写作/编排层，不是搜索 MCP 依赖。

**MCP 性能**：延迟加载 + initialize 后台 warm search/cache；默认紧凑 JSON（无 indent）+ `summary` 砍重字段；社交多平台并行；`argo_research` 支持 `topic`/`auto_topic`。

### 成本感知路由公式

```
score = quality × cost_factor

cost_factor:
  free  = 1.0   (anysearch/zhihu/eastmoney/arxiv/duckduckgo/uapi/...)
  low   = 0.7   (bocha/metaso/byted/brave)
  api   = 0.5   (exa — 有限额度的免费引擎)
  paid  = 0.3   (tavily/felo)
```

### 预算模式

| 模式 | 说明 | 触发条件 |
|------|------|---------|
| fast | 免费引擎优先，禁用付费 | 简单查询 |
| auto | 成本感知评分（默认） | 普通查询 |
| deep | 质量优先，忽略成本 | 深度研究 |
| budget | 配额控制，用完降级 | 配额紧张 |

### Local Search 子技能与 SearXNG 替代策略

local-search 是 argo 内置的「零成本聚合后端」，不依赖独立的 SearXNG 服务：

- **25 个本地引擎，20 个默认启用**：覆盖 web_general、chinese、academic、news、code、reference、vertical 七大类，
  统一通过 HTML/RSS/JSON/XML 解析公开页面。
- **引擎注册表**（`sub-skills/local-search/engine_registry.py`）：唯一真源，加载
  `config.yaml` + `parse_maps.yaml`；新增引擎只需改 YAML。
- **健康探针**（`sub-skills/local-search/health_check.py`）：canary 查询 + 反爬/拦截检测，
  状态缓存 5 分钟；连续 2 次失败或单次 >8s 标记 unavailable，成功 1 次恢复。
  fast/budget 模式下只检查实际要用的引擎，避免全量探针拖慢响应。
- **智能路由**（`sub-skills/local-search/smart_router.py`）：根据查询特征自动选择最优本地引擎组合。
- **统一 schema**：输出与 argo 完全一致，直接参与 RRF 融合与 Bocha reranker。

**SearXNG 替代说明**：当 SearXNG 未启用或不可用时，argo 在 `fast`/`budget` 模式
下会自动将 `local_search` 加入 `engines_combo` 首位，实现同等零成本聚合效果，无需运行
SearXNG 实例。强制使用本地聚合：

```bash
python3 scripts/search.py "查询词" --local-first
```

### 文件结构（`~/.workbuddy/skills/argo/`）

```
~/workbuddy/skills/argo/
├── SKILL.md              # 本文件 — 技能注册文档
├── config.yaml           # 引擎配置 & 路由规则
├── engines/              # [v2.4.4] 外置引擎声明 + 插件
│   ├── _template_http.yaml
│   ├── specs/*.yaml      # L1/L2 声明式引擎
│   └── plugins/*.py      # L3 自定义 builder
├── docs/
│   └── ADDING_NEW_ENGINE.md
├── backends/
│   ├── domain_profiles.json   # TF-IDF 领域文档
│   └── quota_profiles.json    # 配额配置
├── scripts/
│   ├── config.py         # 配置加载器（merge 外置 YAML）
│   ├── engine_env.py     # [v2.4.4] Key 别名 / ENABLE·DISABLE
│   ├── engine_admission.py # [v2.4.4] 生产准入状态
│   ├── engine_status.py  # [v2.4.4] list --detail
│   ├── engine_validate.py # [v2.4.4] 标准化验证 CLI
│   ├── search.py         # CLI 入口 & 执行编排
│   ├── route.py          # 三层路由（routable 过滤）
│   ├── engines.py        # 引擎适配层 + 插件加载
│   ├── mcp_server.py     # MCP 服务层
│   └── social_engines/   # 社交平台引擎
├── sub-skills/
│   └── local-search/     # 本地引擎子技能
└── tests/
```

### 三大增强工具（v2.0 新增）

| 工具 | 功能 | 适用场景 | 依赖 |
|------|------|---------|------|
| `argo_fetch` | HTTP→反检测浏览器自动降级 + BM25 聚焦 + 质量信号 | 反爬网站、CF 保护页、JS 渲染页 | 可选：master_fetch |
| `argo_screenshot` | 页面截图（全页/视口） | 布局验证、网页快照、多模态分析 | playwright |
| `argo_pdf` | PDF→Markdown（表格+目录+元数据） | 论文/报告/白皮书解析 | pdfplumber 或 PyMuPDF |

#### argo_fetch — 智能页面抓取

```bash
# 自动模式（HTTP 优先，失败升级浏览器）
argo fetch "https://example.com"

# BM25 聚焦提取（只返回相关段落）
argo fetch "https://example.com/long-article" --focus "关键词"

# 强制使用反检测浏览器
argo fetch "https://cloudflare-protected.com" --use-browser
```

输出包含 Hound 质量信号：
```json
{
  "content_ok": true,
  "page_type": "article",
  "source_type": "docs-site",
  "is_official": true,
  "is_stale": false,
  "content_age_days": 45,
  "quality_score": 0.85,
  "fetch_method": "http"
}
```

**降级触发条件**：HTTP 失败 / 内容 < 50 字符 / 检测到 CF 挑战 / 检测到 JS shell

#### argo_screenshot — 页面截图

```bash
argo screenshot "https://example.com"
argo screenshot "https://example.com" --full-page --output /tmp/page.png
```

#### argo_pdf — PDF 结构化提取

```bash
argo pdf "https://example.com/paper.pdf"
argo pdf "https://example.com/paper.pdf" --pages "1-5"
argo pdf "/local/file.pdf" --password "secret"
```

### 内容质量信号系统（v2.2 增强）

所有抓取结果自动附带质量信号；搜索快评另附 `credibility_fast`：

| 信号 | 类型 | 说明 |
|------|------|------|
| `content_ok` | bool | 内容是否可信可用（quality_score > 0.3 且 word_count > 50） |
| `page_type` | string | article/list/forum/qa/docs/js_shell/auth_wall/paywall |
| `source_type` | string | gov/edu/github/news/blog/forum/qa/docs-site/ecommerce |
| `is_official` | bool | 是否官方来源（.gov/.edu/github/厂商docs） |
| `is_stale` | bool | 是否过期（> 365 天） |
| `content_age_days` | int | 内容年龄（天） |
| `quality_score` | float | 0-1（长度 0.2 + 密度 0.2 + 结构 0.2 + **证据密度 0.3** + 标题 0.1） |
| `has_numbers` / `has_definition` / `has_comparison` / `has_howto` | bool | 证据块（GEO 吸收信号） |
| `absorption_score` | float | 证据密度综合分 |
| `selection` / `credibility_fast` | float | 搜索结果内嵌两阶段快评 |

### 输出 JSON Schema

```json
{
  "query": "string",
  "engine": "string",
  "engines": ["string"],
  "engines_combo": ["string"],
  "cached": false,
  "cache_level": "L1 | L2",
  "domain": "string | null",
  "elapsed_ms": 0,
  "tfidf_scores": [{"engine": "string", "score": 0.0}],
  "results": [
    {
      "title": "string",
      "url": "string",
      "snippet": "string",
      "score": 0.0,
      "source": "string"
    }
  ],
  "count": 0,
  "engines_used": ["string"],
  "errors": ["string"]
}
```

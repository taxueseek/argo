<p align="center">
  <img src="docs/assets/hero.svg" width="600" alt="Argo 阿尔戈">
</p>

<h3 align="center">Argo · 阿尔戈</h3>

<p align="center">
  给 Agent 用的统一搜索与证据核验。<br>
  不只返回链接，而是尽量给出<strong>能核验、能吸收</strong>的材料。
</p>

<p align="center">
  <a href="#这是什么">介绍</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#适用平台">适用平台</a> ·
  <a href="#能做什么">能力</a> ·
  <a href="#引擎与路由">引擎</a> ·
  <a href="#使用示例">示例</a> ·
  <a href="#安装与配置">配置</a> ·
  <a href="#版本记录">更新</a>
</p>

<p align="center">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.10+-green">
  <img alt="version" src="https://img.shields.io/badge/version-2.4.1-informational">
</p>

---

## 这是什么

**Argo 是一套给 AI Agent 用的搜索基础设施。** 由 我的另一个agent 项目 kimix 开发、测试创作的专业搜索agent  https://github.com/taxueseek/kimix

你问「贵州茅台股价」，它会优先走东方财富；问「transformer attention paper」，会优先走 arXiv；问「React 和 Vue 怎么选」，会多源召回再合并去重。更重要的是：它会尽量判断**哪些结果值得当真**——是不是搜索结果页壳、有没有数字和披露、多个域名是否说同一件事。

一句话：

> 产出不是「链接清单」，而是「证据候选 + 可信度分解」。

### 和「再包一层搜索 API」的差别

| 常见做法 | Argo |
|---------|------|
| 绑死一个引擎、一个 Key | 多引擎自动选路，免费优先、可配预算 |
| 搜完直接拼摘要 | 选择门槛 × 证据密度 × 时效 × 多源共识 |
| 引擎挂了整条链路挂 | 熔断、负缓存、降级到本地引擎或缓存 |
| 每次查询都重新打网 | 双层缓存（内存 + SQLite），热查询约 10ms 级 |
| 只给链接 | 附带 selection / absorption / 引擎状态等字段 |

### 当前大致能力

- **40+ 引擎**：API / 本地零成本 / 社交 / 金融 / 学术，按意图组合
- **语义路由 + 规则域**：TF-IDF 与正则域配合；低分不再误跳垂直引擎
- **证据两阶段**：Selection（能不能进候选）× Absorption（正文/摘要里有没有可吸收证据）
- **日常搜索 vs 深度研究**：日常 SERP 轻量、链接沉底；深度研究走独立子技能与专业选题 profile
- **研究与消歧**：`research` 拆子问题、`clarify` 处理歧义、`evidence` 打可信度
- **抓取栈**：HTTP → 浏览器降级，支持聚焦摘录与 PDF
- **MCP 接入**：紧凑回包、后台预热、社交并行；可挂到 Claude / Grok / Kimi 等客户端
- **斜杠分层（Agent 侧）**：`/argo` 主入口，`/argo-search` / `/argo-research` / academic·finance 等子命令

### v2.4.1 本版亮点（通俗版）

这一版主要解决三件事：**更快更省**、**指令更清楚**、**深度研究更专业**。

| 你关心的 | 用大白话说 |
|----------|------------|
| MCP 慢、回包太胖 | 连上后后台先热身；默认返回「瘦结果」，少占 Agent 上下文；多平台社交一起搜，不再一个个等 |
| 会不会搜得变差？ | **不会改排序和召回**。瘦的是「给 Agent 看的摘要长度」；要完整摘要时传 `summary: false` |
| 主技能 / 子技能怎么用 | 主入口 `/argo`；日常搜用 `/argo-search`；要做深度研究用 `/argo-research` 或 `/deep-research`；科研/金融可直接 `/argo-research-academic`、`/argo-research-finance` |
| 深度研究和普通搜索差在哪 | 普通搜索像「打开搜索引擎看一眼」；深度研究是 argo **内建**的子技能：拆问题、按选题（学术/金融等）选引擎、带质量门禁——**不外挂别的 skill** |
| 日常结果怎么读 | 标题和摘要在上，链接沉底「相关信源」；日常默认不落盘归档，研究默认可归档复盘 |

```
查询
  │
  ├─ 意图消歧（可选）
  ├─ 路由（域规则 + TF-IDF + 预算模式）
  ├─ 多引擎召回（熔断 / 负缓存 / 并行）
  ├─ RRF 融合 + 可选精排
  ├─ 证据快评（权威 · 证据密度 · 时效 · 共识）
  └─ 统一 JSON（含 engine_outcomes，方便 Agent 判断空结果原因）
```

---

## 为什么需要它

| 痛点 | Argo 的做法 |
|------|------------|
| 中文、金融、学术场景要换来换去 | 自动按域选引擎，东财 / 知乎 / arXiv 等有专线 |
| 摘要里有数，正文对不上 | 高后果场景建议再 `fetch` + `evidence`，不只信 snippet |
| 搜索结果页、跳转链被当成信源 | SERP / 跳转壳识别并降权 |
| 付费额度紧张 | `fast` / `auto` / `deep` / `budget` 四档预算 |
| 重复问题反复等几秒 | 分级 TTL + 柔性命中（条数够可截断复用） |
| 引擎空转白等 | 熔断 + 短 TTL 负缓存，二次调用接近 0ms 跳过 |

---

## 快速开始

### 方式一：本地安装（推荐开发者）

```bash
# 1. 克隆仓库
git clone https://github.com/taxueseek/argo.git
cd argo

# 2. 安装依赖（仅 PyYAML，多数路径用标准库即可）
pip install pyyaml

# 3. 验证安装
python3 scripts/search.py "Python asyncio"
```

看到 JSON 输出即表示安装成功。接下来可以试试：

```bash
# 金融查询（自动路由到东方财富）
python3 scripts/search.py "贵州茅台股价" --json

# 查看路由决策（TF-IDF 分数 + 引擎选择）
python3 scripts/search.py "transformer attention paper" --explain

# 列出所有可用引擎
python3 scripts/search.py --list-engines
```

### 方式二：MCP 接入（推荐 Agent 用户）

Argo 提供 16 个 MCP 工具，可直接挂到 Claude Code / Kimi / Grok 等客户端：

```bash
# 启动 MCP 服务
python3 scripts/mcp_server.py
```

然后在客户端配置 MCP server（以 Claude Code 为例）：

```json
{
  "mcpServers": {
    "argo": {
      "command": "python3",
      "args": ["/path/to/argo/scripts/mcp_server.py"]
    }
  }
}
```

配置完成后，Agent 会自动调用 `argo_search`、`argo_research`、`argo_evidence` 等工具，无需手动操作。

### 方式三：作为 Python 库调用

```python
from search import super_search

# 基础搜索
result = super_search("Python asyncio", n=5, mode="fast")
for item in result["results"]:
    print(item["title"], item.get("credibility_fast"), item["url"])

# 指定引擎、跳过缓存
result = super_search("黄金价格", engine="eastmoney", n=3, skip_cache=True)
```

> **零配置可用**：不配任何 API Key 也能运行，免费引擎 + 本地 `local_*` 引擎会兜底。配了 Key 的引擎质量通常更好，没配则自动跳过。

---

## 适用平台

Argo 通过 MCP 协议接入各种 AI Agent 客户端，也支持命令行和库调用：

| 平台 | 接入方式 | 说明 |
|------|---------|------|
| **Claude Code** | MCP Server | 配置 `mcp_server.py` 路径，自动获得 16 个搜索工具 |
| **Kimi** | MCP Server | 同上，Kimi 客户端直接调用 |
| **Grok Build** | MCP Server | 同上 |
| **Cursor** | 手动配置 | 通过 MCP 协议接入 |
| **Cline / Continue** | MCP Server | 支持 MCP 的 IDE 插件均可 |
| **命令行** | 直接运行 | `python3 scripts/search.py "查询词"` |
| **Python 项目** | 库调用 | `from search import super_search` |

### 安装后验证

```bash
# 检查 Python 版本（需要 3.10+）
python3 --version

# 检查依赖
python3 -c "import yaml; print('PyYAML OK')"

# 运行单元测试
python3 -m pytest tests/test_unit.py -q

# 检查引擎健康状态
python3 scripts/search.py --list-engines
```

---

## 能做什么

| 能力 | 说明 | 入口 |
|------|------|------|
| 统一搜索 | 路由 → 召回 → 融合 → 快评 | `search.py` / `argo_search` |
| 深度研究 | 拆子问题、多源采集、缺口提示 | `research.py` / `argo_research` |
| 可信度评估 | 权威 / 证据密度 / 时效 / 交叉验证 | `evidence.py` / `argo_evidence` |
| 意图消歧 | 多义词、品牌碰撞、策略建议 | `clarify.py` / `argo_clarify` |
| 页面抓取 | HTTP 优先，必要时浏览器降级 | `fetch_v3` / `argo_fetch` |
| 站点爬取 / 提取 | 列表页、表格、元数据等 | `crawl` / `extract` |
| 社交与舆情 | 微博 / 小红书 / B 站 / Reddit / X 等 | 社交引擎 / social-sentiment |
| 健康与配额 | 引擎可用性、成本档位 | `health_check` / `quota` |

### 预算模式

| 模式 | 适合 | 行为 |
|------|------|------|
| `fast` | 简单问题、要速度 | 免费引擎优先，跳过付费精排 |
| `auto` | 默认日常 | 成本感知，质量与花费折中 |
| `deep` | 调研、综述 | 质量优先，可多用引擎 |
| `budget` | 额度紧 | 配额控制，用完降级 |

### 证据评分（简版）

```
selection  ≈ 域名权威，SERP/跳转链压到很低
absorption ≈ 数字 / 定义 / 对比 / 披露等证据密度
freshness  ≈ 发布时间（会忽略「2015 年以来」这类历史对比年）
综合       ≈ 0.40·selection + 0.35·absorption + 0.15·freshness + 0.10·引擎分
```

搜索结果里会带 `selection`、`absorption`、`credibility_fast`、`evidence_flags` 等字段，方便 Agent 直接排序，而不必每次再跑一遍完整 evidence。

### Agent 使用纪律（建议）

1. **高后果问题**（持仓、安全、是否属实）：search → 看快评分 → 对 top 结果 `fetch` → 再下结论  
2. **数字**：写清口径，冲突时并列，不要硬合并  
3. **搜索结果页 / 跳转链**：不要当正文信源  
4. **社交帖**：当舆情与叙事，不当事实真值  
5. **事实核查**：宁可多一两条分层查询（来源 / 对比 / 主体）

---

## 引擎与路由

### 直连与垂类（节选）

| 引擎 | 场景 | 成本倾向 |
|------|------|----------|
| anysearch | 通用 / 技术 | 免费 |
| eastmoney | 股票 / 基金 | 免费 |
| zhihu | 中文观点 / 评测 | API |
| arxiv / semantic_scholar / openalex | 学术 | 免费为主 |
| github / stackoverflow | 代码与问答 | 视配置 |
| byted / bocha / metaso | 中文网页 | API / 低成本 |
| tavily / felo | 国际 / 综合 | 付费 |
| exa | 语义检索 | 有额度 |
| wechat_sogou | 公众号检索 | 免费 |
| cls_telegraph / ths_hot / em_global_news | 财经快讯与热点 | 免费 |
| twitter / reddit / xiaohongshu / bilibili / weibo | 社交 UGC | 免费（部分需登录） |

完整列表以 `config.yaml` 与 `--list-engines` 为准。

### 本地零成本层（`local_*`）

不依赖独立的 SearXNG 服务。主路径用进程内 HTML / RSS / JSON 解析（如 `local_bing`、`local_sogou`、`local_arxiv` 等），由路由按语言与主题展开，并参与 RRF 融合。

### 路由怎么选

```
查询
  → 特征（中英比例、是否对比、是否技术词…）
  → 正则域（股价 / 基金 / 学术 / 代码…）
  → TF-IDF 语义分（过低则回退通用引擎，避免误进垂直站）
  → 预算过滤 + 语言补充源
  → engines_combo
```

金融示例仍会进东财；开发文档类不会再因为「零分第一名」误进东财。

---

## 使用示例

### 金融

```bash
$ python3 scripts/search.py "贵州茅台股价" --explain

# 典型：命中 stock_query → 东方财富为主
```

### 学术

```bash
$ python3 scripts/search.py "transformer attention mechanism paper" --json
# domain 常为 academic，引擎组合含 arxiv 等
```

### 研究与核验

```bash
# 深度研究
python3 scripts/research.py "2026 公募基金二季报 持仓结构" --depth deep --json

# 对已有搜索结果打可信度
python3 scripts/search.py "同一查询" --json | \
  python3 scripts/evidence.py "同一查询" --stdin --json
```

### 作为库调用

```python
from search import super_search

result = super_search("Python asyncio", n=5, mode="fast")
for item in result["results"]:
    print(item["title"], item.get("credibility_fast"), item["url"])

# 指定引擎、跳过缓存
result = super_search("黄金价格", engine="eastmoney", n=3, skip_cache=True)
```

### MCP

```bash
python3 scripts/mcp_server.py
```

工具包括：`argo_search`、`argo_research`、`argo_evidence`、`argo_clarify`、`argo_fetch`、`argo_crawl`、`argo_extract`，以及社交相关工具等。以当前 `mcp_server.py` 注册列表为准。

---

## 安装与配置

### 环境要求

| 项目 | 要求 |
|------|------|
| Python | 3.10+ |
| 依赖 | `pip install pyyaml`（仅此一个） |
| Node.js | 不需要 |
| SearXNG | 不需要（内置本地引擎替代） |

### API Key（全部可选）

不配置则跳过对应引擎，免费引擎自动兜底。请用环境变量，不要把真实 Key 写进仓库。

```bash
# 推荐：ARGO_ 前缀（与旧名兼容，新名优先）
export ARGO_TAVILY_API_KEY="你的密钥"     # 国际搜索
export ARGO_BOCHA_API_KEY="你的密钥"      # 中文网页（AI 友好）
export ARGO_METASO_API_KEY="你的密钥"     # 中文 AI 搜索
export ARGO_BRAVE_API_KEY="你的密钥"      # 隐私搜索
export ARGO_FELO_API_KEY="你的密钥"       # AI 综合答案
export ARGO_EXA_API_KEY="你的密钥"        # 语义搜索
export ARGO_OCTEN_API_KEY="你的密钥"
export ARGO_WEB_SEARCH_API_KEY="你的密钥" # 字节 byted
export ZHIHU_ACCESS_SECRET="你的密钥"     # 知乎观点
export GITHUB_TOKEN="你的密钥"            # 提高 GitHub 限频
export ANYSEARCH_API_KEY="你的密钥"       # 垂直域搜索
# 旧名仍可用：TAVILY_API_KEY / BOCHA_API_KEY / EXA_API_KEY / WEB_SEARCH_API_KEY ...

# 可选：引擎白名单 / 黑名单
# export ARGO_ENABLE_ENGINES="hackernews,eastmoney,tavily"
# export ARGO_DISABLE_ENGINES="brave"

# 新引擎接入与验证见 docs/ADDING_NEW_ENGINE.md
# python3 scripts/engine_validate.py --engine <id> --stage health --admit
# python3 scripts/search.py --list-engines --detail
```

### 缓存配置

默认写在用户目录下的 SQLite（路径见 `config.yaml` 的 `cache.db_path`，一般为 `~/.cache/unified-search/cache.db`）。

| 类型 | 大致 TTL | 说明 |
|------|----------|------|
| 金融 | 约 5 分钟 | 股价等实时数据 |
| 新闻 / 实时 | 约 10–15 分钟 | 快讯类内容 |
| 通用 | 约 1 小时 | 非时效域可拉长到当日 |
| 研究 / 常青 | 约 2–24 小时 | 学术类内容 |
| 空结果 | 很短 | 避免把失败固化成「没结果」 |

缓存键会区分预算模式与搜索深度；请求条数更少时，可用已有更多结果做柔性命中。

### 常见问题

**Q：不配 API Key 能用吗？**

A：完全能用。Argo 内置 25+ 本地零成本引擎和多个免费 API 引擎，不配 Key 时自动走免费路径。

**Q：如何确认引擎是否正常工作？**

A：运行 `python3 scripts/search.py --list-engines` 查看引擎列表，或 `python3 scripts/search.py "测试" --explain` 查看路由决策。

**Q：MCP 和命令行模式有什么区别？**

A：底层能力完全相同。MCP 模式适合集成到 Agent 工作流中，由 Agent 自动调用；命令行模式适合脚本和手动查询。

---

## 目录结构（简）

```
argo/
├── README.md
├── SKILL.md                 # Agent 技能说明
├── config.yaml              # 引擎与域配置
├── backends/                # 注册表、配额、中文信源表
├── scripts/                 # 搜索 / 路由 / 缓存 / 证据 / MCP …
├── sub-skills/local-search/ # 本地零成本引擎
├── tests/
└── docs/                    # 图示与路线图
```

---

## 设计取舍

1. **先服务 Agent 吸收，再谈链接数量。**  
2. **免费与本地优先，付费可选增强。**  
3. **失败要可观测**：空结果、超时、熔断分开标，不静默吞掉。  
4. **配置驱动扩引擎**，避免每个源写一套不可维护逻辑。  
5. **不把社交当真理库**；X 精确互动排序以平台原生能力为准时，Argo 做扩维与核验更合适。

---

## CLI 常用参数

```
python3 scripts/search.py [选项] 查询词

  --engine, -e       引擎，默认 auto
  --max-results, -n  条数，默认 5
  --depth, -d        fast | balanced | deep
  --mode             fast | auto | deep | budget
  --no-cache         跳过缓存
  --explain          打印路由说明
  --json             JSON 输出
  --timeout, -t      超时秒数
  --list-engines     列出引擎
```

---

## 适用场景

- Claude Code / Grok Build / Codex / Kimi 等 **Agent 的搜索后端**
- 脚本与流水线里需要 **可复现、可缓存** 的检索
- 中文事实核查、金融公开信息、学术与代码资料的 **多源对照**

不太适合单独承担：平台内 X 的高级互动定榜、需要长期养服务的最大召回聚合器（已用内嵌本地引擎替代外挂 SearXNG 主路径）。

---

## 版本记录

| 版本 | 说明 |
|------|------|
| **v2.4.1** | 见下方「v2.4.1 增量」；性能与 MCP 紧凑回包、搜索体验标准化、深度研究子技能与专业选题、引擎生命周期与归档分层 |
| **v2.4.0** | 路由低分回退与社交误吸过滤；缓存 depth / 柔性命中；熔断与负缓存；`engine_outcomes`；RRF 共识源；fetch URL 缓存；介绍页与发布整理 |
| **v2.2–v2.3** | 证据两阶段、中文信源表、content_signals、fetch 栈、引擎扩充、MCP 能力增强 |
| **v2.1** | 社交引擎层（多平台 UGC） |
| **v1.x** | 统一命名为 Argo，多引擎路由与双层缓存成型 |

### v2.4.1 增量

**性能与 MCP**

- 初始化后台预热 search/cache，摊平首次 `tools/call` 延迟
- 默认紧凑 JSON（无 indent）+ `summary` 精简重字段，显著降低 Agent 上下文占用
- 社交多平台并行抓取；模块进程内缓存
- 检索排序与引擎召回逻辑不变；需要全量摘要时 MCP 传 `summary: false`

**搜索体验标准化**

- 日常搜索：链接沉底「相关信源」，默认不归档
- 深度研究：默认归档、可 `--no-archive`；JSON 统一 `sources[]` / 引用 `[n]`
- 已知 URL 走 fetch/handoff，避免当关键词热搜
- 引擎声明 / env 注入 / 准入校验（`engine_validate`）与 routable 过滤

**深度研究子技能（argo 内建，不外挂其他 skill）**

- 选题 profile：`ai` / `investment` / `finance` / `academic` / `tech` / `tool` / `internet` / `social`
- 模板子查询、引擎优先、质量门禁、建议报告结构、信源级别
- 金融免责声明；学术禁止编造 DOI/论文类主张
- 触发词与斜杠：`/argo`、`/argo-search`、`/argo-research`、`/argo-research-academic`、`/argo-research-finance`、`/deep-research` 等

```bash
python3 scripts/research.py "议题" --topic academic --json
python3 scripts/research.py "议题" --topic finance --json
python3 scripts/research.py --topic help
```

更细的优化说明见 `docs/OPTIMIZATION_ROADMAP_v2.4.md`、`docs/SEARCH_ARCHIVE.md`、`docs/PROBLEM_REFRAME_SEARCH_UX_v2.4.5.md`。

---

## 贡献

欢迎提 Issue 与 Pull Request。改路由或证据逻辑时，请尽量补对应测试（`tests/`）或跑一遍：

```bash
python3 -m pytest tests/test_unit.py tests/test_evidence_v22.py -q
python3 scripts/ab_eval_p0p1.py   # 可选，含在线实测
```

## License

MIT License © 2026 [taxueseek](https://github.com/taxueseek)

---

> 好的搜索不是让你看得更多，是让你更敢下结论——以及知道什么时候还不该下结论。

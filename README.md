<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="Argo 阿尔戈：给 Agent 用的搜索与深度研究工具，278 个搜索源，按域按语言选路，输出可吸收的证据">
</p>

<p align="center">
  <strong>中文</strong> ·
  <a href="README.en.md">English</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.es.md">Español</a>
</p>

<p align="center">
  <a href="#这是什么">介绍</a> ·
  <a href="#它和模型自带搜索--ai-搜索--聚合搜索比强在哪">对比</a> ·
  <a href="#问啥像啥">证明</a> ·
  <a href="#它怎么工作">机制</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#能做什么">能力</a> ·
  <a href="#安装与配置">配置</a> ·
  <a href="#最近更新">更新</a>
</p>

<p align="center">
  <img alt="license" src="https://img.shields.io/badge/license-Apache_2.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.9+-green">
  <img alt="version" src="https://img.shields.io/badge/version-2.9.9-informational">
  <img alt="engines" src="https://img.shields.io/badge/engines-278-orange">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-19%20tools-purple">
</p>

> **这是踏雪寻仙 DeepSeek Harness 插件系列的一员**（[dsh-files](https://github.com/taxueseek/dsh-files) 39★ · [dsh-models](https://github.com/taxueseek/dsh-models) 模型选择 + 子代理路由 + 推理层级）。同作者完整技能系列：[session-digger](https://github.com/taxueseek/session-digger) 29★ · [taxue-invest](https://github.com/taxueseek/taxue-invest) 21★ · [taxueskills](https://github.com/taxueseek/taxueskills) 23★ · [taxue-creative-style](https://github.com/taxueseek/taxue-creative-style) 12★

# Argo 阿尔戈：给 Agent 用的搜索与深度研究工具

## 这是什么

**Argo 是一个装在 Agent 里的搜索与深度研究工具，也可以获取相关网页的信息。背后连着 278 个搜索源，覆盖 99 个业务领域，同时支持本地文件的基础搜索能力。在开源搜索项目里，具备非常强大的搜索能力。**

支持配置 API Key 提升搜索质量（配了 Key 的源质量通常更好，没配则自动跳过），也支持免费引擎和本地引擎自动保底。覆盖中英日韩等 18 种语言，支持金融、学术、影视、体育、代码、宏观等垂直场景。

## 子技能

Argo 的能力通过四个子技能（sub-skills）组织，每个子技能是一条独立可用的管线：

| 子技能 | 能力 |
|--------|------|
| **local-search** | 本机文件搜索 + 本地零成本聚合引擎（32 个本地引擎）。搜代码符号、文件名、文档全文、文件结构。可并入网络搜索结果，加一个开关本机命中就并进同一次搜索 |
| **local-seek** | 本地语义搜索（BM25 + 向量混合），搜笔记、记忆、资料库。三层递进：先定位文件、再看上下文、最后精读段落 |
| **ego-search** | 登录态专业搜索。知乎、小红书、公众号等需要登录才能看的内容，用真实浏览器配合登录态去搜。默认关闭，需时开启 |
| **js-run** | 无浏览器执行网页 JS。不开浏览器跑「环境探测 + 纯计算」型网页脚本，单次上下文 P50 约 1ms |

四个子技能覆盖了从「搜本机」到「搜全网」到「搜登录墙后面」的完整链路。大部分场景用 local-search + 联网搜索就够，特殊场景再开 ego-search 和 js-run。

## DSH 插件

Argo 同时是 DeepSeek Harness 的插件，一行命令装好：

```bash
dsh plugin --profile web add argo-search                  # npm 发布版
dsh plugin --profile web add "github:taxueseek/argo"      # GitHub main
```

装完重启 `dsh web` 即生效。搜索和抓取成为原生一等工具，不依赖 MCP 连接，不占常驻上下文开销。另有 1 个 `wide_research` 并行研究和 19 个 MCP 工具按需开启。完整接入形态见下文。

### 三种调用通道，一套能力

同一个 argo 检索内核（同引擎、同压缩、同守卫）在 DSH 里开三条通道，按宿主能力自动降级、互为冗余：

| 通道 | 默认 | 说明 |
|------|------|------|
| **原生一等工具** | 开 | `argo_search` / `argo_fetch` 等经 `ctx.tools.register` 注册，走 CLI 单发（`--call`）复用 MCP 同一套引擎与守卫，**不依赖 MCP 连接、零常驻 token 开销** |
| **原生 web seam** | 开 | 注册 `argo` 搜索与抓取两个 provider，宿主内置 `web_search` 路由到 argo 引擎链、`web_fetch` 走 argo 抓取链（HTTP 优先，遇 Cloudflare / JS 渲染自动升级反检测浏览器） |
| **MCP 形态** | 按需 | 19 个 `mcp__argo__*` 完整工具面。工具定义每轮注入上下文且压缩清不掉（实测 19 工具 ≈3.7K token/轮），而高频路径已由原生工具与 web seam 覆盖，故默认不挂载；需要长尾取证能力时在 profile patch 中取消 `mcp-argo` 行注释 |

另有 1 个 `wide_research`：规划互补轨道（`depends_on` 依赖分阶段，默认并行）→ 有界并发子代理取证 → 来源账本（仅 http(s) URL 入账）→ 综合报告；输出自带 `quality_gate_results`（`conclusion_cap`：low/medium/high），`passed=false` 时降级表述、先核验账本来源再下结论。worker 取证工具白名单默认双写 MCP 与原生工具名（allow 语义）——MCP 挂载与否两态自洽：MCP 在时用全量面，缺席时自动落到 `argo_search` / `argo_fetch` 与宿主 `web_search` / `web_fetch`。

日常一问用 `argo_search`；单代理取证用 `argo_research`；多视角对比、要引用账本用 `wide_research`。worker 默认只能调 argo 取证工具，不会再调 `argo_research`（硬保护，用户层放行也无效）。

### 自带设置页：六个分区，改完即生效

插件自带浏览器端设置页，两个入口：设置弹窗（`Cmd+,`）左侧一级菜单「Argo」，或 设置 > 插件 > Argo（同一面板的摘要卡片，点开即用）。

<p align="center">
  <img src="assets/readme/settings-tools.webp" width="100%" alt="Argo 设置页「工具」：18 个原生工具的勾选——高频路径默认开 argo_search / argo_fetch，长尾 16 个按需，下方是单次调用超时与返回文本上限">
</p>

<p align="center">
  <img src="assets/readme/settings-engines.webp" width="100%" alt="Argo 设置页「引擎」：argo 本体 28 个行为开关移进面板——缓存 / TF-IDF 重排 / 数值交叉校验 / MinHash 去重 / SERP 守卫与去重阈值，未配置行显示默认态，改完即时生效">
</p>

<p align="center">
  <img src="assets/readme/settings-keys.webp" width="100%" alt="Argo 设置页「密钥」：DeepSeek 官方搜索显示「已配置 · 凭证库」并与宿主 web_search 共用同一把 key；腾讯元宝 / Tavily 等常用引擎 key 可填写、更新、清除，明文不回显">
</p>

| 分区 | 管什么 |
|------|--------|
| **接入** | `web_search` / `web_fetch` seam 开关；引擎入口命令与参数（npx 或本机 `mcp_server.py`）；搜索/抓取超时、正文上限、连接空闲回收、并发连接数 |
| **工具** | 18 个原生一等工具的勾选（默认只开 `argo_search` / `argo_fetch`）；单次调用超时与返回文本上限 |
| **引擎** | argo 本体的行为开关（原环境变量，curated 白名单 28 个：缓存 / TF-IDF 重排 / 数值交叉校验 / MinHash 去重 / SERP 守卫、Jina / 移动端 UA / Parallel 抓取层、语义证据层、对冲宽限与尾宽、引擎启停名单、代理、本机检索根与读取白名单、预热 / 使用日志 / 连接池 / 各类 TTL）。开关类即时生效，数字 / 文本 / 名单类分组保存；未配置行显示 argo 默认态并标注「默认」。授权类开关（可复算执行、内网 URL）刻意不进面板 |
| **密钥** | 常用引擎 key 的填写、更新、清除；显示「已配置 / 未配置 + 来源层（凭证库 / 启动环境 / .env）+ 只读态」，明文不回显；每行可勾选「转发」，等价于把引用名加进 `keyRefs` |
| **研究** | worker / 轨道 / 每轨信源 / token 上限与报告落盘目录 |
| **高级** | worker 工具 allow / deny 名单、凭证转发名单 |

保存即生效（全部为 volatile 配置）：原生工具集合按勾选增删注册，超时、并发、研究上限在下一次调用时读取；引擎开关写成 `argoEnv`（一行一个 `KEY=VALUE`）随 argo 子进程 env 进入，对下一次新建连接生效（池内旧连接按 `searchIdleMs` 回收，默认 60s）。结构性字段（`toolName`、`provider`、`searchProviderId`、`fetchProviderId`）不在设置页，仍走 profile patch，改后需重启。

**密钥安全**：写入 DSH 凭证库（与 Models 页同一处），不写进插件配置、不打日志、不回显明文。凭证服务优先级为**存储文件层 > 启动环境 > 项目 .env > 家目录 .env**：在面板（或 Models 页）保存即覆盖启动环境里的旧值，argo 子进程下一跳新连接用新 key；「清除」只删凭证库里的值，环境变量或 `.env` 提供的 key 会重新露出。设置页只是凭证域的写入口，读取仍由宿主在每次调用时完成。

**DeepSeek 官方 key 原生供给**：argo 的 `deepseek_search` 引擎（官方 `api.deepseek.com/v1/tools/search`，结果自带 authority 权威分级 / rerank_score 排序分 / 发布时间）与 DSH 官方搜索后端**共用同一把 key**。插件经 DSH 原生凭证 seam 取用，不需要任何手工配置：解析路径 `ctx.get('credentials').resolve('DEEPSEEK_API_KEY')`，逐次解析——在 web Models 页轮换 key 后，下一跳新连接即生效，无需重启插件。插件不读 key 文件、不落盘、不打日志；key 值只经内存进入 argo 子进程 env，任何输出与错误信息都不含它。旧宿主无 credentials 服务时静默跳过：argo 侧该引擎按缺 key 停用，其余源不受影响。

**web seam 的 pin 必读**（漏配会打挂 `web_fetch`）：宿主 `dsh-web` 的 provider 选择规则是「配置 id 优先；都没有且恰好一个可用则选它；**多个可用直接报 `WEB_PROVIDER_AMBIGUOUS`**」。web profile 默认挂着官方 http fetch provider（免 key、永远可用），本 bundle 再注册一个就是两个可用——**不 pin 的话 `web_fetch` 整个挂掉**（`web_search` 同理，若 profile 里另有搜索 provider）。故启用 web seam 的 profile 必须在 web 服务行显式指定：

```yaml
- id: web
  config:
    searchProvider: argo    # 已默认启用搜索 seam 时必配
    fetchProvider: argo     # 已默认启用抓取 seam 时必配
```

`searchProviderEnabled: false` / `fetchProviderEnabled: false` 可整体关掉对应 seam（provider 仍注册但不可选，不影响另一个）。搜索引擎侧，Firecrawl 等匿名可用引擎 **keyless 免配置即用**（免费层额度），配置了密钥的引擎自动升级为认证请求；缺密钥不阻断路由。

> 完整配置项、Compatibility 版本对照表与卸载方式见 [packages/dsh-plugin/README.md](packages/dsh-plugin/README.md)。

## 核心优势

**278 个源，240 个免密钥开箱可用。** 不配任何 API Key 就能跑，免费引擎和本地引擎自动保底。配了 Key 的源质量通常更好，没配则自动跳过。一次安装，全场景可用。

**本地搜索 + 联网搜索一体。** 这是 Argo 独一份的能力。本机文件搜索底层是 rg、mdfind 那套开发者日常工具，搜代码符号、文件名、文档全文、文件结构。加一个开关，本机命中会并进同一次网络搜索结果里，结果上标着来源是本机文件。写东西时记得自己半年前记过一段话但忘了在哪，用这个搜比翻文件夹快。

**双层缓存，热查询约 10ms。** 时效性没那么强的内容（百科类、历史数据这类），第一次查完会进缓存，之后同样的查询直接命中，不再每次都走一遍 API。既省钱也省时间。

**多语言 + 垂直场景。** 实测过 18 种语言，中文、英文和日文能查到当地一手来源。问论文走学术库，问影视走条目库，问代码走问答社区，问行情走金融源。泛泛的问题才走通用搜索。

## 装完就能用

零配置就能跑。三种装法，按你的习惯挑：

**想让 AI 助手自己会搜**，把技能包装进它的技能目录。以后你说「帮我查一下」「核实一下」，它会自己去查。

**想随手试**，用一条 npx 命令即用即走，不用装。

**想让助手常驻着用**，给它挂上 MCP，19 个检索工具随时可用。还有一键注入命令，能给 Claude Code、Cursor 和 Windsurf 自动配好，Codex、OpenCode 和 Cline 也在这个名单里。写配置前会备份，随时能撤。

```bash
# 方式一：一键脚本（推荐本机长期用）
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash

# 方式二：npm / npx（已支持，当前 2.9.9；按 tag 发布，版本固定）
npm install -g argo-search      # 全局安装 CLI
npx -y argo-search              # 或即用即走，不装
npx -y github:taxueseek/argo    # 要跟 main 最新提交时用这条

# 方式三：git clone（开发 / 改源码）
git clone https://github.com/taxueseek/argo.git
```

装完随手试一条：

```bash
argo search "python asyncio error handling"
```

它给你的是 StackOverflow 官方 API 里按社区得分排过序、标了采纳状态的问题，不是一堆 SEO 农场。

想看看自己机器上现在有几个源可用，一条命令就列出来：

```bash
argo search --list-engines
```

## 接入形态

Argo 支持四种接入方式，按你的场景挑：

| 形态 | 适合 | 说明 |
|------|------|------|
| **MCP Server** | Agent 常驻挂载 | 19 个检索工具随时可用，一条命令注入 Claude Code / Cursor / Windsurf / Codex / OpenCode / Cline，写配置前自动备份 |
| **DSH 插件** | DeepSeek Harness 用户 | 一行 `dsh plugin add` 装好，搜索和抓取成为原生一等工具，不依赖 MCP 连接，不占常驻上下文开销；自带六分区设置页，改完即生效 |
| **Skill 文件** | Claude Code / Grok Build / Kimi 等 | 技能包装进 Agent 的技能目录，你说「帮我查一下」「核实一下」，它自己去查 |
| **npx 即用即走** | 快速试一下 | `npx -y argo-search`（npm 发布版）或 `npx -y github:taxueseek/argo`（main），不用装，适合临时把 MCP 挂进任意客户端 |

四种形态底层是同一套 Python 代码，CLI 与 MCP 工具一一对应，装哪种都不缺能力。

## 它和「模型自带搜索 / AI 搜索 / 聚合搜索」比，强在哪

> 简单来说：前三种方案解决「**人**找信息」，Argo 解决「**Agent 及搜索核查于一身，具备一条龙的搜索服务**」。差别不在界面，在交付物，给人看的叫总结页或链接清单，给 Agent 的应是能排序、能复核、不撑爆上下文的优质内容，更可靠的搜索信息。

<p align="center">
  <img src="assets/readme/why-better.svg" width="100%" alt="左侧三种默认搜索给人看的结果，右侧 Argo 给 Agent 的可吸收证据 JSON">
</p>

| 维度 | 模型自带搜索 | AI 搜索（总结型） | 聚合搜索 / 搜索引擎 | **Argo** |
|------|------------|-------------------|---------------------|----------|
| 结果形态 | 拼好的长文本 | 给人看的总结页 | SERP 链接清单 | **精简 JSON：证据候选 + 可信度分解** |
| 垂直问题（行情 / 化学式） | 泛搜网页 | 泛搜再总结 | 泛搜网页 | **直连垂直源，直接给答案** |
| 证据可信度 | 无评分 | 无结构化评分 | 无评分 | **selection · absorption · freshness · 共识** |
| 重复查询 | 每次都打网 | 每次都打网 | 靠页面缓存 | **双层缓存（内存 + SQLite），热查询约 10ms** |
| 成本控制 | 不可控 | 单次贵 | 免费但费事 | **预算模式，免费优先，Key 全可选** |
| 多语言 | 随模型走 | 随模型走 | 随引擎走 | **语言检测 + 引擎语言参数 + 多国语言支持** |

> 机制上，Argo 把搜索当成一条**证据管线**：语言检测 → 领域路由 → 多引擎召回 → RRF 融合 → 证据快评，交付的是 Agent 可直接排序、可 `fetch` 复核、不撑爆上下文的材料。工具链不用换，需要换的是「搜索结果应该长什么样」——再往下一层，还有和「再包一层搜索 API 的差别」，那是实现细节的对比。

---

## 2026，检索正在发生什么变化

Agent 干活的量级变了，检索的玩法跟着变了五件事，每一件 Argo 都已经落在代码里：

1. **检索从「给链接」变成「给证据」。** Agent 不看网页排版，它要能排序、能复核、不撑爆上下文的结构化材料——所以 Argo 交付的是带可信度分解的 JSON，而不是 SERP 清单。
2. **上下文成为第一成本。** 一次网页抓取动辄上万 token，Agent 的窗口烧不起。Argo 的 Agent 档单次约 3.7KB，字段集合与字节预算都有检查卡着，升级不会悄悄变胖。
3. **网站开始为 AI 备料。** llms.txt 标准与 `.md` 直出在头部文档站快速铺开——Argo 抓取链第零级自动探测这些 AI 友好变体，命中即跳过整条反爬链；HTTP/TLS 都失败还有 r.jina.ai 阅读器级保底。
4. **免费开放生态够用了。** 政府、学术、标准、安全机构的开放 API + 免 key 引擎，已经能覆盖大多数领域（240 个免配置源）；稀缺免费额度（firecrawl 1000 credits/月、stackexchange 300 次/天）做了「日常补位、关键顶上」的分层，订阅墙不是唯一解。
5. **检索质量从「感觉」到「度量」。** 排序有金标（MRR/nDCG 地板）、融合有增益消融检查、路由有负向控制矩阵——「这版比上版好吗」从此是数字问题，不是玄学。

> 以上五项已经在 v2.9.x 系列里全部落地：277 个源、98 个领域、240 个免配置开箱。逐项细节见 [docs/为什么选择argo.md](docs/为什么选择argo.md) 与 [发布说明](docs/RELEASE_NOTES_v2.9.0.md)。

---

## 问啥像啥

<p align="center">
  <img src="assets/readme/proof-routes.svg" width="100%" alt="四类真实路由：金融、影视、多语言、地理">
</p>

| 你这样问 | 大致会怎样 |
|----------|------------|
| python asyncio error handling | 编程问答域 → StackOverflow/StackExchange 官方 API，带得分与采纳标记 |
| AAPL / 美股盘前 | 美股域，与 A 股分流 |
| 肖申克的救赎 主演 / Inception director | 影视域 → IMDb 等 |
| 梅西 俱乐部 / 库里 球队 | 体育域 → TheSportsDB 等 |
| 埃菲尔铁塔在哪 / where is Eiffel Tower | 地理实体 → OpenStreetMap 等 |
| NASA founding year / 国务院职能 | 组织实体 → Wikidata 等 |
| 周杰伦 专辑 / Taylor Swift album | 媒体域 → iTunes 等 |
| アニメ おすすめ / 한국 영화 추천 | 识别日/韩语 → 语言友好源，少塞中文专用站 |
| 美国 CPI、日本 通胀率 | 宏观数据域；国别分流（本国权威源优先） |
| log4j 漏洞 CVSS / nodejs 22 end of life | 安全域 → NVD 官方；生命周期域 → endoflife.date |
| attention is all you need | 学术域 → arXiv/OpenAlex/CrossRef，元数据与 DOI 直连 |
| 阿司匹林 分子式 | 化学域 → PubChem 类答案 |
| 台积电估值分歧（深度研究） | 拆子问题 + 多源并行，垂直源被 boost |

---

## 它怎么工作

<p align="center">
  <img src="assets/readme/workflow.svg" width="100%" alt="查询 → 语言与域 → 多引擎召回 → RRF → 证据快评 → 统一 JSON">
</p>

```
查询
  ├─ 意图消歧（可选）
  ├─ 查询改写（可选；路由仍看原始意图）
  ├─ 语言检测 + 语言偏好
  ├─ 路由（域规则 + TF-IDF + 预算 + 语言补充源 + 热路径缓存）
  ├─ 多引擎召回（熔断 / 负缓存 / 并行）
  ├─ 空结果分阶恢复（放宽 → 换同族/通用 → 跨语言；防污染）
  ├─ RRF 融合 + 可选精排
  ├─ 证据快评（权威 · 证据密度 · 时效 · 共识）
  └─ 统一 JSON（含 engine_outcomes / recovery / route_reason）
```

### 证据评分（简版）

```
selection  ≈ 域名权威，SERP/跳转链压到很低
absorption ≈ 数字 / 定义 / 对比 / 披露等证据密度
freshness  ≈ 发布时间（会忽略「2015 年以来」这类历史对比年）
综合       ≈ 0.40·selection + 0.35·absorption + 0.15·freshness + 0.10·引擎分
```

结果字段含 `selection`、`absorption`、`credibility_fast`、`evidence_flags` 等，方便 Agent 直接排序。

### Agent 使用纪律（建议）

1. **高后果问题**（持仓、安全、是否属实）：search → 看快评分 → 对 top 结果 `fetch` → 再下结论  
2. **数字**：写清计算方式，冲突时并列，不要硬合并  
3. **搜索结果页 / 跳转链**：不要当正文信源  
4. **社交帖**：当舆情与叙事，不当事实真值  
5. **事实核查**：宁可多一两条分层查询（来源 / 对比 / 主体）

---

## 快速开始

任选一种即可。**npm 安装已支持，当前版本 2.9.9**，与 GitHub 同为本仓库发布。npm 路线用 `npm install -g argo-search` 或 `npx -y argo-search`，按 tag 发布、版本固定、下载体积更小；GitHub 路线用 `install.sh` / `install.ps1` 或 `npx github:taxueseek/argo`，跟 main 走。之后每次打 tag，CI 会把两个 npm 包和 GitHub Release 一起发出；registry 上更早的 v1.0.1 是历史版本，不要锁定到它。

**零配置就能跑**：不配 API Key 时走免费引擎 + 本地 `local_*` 引擎；配了 Key 的源质量通常更好，没配则自动跳过。

### 方式一：一键脚本（推荐本机长期用）

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
```

Windows（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.ps1 | iex"
# 或下载后执行：
# powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

装到指定目录、并挂 Skill 入口：

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh \
  | bash -s -- --home "$HOME/.local/share/argo" --link "$HOME/.claude/skills/argo"
```

验证：

```bash
python3 ~/.local/share/argo/scripts/search.py "python asyncio error handling" --json
python3 ~/.local/share/argo/scripts/search.py --list-engines
```

### 方式二：MCP 不装包，直接用 GitHub（推荐 Agent 快速挂载）

需要 **Node.js 18+** 和 **Python 3.9+**，首次执行一次：

```bash
pip3 install pyyaml
```

```bash
npx -y github:taxueseek/argo
```

客户端配置示例（Claude Code / Cursor / Kimi 等）：

```json
{
  "mcpServers": {
    "argo": {
      "command": "npx",
      "args": ["-y", "github:taxueseek/argo"]
    }
  }
}
```

### DeepSeek Harness 一键插件（.dsh-plugin bundle）

在 DeepSeek Harness 里一行安装（两种装法）：

```bash
# 装法 A：19 个 mcp__argo__* 工具（主包自带 bundle，与 MCP 全量相同）
dsh plugin --profile web add argo-search                  # npm 发布版
dsh plugin --profile web add "github:taxueseek/argo"      # GitHub main

# 装法 B：搜索工具 + wide_research 并行研究调度（子包）
dsh plugin --profile web add @taxueseek/argo-dsh          # npm 发布版
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

重启 `dsh web` 后生效。包结构见 `packages/dsh-plugin/`；同 id `mcp-argo` / `wide-research` 可在用户层 `cordis.patch.yml` 覆盖。装法 B 的 `wide_research` 模块只存在于子包依赖树，主包 patch 不引用它，避免只装主包时 Cordis 解析失败。DSH 0.2.1-alpha.1 与 0.2.1-rc.2 均已实测：rc.2 上 `wide_research`（2 worker，passed=true / 14 sources）与原生 `argo_search`（count=3）真跑通，`dsh web` 启动无 argo 报错；web seam 随启动注册通过（headless 无 web 服务，未做实际调用）。

### 依赖清单（通俗版）

| 依赖 | 必需？ | 干什么用 | 不装会怎样 |
|------|:------:|---------|-----------|
| **PyYAML** | ✅ 必需 | 读配置文件 | 完全跑不起来，安装脚本会自动装 |
| **curl_cffi** | ❌ 可选（v2.7.3 新增） | 模拟浏览器 TLS 指纹，过反爬站（Cloudflare 等） | 反爬站抓取成功率低一些，日常搜索无影响 |
| **rapidfuzz** | ❌ 可选（v2.9.5 新增） | 去重的模糊近重复复核：同文换站后缀/镜像站在 minhash 近似命中带内二次判定 | 跨站同文少合几条，去重召回略降；站后缀归一仍生效 |
| **ddgs CLI** | ❌ 可选 | 本地免 key 搜索的 10 个后端引擎 | 少一批零成本本地搜索源，其余不受影响 |
| **realtime-index CLI** | ❌ 可选 | 实时索引引擎（搜刚发布的内容） | 该引擎自动禁用，显式指定会回退通用引擎 |
| **Chrome** | ❌ 可选 | 页面截图、JS 渲染页、登录态抓取 | 截图工具不可用，其余正常 |
| **pdfplumber / PyMuPDF** | ❌ 可选 | PDF 提取 | argo_pdf 不可用，其余正常 |
| **Playwright** | ❌ 可选 | 截图增强 | 截图工具回退 Chrome CDP，其余正常 |

安装脚本（install.sh）只自动装前两个；其余可选依赖按需 `pip install` 或 `brew install` 即可。
更稳、完全不依赖 Node 的写法：先装脚本（方式一），再指向本机 Python：

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

Python 路径特殊时：`export ARGO_PYTHON=/path/to/python3`（仅 npx 入口会读）。

### 方式三：git clone（开发 / 改源码）

```bash
git clone https://github.com/taxueseek/argo.git
cd argo
pip3 install pyyaml
bash scripts/install.sh --link ~/.claude/skills/argo   # 可选
python3 scripts/search.py --list-engines
```

---

## 适用平台

| 平台 | 接入方式 | 说明 |
|------|---------|------|
| **Claude Code** | MCP / Skill 链接 | `npx` 或 `mcp_server.py`；也可用 `link_source.py` |
| **Kimi / Grok Build** | MCP Server | 同上 |
| **Cursor / Cline / Continue** | MCP | 支持 MCP 的 IDE 插件均可 |
| **命令行** | `search.py` / `bin/argo` | 脚本、定时任务、人工排查 |
| **Python 项目** | `from search import super_search` | 库调用 |

### 安装后自检

```bash
python3 --version          # 需要 3.9+（Windows 可用 py -3）
python3 -c "import yaml; print('PyYAML OK')"
python3 scripts/search.py --list-engines            # 引擎面与状态总览
python3 scripts/search.py --list-engines --detail   # 逐源状态：配了 key 却没结果，
                                                    # 先看 missing_env 给出的确切变量名
python3 scripts/health_check.py --all               # 真联网探针：验证 key 对端真认（可选）
python3 bin/argo paths --check                      # 本机路径自检（可选）
python3 bin/argo mcp status                         # MCP 注入/连通诊断（可选）
python3 -m pytest tests/test_unit.py -q             # 单测（可选）
```

> MCP 起不来时：`python3 scripts/mcp_diag.py` 会把 stdin 往来记录到
> `~/.kimi/argo_diag.log`。`health_check.py` 是给人用的联网探针，
> `health_probe.py` 写健康库供路由消费——名字相近，职责不同。
> CLI 退出码恒 0（含断网）：成败判断走 JSON 的 `status` / `errors` 字段。

---

## 能做什么

### 七种能力，通俗说

**1. 通用搜索 + 垂直搜索，双管齐下**

日常问题走通用网页搜索；一问到行情、影视、体育、宏观这类「有标准答案」的问题，自动切到垂直源直接给答案，而不是扔给你一堆链接。目前 278 个源（240 个免密钥开箱可用）、99 个业务域，金融 / 宏观 / 影视 / 体育 / 地理 / 组织 / 媒体 / 化学 / 学术 / 代码等都有专门的路。

**2. 缓存：不重复花冤枉钱**

时效性没那么强的内容（百科类、历史数据这类），第一次查完会进缓存，之后同样的查询直接命中，不再每次都走一遍 API。双层缓存（内存 + SQLite），热查询约 10ms 级返回，既省钱也省时间。

**3. 专为 Agent 设计，更省 Token**

产出是「证据候选 + 可信度分解」的精简 JSON，不是长篇网页；MCP 响应可以按需裁剪，snippet 可控，不会撑爆 Agent 的上下文。比常规模型自带的搜索能力更专业、更省 Token。

**4. 深度研究**

把一个笼统的问题拆成多个子问题，多源并行采集，最后给出「还差什么证据」的缺口提示。适合综述、调研这类要全面、要扎实的场景。

**5. 登录态专业搜索（专业模式，默认关闭）**

知乎、小红书、公众号这类要登录才能看的内容，以及 JS 渲染页、反爬页，用真实浏览器配合登录态去搜。默认关闭，需要时开启，依赖 ego lite 和 WebBridge 两个东西，详见下节「登录态专业搜索」。

**6. 时间能力（时间窗 + 方向排序）**

要搜「最近几天刚发布的内容」，用 `--since 7d` 这类时间窗框住发布时间范围，判断新旧不再靠猜。支持引擎（如 `realtime_index`，免 Key 实时索引源、需自装 CLI，结果自带发布时间）把窗口下推给数据源；其余引擎的融合结果会按 `published_at` 最后再剔除明确超窗的条目（宽松策略，无时间字段的保留），返回包带 `time_filtered` 统计，CLI 与 MCP 均支持。`--since 7d` 与「7 天前的绝对日期」等价、共享缓存；`--until 2026-08-01` 含当天。配合 `--sort newest|oldest` 按发布时间重排——找最新动态用 `newest`，找最早出处用 `oldest`。

**7. 在 DSH 里当原生工具用**

装成 DSH 插件后，搜索与抓取是宿主的一等工具，不是外挂的 MCP 连接：`argo_search` / `argo_fetch` 直接可调，宿主的 `web_search` / `web_fetch` 也自动路由到 argo 的引擎链与抓取链。高频路径零常驻 token 开销，长尾取证能力（截图 / PDF / 社交 / 公众号 / 招聘 / 结构化提取 / 证据核验 / 意图消歧 / 站点爬取 / 批量预检 / 变化监控 / DOI 引用 / 本机检索）按需勾选开启。改配置不用碰文件——设置页六个分区改完即生效，密钥写进宿主凭证库、明文不回显。详见上文「DSH 插件」。

### 能力入口速查

| 能力 | 说明 | 入口 |
|------|------|------|
| 统一搜索 | 路由 → 召回 → 融合 → 快评 | `search.py` / `argo_search` |
| 本地文件搜索 | 本机代码/笔记/记忆（非联网） | `argo_local_search` |
| 本地文本预览 | 白名单目录内预览（fail-closed） | `argo_local_read` |
| 可复算 | 受限子进程重算数字（默认拒绝） | `argo_recompute` |
| 深度研究 | 拆子问题、多源采集、缺口提示 | `research.py` / `argo_research` |
| 可信度评估 | 权威 / 证据密度 / 时效 / 交叉验证 | `evidence.py` / `argo_evidence` |
| 证据核验（完整链路） | 高后果问题标记 `fetch_required` + 每条 `fetch_suggested`；`--verify` 一键抓正文核验、回填「核实后证据分」、核实过的链接自动记住 | `search.py --verify 3` / `research.py --verify 3` |
| 意图消歧 | 多义词、品牌碰撞、策略建议 | `clarify.py` / `argo_clarify` |
| 页面抓取 | HTTP 优先，必要时浏览器降级 | `argo_fetch`（`mode=extract` 可结构化） |
| 截图 / PDF | 页面截图、PDF 结构化提取 | `argo_screenshot` / `argo_pdf` |
| 站点爬取 | 列表页批量抓取 | `argo_crawl` |
| 查询操作符 | `site:`/`-site:`/`-词`/`-"短语"`/`"短语"`/`lang:`/`after:`/`before:` 从路由与检索两侧剥离（用法见 references/usage.md） | 查询词内直接写 |
| 结果可信信号 | `authority_level` 离散等级（0-4，≥3 极权威）+ `summary` 摘要档（核验后回填） | 结果字段，`--fields agent` 可见 |
| 社交与舆情 | 微博 / 小红书 / B 站 / Reddit / X 等 | `argo_social_search` |
| 实时索引搜索 | 免 Key 实时索引源，结果带发布时间，适合「最近几天有什么新东西」。**需自装 `realtime-index` CLI**（未安装时该源停用，见 ENGINE_CATALOG 已停用清单；多数查询走其他源不受影响） | `--engine realtime_index` |
| 时间窗过滤 | `--since` / `--until`（`7d` 或 `2026-08-01`）限定发布时间范围；支持引擎下推 + 融合后过滤（`time_filtered`），CLI 与 MCP 均支持 | `--since 7d` |
| 时间方向排序 | `--sort newest\|oldest` 按发布时间重排（最新动态 / 最早出处） | `--sort oldest` |
| 登录态专业搜索 | 知乎 / 小红书等登录墙正文、JS 渲染页、登录站点接口直取 | `sub-skills/ego-search/scripts/ego_search.py`（默认关闭，见下） |
| DSH 原生工具 | 装成 DSH 插件后 `argo_search` / `argo_fetch` 等成为宿主一等工具，走 CLI 单发、零常驻 token 开销 | `dsh plugin add` 后直接调 |
| DSH web seam | 宿主内置 `web_search` / `web_fetch` 路由到 argo 引擎链与抓取链（HTTP 优先，Cloudflare / JS 渲染自动升级浏览器） | 插件默认注册，profile 里 pin `searchProvider` / `fetchProvider` |
| DSH 设置页 | 接入 / 工具 / 引擎 / 密钥 / 研究 / 高级 六分区，改完即生效；密钥进宿主凭证库、明文不回显 | 设置弹窗 `Cmd+,` → 左侧「Argo」 |

### 预算模式

| 模式 | 适合 | 行为 |
|------|------|------|
| `fast` | 简单问题、要速度 | 免费引擎优先，跳过付费精排 |
| `auto` | 默认日常 | 成本感知，质量与花费折中 |
| `deep` | 调研、综述 | 质量优先，可多用引擎 |
| `budget` | 额度紧 | 配额控制，用完降级 |

### 当前大致能力（v2.9.9）

Argo 干的事，一句话能说完：把一个问题按领域和语言分给合适的源，取回结果后按可信度排个序，交回给 Agent 的是能排序、能复核的材料。搜索、抓取、核验、本机文件检索走同一条管线，19 个 MCP 工具和 CLI 命令一一对应，装哪种形态都不缺能力。另有几个单点命令随用随取：argo tweet 打包 X 帖子，argo auth 管登录态，argo job 聚合招聘信息。

装成 DSH 插件后，这些能力直接变成宿主的原生工具：高频的搜索与抓取默认就开，长尾取证能力在设置页里按需勾选，argo 本体的 28 个行为开关（缓存、重排、去重、守卫、代理、TTL 等）也从环境变量搬进了面板。密钥写进宿主凭证库，明文不回显，改完即生效。

最近几个版本各修了一类问题。

v2.9.9 做的是安全与交付质量。内容安全引擎原先只评分不拦截，高风险注入页照样进上下文；现在评分过阈值就隔离，用清洗后的正文替换原文，也不再进核验证据。网络出口补了两道纵深：重定向逐跳校验，DNS 解析结果钉死在当次连接上。调度侧，早停弃置的慢源交付不再丢弃：转后台收割，三秒内完成的照常交付并记进学习器。路由上，六族真实场景扫描收了五类误路由，A 股财务查询、企业尽调/开票/人查公司、民事实务、药品说明书、论文写作 howto 各归其位。另有 argo engine add 一键接入新引擎，DSH 设置页新增路由观测卡片与工具推荐组合。

v2.9.8 做的是路由精度与反馈闭环。issue #18 报告的三件事——「latest」被误判进实时新闻、人民网匹配不到时回默认 feed、local_bing 只命中「2026」——都已复现并修掉；顺着这条线回放真实查询，又把 16 类同类误触发收窄：品牌名里的「雪」、产品名里的「火山引擎」、UI 排障里的「图标」、工商与金融字段里的「代码」、代码语境里的「汇率」等。SERP 覆盖率门与 CJK 交叉验证同时落地，避免「有结果就算相关」。感谢 @wpp666 提供的复现与定位。

v2.9.7 修的是三个老毛病。DSH 宿主里的 AI 要读一个网址时，走的是宿主自带的抓取，短链跳转和 JS 挑战页经常直接失败，argo 的抓取链根本用不上；这版把宿主的读网址接到 argo 上，这类页面从失败变成成功，普通页面没有变化。路由候选池里原先混着 18 个「只能显式指定」的引擎，万一被自动选中，唯一的付费引擎就会悄悄扣费；如今候选池里没有它们，要用必须写明白。引擎每成功一次，熔断器都要加锁重写一次磁盘；健康状态下这版已经零文件开销，多个引擎的记账也合并成一笔事务。开源协议同时从 MIT 换成了 Apache-2.0，公司商用更放心。

这之后 DSH 插件又补了一层：设置页从五个分区扩到六个，「引擎」分区把 argo 本体的行为开关收进面板（原先只能改环境变量），密钥页能看出每个 key 来自凭证库、启动环境还是 .env，工具勾选、超时、并发、研究上限全部改完即生效。取消信号也贯通到了连接建立阶段——AI 中途喊停，正在拉起的 argo 子进程会被立刻杀掉，不再空等超时。

v2.9.6 做的是查询指令和传输层。搜「site:github.com 某某」这类查询，指令有时被当成普通词，有时解析了也不过滤；现在有统一的契约层，路由和缓存只认剥掉指令后的查询，site: 真正执行过滤并进缓存键，两次搜索不会串味。传输上，以前每次 POST 都重新付一次 TLS 握手，三百到五百毫秒；连接复用之后，常驻的 MCP 进程每笔查询都省这笔钱。这版还接入了官方 DeepSeek 搜索，按次计费，不进日常组合。

v2.9.5 补的是汇率和去重。问「人民币兑美元汇率」，查询会在宏观域空转一圈，最后给个百科词条，1.4 秒也没有答案；专属汇率域建起来后，675 毫秒直接给即期汇率和每日快讯。去重加了站后缀归一和模糊复核两层，标题带分隔符的官方文档不再被误杀到后排。

v2.9.4 修的是显式选引擎的场景。用 --engine X 指定引擎时，缓存有效期原先一刀切；补上域判定之后，缓存按域走，没有备选引擎时也不再同引擎空跑一次。

逐版本细节见「最近更新」，各版发布说明在 docs/。
---

## 引擎与路由

当前配置 **278** 个源（**240** 个免密钥开箱可用）、**99** 个业务域（以 `config.yaml` 与 `--list-engines` 为准）。

### 直连与垂类（节选）

| 引擎 | 场景 | 成本倾向 |
|------|------|----------|
| anysearch / duckduckgo | 通用 / 技术 | 免费 |
| sina_quote / tencent_quote / eastmoney | A 股行情 / 资金 | 免费 |
| finviz / seeking_alpha | 美股与海外金融 | 视配置 |
| imdb / itunes / thesportsdb | 影视 / 音乐 / 体育 | 免费为主 |
| local_openstreetmap / wikidata / wikipedia | 地理 / 组织 / 百科 | 免费 |
| arxiv / semantic_scholar / openalex | 学术 | 免费为主 |
| pubchem / gbif / rfc_editor | 化学 / 物种 / 标准 | 免费 |
| github / stackoverflow / pypi / npm | 代码与包 | 视配置 |
| byted / bocha / octen / yuanbao | 中文网页 / AI 搜索 | API / 低成本 |
| hkex / shfe / cfets / defillama / gleif | 港股公告 / 上期所 / 人民币中间价 / DeFi / 法人标识 | 免费 |
| zhihu / wechat_sogou | 中文观点 / 公众号 | API / 免费 |
| tavily / exa | 国际 / 语义 | 付费或额度 |
| stackexchange / doi / endoflife / nvd / flk_law | 问答官方 API / 论文元数据 / 生命周期 / 漏洞 / 法规原文 | 免费 |
| twitter / reddit / xiaohongshu / bilibili / weibo | 社交 UGC | 免费（部分需登录） |

### 本地零成本层（`local_*`）

不依赖独立的 SearXNG 服务。主路径用进程内 HTML / RSS / JSON 解析（如 `local_bing`、`local_sogou`、`local_google`、`local_arxiv` 等）。**多语言查询**时，路由会按语种动态改写引擎语言参数（例如 Bing `setlang`），并参与 RRF 融合。

### 登录态专业搜索（ego-search，默认关闭）

普通搜索拿不到知乎、小红书、微博、公众号等**需要登录**的内容，也常抓不回来 JS 渲染页 / 反爬页。ego-search 用**真实浏览器**去做这类搜索：继承登录态、抓登录墙后的正文、在已登录站点直接调接口取数据（如知乎搜索接口）。

**默认关闭**，需要时开启（在 argo 目录下运行）：

```bash
python3 sub-skills/ego-search/scripts/ego_search.py enable
```

常用命令：`python3 sub-skills/ego-search/scripts/ego_search.py status`（查看状态）、`... disable`（关闭）、`search` / `fetch` / `api`（分别对应浏览器搜索、抓正文、调站点接口）。

**依赖**（装好其中一个就能用，两个都装更好）：

| 依赖 | 官方项目 | 是什么 | 什么时候用 |
|------|----------|--------|-----------|
| **ego lite** | [lite.ego.app](https://lite.ego.app/) | 专门给 Agent 用的浏览器应用（仅 macOS），初始化后提供 `ego-browser` 命令 | 默认首选：独立空间，不抢你正在用的浏览器标签 |
| **WebBridge** | [Kimi WebBridge 官方帮助中心](https://www.kimi.com/zh-cn/help/kimi-webbridge/kimi-webbridge-introduction) | 浏览器扩展桥，复用 Chrome / Edge 里已登录的会话 | ego lite 没装，或想直接沿用日常登录态时 |

登录态搜到的结果**不写**公共搜索缓存（避免污染共享缓存），需要融合时用 `merge` 命令把常规结果和登录态结果放一起分析。

### 本机配置覆盖层（`config.local.yaml`）

`config.yaml` 是随 argo 一起发版的**公共配置**。本机状态（某个引擎账户欠费要临时停用、本机路径、本机调优）不该写进去——写了就会随升级推给所有用户。本机覆盖层解决这件事：复制示例文件，只写要改的键。

```bash
cp config.local.yaml.example config.local.yaml
```

```yaml
# 例：本机临时停用某引擎（路由自动改用别的源）
engines:
  parallel:
    enabled: false
    disabled_reason: "本机账户欠费，路由走 parallel_free；充值后删掉这段"
```

- **深度合并**：只写要改的键，不用整份抄引擎声明（`engines.parallel.enabled: false` 只翻一个布尔，其余字段原样保留）。
- **优先级最高**：`config.yaml` → `engines/*.yaml` 外置声明 → `config.local.yaml`。
- **改完立即生效**：进程内缓存与跨进程磁盘缓存都认它。
- **不进公开仓**：`config.local.yaml` 已在 `.gitignore` 里，不会被提交或推送。
- **损坏不影响使用**：文件写坏时静默忽略，退化为只用 `config.yaml`。
- **派生件不含本机状态**：`engine_registry.yaml` / `quota_profiles.json` 等随仓发布的派生文件按公共配置生成，本机跑 `sync_backends` 不会把本地状态写进公开仓。

不想用时删掉或改名即可，`config.yaml` 的行为完全不受影响。

---

## 使用示例

### 金融

```bash
python3 scripts/search.py "贵州茅台股价" --explain
# 典型：命中 stock_query → 行情快照源
```

### 学术

```bash
python3 scripts/search.py "transformer attention mechanism paper" --json
# domain 常为 academic，引擎组合含 arxiv 等
```

### 研究与核验

```bash
python3 scripts/research.py "2026 公募基金二季报 持仓结构" --depth deep --json

python3 scripts/search.py "同一查询" --json | \
  python3 scripts/evidence.py "同一查询" --stdin --json
```

### MCP 工具一览（19）

| 工具 | 用途 |
|------|------|
| `argo_search` | 统一搜索 |
| `argo_local_search` | 本地文件搜索（非联网） |
| `argo_local_read` | 白名单本地文本预览（fail-closed） |
| `argo_recompute` | 受限子进程可复算（默认拒绝，需授权） |
| `argo_research` | 深度研究（含 social-sentiment 模式） |
| `argo_evidence` | 可信度评估 |
| `argo_clarify` | 意图消歧 |
| `argo_fetch` | 智能抓取（`mode=extract` 结构化提取） |
| `argo_crawl` | 站点爬取 |
| `argo_screenshot` | 页面截图 |
| `argo_pdf` | PDF 提取 |
| `argo_social_search` | 多平台社交搜索（`mode=sentiment` 舆情聚合） |

---

## 安装与配置

### 环境要求

| 项目 | 要求 |
|------|------|
| Python | 3.9+（命令行与 MCP 核心） |
| 依赖 | `pip install pyyaml`（仅此一个硬依赖） |
| 可选增强 | `pip install curl_cffi`（TLS 指纹伪造，MIT 许可）。装上后对指纹检测型反爬站（Cloudflare 等）免起浏览器即可抓取；缺失时自动降级，核心功能不受影响 |
| Node.js | **仅**在使用 `npx` 入口时需要 18+ |
| SearXNG | 不需要（内置本地引擎替代） |

### API Key（全部可选）

不配置则跳过对应引擎，免费引擎自动保底。**请用环境变量**，不要把真实 Key 写进仓库或贴到 Issue。

```bash
# 推荐（提升搜索质量）
export TAVILY_API_KEY="你的密钥"
export BOCHA_API_KEY="你的密钥"
export METASO_API_KEY="你的密钥"
export ZHIHU_ACCESS_SECRET="你的密钥"

# 可选
export BRAVE_API_KEY="你的密钥"
export FELO_API_KEY="你的密钥"
export GITHUB_TOKEN="你的密钥"
export WEB_SEARCH_API_KEY="你的密钥"
export ANYSEARCH_API_KEY="你的密钥"
export OCTEN_API_KEY="你的密钥"
export ARGO_KEENABLE_API_KEY="你的密钥"   # 可选；Keenable 免费体验期
```

`config.yaml` 里只写 `{ENV_NAME}` 占位，不会提交明文密钥。

### 缓存

默认 SQLite 路径见 `config.yaml` 的 `cache.db_path`（一般为 `~/.cache/unified-search/cache.db`）。

| 类型 | 大致 TTL |
|------|----------|
| 金融 | 约 5 分钟 |
| 新闻 / 实时 | 约 10–15 分钟 |
| 通用 | 约 1 小时 |
| 研究 / 常青 | 约 2–24 小时 |
| 空结果 | 很短（避免把失败固化） |

### 常见问题

**不配 API Key 能用吗？**  
能。内置大量本地零成本引擎和免费 API 源，不配 Key 时自动走免费路径。

**安装脚本和 npx 有什么区别？**  
安装脚本适合固定装在本机、改配置、挂 Skill；npx 适合快速把 MCP 挂进 Agent。两者底层同一套 Python 代码。

**如何确认引擎是否正常？**  
`python3 scripts/search.py --list-engines`，或加 `--explain`。

**会不会在仓库里复制多份代码？**  
不会。推荐「一份源文件 + 符号链接」。用 `link_source.py`，不要 rsync 多副本。

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

## 设计取舍

1. **先服务 Agent 吸收，再谈链接数量。**
2. **免费与本地优先，付费可选增强。**
3. **失败要可观测**：空结果、超时、熔断分开标，不静默吞掉。
4. **配置驱动扩引擎**，`config.yaml` 为唯一来源。
5. **唯一来源安装**：链接入口、不 rsync 多副本。
6. **不把社交当真理库**；社交内容适合扩维与舆情，不适合单独当真值。

---

## 适用场景

- Claude Code / Grok Build / Codex / Kimi 等 **Agent 的搜索后端**
- **多语言、多领域**日常问答：中英日韩等 + 金融 / 影视 / 体育 / 学术 / 代码
- 脚本与流水线里需要 **可复现、可缓存** 的检索
- 事实核查、金融公开信息、实体与公开资料的 **多源对照**

不太适合单独承担：平台内高级互动定榜、需要长期养服务的最大召回聚合器（已用内嵌本地引擎替代外挂 SearXNG 主路径）。

---

## 目录结构（简）

```
argo/
├── README.md                # 中文介绍（默认）
├── README.en.md             # English
├── README.ja.md             # 日本語
├── README.ko.md             # 한국어
├── README.es.md             # Español
├── SKILL.md
├── package.json             # npx 入口
├── bin/argo.js              # Node 启动 MCP
├── bin/argo                 # Python CLI
├── config.yaml              # 引擎与域配置（来源）
├── assets/readme/           # README 视觉资源
├── backends/
├── mcp/                     # 多客户端 MCP 注入配置（clients.yaml）
├── scripts/                 # search / research / mcp / install …
├── sub-skills/local-search/
├── sub-skills/ego-search/      # 登录态专业搜索（默认关闭，开启命令见正文）
├── tests/
└── docs/                    # 含 ENGINE_CATALOG.md（搜索源使用文档，生成）
```

---

## 最近更新

只列最近三个版本。完整历史见[版本记录](#版本记录)，逐版详情见 [Releases](https://github.com/taxueseek/argo/releases)。

### v2.9.9：安全纵深 + 慢源交付不丢 + 五类误路由收口

- **高风险正文进不了上下文**：内容安全引擎从「只评分」升级为「隔离+清洗替换」，`risk_score ≥ 0.5` 的正文被清洗后正文替换、不进证据分；默认开，`ARGO_CONTENT_RISK_GATE=0` 应急放行
- **网络出口纵深防御**：重定向逐跳校验（`ARGO_MAX_REDIRECTS`，默认 5）+ 解析即锁定，封堵 DNS 重绑定窗口
- **早停弃置的慢源交付不再丢**：弃置引擎转后台收割，`ARGO_TAIL_COMPLETE_S`（默认 3s，0=关）内完成即照常交付并记真实成绩，交付去重合并回缓存
- **五类误路由收口**：A 股财务查询进 `stock_query`、企业尽调/开票/人查公司进 `company_search`、民事实务进 `flk_law`、药品说明书不再被「用法」抢走、论文写作 howto 不再落学术域
- **`argo engine add`**：新引擎一键接入，spec/env/验证/同步/combo/白名单的人工多步链编排成一步
- **场景分桶学习 + ZCode 插件包**：路由能区分「引擎在 A 域强、B 域弱」；skill+CLI 直连与零 token 面板进 zcode-plugins

### v2.9.8：路由精度审计 + 腾讯元宝搜索 + 免 key 三源 + 结构化失败清单

- **issue #18/#19 全修**：`latest` 误判实时新闻、人民网默认 feed、local_bing 只命中「2026」、中文交叉验证恒 0、nightly 空日志崩溃；顺线收窄 16 类同构误触发
- **新增元宝搜索**：中文一手/官方口径源，进中文通用域第 2 位做权威源对冲；key 用 `ARGO_YUANBAO_API_KEY`
- **免 key 可用面扩三源**：`tavily_free`、`keenable_free`、`exa` 匿名回退，不配 key 也能用
- **失败能照着处置**：失败按来源与类别给出「可重试/换源/去配 key」，结果带 `meta.uncertainty` 综合可信度
- **本机配置覆盖层 `config.local.yaml`**：本机调优不再写进公共配置
- **DSH 设置页新增「引擎」分区**：28 个行为开关从环境变量搬进面板

### v2.9.7：架构收口 + 宿主网页抓取交给 argo + License 转 Apache-2.0

- **License 变更**：MIT → Apache License 2.0，专利授权明示，使用方式不变
- **宿主抓不到的网页现在能抓到**：短链跳转、协议升级、跳转壳与 Cloudflare / JS 挑战页交给 argo 的抓取链（HTTP 优先，遇反爬自动升级浏览器）；默认开启，`fetchProviderEnabled` 可关
- **auto 档不会再误调付费引擎**：18 个「只接受显式指定」的引擎从路由候选池剔除，只有 `--engine` 点名才走
- **换新 key 下一跳就恢复**：配额耗尽的引擎不再等恢复窗口到点，路由缓存同步重算
- **crawl / research / MCP 里跑 JS 不再崩**

---

## 版本记录

| 版本 | 说明 |
|------|------|
| **v2.9.9** | **安全纵深 + 慢源交付不丢 + 五类误路由收口**：高风险正文隔离+清洗替换（不进证据分）、逐跳重定向校验与 DNS 重绑定封堵；早停弃置的慢源转后台收割（`ARGO_TAIL_COMPLETE_S`），学习器记真实成绩；六族真实场景扫描收口学术/财务/企业/法律/医药误路由；`argo engine add` 一键接引擎、场景分桶学习、ZCode 插件包。详见 [发布说明](docs/RELEASE_NOTES_v2.9.9.md) |
| **v2.9.8** | **路由精度审计 + issue #18/#19 修复 + 测试态隔离**：回放真实查询收窄 16 类误触发（品牌名/产品名/字段名/ASCII 子串），SERP 覆盖率门与 CJK 交叉验证落地；nightly 空日志、people_daily 默认 feed、缓存命中守卫一并收口；全量测试按用例隔离 adaptive/熔断状态。感谢 @wpp666 的复现与定位。详见 [发布说明](docs/RELEASE_NOTES_v2.9.8.md) |
| **v2.9.7** | **架构收口 + DSH web_fetch 占位 + License 转 Apache-2.0**：宿主 web_fetch 改走 argo 抓取链，JS 挑战页与跨源跳转从失败变成功；18 个 explicit_only 引擎从路由候选池剔除，堵住 auto 档误调付费引擎扣费；熔断、记账、配额恢复与路由缓存一批状态契约收口。 |
| **v2.9.6** | **DeepSeek Search 接入 + 查询操作符契约 + 传输层去重付费 + 路由快照门**：新引擎 `deepseek_search` 默认启用（445-510ms，权威分级 / rerank_score / 时间窗唯一全覆盖）；查询操作符收敛为契约层；POST 连接池复用省 320-561ms/次；路由决策变成可 diff 的 golden。 |
| **v2.9.5** | **汇率查询专域 + 去重双轨 + 排序误杀修复 + daemon 多检出隔离**：新增 `fx_query` 域，汇率与中间价直接出即期与快讯，不再落进百科词条；去重补站后缀归一与模糊复核；daemon 落盘名字按检出目录隔离，多 worktree 不再互抢。 |
| **v2.9.4** | **显式引擎域归因 + 恢复链止损 + 门禁同步**：`--engine X` 补域判定，修复缓存 TTL 一刀切 general；显式单引擎无备选时不再空跑恢复链；`org_entity` / `fund_query` 域规则补限定词。 |
| **v2.9.3** | **X 帖子完整打包 + 浏览器登录态车道 + 本地链质速双修**：新增 `argo tweet`（正文 / 串 / 引用 / 转发与互动数据一次打包）与 `argo auth`（站点持久登录态，cookie 只存 Chrome 加密存储）；本地中文链跳转壳无会话解析，保留结果 1→5 条；共识宽限自适应提速。 |
| **v2.9.2** | **DSH 0.2.1-alpha.1 适配验证 + 路由/守卫一批修复 + js-run 子技能**：DSH 三形态在新版实测启用，`argo mcp inject` 覆盖到第 7 宿主；TF-IDF 弱证据辅源化、死源与熔断槽位回填、配额耗尽引擎隔离；新增 js-run 子技能，不开浏览器跑网页 JS。 |
| **v2.9.1** | **内容质量优化 + 对抗内容农场 + 适配最新版本**：提升低质批量生成内容的识别与过滤；本地搜索新增自身安装树守卫，避免结果被自身源码污染。 |
| **v2.9.0** | **搜索源优化 + 插件与 CLI 持平 + 多语言小语种**：搜索源 237→253（免配置 198→219），新增 14 个小语种源与 CORE 全文源，百度与搜狗修活；导入链 −79%，重排序 33ms→1.5ms；新增 `argo stats` 本地使用日志。详见 [发布说明](docs/RELEASE_NOTES_v2.9.0.md) |
| **v2.8.9** | **输出减重 + 检索更全更快 + 5 个外媒与核查源**：默认输出 15.5KB→5.2KB（−66%，同一份结果不再写三遍）；召回 +35%、抓取时延 −30%、正文结构保真 0/6→6/6；新增卫报 / France 24 / 德国之声 / FactCheck.org / Full Fact 五个免密钥源，gdelt 死源下线。详见 [发布说明](docs/RELEASE_NOTES_v2.8.9.md) |
| **v2.8.8** | **真实用户报障修复（#12 密钥别名静默失效、#13 抓取链不走代理）+ 全面提速 + 搜索源 218 → 235**：出口调度统一走 `ARGO_PROXY` / 按域规则并尊重 `NO_PROXY`；命令冷启动 2.1s→0.55s；新增 `argo answer` 直答与 `argo watch` 网页观察，14 个免密钥源。详见 [发布说明](docs/RELEASE_NOTES_v2.8.8.md) |
| **v2.8.7** | **218 源 / 89 域 + 取信技巧三通道 + 路由触发纪律 + macro 零结果修复**：批次七 / 八 / 九累计 50 个新源；抓取链新增 llms.txt 与 `.md` 直出探测；`--engine` 逗号多引擎修复，中国宏观查询国家统计局前置。详见 [发布说明](docs/RELEASE_NOTES_v2.8.7.md) |
| **v2.8.6** | **hedged 竞速 + 知乎三源分工 + 声明式语言分发 + 可达性门 + 学术检索协议**：首引擎宽限窗竞速，快引擎只付 1 次调用；zhihu 全网搜接入并防饿死；子查询语言与学术分发；死源显形检查。详见 [发布说明](docs/RELEASE_NOTES_v2.8.6.md) |
| **v2.8.5** | **DSH 插件工具原生化 + MCP 默认关闭 + Windows 兼容 + 配额自愈 + 抓取 deadline**：`argo_search` / `argo_fetch` 以原生一等工具注册，默认可用；Windows 全平台兼容；配额耗尽自动识别、切备用源、周期结束自愈，无需人工改配置。详见 [发布说明](docs/RELEASE_NOTES_v2.8.5.md) |
| **v2.8.4** | **本地数据融合 + 多客户端 MCP 接入 + 结构化搜索增强 + Keenable**：深度研究可吃本地一手数据（`file_inputs` + `recompute` 可复算执行器）；`argo mcp inject` 一条命令注入 6 个客户端；查询归一化与复杂度门控。详见 [发布说明](docs/RELEASE_NOTES_v2.8.4.md) |
| **v2.8.3** | **多语言路由修复 + anysearch 进程化 + weighted RRF**：anysearch 从子进程改为进程内调用，省 Python 启动开销；weighted RRF 新增动态可靠性因子，弱源降权；ja/ko 与欧语言查询返回目标语言。详见 [发布说明](docs/RELEASE_NOTES_v2.8.3.md) |
| **v2.8.2** | **Windows 全平台 + 证据语义统一**：全链路 UTF-8 防线根治 GBK 崩溃，Chrome / Edge 自动发现；npm 包补 `dsh.bundle` 声明，一行命令即得 MCP 工具；`wide_research` 新增质量门与递归硬保护。详见 [发布说明](docs/RELEASE_NOTES_v2.8.2.md) |
| **v2.8.0** | **证据完整链路 + 求职 v3 + 天气双源**：搜索输出自带证据门控，`--verify` 一键核验并记住已核实链接；`argo job` 求职搜索 v3（结构化字段 + 增量监控）；天气双源并行。详见 [发布说明](docs/RELEASE_NOTES_v2.8.0.md) |
| **v2.7.3** | **本轮修复 + 引擎激活**：引擎层接入 HTTP 客户端（UA 轮换 / 重试 / 重定向跟随，arxiv 从 5s 超时变 2s 内 10 条）；TF-IDF 强语义注入激活 25 个此前永远选不中的垂直引擎；70 域缓存 TTL 全覆盖；国际引擎中文查询 URL 编码修复。详见 [发布说明](docs/RELEASE_NOTES_v2.7.3.md) |
| **v2.7.2** | **登录态专业搜索**：新增 ego-search 子技能（默认关闭，开启方法与依赖见上方「登录态专业搜索」节）；搜索保底与多意图路由；日韩文查询不再混入中文引擎。详见 [发布说明](docs/RELEASE_NOTES_v2.7.2.md) |
| **v2.7.1** | **安全加固 + 路由修复**：SSRF 防护（URL 白名单 + IP 段检查）；路由健康状态语义漂移根因修复，只对 `local_*` 做健康判定；深度研究 local_first 浪费修复。详见 [发布说明](docs/RELEASE_NOTES_v2.7.1.md) |
| **v2.7.0** | **垂直结构化模态卡**：内建 `bocha` / `bocha_ai` 原生引擎，`modal_card` 域统一识别火车票 / 油价 / 贵金属 / 万年历 / 星座等实时卡片；修复 `bocha` web 解析缺陷。详见 [发布说明](docs/RELEASE_NOTES_v2.7.0.md) |
| **v2.6.2** | 合并独立改进线：网络环境感知 / 加权 RRF 与语义缓存 / 自适应引擎禁用 / 内容安全与查询变体 / 三大垂直引擎；含 v2.6.1 路由修复。详见 [发布说明](docs/RELEASE_NOTES_v2.6.2.md) |
| **v2.6.1** | v2.6.0 修复版：路由误伤修复（`capital of` 不再抢 fact_check）；版本同步。详见 [发布说明](docs/RELEASE_NOTES_v2.6.1.md) |
| **v2.6.0** | **多语言搜索**（检测 / 引擎参数 / 跨语言回退）；影视·体育·地理·组织·媒体等垂直补全；约 120+ 源。详见 [发布说明](docs/RELEASE_NOTES_v2.6.0.md) |
| **v2.5.1** | 金融/宏观/化学等垂直答案源加厚；引擎分层 + combo 预算；[v2.5.1 说明](docs/RELEASE_NOTES_v2.5.1.md) |
| **v2.5.0** | 安装脚本 + npx；查询改写与路由拆开；路由热路径缓存；MCP 紧凑响应 |
| **v2.4.0** | 路由低分回退与社交误吸过滤；缓存 depth / 柔性命中；熔断与负缓存；`engine_outcomes` |
| **v2.2–v2.3** | 证据两阶段、中文信源表、content_signals、fetch 栈、引擎扩充 |
| **v2.1** | 社交引擎层（多平台 UGC） |
| **v1.x** | 统一命名为 Argo，多引擎路由与双层缓存成型 |

更细说明见 `docs/RELEASE_NOTES_v2.7.0.md` 与 `docs/OPTIMIZATION_ROADMAP_v2.4.md`。

---

## 贡献

欢迎提 Issue 与 Pull Request。改路由或证据逻辑时，请尽量补对应测试：

```bash
python3 -m pytest tests/test_unit.py tests/test_multilingual.py -q
python3 scripts/regression_p0p1.py --offline
python3 scripts/matrix_search_eval.py --offline
python3 scripts/ab_eval_p0p1.py   # 可选，含在线实测
```

提交前请确认：不含真实 API Key、本机绝对路径、账号 cookie 等敏感信息。本机 Skill 路径请写在 `installs.local.yaml`（已忽略）。

## License

Apache License 2.0 © 2026 [taxueseek](https://github.com/taxueseek)

<p align="center">
  <a href="https://github.com/oil-oil/beautify-github-readme"><img src="assets/readme/made-with-beautify.svg" width="300" alt="README made with beautify-github-readme"></a>
</p>

---

> 好的搜索不是让你看得更多，是让你更敢下结论——以及知道什么时候还不该下结论。

---

## 生态联动：与 Invest 系列投资技能集成

本引擎的财经垂直源（`eastmoney`、`nbs_stats`、`jin10`、`nbs`、`fred`、`eurostat`、`finviz` 等）已接入 [invest 系列投资技能](https://github.com/taxueseek/taxue-invest)。

invest-cli 通过 `info`（`invest-cli info <词> --engine eastmoney`）与 `intent macro` 直接复用 argo 的财经 / 宏观 / 资讯取数，用于环节配额、低成本、多源数据，与 invest-cli 的结构化数据层（Wind / 盈米 / 东财 / yfinance / 天天基金）协同。

> 最新版已完成 argo 数据与 invest 系列技能（v2.0.5）的集成。

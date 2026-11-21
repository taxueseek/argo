<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="Argo 阿尔戈：面向 AI Agent 的多引擎统一搜索与证据核验">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <strong>中文</strong> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.es.md">Español</a>
</p>

<p align="center">
  <a href="#快速上手零配置可用">快速上手</a> ·
  <a href="#为什么选择-argo">对比评测</a> ·
  <a href="#按查询形态自动路由">路由实证</a> ·
  <a href="#工作原理与证据核验">工作原理</a> ·
  <a href="#ai-搜索与子技能矩阵">AI 搜索与子技能</a> ·
  <a href="#dsh-插件">DSH 插件</a> ·
  <a href="#文档与更新记录">文档</a>
</p>

<p align="center">
  <a href="https://github.com/taxueseek/argo/stargazers"><img alt="stars" src="https://img.shields.io/github/stars/taxueseek/argo?style=social"></a>
  <a href="https://www.npmjs.com/package/argo-search"><img alt="npm downloads" src="https://img.shields.io/npm/dm/argo-search?color=cb3837&label=npm%20downloads"></a>
  <a href="https://skills.sh/taxueseek/argo"><img alt="skills.sh" src="https://img.shields.io/badge/skills.sh-argo-black"></a>
  <img alt="license" src="https://img.shields.io/badge/license-Apache_2.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.9+-green">
  <img alt="version" src="https://img.shields.io/badge/version-2.17.0-informational">
  <img alt="engines" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Ftaxueseek%2Fargo%2Fmain%2Fassets%2Freadme%2Fcounts.json">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-19%20tools-purple">
</p>

> **属于 taxueseek Skill 系列**（负责搜索与深度研究）：[plainspoken-writing](https://github.com/taxueseek/plainspoken-writing) 77★（专业消除 AI 味）· [session-digger](https://github.com/taxueseek/session-digger) 34★（跨环境会话历史挖掘）· [taxue-invest](https://github.com/taxueseek/taxue-invest) 30★（投资分析方法论）· [taxueskills](https://github.com/taxueseek/taxueskills) 25★（踏雪决策系统）· [taxue-creative-style](https://github.com/taxueseek/taxue-creative-style) 19★（创意插画风格库）· [ime-lexicon](https://github.com/taxueseek/ime-lexicon) 16★（豆包/微信输入法词库工具）。
>
> **同作者 DeepSeek Harness（DSH）插件生态**：[plugin-fast-adapt](https://github.com/taxueseek/plugin-fast-adapt) 48★（插件升级自适应）· [dsh-files](https://github.com/taxueseek/dsh-files) 44★（文件夹上传与文档解析）· [taxue-dsh-artisan](https://github.com/taxueseek/taxue-dsh-artisan) 3★（一体化视觉创作）· [dsh-plugin-guard](https://github.com/taxueseek/dsh-plugin-guard) 2★（插件安装前安全体检）。

**Argo（阿尔戈）** 是专为 AI Agent 打造的开源多引擎搜索、网页抓取与证据核验工具。它把「**免费与本地引擎兜底 + 主流 AI 搜索聚合 + 公众号/知乎/小红书垂直穿透 + 原文抓取与证据核验**」融进同一套 CLI / MCP / Skill 接口，覆盖 18 种语言与近百个业务域。

---

## 快速上手（零配置可用）

- **Claude Code（MCP）**：
  ```bash
  claude mcp add argo -- npx -y argo-search
  ```
- **Agent Skill（`skills.sh` — Claude Code / Cursor / Codex / Antigravity）**：
  ```bash
  npx skills add taxueseek/argo
  # 或一次安装全部 3 个独立 Skill（argo / argo-wide-research / local-seek）：
  npx skills add taxueseek/argo --full-depth --all
  ```
- **Cursor / Windsurf / LibreChat（`mcpServers` JSON）**：
  ```json
  {
    "mcpServers": {
      "argo": { "command": "npx", "args": ["-y", "argo-search"] }
    }
  }
  ```
- **本地 CLI（长期使用推荐）**：
  ```bash
  curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
  # 或 npm install -g argo-search
  ```
- **DeepSeek Harness（DSH）插件**：
  ```bash
  dsh plugin --profile web add argo-search
  ```

<details>
<summary>安装自检与可选 API Key 说明</summary>

```bash
argo search "贵州茅台最新财报核心指标" --verify 3 --json --fields agent
argo search --list-engines
```

- 运行要求：**Python 3.9+**（`pip install pyyaml`）与 **Node.js 18+**（使用 `npx` 时）。
- 所有 API Key 均为可选：有 Key 自动提质（如 `DEEPSEEK_API_KEY`、`ARGO_YUANBAO_API_KEY`、`ARGO_TAVILY_API_KEY`、`EXA_API_KEY`、`ARGO_METASO_API_KEY`、`ARGO_BOCHA_API_KEY`），无 Key 自动跳过并回落免密钥源与本地引擎。详见 [docs/MCP_SETUP.md](docs/MCP_SETUP.md) 与 [docs/ENGINE_CATALOG.md](docs/ENGINE_CATALOG.md)。

</details>

---

## 为什么选择 Argo

<p align="center">
  <img src="assets/readme/why-better.svg" width="100%" alt="Argo 与内置搜索、商业搜索 API、通用元搜索的对比">
</p>

| 对比维度 | 宿主内置搜索 | 单一商业 API（Tavily / Exa / Serper） | Argo |
| :--- | :--- | :--- | :--- |
| **费用与门槛** | 绑定宿主配额 | 需外币卡按次付费、强依赖 Key | **免密钥可用**，本地与免费源自动兜底，有 Key 自动增强 |
| **AI 搜索聚合** | 单一内置后端 | 仅自家单一索引 | **同时聚合免 Key AI 搜索（`tavily_free`、`parallel_free`、`anysearch`）与主流可选 Key AI 搜索（DeepSeek 官方搜索、腾讯元宝/混元、秘塔、博查、Tavily、Exa）** |
| **中文与垂直穿透** | 泛网页居多，SEO 噪音重 | 几乎无法穿透中文生态 | **原生覆盖公众号、知乎、小红书（`ego-search`）、A 股行情、学术论文与本地知识库** |
| **事实与数值核验** | 仅看摘要片段 | 摘要或原始切片 | **内置 `--verify` 与 `--numverify` 抓回正文并交叉印证数值，给出证据分** |
| **上下文体积** | 冗长 HTML/JSON | 原始文本块 | **`--fields agent` 极简输出（~4.3 KB）**，不撑爆 Agent 上下文 |

---

## 按查询形态自动路由

<p align="center">
  <img src="assets/readme/proof-routes.svg" width="100%" alt="Argo 按域与语言自动路由实证">
</p>

Argo 会在派发前识别查询的**域与语种**，直击一手垂直源并在达成共识后触发 **early-stop（早停）**：

- **AI 搜索聚合**：免 Key（`tavily_free`、`parallel_free`、`anysearch`）+ 可选 Key（**DeepSeek 官方搜索 `deepseek_search`**、**腾讯元宝/混元搜索 `yuanbao`**、**秘塔 `metaso`**、**博查 `bocha`/`bocha_ai`**、**Tavily**、**Exa**、**You.com**）
- **金融与宏观**：`sec_edgar`、`yahoo_finance`、`sina_quote`、`eastmoney`
- **学术与论文**：`arxiv`、`semantic_scholar`、`openalex`、`crossref`、`pubmed`（支持 `--academic-deep`）
- **代码与技术**：`stackoverflow`、`github`、`octen`、`context7`
- **深度研究取证包**：`python3 scripts/research.py "复杂问题" --depth deep --json` 产出带来源账本、覆盖缺口与质量门控的 **dossier（取证包）**

---

## 工作原理与证据核验

<p align="center">
  <img src="assets/readme/workflow.svg" width="100%" alt="Argo 检索、RRF 融合、抓取降级链与证据评分流水线">
</p>

### 证据评分（选拔 × 吸收两维）

搜索引擎按点击率排序，但 Agent 需要**可核验的事实**：
- **选拔分（Selection）**：域权威度、多引擎共识（`RRF`）、时效性，并主动拦截 `baidu/s`、`sogou/link` 等 SERP 跳转空壳。
- **吸收分（Absorption）**：通过 `--verify N` 抓回正文后计算真实可读信息密度，高后果财务/价格/指标支持 `--numverify N` 抓原文做数值交叉印证。

### 四档预算模式（`--mode`）

| 模式 | 策略说明 | 适用场景 |
| :--- | :--- | :--- |
| **`fast`** | 仅用免费源与本地 `local_*` 引擎，零 API 消耗 | 高频日常查询 |
| **`auto`** | 成本感知默认档：免费优先，必要时再升级付费源 | 日常编码与常规调研 |
| **`deep`** | 多源并行展开、禁用早停，配合 `--verify` 深抓正文 | 金融、医疗、法律、深度研究取证 |
| **`budget`** | 严格遵守日/月配额上限，超限自动回落本地源 | 定时任务与无人值守批处理 |

---

## AI 搜索与子技能矩阵

### Skill 入口与内部模块

Argo 对外只暴露 **3 个正交 Skill**（`argo`、`argo-wide-research`、`local-seek`），其余能力收归 `sub-skills/` 内部管线或子命令：

| 模块 | 定位 | 核心能力 |
| :--- | :--- | :--- |
| **argo-wide-research** | 独立 Skill | 多轨并行深度研究与取证编排（引用来源账本与质量门） |
| **local-seek** | 独立 Skill | 本地笔记、Markdown 与代码的三层高效检索（`argo seek`） |
| **local-search** | 内部管线 | 30 个零成本本地聚合引擎（`--local-first`） |
| **ego-search** | 主技能子命令 | 基于真实浏览器的登录态垂直搜索（`argo ego-search`，默认关闭） |
| **js-run** | 内部管线 | 无浏览器毫秒级沙箱跑网页 JS（环境探测与纯计算页面） |

### 19 个 MCP 工具

`argo_search`、`argo_local_search`、`argo_local_read`、`argo_recompute`、`argo_research`、`argo_evidence`、`argo_clarify`、`argo_fetch`、`argo_crawl`、`argo_screenshot`、`argo_pdf`、`argo_social_search`、`argo_article`、`argo_job`、`argo_extract`、`argo_preflight`、`argo_answer`、`argo_watch`、`argo_cite`。

---

## DSH 插件

Argo 提供完整的 **DeepSeek Harness（DSH）** 插件（`@taxueseek/argo-dsh`），同一套能力支持三条调用通道：
1. **19 个 `mcp__argo__*` 工具**（主包全量；MCP 默认暴露 4 个核心工具，设 `ARGO_MCP_TOOLS=all` 开启全部 19 个）。
2. **搜索工具 + `wide_research` 并行研究编排**（子包）。
3. **接管宿主 `web_search` / `web_fetch` 接缝**（在 web profile 中需显式指定 provider pin，避免 `WEB_PROVIDER_AMBIGUOUS`，详见 [packages/dsh-plugin/README.md](packages/dsh-plugin/README.md)）。

内置 **Argo 搜索工作台** 提供浏览器端五大模式（全网搜索、热点分析、深度研究、本机检索、定时搜索）、首页自适应推荐与「左文右证」核验视图；图形设置中心覆盖 **18 个原生一级工具开关**、**35 个行为开关**，并与宿主原生共享 `DEEPSEEK_API_KEY` 凭据库（永不明文回显）：

<table>
  <tr>
    <td align="center" width="50%">
      <img src="assets/readme/workbench-home.webp" width="100%" alt="Argo 搜索工作台首页：五大模式、自适应推荐与特定主题卡片"><br>
      <sub><b>Argo 工作台 — 首页</b>：五大搜索模式、自适应推荐与特定主题卡片</sub>
    </td>
    <td align="center" width="50%">
      <img src="assets/readme/workbench-evidence.webp" width="100%" alt="Argo 搜索工作台结果页：左文右证双栏与 local-seek 行号级出处核验"><br>
      <sub><b>Argo 工作台 — 核验视图</b>：结构化综述 + 左侧引用编号与右侧原文出处面板</sub>
    </td>
  </tr>
</table>

<table>
  <tr>
    <td align="center" width="33%">
      <img src="assets/readme/settings-tools.webp" width="100%" alt="DSH 工具开关面板"><br>
      <sub><b>设置中心 — 工具</b>（18 个原生工具）</sub>
    </td>
    <td align="center" width="33%">
      <img src="assets/readme/settings-engines.webp" width="100%" alt="DSH 引擎开关面板"><br>
      <sub><b>设置中心 — 引擎</b>（35 个开关与配额）</sub>
    </td>
    <td align="center" width="33%">
      <img src="assets/readme/settings-keys.webp" width="100%" alt="DSH 密钥管理面板"><br>
      <sub><b>设置中心 — 密钥</b>（共享宿主凭据库）</sub>
    </td>
  </tr>
</table>

---

## 文档与更新记录

- [docs/ENGINE_CATALOG.md](docs/ENGINE_CATALOG.md) · [docs/MCP_SETUP.md](docs/MCP_SETUP.md) · [docs/为什么选择argo.md](docs/为什么选择argo.md) · [docs/ARGO_INTRO.md](docs/ARGO_INTRO.md) · [docs/CHANGELOG.md](docs/CHANGELOG.md)

| 版本 | 核心更新 |
| :--- | :--- |
| **v2.17.0** | 查询操作符原生下推、登录态四态防抖与 `ego-search` 跨进程节奏锁、确定性证据锚定、工作台每日热点与 TCP 回环围栏（313 个源） |
| **v2.16.0** | DSH 插件 Argo 搜索工作台、直调 CLI 四阶段查证管线、`linux.do` 与 Keenable 免费通道（312 个源）、Jina 无 Key 兜底、并发与缓存状态机收敛 |
| **v2.15.0** | 知乎全链、证据层权威透传、无 key 语义兜底、MCP 通用连接器（`type: mcp`）、路由零基域评分、311 个源 |
| **v2.14.1** | 信源架构精简、MCP 3+1 收敛与向后兼容别名、304 个源、`secret_guard.py` 故障安全脱敏 |
| **v2.14.0** | 解析层收敛、300 个源、坏例账本闭环 |
| **v2.13.0** | 文档收口、22 台状态机单一真源、派发尾宽宽限 |

<details>
<summary>v2.17.0 版本摘要与当前能力</summary>

### 当前大致能力（v2.17.0）

v2.17.0 将检索、登录态与工作台演进收口为硬机制约束：落地查询操作符原生下推（含 Tavily 原生 `language`）、浏览器四态登录防抖与 CDP 捕获、`ego-search` 跨进程 OS 锁节奏控制与主仓单真源登录态词表、确定性数字证据锚定与隔离子进程新源探测（`argo engine add`）；DSH 工作台新增每日实时热榜与零 LLM 兴趣重排、TCP 回环安全围栏并全面对齐 DSH `v0.2.2-rc.5`；打通熔断指纹自愈、纳入 `helve` 付费源（源 312→313）并实现对外证据图数据驱动生成。

### v2.17.0：查询原生下推 + 登录态与证据锚定 + 工作台每日热榜与门禁收口

详见 [docs/RELEASE_NOTES_v2.17.0.md](docs/RELEASE_NOTES_v2.17.0.md) 与 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

### v2.16.0：DSH 搜索工作台 + 直调四阶段管线 + 信源与并发收敛

详见 [docs/RELEASE_NOTES_v2.16.0.md](docs/RELEASE_NOTES_v2.16.0.md) 与 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

### v2.15.0：知乎全链 + 证据层权威透传 + MCP 连接器

详见 [docs/RELEASE_NOTES_v2.15.0.md](docs/RELEASE_NOTES_v2.15.0.md) 与 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

### v2.14.1：信源架构精简 + MCP 3+1 收敛 + 安全脱敏

详见 [docs/RELEASE_NOTES_v2.14.1.md](docs/RELEASE_NOTES_v2.14.1.md) 与 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

</details>

---

> 好的搜索不仅仅让你看得更多更广，能够消除信息差，找出你最关心的地方。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)"
      srcset="https://api.star-history.com/svg?repos=taxueseek/argo&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)"
      srcset="https://api.star-history.com/svg?repos=taxueseek/argo&type=Date" />
    <img alt="Star History Chart" width="560"
      src="https://api.star-history.com/svg?repos=taxueseek/argo&type=Date" />
  </picture>
</p>

如果 Argo 帮你下过一次更踏实的结论，[点个 Star](https://github.com/taxueseek/argo/stargazers) 让更多人看到它。

## 参与贡献与开源协议

欢迎提交 Issue 与 PR（新增搜索源请见 [docs/ADDING_NEW_ENGINE.md](docs/ADDING_NEW_ENGINE.md)）。项目采用 [Apache-2.0](LICENSE) 协议开源。

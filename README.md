<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="Argo 阿尔戈：给 Agent 用的搜索，argo 的定位是不依赖任何订阅/账号体系的自主搜索基础设施，能够超越大部分 agent 的原生搜索能力">
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
  <a href="#它和模型自带搜索-ai-搜索-聚合搜索比强在哪">对比</a> ·
  <a href="#问啥像啥">证明</a> ·
  <a href="#它怎么工作">机制</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#能做什么">能力</a> ·
  <a href="#安装与配置">配置</a> ·
  <a href="#版本记录">更新</a>
</p>

<p align="center">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.10+-green">
  <img alt="version" src="https://img.shields.io/badge/version-2.8.4-informational">
  <img alt="engines" src="https://img.shields.io/badge/engines-150+-orange">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-12%20tools-purple">
</p>

> **这是踏雪寻仙 DeepSeek Harness 插件系列的一员**，作者还有其他的优秀插件：[dsh-files](https://github.com/taxueseek/dsh-files)（传文件读文档） · [dsh-snippets](https://github.com/taxueseek/dsh-snippets)（片段收藏夹） · [dsh-healthcheck](https://github.com/taxueseek/dsh-healthcheck)（只读体检） · [dsh-plugin-guard](https://github.com/taxueseek/dsh-plugin-guard)（插件安全审计） · [taxue-dsh-artisan](https://github.com/taxueseek/taxue-dsh-artisan)（提示词反推与多供应商生图）—— 完整插件栏目见[个人主页](https://github.com/taxueseek#deepseek-harness-%E6%8F%92%E4%BB%B6)

---

## v2.8.4 更新亮点（在 v2.8.3 基础上再进一步）

> 一句话：这版把「帮你搜到」再往前推进——**本地一手数据能进研究**、**给各种 Agent 挂 MCP 一条命令搞定**、**简单问题不再被多轮拖慢**、**多接了一个免费搜索源**，顺手加固了几处安全细节。

- **深度研究能吃你本地的数据了**：以前 `argo research` 只吃网页，现在工作包能带 `file_inputs`（你的一手 CSV / XLSX / 文献，登记哈希、内容不入账）+ `recompute`（可复算执行器，安全地重算数字并和检索对账，对不上就提示）
- **挂 MCP 不再手改配置**：`argo mcp inject` 一条命令把 argo 注入 Claude Code / Cursor / Windsurf / Codex / OpenCode / Cline（原子写 + 自动备份 + 可还原）
- **简单问题不被拖成多轮**：查询归一化（全角→半角、版本号拆斜杠）+ 复杂度门控（低复杂度查询不放行高价多源）+ 社交/平台检索语法优先 + 丢了一个中文引擎会继续看备选
- **多接一个免费搜索源 Keenable**（免费体验期、量较大，到期出问题直接停用即可）
- **安全加固**：recompute 封死外部进程出网、主机路径全部改为安装感知（不再写死 `~/.agents` 这类）、修了一处会把用户引向陈旧 npm 包的安装提示

> 完整变更见文末 [版本记录](#版本记录) 与 [发布说明](docs/RELEASE_NOTES_v2.8.4.md)。

---

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

## 这是什么

**Argo 是给 AI Agent 用的多语言搜索基础设施。**

真实检索从来不是「一种语言 + 一个搜索框」：有人问 A 股行情，有人问 World Cup，有人用日文找动画，有人要 IMDb 上的导演信息。Argo 的出发点很朴素——**按领域、按语言、按需求选路**，把问题送到合适的源，而不是一律扫网页标题。联网搜索与本机文件搜索一体可用。

> 产出不是「链接清单」，而是「证据候选 + 可信度分解」。路选对了，证据才站得住。

### 和「再包一层搜索 API」的差别

| 常见做法 | Argo |
|---------|------|
| 绑死一个引擎、一个 Key | 多引擎自动选路，免费优先、可配预算 |
| 啥问题都泛搜网页 | **垂直源优先**：行情、影视、体育、宏观、化学等先给答案型结果 |
| 默认只按中英优化 | **多语言识别 + 引擎语言参数 + 跨语言回退** |
| 搜完直接拼摘要 | 选择门槛 × 证据密度 × 时效 × 多源共识 |
| 引擎挂了整条链路挂 | 熔断、负缓存、分阶恢复（防垂直源串味） |
| 每次查询都重新打网 | 双层缓存（内存 + SQLite），热查询约 10ms 级 |
| 日常和研究一个慢 | **日常少开引擎、研究再放宽** |
| Agent 上下文被长 JSON 撑爆 | MCP 响应可紧凑裁剪，snippet 可控 |

---

## 问啥像啥

<p align="center">
  <img src="assets/readme/proof-routes.svg" width="100%" alt="四类真实路由：金融、影视、多语言、地理">
</p>

| 你这样问 | 大致会怎样 |
|----------|------------|
| 贵州茅台股价 | A 股行情域，优先快照源，够用就停 |
| AAPL / 美股盘前 | 美股域，与 A 股分流 |
| 肖申克的救赎 主演 / Inception director | 影视域 → IMDb 等 |
| 梅西 俱乐部 / 库里 球队 | 体育域 → TheSportsDB 等 |
| 埃菲尔铁塔在哪 / where is Eiffel Tower | 地理实体 → OpenStreetMap 等 |
| NASA founding year / 国务院职能 | 组织实体 → Wikidata 等 |
| 周杰伦 专辑 / Taylor Swift album | 媒体域 → iTunes 等 |
| アニメ おすすめ / 한국 영화 추천 | 识别日/韩语 → 语言友好源，少塞中文专用站 |
| 美国 CPI、中国 GDP | 宏观数据域；国别分流 |
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
  └─ 统一 JSON（含 engine_outcomes / recovery）
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
2. **数字**：写清口径，冲突时并列，不要硬合并  
3. **搜索结果页 / 跳转链**：不要当正文信源  
4. **社交帖**：当舆情与叙事，不当事实真值  
5. **事实核查**：宁可多一两条分层查询（来源 / 对比 / 主体）

---

## 快速开始

任选一种即可。**以 GitHub 为唯一安装真源**（`npx github:taxueseek/argo` 或 `install.sh`），当前推荐 **v2.8.4**。**请勿用 `npm install argo-search`**——npm registry 上那份是**非官方陈旧版 v1.0.1**（非本仓库维护，功能残缺、不随本项目更新）。本包 `package.json` 已设 `private: true` 防止误发布到 npm registry。

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
python3 ~/.local/share/argo/scripts/search.py "贵州茅台股价" --json
python3 ~/.local/share/argo/scripts/search.py --list-engines
```

### 方式二：MCP 不装包，直接用 GitHub（推荐 Agent 快速挂载）

需要 **Node.js 18+** 和 **Python 3.10+**，首次执行一次：

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
# 装法 A：12 个 mcp__argo__* 工具（主包自带 bundle，与 MCP 全量相同）
dsh plugin --profile web add "github:taxueseek/argo"

# 装法 B：搜索工具 + wide_research 并行研究编排（子包）
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

重启 `dsh web` 后生效。包结构见 `packages/dsh-plugin/`；同 id `mcp-argo` / `wide-research` 可在用户层 `cordis.patch.yml` 覆盖。装法 B 的 `wide_research` 模块只存在于子包依赖树，主包 patch 不引用它，避免只装主包时 Cordis 解析失败。

### 依赖清单（通俗版）

| 依赖 | 必需？ | 干什么用 | 不装会怎样 |
|------|:------:|---------|-----------|
| **PyYAML** | ✅ 必需 | 读配置文件 | 完全跑不起来，安装脚本会自动装 |
| **curl_cffi** | ❌ 可选（v2.7.3 新增） | 模拟浏览器 TLS 指纹，过反爬站（Cloudflare 等） | 反爬站抓取成功率低一些，日常搜索无影响 |
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
python3 --version          # 需要 3.10+
python3 -c "import yaml; print('PyYAML OK')"
python3 -m pytest tests/test_unit.py -q   # 可选
python3 scripts/search.py --list-engines
```

---

## 能做什么

### 五种能力，通俗说

**1. 通用搜索 + 垂直搜索，双管齐下**

日常问题走通用网页搜索；一问到行情、影视、体育、宏观这类「有标准答案」的问题，自动切到垂直源直接给答案，而不是扔给你一堆链接。目前约 150+ 个源、70+ 业务域，金融 / 宏观 / 影视 / 体育 / 地理 / 组织 / 媒体 / 化学 / 学术 / 代码等都有专门的路。

**2. 缓存：不重复花冤枉钱**

时效性没那么强的内容（百科类、历史数据这类），第一次查完会进缓存，之后同样的查询直接命中，不再每次都走一遍 API。双层缓存（内存 + SQLite），热查询约 10ms 级返回，既省钱也省时间。

**3. 专为 Agent 设计，更省 Token**

产出是「证据候选 + 可信度分解」的精简 JSON，不是长篇网页；MCP 响应可以按需裁剪，snippet 可控，不会撑爆 Agent 的上下文。比常规模型自带的搜索能力更专业、更省 Token。

**4. 深度研究**

把一个笼统的问题拆成多个子问题，多源并行采集，最后给出「还差什么证据」的缺口提示。适合综述、调研这类要全面、要扎实的场景。

**5. 登录态专业搜索（专业模式，默认关闭）**

知乎、小红书、公众号这类要登录才能看的内容，以及 JS 渲染页、反爬页，用真实浏览器配合登录态去搜。默认关闭，需要时开启，依赖 ego lite 和 WebBridge 两个东西，详见下节「登录态专业搜索」。

**6. 时间能力（时间窗 + 方向排序）**

要搜「最近几天刚发布的内容」，用 `--since 7d` 这类时间窗框住发布时间范围，判断新旧不再靠猜。支持引擎（如 `realtime_index`，免 Key 实时索引源，结果自带发布时间）把窗口下推给数据源；其余引擎的融合结果会按 `published_at` 兜底剔除明确超窗的条目（宽松策略，无时间字段的保留），返回包带 `time_filtered` 统计，CLI 与 MCP 均支持。`--since 7d` 与「7 天前的绝对日期」等价、共享缓存；`--until 2026-08-01` 含当天。配合 `--sort newest|oldest` 按发布时间重排——找最新动态用 `newest`，找最早出处用 `oldest`。

### 能力入口速查

| 能力 | 说明 | 入口 |
|------|------|------|
| 统一搜索 | 路由 → 召回 → 融合 → 快评 | `search.py` / `argo_search` |
| 本地文件搜索 | 本机代码/笔记/记忆（非联网） | `argo_local_search` |
| 本地文本预览 | 白名单目录内预览（fail-closed） | `argo_local_read` |
| 可复算 | 受限子进程重算数字（默认拒绝） | `argo_recompute` |
| 深度研究 | 拆子问题、多源采集、缺口提示 | `research.py` / `argo_research` |
| 可信度评估 | 权威 / 证据密度 / 时效 / 交叉验证 | `evidence.py` / `argo_evidence` |
| 证据核验（闭环） | 高后果问题标记 `fetch_required` + 每条 `fetch_suggested`；`--verify` 一键抓正文核验、回填「核实后证据分」、核实过的链接自动记住 | `search.py --verify 3` / `research.py --verify 3` |
| 意图消歧 | 多义词、品牌碰撞、策略建议 | `clarify.py` / `argo_clarify` |
| 页面抓取 | HTTP 优先，必要时浏览器降级 | `argo_fetch`（`mode=extract` 可结构化） |
| 截图 / PDF | 页面截图、PDF 结构化提取 | `argo_screenshot` / `argo_pdf` |
| 站点爬取 | 列表页批量抓取 | `argo_crawl` |
| 社交与舆情 | 微博 / 小红书 / B 站 / Reddit / X 等 | `argo_social_search` |
| 实时索引搜索 | 免 Key 实时索引源，结果带发布时间，适合「最近几天有什么新东西」 | `--engine realtime_index` |
| 时间窗过滤 | `--since` / `--until`（`7d` 或 `2026-08-01`）限定发布时间范围；支持引擎下推 + 融合后兜底过滤（`time_filtered`），CLI 与 MCP 均支持 | `--since 7d` |
| 时间方向排序 | `--sort newest\|oldest` 按发布时间重排（最新动态 / 最早出处） | `--sort oldest` |
| 登录态专业搜索 | 知乎 / 小红书等登录墙正文、JS 渲染页、登录站点接口直取 | `sub-skills/ego-search/scripts/ego_search.py`（默认关闭，见下） |

### 预算模式

| 模式 | 适合 | 行为 |
|------|------|------|
| `fast` | 简单问题、要速度 | 免费引擎优先，跳过付费精排 |
| `auto` | 默认日常 | 成本感知，质量与花费折中 |
| `deep` | 调研、综述 | 质量优先，可多用引擎 |
| `budget` | 额度紧 | 配额控制，用完降级 |

### 当前大致能力（v2.8.4）

- **本地数据融合（v2.8.4 新增，深度研究 L1）**：工作包可带 `file_inputs`（本地一手数据白名单入账，登记 sha256/血缘）与 `recompute`（可复算执行器，受限子进程安全重算，重算值与检索数字冲突触发 `recompute_conflict`）；dossier 输出 `local_sources`，本地一手计入一手命中（防 `no_sources` 假阴性）
- **多客户端 MCP 一键接入（v2.8.4 新增）**：`argo mcp inject` 一键给 Claude Code / Cursor / Windsurf / Codex / OpenCode / Cline 注入 argo MCP（原子写 + 备份 + 可逆，客户端真源 `mcp/clients.yaml`）
- **结构化搜索增强（v2.8.4 新增）**：查询归一化（全角→半角、点号版本拆斜杠）+ 检索变体 + 复杂度门控（低复杂度查询不放行高价多轮）；social 域语法优先（from:/subreddit:/lang: 命中提前）+ TF-IDF 检索修复（丢弃中文引擎后继续看 top-2/3）；`--include-local` 本机命中并入（默认关）
- **Keenable（v2.8.4 新增）**：接入通用网页搜索引擎（L1 声明式 HTTP，免费体验期，`ARGO_KEENABLE_API_KEY`）

- **多语言路由修复（v2.8.3 新增）**：ja/ko 查询返回目标语言（不再被中文引擎污染）；德法西意等多语言走 anysearch 返回对应语言；weighted RRF 弱源降权（weakest-link，论文 2508.01405）；anysearch 进程内 builder（更快更稳）

- **Windows 全平台可用（v2.8.2 新增）**：移除 npm `os` 限制；全链路 UTF-8 防线（`PYTHONUTF8` + `-X utf8` + 6 处 JSON `read_bytes`）根治 GBK 崩溃；工具探测改 `shutil.which`；Chrome/Edge 自动发现；Ctrl+C 干净退出
- **DSH 插件两种装法（v2.8.2 新增）**：主包自带 `dsh.bundle`，`dsh plugin add github:taxueseek/argo` 即得 12 个 MCP 工具；子包再加 `wide_research` 并行研究编排
- **wide_research 证据门禁（v2.8.2 新增）**：输出自带 `quality_gate_results`（`conclusion_cap` low/medium/high），与 dossier 同一套语义；`depends_on` 依赖分阶段；仅 http(s) 入证据账本；worker 不可调 `argo_research`（防研究套研究）
- **取证协议化（v2.8.2）**：深度研究只产 dossier（来源/覆盖/缺口/门禁），判断稿归 Agent；`--work-packages` 按 `depends_on` 分阶段
- **证据闭环（v2.8.0 新增）**：搜索输出自带证据门控——高后果问题（金融/医疗/法律）标 `fetch_required`，每条结果标 `fetch_suggested`；`--verify` 一键核验正文并回填「核实后证据分」，核实过的链接自动记住，下次搜索直接显示已核实
- **求职搜索 v3（v2.8.0 升级）**：`argo job` 结构化字段 + 增量监控 + 指纹去重，新增 Ashby ATS 免 Key 后端与北京高校就业源
- **天气双源并行（v2.8.0 升级）**：wttr.in + Open-Meteo 双源，地理编码 + 空气质量，问天气不落空
- **通用搜索增强（v2.8.0 新增）**：Parallel 搜索（长文摘录、多路召回）与 You.com（网页+新闻合并、时效动态化），无 Key 优雅降级
- **约 150+ 个搜索源、70+ 业务域**：通用网页 + 金融 / 宏观 / 影视 / 体育 / 地理 / 组织 / 媒体 / 化学 / 学术 / 代码等（真源：`config.yaml`）
- **垂直结构化模态卡**：火车票 / 油价 / 贵金属 / 万年历 / 星座 / 手机参数 / 汽车 / 医疗挂号等查询返回实时结构化卡片（`modal_card` 域 → `bocha_ai` 原生引擎，失败自动回落 web 搜索）
- **双层缓存**：内存 LRU + SQLite 持久化，时效性弱的内容不重复打 API；登录态结果单独隔离，不污染公共缓存
- **为 Agent 节省 Token**：MCP 响应可紧凑裁剪、snippet 可控，输出为精简 JSON 而非整页文本
- **12 个 MCP 工具**：搜索、研究、证据、消歧、抓取、截图、PDF、社交舆情、本地文件搜索、站点爬取、本地预览、可复算
- **多语言搜索**：中、英、日、韩、西里尔、泰、阿、希伯来、希腊、天城体等；路由与引擎参数跟着语言走；非中文查询避免误入知乎 / 搜狗微信 / A 股快照等中文专用源
- **登录态专业搜索**：ego-search 子技能，登录墙正文 / JS 渲染页 / 登录站点接口直取（默认关闭，见上节）
- **垂直域门禁**：空结果恢复时不把 pypi / npm / 快讯等无关源「串」进影视、体育查询
- **日常更快、研究更全**：`engine_policy` 分层——日常 combo 收紧，deep / research 再放开长尾源
- **引擎层 HttpClient 接入（v2.7.3 追加）**：HTTP/HTML 引擎 GET 统一走 UA 轮换 + 重试 + 重定向跟随层，arxiv 类 UA 敏感引擎从 5s 超时空返回变为 2s 内 10 条有效结果；`ARGO_ENGINE_HTTP_CLIENT=0` 回退 urllib
- **TF-IDF 强语义注入（v2.7.3 追加）**：marginalia / open_meteo / usda / gov_policy / cnii 等 25 个垂直引擎不再被正则域压制——「独立博客 长尾」路由到 marginalia、「营养成分 热量」路由到 usda、「国务院 政策」路由到 gov_policy
- **70 域 TTL 全覆盖（v2.7.3 追加）**：实时卡片/快讯/行情缓存 5-15 分钟（此前 1 小时），学术稳定型放宽到 2 小时
- **垂直源中英双语（v2.7.3 追加）**：worldbank / eurostat 英文国家与指标名（China GDP / US inflation / Japan population 实测命中）；快讯类引擎触发词放行全量榜单；百科条目页直接命中兜底

---

## 引擎与路由

当前配置大约 **150+** 个源、**70+** 业务域（以 `config.yaml` 与 `--list-engines` 为准）。

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
| byted / bocha / metaso / octen | 中文网页 / AI 搜索 | API / 低成本 |
| zhihu / wechat_sogou | 中文观点 / 公众号 | API / 免费 |
| tavily / felo / exa | 国际 / 语义 | 付费或额度 |
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

### MCP 工具一览（12）

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
| Python | 3.10+（命令行与 MCP 核心） |
| 依赖 | `pip install pyyaml`（仅此一个硬依赖） |
| 可选增强 | `pip install curl_cffi`（TLS 指纹伪造，MIT 许可）。装上后对指纹检测型反爬站（Cloudflare 等）免起浏览器即可抓取；缺失时自动降级，核心功能不受影响 |
| Node.js | **仅**在使用 `npx` 入口时需要 18+ |
| SearXNG | 不需要（内置本地引擎替代） |

### API Key（全部可选）

不配置则跳过对应引擎，免费引擎自动兜底。**请用环境变量**，不要把真实 Key 写进仓库或贴到 Issue。

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
不会。推荐「一份真源 + 符号链接」。用 `link_source.py`，不要 rsync 多副本。

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
4. **配置驱动扩引擎**，`config.yaml` 为单一真源。
5. **单一真源安装**：链接入口、不 rsync 多副本。
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
├── config.yaml              # 引擎与域配置（真源）
├── assets/readme/           # README 视觉资源
├── backends/
├── mcp/                     # 多客户端 MCP 注入真源（clients.yaml）
├── scripts/                 # search / research / mcp / install …
├── sub-skills/local-search/
├── sub-skills/ego-search/      # 登录态专业搜索（默认关闭，开启命令见正文）
├── tests/
└── docs/
```

---

## 版本记录

| 版本 | 说明 |
|------|------|
| **v2.8.4** | **本地数据融合 + 多客户端 MCP 接入 + 结构化搜索增强 + Keenable**：深度研究 L1 本地一手数据入账（`file_inputs` 白名单 + `recompute` 可复算执行器 + `local_sources`，`no_primary_sources` 计入本地一手）；`argo mcp inject` 多客户端 MCP 一键注入/诊断/还原（`mcp/clients.yaml` 声明式真源）；结构化搜索增强（查询归一化 + 检索变体 + 复杂度门控 + social 域优先 + TF-IDF 检索修复 + `--include-local`）；接入 Keenable 通用网页搜索（L1 声明式 HTTP，免费体验期）；安全加固（recompute 封死 `subprocess`/`os.system` 出网通道 + 主机路径单真源化）。详见 [发布说明](docs/RELEASE_NOTES_v2.8.4.md) |
| **v2.8.3** | **多语言路由修复 + anysearch 进程化 + weighted RRF**：anysearch 从 subprocess 改为进程内 builder（省 python 启动开销 + `HttpClient.post` UA 轮换/重试/退避）；weighted RRF 新增动态可靠性因子（weakest-link 弱源降权，论文 2508.01405）；多语言路由修复——ja/ko 查询返回目标语言（韩语/日语），欧语言（德法西意）走 anysearch 返回目标语言，中文内容/金融/技术引擎双层过滤（域命中 + TF-IDF），Bing `mkt` 市场码 + 语言偏好软排序。详见 [发布说明](docs/RELEASE_NOTES_v2.8.3.md) |
| **v2.8.2** | **Windows 全平台 + 证据语义统一**：移除 npm `os` 限制；全链路 UTF-8 防线（`PYTHONUTF8` + `-X utf8` + JSON `read_bytes`）根治 GBK 崩溃；工具探测改 `shutil.which`；Chrome/Edge 自动发现；Ctrl+C 干净退出。主包新增 `dsh.bundle` 声明（`dsh plugin add github:taxueseek/argo` 即得 MCP 工具）；npm 包补 `engines/`、`data/`。`wide_research` 输出新增 `quality_gate_results` 门禁 + `depends_on` 分阶段 + SSRF 防线 + 研究递归硬保护；深度研究协议化（机器产 dossier、Agent 写判断稿，`--work-packages` 分阶段取证）。详见 [发布说明](docs/RELEASE_NOTES_v2.8.2.md) |
| **v2.8.0** | **证据闭环 + 求职 v3 + 天气双源**：搜索输出自带证据门控（高后果问题标 `fetch_required`、每条结果标 `fetch_suggested`、`--verify` 一键核验回填「核实后证据分」、核实过的链接自动记住下次搜索直接显示已核实）；`argo job` 求职搜索 v3（结构化字段 + 增量监控 + 指纹去重 + Ashby ATS / 北京高校源）；天气双源并行（wttr.in + Open-Meteo，地理编码 + 空气质量）；新增 Parallel / You.com 通用引擎；health_check 崩溃修复 + 日韩股票查询错配修复。详见 [发布说明](docs/RELEASE_NOTES_v2.8.0.md) |
| **v2.7.3** | **本轮修复 + 引擎激活**：引擎层 HttpClient 接入（UA 轮换 / 重试 / 重定向跟随，arxiv 从 5s 超时空返回变为 2s 内 10 条有效结果）；TF-IDF 强语义注入激活 25 个垂直引擎（marginalia / open_meteo / usda / gov_policy / cnii 等，此前有 profile 但永远选不中）；env 占位缺失过滤（github 无 token 从 401 恢复匿名 API）；70 域 TTL 全覆盖（金价/快讯/行情缓存从 1 小时缩短到 5-15 分钟）；垂直源中英双语覆盖（worldbank / eurostat 英文国家与指标名，实测 China GDP / US inflation / Japan population 全部命中）；快讯类引擎触发词放行（「快讯」不再被当关键词滤空）；百科条目页直接命中兜底（moegirl 等搜索跳转条目页不再空结果）；熔断 empty 语义修复（查询无结果不误判引擎故障）；国际引擎中文查询 URL 编码修复（18 处）；单一真源文档修正（engines/specs/ 外置目录）。详见 [发布说明](docs/RELEASE_NOTES_v2.7.3.md) |
| **v2.7.2** | **登录态专业搜索**：新增 ego-search 子技能（默认关闭，开启方法与依赖见上「登录态专业搜索」节）；搜索兜底 / 多意图路由 / 统一健康视图；日韩文查询不再混入中文引擎、显式语言指定生效；MCP 服务拆三模块；具备安全防护（登录态结果与公共缓存隔离、URL 安全检查）。详见 [发布说明](docs/RELEASE_NOTES_v2.7.2.md) |
| **v2.7.1** | **安全加固 + 路由修复**：SSRF 防护（URL 白名单 + IP 段检查）；路由健康状态语义漂移根因修复（只对 `local_*` 做健康判定）；深度研究 local_first 浪费修复；配置清理（单一真源）。详见 [发布说明](docs/RELEASE_NOTES_v2.7.1.md) |
| **v2.7.0** | **垂直结构化模态卡**：内建 `bocha` / `bocha_ai` 原生引擎，`modal_card` 域统一识别火车票 / 油价 / 贵金属 / 万年历 / 星座 / 手机 / 汽车 / 挂号等实时卡片；`bocha` web 解析缺陷修复。详见 [发布说明](docs/RELEASE_NOTES_v2.7.0.md) |
| **v2.6.2** | 合并独立改进线：网络环境感知 / 加权 RRF + 语义缓存 / 自适应引擎禁用 / 内容安全 + 查询变体 / 三大垂直引擎 / 日韩域路由补全；含 v2.6.1 路由修复。详见 [发布说明](docs/RELEASE_NOTES_v2.6.2.md) |
| **v2.6.1** | v2.6.0 修复版：路由误伤修复（`capital of` 不再抢 fact_check）；版本同步。详见 [发布说明](docs/RELEASE_NOTES_v2.6.1.md) |
| **v2.6.0** | **多语言搜索**（检测 / 引擎参数 / 跨语言回退）；影视·体育·地理·组织·媒体等垂直补全；recovery 防污染；能力族与矩阵回归；约 120+ 源。详见 [发布说明](docs/RELEASE_NOTES_v2.6.0.md) |
| **v2.5.1** | 金融/宏观/化学等垂直答案源加厚；引擎分层 + combo 预算；[v2.5.1 说明](docs/RELEASE_NOTES_v2.5.1.md) |
| **v2.5.0** | 安装脚本 + npx；查询改写与路由解耦；路由热路径缓存；MCP 紧凑响应 |
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

MIT License © 2026 [taxueseek](https://github.com/taxueseek)

<p align="center">
  <a href="https://github.com/oil-oil/beautify-github-readme"><img src="assets/readme/made-with-beautify.svg" width="300" alt="README made with beautify-github-readme"></a>
</p>

---

> 好的搜索不是让你看得更多，是让你更敢下结论——以及知道什么时候还不该下结论。

---

## 生态联动：与 Invest 系列投资技能集成

本引擎的财经垂直源（`eastmoney`、`nbs_stats`、`jin10`、`nbs`、`fred`、`eurostat`、`finviz` 等）已接入 [invest 系列投资技能](https://github.com/taxueseek/fund-investment-guide)。

invest-cli 通过 `info`（`invest-cli info <词> --engine eastmoney`）与 `intent macro` 直接复用 argo 的财经 / 宏观 / 资讯取数，用于环节配额、低成本、多源数据，与 invest-cli 的结构化数据层（Wind / 盈米 / 东财 / yfinance / 天天基金）协同。

> 最新版已完成 argo 数据与 invest 系列技能（v2.0.5）的集成。

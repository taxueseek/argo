<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="Argo 阿尔戈：给 Agent 用的统一搜索与证据核验">
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
  <img alt="version" src="https://img.shields.io/badge/version-2.6.0-informational">
  <img alt="engines" src="https://img.shields.io/badge/engines-120+-orange">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-10%20tools-purple">
</p>

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

任选一种即可。**不依赖 npm 官方包**也能用最新版（v2.5.1 起以 **GitHub** 为安装真源；当前推荐 **v2.6.0**）。

**零配置就能跑**：不配 API Key 时走免费引擎 + 本地 `local_*` 引擎；配了 Key 的源质量通常更好，没配则自动跳过。

### 方式一：一键脚本（推荐本机长期用）

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
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

### 方式三：Release 源码包

打开 [Releases](https://github.com/taxueseek/argo/releases)，下载 **`argo-2.6.0.tar.gz`**：

```bash
tar -xzf argo-2.6.0.tar.gz
cd argo-2.6.0
pip3 install pyyaml
python3 scripts/search.py "Python asyncio" --json
python3 scripts/mcp_server.py
```

### 方式四：git clone（开发 / 改源码）

```bash
git clone https://github.com/taxueseek/argo.git
cd argo
pip3 install pyyaml
bash scripts/install.sh --link ~/.claude/skills/argo   # 可选
python3 scripts/search.py --list-engines
```

### 方式五：挂到 Skill 目录（符号链接，单一真源）

```bash
python3 scripts/link_source.py --to ~/.claude/skills/argo
python3 scripts/link_source.py --to ~/.agents/skills/argo

# 或复制示例后编辑本机声明（installs.local.yaml 已 gitignore）
cp installs.local.yaml.example installs.local.yaml
python3 scripts/link_source.py
python3 scripts/link_source.py --check
```

### 方式六：作为 Python 库

```python
import sys
sys.path.insert(0, "/path/to/argo/scripts")
from search import super_search

result = super_search("Python asyncio", n=5, mode="fast")
for item in result["results"]:
    print(item["title"], item.get("credibility_fast"), item["url"])
```

```bash
# 若已把 bin/argo 放进 PATH
argo search "Python asyncio"
argo research "2026 公募基金持仓结构变化"
argo evidence "某条待核实的说法"
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

| 能力 | 说明 | 入口 |
|------|------|------|
| 统一搜索 | 路由 → 召回 → 融合 → 快评 | `search.py` / `argo_search` |
| 本地文件搜索 | 本机代码/笔记/记忆（非联网） | `argo_local_search` |
| 深度研究 | 拆子问题、多源采集、缺口提示 | `research.py` / `argo_research` |
| 可信度评估 | 权威 / 证据密度 / 时效 / 交叉验证 | `evidence.py` / `argo_evidence` |
| 意图消歧 | 多义词、品牌碰撞、策略建议 | `clarify.py` / `argo_clarify` |
| 页面抓取 | HTTP 优先，必要时浏览器降级 | `argo_fetch`（`mode=extract` 可结构化） |
| 截图 / PDF | 页面截图、PDF 结构化提取 | `argo_screenshot` / `argo_pdf` |
| 站点爬取 | 列表页批量抓取 | `argo_crawl` |
| 社交与舆情 | 微博 / 小红书 / B 站 / Reddit / X 等 | `argo_social_search` |

### 预算模式

| 模式 | 适合 | 行为 |
|------|------|------|
| `fast` | 简单问题、要速度 | 免费引擎优先，跳过付费精排 |
| `auto` | 默认日常 | 成本感知，质量与花费折中 |
| `deep` | 调研、综述 | 质量优先，可多用引擎 |
| `budget` | 额度紧 | 配额控制，用完降级 |

### 当前大致能力（v2.6.0）

- **约 120+ 个搜索源、60+ 业务域**：通用网页 + 金融 / 宏观 / 影视 / 体育 / 地理 / 组织 / 媒体 / 化学 / 学术 / 代码等（真源：`config.yaml`）
- **10 个 MCP 工具**：搜索、研究、证据、消歧、抓取、截图、PDF、社交舆情、本地文件搜索、站点爬取
- **多语言搜索**：中、英、日、韩、西里尔、泰、阿、希伯来、希腊、天城体等；路由与引擎参数跟着语言走；非中文查询避免误入知乎 / 搜狗微信 / A 股快照等中文专用源
- **垂直域门禁**：空结果恢复时不把 pypi / npm / 快讯等无关源「串」进影视、体育查询
- **日常更快、研究更全**：`engine_policy` 分层——日常 combo 收紧，deep / research 再放开长尾源

---

## 引擎与路由

当前配置大约 **120+** 个源、**60+** 业务域（以 `config.yaml` 与 `--list-engines` 为准）。

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

### MCP 工具一览（10）

| 工具 | 用途 |
|------|------|
| `argo_search` | 统一搜索 |
| `argo_local_search` | 本地文件搜索（非联网） |
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
├── scripts/                 # search / research / mcp / install …
├── sub-skills/local-search/
├── tests/
└── docs/
```

---

## 版本记录

| 版本 | 说明 |
|------|------|
| **v2.6.0** | **多语言搜索**（检测 / 引擎参数 / 跨语言回退）；影视·体育·地理·组织·媒体等垂直补全；recovery 防污染；能力族与矩阵回归；约 120+ 源。详见 [发布说明](docs/RELEASE_NOTES_v2.6.0.md) |
| **v2.5.1** | 金融/宏观/化学等垂直答案源加厚；引擎分层 + combo 预算；[v2.5.1 说明](docs/RELEASE_NOTES_v2.5.1.md) |
| **v2.5.0** | 安装脚本 + npx；查询改写与路由解耦；路由热路径缓存；MCP 紧凑响应 |
| **v2.4.0** | 路由低分回退与社交误吸过滤；缓存 depth / 柔性命中；熔断与负缓存；`engine_outcomes` |
| **v2.2–v2.3** | 证据两阶段、中文信源表、content_signals、fetch 栈、引擎扩充 |
| **v2.1** | 社交引擎层（多平台 UGC） |
| **v1.x** | 统一命名为 Argo，多引擎路由与双层缓存成型 |

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

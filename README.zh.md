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
  <a href="https://github.com/taxueseek/argo/stargazers"><img alt="stars" src="https://img.shields.io/github/stars/taxueseek/argo?style=social"></a>
  <a href="https://www.npmjs.com/package/argo-search"><img alt="npm downloads" src="https://img.shields.io/npm/dm/argo-search?color=cb3837&label=npm%20downloads"></a>
  <a href="https://skills.sh/taxueseek/argo"><img alt="skills.sh" src="https://img.shields.io/badge/skills.sh-argo-black"></a>
  <img alt="license" src="https://img.shields.io/badge/license-Apache_2.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.9+-green">
  <img alt="version" src="https://img.shields.io/badge/version-2.14.1-informational">
  <img alt="engines" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Ftaxueseek%2Fargo%2Fmain%2Fassets%2Freadme%2Fcounts.json">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-19%20tools-purple">
</p>

**Argo（阿尔戈）** 是专为 AI Agent 打造的开源多引擎搜索、网页抓取与证据核验工具。按查询域与 18 种语言自动选路，无 API Key 时自动走免密钥源与本地引擎兜底，并支持抓回正文核验证据分（`--verify`），直接把「公众号 / 知乎 / 小红书 + 本地知识库 + 外网学术与金融」接入你的 Agent。

---

## 快速上手（零配置可用）

- **Claude Code（MCP）**：
  ```bash
  claude mcp add argo -- npx -y argo-search
  ```
- **Agent Skill（`skills.sh` — Claude Code / Cursor / Codex / Antigravity）**：
  ```bash
  npx skills add taxueseek/argo
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

---

## 中文场景为什么选 Argo

| 对比项 | 宿主内置搜索 | 海外商业 API（Tavily / Exa） | Argo |
| :--- | :--- | :--- | :--- |
| **中文垂直源** | 泛网页居多，缺垂直深度 | 几乎无法穿透中文生态 | **原生覆盖公众号、知乎、小红书（`ego-search`）、A 股行情与中文学术** |
| **费用与密钥** | 绑定宿主配额 | 需外币卡按次付费 | **免密钥可用**，有 Key 自动提质，无 Key 自动跳过 |
| **事实核验** | 仅摘要 | 摘要或原始文本 | **内置 `--verify` 与 `--numverify` 抓原文核验并打证据分** |
| **本地知识库** | 无 | 无 | **内置 `local-search` + `local-seek`（BM25 + 向量混合检索）** |

```bash
# 实测：金融/中文查询自动命中一手行情源并回填证据分
argo search "贵州茅台最新财报核心指标" --verify 3 --json --fields agent
```

---

## 核心能力与 DSH 插件

- **19 个 MCP 工具**：`argo_search`、`argo_fetch`、`argo_research`、`argo_evidence`、`argo_local_search`、`argo_pdf` 等全量对齐 CLI。
- **四个独立子技能**：`local-search`（本地文件与 32 个零成本引擎）、`local-seek`（本地笔记语义检索）、`ego-search`（登录态搜公众号/知乎/小红书，默认关闭）、`js-run`（无浏览器毫秒级跑网页 JS）。
- **DSH 插件（`@taxueseek/argo-dsh`）**：提供 18 个原生工具、`wide_research` 并行编排与六大图形设置页，与宿主共享 `DEEPSEEK_API_KEY` 凭据库，详见 [packages/dsh-plugin/README.md](packages/dsh-plugin/README.md)。

---

## 文档与更新记录

- [docs/ENGINE_CATALOG.md](docs/ENGINE_CATALOG.md) · [docs/MCP_SETUP.md](docs/MCP_SETUP.md) · [docs/为什么选择argo.md](docs/为什么选择argo.md) · [docs/CHANGELOG.md](docs/CHANGELOG.md)

| 版本 | 核心更新 |
| :--- | :--- |
| **v2.14.1** | 信源架构精简、MCP 3+1 收敛与向后兼容别名、304 个源、`secret_guard.py` 故障安全脱敏 |
| **v2.14.0** | 解析层收敛、300 个源、坏例账本闭环 |
| **v2.13.0** | 文档收口、22 台状态机单一真源、派发尾宽宽限 |

<details>
<summary>v2.14.1 版本摘要与当前能力</summary>

### 当前大致能力（v2.14.1）

v2.14.1 完成信源架构精简、免认证新源接入（Hugging Face Datasets、Maven Central、NuGet、Open-Meteo）、MCP 3+1 收敛与向后兼容别名，以及 `secret_guard.py` 故障安全脱敏。

### v2.14.1：信源架构精简 + MCP 3+1 收敛 + 安全脱敏

详见 [docs/RELEASE_NOTES_v2.14.1.md](docs/RELEASE_NOTES_v2.14.1.md) 与 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

</details>

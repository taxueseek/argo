# Argo Agent 安装与阅读指南

本文档面向 AI Agent（Claude Code、Cursor、Kimi、Grok Build 等），提供 Argo 的安装、配置与能力速查。人类用户建议先读 [README.md](README.md)。

## 安装

### 方式一：一键脚本（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
```

macOS / Linux 直接跑。Windows 用 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.ps1 | iex"
```

### 方式二：npm / npx（已支持，当前 2.10.0）

```bash
npm install -g argo-search    # 全局安装 CLI
npx -y argo-search            # 或即用即走，不装
npx -y github:taxueseek/argo  # 要跟 main 最新提交时用这条
```

需要 Node.js 18+ 和 Python 3.9+。npm 上的 `argo-search` 由本仓库发布，当前版本 2.10.0，按 tag 更新。

### 方式三：DSH 插件

```bash
dsh plugin --profile web add argo-search                   # npm 发布版，当前 2.10.0
dsh plugin --profile web add "github:taxueseek/argo"       # GitHub main
```

装完重启 `dsh web` 生效。

### 方式四：MCP 客户端直接填入（免预装）

直接在客户端配置文件中填入：

```json
{
  "mcpServers": {
    "argo": {
      "command": "npx",
      "args": ["-y", "argo-search"]
    }
  }
}
```
已支持一键探测与自动配置的客户端：Claude Code、Claude Desktop、Cursor、Windsurf、Codex、Zed、Roo Code、Cline、OpenCode、WorkBuddy。
本地克隆后也可直接执行：`python3 scripts/mcp_setup.py inject --all`。

## 能力速查

### MCP 工具（19 个）

| 工具 | 用途 |
|------|------|
| `argo_search` | 统一搜索（路由 → 召回 → 融合 → 快评） |
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

完整 19 个工具含 `argo_tweet`、`argo_auth`、`argo_job`、`argo_stats` 等，见 `scripts/mcp_tools.py`。

### CLI 命令

```bash
python3 scripts/search.py [选项] 查询词
  --engine, -e       引擎，默认 auto
  --max-results, -n  条数，默认 5
  --depth, -d        fast | balanced | deep
  --mode             fast | auto | deep | budget
  --no-cache         跳过缓存
  --explain          打印路由说明
  --json             JSON 输出
  --verify           抓正文核验并回填证据分
  --since, --until   时间窗过滤
  --sort             newest | oldest
```

### 常用场景

**搜代码问题**
```bash
argo search "python asyncio error handling" --json
```

**搜学术**
```bash
argo search "transformer attention mechanism paper" --engine arxiv --json
```

**搜行情**
```bash
argo search "贵州茅台股价" --explain
```

**深度研究**
```bash
argo research "2026 公募基金二季报 持仓结构" --depth deep --json
```

**搜本机文件**
```bash
argo local_search "半年前记的那段话" --path ~/notes
```

**抓网页**
```bash
argo fetch https://example.com/article --mode extract
```

## 配置文件

安装后配置文件在 `~/.local/share/argo/config.yaml`（或安装时指定的路径）。

API Key 全部可选，用环境变量配置：

```bash
export TAVILY_API_KEY="你的密钥"
export BOCHA_API_KEY="你的密钥"
export METASO_API_KEY="你的密钥"
```

不配 Key 时自动走免费引擎 + 本地 `local_*` 引擎。

## 自检

```bash
python3 ~/.local/share/argo/scripts/search.py --list-engines
python3 ~/.local/share/argo/scripts/search.py --list-engines --detail
python3 ~/.local/share/argo/scripts/health_check.py --all
```

## 注意事项

1. **高后果问题**（持仓、安全、事实核查）：search → 看快评分 → 对 top 结果 `fetch` → 再下结论
2. **搜索结果页 / 跳转链**：不要当正文信源
3. **社交帖**：当舆情与叙事，不当事实真值
4. **CLI 退出码恒 0**（含断网）：成败判断走 JSON 的 `status` / `errors` 字段
5. **npm 安装已支持**：`argo-search` 由本仓库发布，最新版本 2.10.0，可放心 `npm install -g argo-search` 或 `npx -y argo-search`；registry 上更早的 v1.0.1 是历史版本，不要锁定到它

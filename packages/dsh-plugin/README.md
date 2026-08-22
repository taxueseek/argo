# argo-dsh

DeepSeek Harness 插件：一行同时挂上 [Argo](https://github.com/taxueseek/argo) 搜索 MCP 和 `wide_research` 并行研究编排。

## 安装

```bash
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

重启 `dsh web` 后生效。模型将看到：

- 10 个 `mcp__argo__*`：`argo_search` / `argo_research` / `argo_evidence` / `argo_clarify` / `argo_fetch` / `argo_crawl` / `argo_screenshot` / `argo_pdf` / `argo_social_search` / `argo_local_search`
- 1 个 `wide_research`：规划互补轨道（`depends_on` 依赖分阶段，默认并行）→ 有界并发子代理取证 → 来源账本（仅 http(s) URL 入账）→ 综合报告；输出自带 `quality_gate_results`（`conclusion_cap`：low/medium/high），`passed=false` 时降级表述、先核验账本来源再下结论

日常一问用 `argo_search`；单代理取证用 `argo_research`；多视角对比、要引用账本用 `wide_research`。worker 默认只能调 argo 取证工具，不会再调 `argo_research`（硬保护，用户层放行也无效）。

## 依赖

- Node.js ≥ 18（npx 拉取 argo 包）
- Python 3.10+，`pip install pyyaml`
- DSH 子代理 provider 需支持 `outputSchema`、`toolFilter`、`depthLimit`（标准 `spawn` 即可）

## 自定义（可选）

两行配置 id 分别为 `mcp-argo` 和 `wide-research`。用户层 `cordis.patch.yml` 同 id + name 才是替换：

```yaml
- id: mcp-argo
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: argo
    transport: stdio
    command: /usr/bin/python3
    args:
      - /path/to/argo/scripts/mcp_server.py

- id: wide-research
  name: '@taxueseek/argo-dsh'
  config:
    defaultWorkers: 6
    maxWorkers: 9
    maxTracks: 9
    childToolAllow:
      - mcp__argo__argo_search
      - mcp__argo__argo_evidence
      - mcp__argo__argo_fetch
      - web_search
      - web_fetch
```

## 卸载

在 DSH 的 Settings → Plugins 管理面板中移除本 bundle，或从 profile `package.json` 的 `dsh.profile.bundles` 中删除 `@taxueseek/argo-dsh` 后重启。

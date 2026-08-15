# argo-dsh

DeepSeek Harness 插件：一行安装 [Argo 阿尔戈](https://github.com/taxueseek/argo) 统一搜索与证据核验 MCP。

## 安装

```bash
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

重启 `dsh web` 后生效。模型将看到 10 个 `mcp__argo__*` 工具：

`argo_search` / `argo_research` / `argo_evidence` / `argo_clarify` / `argo_fetch` / `argo_crawl` / `argo_screenshot` / `argo_pdf` / `argo_social_search` / `argo_local_search`

## 依赖

- Node.js ≥ 18（npx 拉取 argo 包）
- Python 3.10+，`pip install pyyaml`

## 自定义（可选）

本插件的配置行 id 为 `mcp-argo`。如需改用本地 argo 源码（更快、离线可用），在 profile 的 `cordis.patch.yml` 用户层用**非 insert 的 id 定向覆盖**：

```yaml
- id: mcp-argo
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: argo
    transport: stdio
    command: python
    args:
      - C:/path/to/argo/scripts/mcp_server.py
    toolCallTimeoutMs: 180000
    failOnStartupError: true
```

注意：
- 覆盖 patch 必须写成上面的顶层 `id` 形式，不能再用 `- insert: - id: mcp-argo`。`insert` 只会追加或向 group 追加，不会替换同 id 行；追加第二条 `mcp-argo` 会在 DSH loader 中触发 `duplicate loader entry id` 并导致 profile 启动失败。
- `name` 必须与 bundle 行完全一致，否则覆盖会被跳过。
- `config` 是整体替换，必须写全 `serverName`、`transport`、`command`、`args`。
- Windows 使用 `command: python`；macOS/Linux 可替换为 `/usr/bin/python3`。
- 如果不想安装 bundle，也可以直接用唯一 id（如 `mcp-argo-local`）通过 `insert` 创建这行，不要与 bundle 的 `mcp-argo` 混用同一个 `serverName`。

## 卸载

在 DSH 的 Settings → Plugins 管理面板中移除本 bundle，或从 profile `package.json` 的 `dsh.profile.bundles` 中删除对应行后重启。

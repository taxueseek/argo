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

本插件的配置行 id 为 `mcp-argo`。如需改用本地 argo 源码（更快、离线可用），在 profile 的 `cordis.patch.yml` 用户层覆盖：

```yaml
- insert:
    - id: mcp-argo
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: argo
        transport: stdio
        command: /usr/bin/python3
        args:
          - /path/to/argo/scripts/mcp_server.py
```

Cordis patch 语义：用户层同 id 最后写入者胜，覆盖插件默认配置。

## 卸载

在 DSH 的 Settings → Plugins 管理面板中移除本 bundle，或从 profile `package.json` 的 `dsh.profile.bundles` 中删除对应行后重启。

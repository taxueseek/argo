# argo-dsh

DeepSeek Harness 插件：一行同时挂上 [Argo](https://github.com/taxueseek/argo) 搜索 MCP 和 `wide_research` 并行研究编排。

## 安装

```bash
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

重启 `dsh web` 后生效。本 bundle 提供 **三形态接入**——同一套 argo 搜索能力（同引擎、同压缩、同守卫），三种调用通道互为冗余，按宿主能力自动降级：

- **原生一等工具**（默认开）：`argo_search` / `argo_fetch`（`ctx.tools.register`），走 CLI 单发（`--call`）复用 MCP 同一套引擎与守卫，**不依赖 MCP 连接、零常驻 token 开销**；`nativeTools: []` 关闭
- **原生 web seam**（默认开）：注册 `argo` 搜索 provider，宿主内置 `web_search` 路由到 argo 引擎链；`searchProviderEnabled: false` 时注册但不可选
- **MCP 形态**（按需开）：14 个 `mcp__argo__*` 完整工具面（`argo_search` / `argo_local_search` / `argo_local_read` / `argo_recompute` / `argo_research` / `argo_evidence` / `argo_clarify` / `argo_fetch` / `argo_crawl` / `argo_screenshot` / `argo_pdf` / `argo_social_search` / `argo_article` / `argo_job`）。工具定义每轮注入上下文且压缩清不掉（实测 14 工具 ≈2.3K token/轮），而搜索/抓取高频路径已由原生工具与 web seam 覆盖，故默认不挂载；需要长尾取证能力（screenshot / pdf / social / article / job / evidence / clarify / crawl / local_*）时在 profile patch 中取消 `mcp-argo` 行注释

另有 1 个 `wide_research`：规划互补轨道（`depends_on` 依赖分阶段，默认并行）→ 有界并发子代理取证 → 来源账本（仅 http(s) URL 入账）→ 综合报告；输出自带 `quality_gate_results`（`conclusion_cap`：low/medium/high），`passed=false` 时降级表述、先核验账本来源再下结论。worker 取证工具白名单默认双写 MCP 与原生工具名（allow 语义）——MCP 挂载与否两态自洽：MCP 在时用全量面，缺席时自动落到 `argo_search` / `argo_fetch` 与宿主 `web_search` / `web_fetch`。

日常一问用 `argo_search`；单代理取证用 `argo_research`；多视角对比、要引用账本用 `wide_research`。worker 默认只能调 argo 取证工具，不会再调 `argo_research`（硬保护，用户层放行也无效）。

搜索引擎侧，Firecrawl 等匿名可用引擎 **keyless 免配置即用**（免费层额度），配置了密钥的引擎自动升级为认证请求；缺密钥不阻断路由。

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
    # 原生一等工具（可选调整）：默认只注册 argo_search / argo_fetch（高频路径，
    # 零常驻 token 开销）。全部长尾工具（article / job / social_search / pdf /
    # crawl / evidence / clarify / screenshot / local_* / recompute / local_read /
    # local_search，共 13 个，schema 自动派生自 MCP 真源）都可按需追加，例如：
    # nativeTools: ['argo_search', 'argo_fetch', 'argo_pdf', 'argo_article']
    # nativeTools: []                               # 空数组关闭
    # nativeTimeoutMs: 60000                        # 单次进程超时（ms）
    # nativeMaxChars: 24000                         # 返回文本上限（字符）
    # searchProviderEnabled: false                  # 关闭原生 web seam
    childToolAllow:
      - mcp__argo__argo_search
      - mcp__argo__argo_evidence
      - mcp__argo__argo_fetch
      - web_search
      - web_fetch
```

> 注意：`argo_research` 不提供原生单发形态——它是分钟级编排，默认 60s 进程超时会中断研究；深度研究请用 `wide_research`（本 bundle 已注册）或挂载 MCP 形态。

## 卸载

在 DSH 的 Settings → Plugins 管理面板中移除本 bundle，或从 profile `package.json` 的 `dsh.profile.bundles` 中删除 `@taxueseek/argo-dsh` 后重启。

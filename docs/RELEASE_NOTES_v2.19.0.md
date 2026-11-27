# Argo v2.19.0

## Highlights (English / TL;DR)

- **What's New**:
  - **Agent-Native Compact YAML & Offline Schema (`argo search --yaml` / `--schema`)**: Added lossless compact YAML output that cuts payload size by **39.8%–46.7%** compared to `--agent` JSON while preserving all evidence, URLs, and quality signals; added zero-network `--schema` for instant self-describing flags and mutual-exclusion rules.
  - **Higher-Precision, One-Hop Local Code Search**: Upgraded local full-text indexing (`ARGO_LOCAL_FTS=1`) to resolve camelCase/snake_case symbols from natural spaced queries (`0.55 ms` Top-1 hit vs. `0` hits previously), prioritized core definitions in multi-file matches, and added `--block` to return enclosing function/class/section line ranges in **1 call (-47.5% latency)**.
  - **Syntax-Aware Structural Search & Local Data Tools**: Upgraded `local-seek --structural` with optional `ast-grep` syntax-tree matching (zero-dependency fallback built in) and fixed empty/error `--json` output; added `argo table` read-only SQL analytics with PDF table extraction and local image text/thumbnail indexing (`ARGO_LOCAL_IMAGE=1`).
- **Key Improvements**:
  - **Hot-Topic Search & DSH Plugin Stability**: Fixed early-stop, false-drop, and single-source dominance issues in hot-topic aggregation; fixed DSH 0.2.3 plugin detail blank config, duplicate summary, and English locale CJK leaks; enhanced cross-platform Python/`uv` launcher probing; restored `plugin-fast-adapt` continuous adaptation highlights across 5-language READMEs; upgraded release preflight with commit-coverage drafts, anti-leakage checks, and single-source sync.
- **Breaking Changes**: None. Existing `--agent`, `--json`, MCP, and sub-skill contracts remain backward-compatible.

---

# Argo v2.19.0 发布说明（中文）

**版本**：2.19.0
**定位**：Agent 紧凑 YAML 输出（体积再降 40%+）+ 零依赖本地代码检索精度与一跳效率跃升 + 本地表格 SQL 分析 + 热榜与 DSH 插件稳定性加固。

---

## 这次更新有什么（通俗版）

### 1. 本地搜索更准、更省轮次
- **跨词与连写符号精准直达**：开启本地索引加速（`ARGO_LOCAL_FTS=1`）后，用自然空格词组也能直接搜到驼峰或下划线命名的代码定义，同口径实测从「0 命中」提升至 **0.55 ms 内 Top-1 精准命中**，常规代码与文档检索耗时平均降低 **10%–23%**。
- **核心定义优先**：多文件命中时自动识别并置顶核心定义文件，减少测试文件与顺带提及的干扰（默认开启，可设 `ARGO_SEEK_GRAPH_PAGERANK=0` 关闭）。
- **一跳直达代码块边界**：`local-seek` 新增 `--block` 参数，单次搜索即可直接拿到所属函数、类或章节的起止行与签名，将原本「先搜行号、再查大纲」的两步操作合并为一步，**调用轮次减半，耗时降低 47.5%**。
- **更精准的代码结构搜索**：`local-seek --structural` 升级语法级识别，显著减少注释与字符串误报（本机安装 `ast-grep` 后自动生效，未安装时自动无缝回落），并修复无匹配或报错时 `--json` 输出格式异常的问题。
- **本地表格与图片能力扩展**：新增 `argo table` 只读 SQL 表格分析与 PDF 抽表联动；本地图片检索（需设 `ARGO_LOCAL_IMAGE=1` 并先运行 `argo local-image index <目录>` 建库）升级文字与缩图检索体验。

### 2. 公网搜索与 Agent 调用更省上下文
- **紧凑 YAML 输出模式**：`argo search` 新增 `--yaml`（及 `--format yaml`），在完整保留所有结果、出处链接与质量信号的前提下，实测输出体积比 `--agent` JSON **再减少 39.8%–46.7%**；原有 `--agent` / `--json` 管道保持完全兼容。
- **离线自描述契约**：新增 `argo search --schema`，零网络开销即可返回参数说明、互斥规则与结果字段。

### 3. 热点检索、插件体验与发版工程加固
- **全网热榜体验修复**：解决多源热榜搜索早停、误杀与单源霸屏问题，优化工作台热点推荐。
- **插件与跨平台稳定性**：修复 DSH 插件详情页配置空白、摘要重复及英文界面中文残留，增强跨平台 Python / `uv` 运行环境自动探测与兜底引导，并在五语 README 补齐 `plugin-fast-adapt` 持续适配说明。
- **发版流程加固**：发版校验新增变更全覆盖草稿、隐私与实现细节脱敏扫描，以及发布说明单源同步能力。

## 验证

- `scripts/pre-commit`（86 项快门禁 4 片并行 + L2 增量选测 + 314 引擎派生件校验 + DSH 插件 154 项 Node 测试与 TS 静态检查）全部通过。
- 同口径基准实测：`argo search --yaml` 体积降 39.8%–46.7% 且 URL 无损；`local-seek --block` 耗时降 47.5%；FTS5 跨词符号检索 0.55 ms 命中。

## 兼容性

- 无破坏性变更。`--agent`、`--json`、`--envelope`、MCP 工具及既有子技能接口保持完全向后兼容。

## 社区广播模板（Broadcast Snippets）

**X / Twitter (280 chars)**:
Argo v2.19.0 is out!
- `argo search --yaml`: 40–47% smaller payloads with zero evidence loss + offline `--schema`
- Local code search: 0.55ms symbol hits, core-definition ranking & `--block` one-hop reads (-47.5% latency)
- Added `argo table` SQL analytics & DSH 0.2.3 polish
`npx skills add taxueseek/argo`

**开发者群广播**:
Argo v2.19.0 发布了：`argo search --yaml` 让 Agent 上下文体积再降 40%–47%；本地搜索新增连写符号毫秒级精准命中、核心定义置顶与 `--block` 一跳拿函数起止行（耗时减半）；新增 `argo table` 只读 SQL 表格分析，并完成全网热榜与 DSH 插件多处稳定性修复。

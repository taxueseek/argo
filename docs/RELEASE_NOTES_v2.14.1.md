# Argo v2.14.1

## Highlights (English / TL;DR)

- **Architecture Slimming & Spec Unification**: Deprecated batch16 and eliminated ≥14 dedicated builder scripts, migrating sources to declarative schema specs and slashing redundant glue code.
- **MCP Surface Consolidated (3+1 Core Tools)**: Streamlined MCP tools into 4 orthogonal primitives (`search`, `fetch`, `deep_research`, `manage_settings`), aligning strictly with MaaS compliance while preserving backward-compatible aliases (resolves #46).
- **New Free & Open Sources**: Integrated Hugging Face Datasets, Maven Central, NuGet, China Railway official fare indicators, and Open-Meteo daily forecasts (registered sources expanded from 300 to 304).
- **Security Redaction & Release Asset Slimming**: Built-in fail-closed output stream credential redaction (`secret_guard.py`); slimmed release distribution down to standalone wheel + sdist packages with per-asset SHA256 checksums.

---

# Argo v2.14.1 发布说明（中文）

**版本**：2.14.1
**定位**：信源架构精简 + 免认证新源接入 + MCP 3+1 收敛 + 安全脱敏与 Release 瘦身。用奥卡姆剃刀做架构减法，退役历史专用胶水并消减专用构建器；收敛极简 MCP 核心工具面以对齐 MaaS 规范；新增 Hugging Face、Maven、NuGet 等开发者免认证开放数据源；落地全链路输出脱敏与轻量 Release 打包。

---

## 这次更新有什么（通俗版）

### 1. 信源架构做减法：退役历史专用构建器，收敛为声明式 Spec

**以前**：很多早期接入的信源（如 batch16、clinicaltrials、cnii、usda、europeana、cleveland 等）各自维护独立的 Python 构建器胶水代码，代码分散、维护成本高，且多个站点各自写一套 RSS 引擎。

**现在**：
- 退役 `engines_builders_batch16.py`（清空 185 行专用胶水转为存根），消减 ≥14 个专用构建器，全部平滑收敛为声明式 YAML Spec；
- 新增统一 `feed` 通用引擎，以一份种子注册表统一订阅美联储、Nature、Lobsters、Solidot 四大资讯源，消减多余站点实体。

### 2. MCP 工具面极致收敛（3+1 极简设计，解决 Issue #46）

**以前**：向外部 Agent 暴露 19 个独立工具接口，工具列表过长，不仅占用模型上下文，且非标 schema 在部分 MaaS 网关解析报错。

**现在**：
- 收敛为 4 个正交核心工具：`search`、`fetch`、`deep_research`，以及配置管理 `manage_settings`；
- 所有专项能力通过参数分流，严格对齐 MaaS 参数规范，并为原有工具保留向后兼容别名，老客户端无感升级。

### 3. 新增免认证开放数据源（源从 300 增至 304）

- **开发与科研源**：免 API Key 直连 Hugging Face Datasets、Maven Central 与 NuGet 软件源；
- **便民与气象**：补齐中国铁路官方票价与复兴号动车组标记；Open-Meteo 补全逐日天气预报；
- **测试探针补齐**：补齐 security_search 与 english_tech 新源端到端探针。

### 4. 输出流自动脱敏拦截器与私域隔离

- **凭据泄露零容忍**：新增 `secret_guard.py`，在 CLI 与 MCP 边界全量挂接输出拦截器，API Key、Token、私钥等凭据即使被搜索召回也会自动替换为脱敏掩码，采用 fail-closed 容灾保障；
- **私域路径强拦截**：收窄强本地意图正则，`file://` 私域路径严禁流入公域检索；坏例账本查询词实施敏感掩码。

### 5. Release 资产瘦身与打包合规

- **去除冗余运行时源码包**：GitHub Release 页面已由平台自动提供 Source code 快照，去除无触达路径的手动 `argo-v<ver>.tar.gz`；
- **sdist 瘦身**：`MANIFEST.in` 显式排除 `tests/`，源码包体积显著降低；
- **独立校验和**：统一产出 wheel + sdist 及其对应的 `.sha256` 独立校验文件。

---

## 验证

- `bash scripts/pre-commit` 快门禁全部绿灯（通过全部 24 项集成校验）
- `python3 scripts/release_check.py` 结构化声明对账通过
- 引擎目录一致性：`python3 scripts/gen_engine_catalog.py --check` 一致（304 源）

## 兼容性

- 严格向后兼容：既有 Spec 声明与 CLI 命令无破坏性变更；
- MCP 工具通过别名兼容老版本客户端调用参数。

---

## 社区广播模板（Broadcast Snippets）

**X / Twitter (280 chars)**:
Argo v2.14.1 is out!
• Architecture Slimming: Eliminated ≥14 dedicated builders into declarative specs
• MCP 3+1 Tools: Consolidated minimal surface (Issue #46)
• 304 Sources: HF Datasets, Maven, NuGet & Open-Meteo added
• Security: Fail-closed output secret redaction
https://github.com/taxueseek/argo

**开发者群广播**:
Argo v2.14.1 发布：信源架构做减法，消减 ≥14 个专用构建器并退役 batch16 胶水；MCP 收敛至 3+1 极简工具面（彻底解决 Issue #46）；新增 HF Datasets、Maven、NuGet、官方高铁票价与逐日天气等免认证源（总源数达到 304）；输出流集成自动凭据脱敏拦截器，Release 资产瘦身合规。

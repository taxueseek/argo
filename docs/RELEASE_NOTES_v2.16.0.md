# Argo v2.16.0

## Highlights (English / TL;DR)

- **What's New**:
  - **Argo Workbench UI (DSH Plugin)**: Built-in browser search workbench with 5 modes (Web Search, Hot Topics, Deep Research, Local Search, Scheduled Search), specialized theme cards (Fact Check, Comparison, Academic, Plugin Review, Local Search), adaptive home suggestions, and a side-by-side verified evidence & line-level citation panel.
  - **Direct 4-Stage CLI Verification Pipeline**: Workbench invokes `bin/argo` directly (bypassing MCP overhead) and integrates `local-seek` for local code & document retrieval.
  - **New & Activated Sources**: Added `linux.do` community source (RSS hot pool + local filtering; total sources 311→312) and activated `keenable` free channel (100k req/month).
- **Key Improvements**:
  - **Fetch & Engine Reliability**: Jina Reader refactored to "keyed when available, keyless fallback"; TheSportsDB exact-entity early-stop, transient error retry, and `sports_search` relevance floor; `QDII`/SAFE vocabularies added to `fund_query` & `financial_news`; `route_combo.py` wired to spec `recommended` priority and `latency_ms` sorting.
  - **Concurrency, Cache & State Hardening**: `circuit_breaker.py` half-open 30s single-probe gate, `quota.py` `RLock`, `fetch_v3.py` identity lock & `file_lock`, `runner.js` timer leak fix, `config_cache.py` canonical path slots & 3d/24h stale cleanup, schema-bump cache self-healing, and `cache_hit` internal usage accounting.
  - **Skills & Upgrade Ergonomics**: Sub-skills consolidated 7→5 with unified `argo seek` entry; added `docs/UPGRADE.md` and `install.sh` restart hints.
- **Breaking Changes**: None. All CLI, MCP, and DSH plugin interfaces remain backward-compatible.

---

# Argo v2.16.0 发布说明（中文）

**版本**：2.16.0
**定位**：DSH 插件可视化搜索工作台 + 直调 4 阶段查证管线 + `linux.do`/Keenable 信源扩展 + 并发状态机收敛。把 Argo 从命令行/MCP 工具进一步延伸到浏览器端「搜得到、查得快、有出处」的一体化工作台体验，并收口一批高并发、缓存隔离与路由排序工程细节。

---

## 这次更新有什么（通俗版）

- **DSH 插件新增「Argo 搜索工作台」可视化页面**：
  - 首页提供「全网搜索 / 热点分析 / 深度研究 / 本机检索 / 定时搜索」模式切换、首页自适应推荐词条，以及「真假核验 / 选型对比 / 学术文献 / 插件评测 / 本机检索」五大特定主题卡片。
  - 结果页采用左文右证双栏布局：左侧输出带引用编号的结构化结论与缺失提示，右侧展示「证据与原文出处」核验面板（支持 `local-seek` 精确到文件路径与行号的原文片段定位）。
  - 工作台后端去 MCP 化直调 `bin/argo` CLI 四阶段查证管线，并补齐 launchd PATH 兼容。
- **新源与免费通道扩展（源 311→312）**：
  - 新增 `linux.do` 社区源（RSS 热帖池 + 本地过滤）。
  - 激活 `keenable` 免费通道（10 万次/月）。
  - Jina Reader 改为「有 Key 用 Key、无 Key 兜底」并完成模块化拆分。
  - TheSportsDB 增加裸实体名强命中早停、瞬时错误单次重试与 `sports_search` 相关性保底。
- **路由与缓存/并发状态机收敛**：
  - `QDII`/外管局词表纳入 `fund_query` 与 `financial_news`；`route_combo.py` 接入 spec 的 `recommended` 优先与 `latency_ms` 升序排序。
  - 缓存层支持 schema bump 自动自愈旧库、`config_cache.py` 规范化 `config_path` 槽位（消除 symlink/相对路径重复落盘）并收紧过期回收（3 天 / 临时目录 24 小时），命中写入内部 `cache_hit` 账。
  - `circuit_breaker.py` half-open 30s 单探测防惊群、`quota.py` RLock、`fetch_v3.py` 身份锁与 `file_lock`、`runner.js` 异常定时器清理、`single_flight.py` 覆盖 `BaseException`，以及 UA/mailto 单源化门禁。
- **子技能与升级体验**：子技能 7→5 收敛并统一 `argo seek` 入口；新增 `docs/UPGRADE.md` 升级指引与 `install.sh` 重启提醒。

## 验证

- `bash scripts/pre-commit`（52 个 pytest 文件 + 14 个 DSH node 测试 + 312 个引擎派生件一致性校验）全绿
- `scripts/release.py check`（结构、五语 README 与 `ENGINE_CATALOG.md` 目录计数校验）全绿

## 兼容性

- **无破坏性变更**：CLI、MCP 与 DSH 插件接口保持向后兼容；旧缓存库会在首次读取时按 schema bump 自动自愈迁移。

## 社区广播模板（Broadcast Snippets）

**X / Twitter (280 chars)**:

Argo v2.16.0: Introducing the Argo Workbench UI in DSH (5 modes, adaptive suggestions & side-by-side verified citations), direct 4-stage CLI verification, linux.do + Keenable free channel (312 sources), and concurrency/cache hardening. Apache-2.0.

**开发者群广播**:

Argo v2.16.0 发布：DSH 插件新增「Argo 搜索工作台」双栏可视化界面（全网/热点/深度/本机检索 + 右侧原文出处核验面板），直调 `bin/argo` 四阶段查证管线；新增 `linux.do` 社区源并激活 Keenable 10 万次/月免费通道（共 312 源）；同步完成 Jina 无 Key 兜底、TheSportsDB 早停、路由延迟排序与缓存/熔断并发收敛。详见 docs/RELEASE_NOTES_v2.16.0.md。

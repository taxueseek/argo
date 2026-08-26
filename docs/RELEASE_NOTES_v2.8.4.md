# Argo v2.8.4 发布说明

**版本**：2.8.4
**定位**：深度研究本地数据融合 L1 + 多客户端 MCP 一键接入 + 结构化搜索增强 + Keenable 接入 + 安全加固。
**相对上一版的改进**：本地一手数据能入账、MCP 接入从「手改配置」变成「一条命令」、搜索命中率与成本控制增强、引擎覆盖加一个、recompute 断网承诺真正成立。

---

## 这次更新有什么（通俗版）

### 1. 深度研究终于能吃「你本地的数据」

**以前**：`argo research` 只能吃 URL / 网页文本，你手里的一手数据（CSV / XLSX / 文献 PDF）进不了研究账本，也没法对数字做「可重算」核验。

**现在**（P0-1 / P0-2，深度研究 L1 已落地）：

- 工作包可带 `file_inputs`（本地一手数据，白名单制：仅显式声明文件可入账，内容不入账、只登记路径 / sha256 / 性质 / 角色，dossier 输出 `local_sources` 章节）
- 工作包可带 `recompute`（可复算执行器：只读挂载输入 → 受限子进程执行计算脚本 → 输出 `{ok, exit_code, stdout, elapsed_ms}`，重算值进 dossier `recomputed_values`）
- 门禁闭环：重算值能与检索到的手数字对上 → 标 `recomputed`；冲突命中 `recompute_conflict`（结论上限 medium）；声明可复算但没跑成 → `recompute_skipped`
- 本地一手计入一手命中，修复「明明给了原始数据，却判 `no_sources`」的假阴性
- 配合插件 `wide_research`：worker 侧 `file_inputs` 入账、recompute 契约执行、`quality_gate_results` 同步这些门禁

### 2. 给各种 Agent 挂 MCP，一条命令搞定

**以前**：要给 Claude Code / Cursor / Windsurf / Codex / OpenCode / Cline 挂 argo MCP，得人肉改各家 JSON/TOML 配置，容易写错、易覆盖、难还原。

**现在**：

- 客户端描述真源 `mcp/clients.yaml`（加一个客户端 = 加一行 YAML，不改代码）
- `argo mcp status` 诊断各客户端（已安装 / 已配置 / 未安装）
- `argo mcp inject --all` 一键注入所有已安装客户端（原子写 + 写前自动备份到 `~/.argo/mcp-backup/` + 含密钥配置 0600 权限）
- `argo mcp undo --all` 精确移除 entry 或从备份回滚；`--dry-run` 只预览不写
- JSON 走 patch 保留未改动子树、TOML 走行级 append 不破坏手写注释

### 3. 简单问题不再被「多轮恢复」拖慢

**以前**：搜索空结果时的恢复流程，对「简单查询」也可能一路放到高价多源 / 跨语言，浪费 token 和时间。

**现在**：

- 查询归一化：全角→半角、点号版本号拆斜杠、压多余空格，让型号 / 版本 / 分隔符更易被精确源命中
- 复杂度门控：低复杂度查询只允许低成本放宽（L1/L2），禁掉高价多源 / 跨语言（L3/L4）
- 检索变体：首轮多路召回，复杂问题才走多轮
- social 域优先：`from:/subreddit:/lang:/filter:` 等平台语法命中时把社交域提前（不再被查询里的实体词抢走）
- TF-IDF 检索修复：日 / 韩语查询丢弃中文引擎候选后继续看 top-2/3（旧逻辑只查 top-1，可能漏掉合格候选）
- `--include-local`：联网结果尾部并入本机文件命中（默认关，零成本开）

### 4. 引擎覆盖加一个（Keenable）

接入通用网页搜索引擎（L1 声明式 HTTP，`ARGO_KEENABLE_API_KEY`）。当前为免费体验期、量较大、按 free 源对待（auto/fast 均可，不参与 budget 成本过滤）。免费期结束或出问题时直接置 `enabled: false` 停用即可，不做「到期自动切付费」的脆弱逻辑。

### 5. 安全加固

- **recompute 断网承诺真正成立**：此前纯 Python socket 层断网挡不住 `subprocess.run(['curl',…])` / `os.system(...)` 这类出网通道；现在 meta_path 拦截 `subprocess/multiprocessing/ctypes/pty/pexpect` 导入 + 覆盖 os 进程 / 执行入口，封死外部进程出网
- **主机路径单真源化（安装感知、全链路无硬编码）**：清掉三处写死的主机路径——
  1. **local-seek 发现收敛到一处**：曾散落在 `search.py`（只查仓库真源）与 `mcp_handlers.py`（硬编码 `~/.agents/skills/`、`~/.claude/skills/`）两处、各执一词易漂移。现抽成 `scripts/seek_locator.py` 统一发现：`ARGO_LOCAL_SEEK_PATH`（显式文件）> `ARGO_LOCAL_SEEK_ROOTS`（显式候选根，os.pathsep 分隔）> 打包子技能（`<ARGO_ROOT>/sub-skills/local-seek`，作兜底），`search.py` 与 `mcp_handlers._seek_py()` 共用。
  2. **config.py DEFAULT_CONFIG 的 anysearch**：旧的 `type: cli` + `cmd: ~/.agents/skills/anysearch-skill/...` 是过时回退（anysearch 早已改进程内 `type: anysearch` builder），现对齐真源去掉主机路径。
  3. **mcp_server.py `--test` 自测**：`argo_local_search` 的搜索目录从写死 `~/.agents/skills` 改为安装锚点（argo 根目录，恒存在）。
- **install.sh 修正**：npx 入口从 `npx -y argo-search`（会拉到 npm registry 上的陈旧 v1.0.1）改为 `npx -y github:taxueseek/argo`（GitHub 唯一真源），与 README/SKILL/ARGO_INTRO 对齐

---

## 技术细节

- `scripts/mcp_setup.py`：自研零第三方依赖的 MCP 注入/诊断/还原；`atomic_write`（同目录 temp + `os.replace`）、写前备份、TOML 行级 append、JSON patch 保留未改动子树
- `data` 融合栈：`scripts/research_work_packages.py`（`file_inputs` / `recompute` schema + 归一化）、`scripts/research_dossier.py`（`local_sources` / `recomputed_values` 入账）、`scripts/research_gates.py`（`recompute_skipped` / `recompute_conflict` 门禁）
- `scripts/recompute.py`：fail-closed 可复算执行器（默认拒绝，需显式授权；白名单只读输入、断网 + 禁外部进程、RLIMIT_AS 内存软限、进程组 killpg 超时硬杀、全新临时工作目录）
- `scripts/query_enhance.py`：纯规则改写的查询归一化 + 变体 + 复杂度门控（无 LLM 依赖）
- `scripts/single_flight.py`：线程版单飞合并，并发相同引擎调用只打一次上游
- `packages/dsh-plugin/dsh/index.js`：新增 `argo_local_read` / `argo_recompute` 工具白名单、`local_sources` / `recomputed_values` schema、本地一手计数入一手命中、recompute 门禁
- 引擎：`engines/specs/keenable.yaml`（L1 声明式 HTTP）

## 依赖

不变（PyYAML 必需；`curl_cffi` / openpyxl（读 xlsx 的可选）/ Chrome 可选）。

## 测试

全量 1000 项（982 passed + 18 skipped），新增：query_enhance 斜杠语义回归、recompute 外部进程/`os.system` 阻断等。仅依赖真实网络与 API Key 的端到端用例可能因环境波动 flaky，非代码回归。

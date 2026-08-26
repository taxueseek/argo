# 设计：多客户端 MCP 一键注入（mcp/clients.yaml 声明式真源）

> 状态：已实施（2026-08-26）。目标：让 argo 的 MCP 工具（argo_search/argo_fetch/argo_research 等）能被
> 主流 AI 客户端一键接入，无需手动贴 JSON。客户端描述声明式化（改客户端不改代码），写入安全可逆。

## 0. 背景与问题重定义

Argo 的 MCP server（`scripts/mcp_server.py`，stdio）此前只有两条分发路径：

- **DeepSeek Harness**（`packages/dsh-plugin`）—— 单一平台、硬件绑定。
- **install.sh 结尾**贴一段手写 `mcpServers` 示例 —— 只给 Claude Code，需用户手动复制。

缺口：没有「探测已装客户端 + 精细写对各客户端配置格式 + 可逆还原」的通用接入层。
用户想在 Cursor / Windsurf / Codex / OpenCode / Cline 等用 argo 搜索，只能各自手工配。

## 1. 设计原则（从第一性原理）

Keenable 的同款能力（`configure-mcp`）用 **Rust 结构体硬编码客户端**：加一个客户端 = 改 Rust 代码 + 重新编译发布。学它思路、规避它的三个短板：

1. **客户端描述硬编码** → 我们用**声明式真源** `mcp/clients.yaml`（对齐 Argo 已有的
   `config.yaml` / `engines/specs/*.yaml`「配置真源」哲学）。加客户端 = YAML 一行，零改代码。
2. **对 TOML 需要第三方库**（它将 TOML 用 `toml_edit` 全量重序列化，会重排注释）→ 我们用
   **行级 append section**（`[mcp_servers.argo]` block）：精确追加、删除，**已存在内容一字不动**，
   注释零破坏。纯 stdlib 可达，零新依赖。
3. **不可逆性** → 我们做 **备份 + undo 双通道**：写入前备份到 `~/.argo/mcp-backup/<ts>_<file>`，
   undo 精确移除 entry；无 entry 时从最近备份回滚。

## 2. 分层架构

| 层 | 职责 | 文件 |
|---|---|---|
| 真源 | 客户端描述（路径/servers_key/格式/detect/标准工具） | `mcp/clients.yaml` |
| 引擎 | 探测 / 注入 / 诊断 / 还原 | `scripts/mcp_setup.py` |
| 入口 | CLI 子命令 | `bin/argo mcp <sub>` |

`mcp_setup.py` 全 stdlib（`sys/json/yaml/os/shutil/tempfile/datetime/pathlib`），
不引入 `toml`/`tomlkit`，Windows/macOS/Linux 通吃。

## 3. 写入安全模型（核心）

### 3.1 原子写
`atomic_write`：**同目录**临时文件（`tempfile.mkstemp`）→ `os.replace`。并发读者只见旧或新，绝无半截。
见 `scripts/mcp_setup.py::atomic_write`。

### 3.2 备份（可回滚）
注入任何配置前 `backup_file` 原文件 COPY 到 `~/.argo/mcp-backup/<ts>_<name>`。
undo 时若 entry 已不存在（比如被手动删了），从最近备份 `copy2` 恢复——回到我们写入前的安全态。

### 3.3 权限收紧
含密钥的配置（`.claude.json` / `config.toml` 可能含 API key）以 `os.chmod(0600)` 写入，
避免其他用户可读。

### 3.4 fail-loud 不覆盖用户文件
- JSON 文件已存在但**非法** → 抛异常，**绝不**用空对象覆盖（避免毁掉用户 `~/.claude.json`）。
- TOML 已含 `[mcp_servers.argo]` → 报错提示先 undo，**不重复追加**。
- `read_config_for_write` 语义：读失败 = 提醒修复，不改文件。

## 4. 双格式处理

### JSON（Claude Code / Cursor / Windsurf / OpenCode / Cline）
`_inject_json`：`config[servers_key][ENTRY_NAME] = entry`，其余子树**原样保留**（兄弟 entry 不动）。
`_remove_json`：删 `ENTRY_NAME`，兄弟保留，无 entry 返回 None。

### TOML（Codex `~/.codex/config.toml`，手维护含注释）
`_inject_toml`：在文件尾**追加** `[mcp_servers.argo]` block（`command`/`args`/`env`）。
`_remove_toml`：按 section 帧边界（行首 `[x]`）**只删该 block**，其余（含注释）一字不动。
已有该 block 则报错不覆盖。

## 5. 客户端描述真源（mcp/clients.yaml）

关键字段：`id`（CLI 用）、`config_path`（相对 home）、`servers_key`（mcpServers/servers/mcp_servers/mcp）、
`format`（json/toml）、`url_key`、`transport`、`detect`（探测目录，存在即"已安装"）、`standard_tools`（未用，预留）。

当前支持：Claude Code、Cursor、Windsurf、Codex、OpenCode、Cline。

## 6. 执行流程

```
argo mcp status        # 逐客户端：已安装/已配置/未安装
argo mcp inject --all  # 对已安装客户端：备份 → patch（JSON merge 或 TOML append）→ 原子写
argo mcp inject --cursor --dry-run  # 只预览
argo mcp undo --all    # 精确移除 entry；无 entry 从备份回滚
```

`--client` 支持逗号分隔（`--client codex,cursor`）。未知 id 忽略并提示。

## 7. 明确不做 / 边界

- **不做 keyless / 认证管理**：本模块只写 MCP server entry，不涉及 API key 的登录/存储（那是
  `engine_env.py` 的职责）。entry 里的 `env` 只放运行必需项。
- **不做后台 daemon**：写入是同步原子操作，无需常驻进程。
- **不覆盖用户手写配置**：任何可能破坏既有内容的路径都 fail-loud 报错而非静默重写。
- **不引入第三方依赖**：TOML 用行级 append 而非 toml_edit 全量重序列化。

## 8. 验收清单

- [x] `status` 正确反映各客户端已安装/已配置。
- [x] `inject` JSON：兄弟 entry 保留，原子写，备份生成。
- [x] `inject` TOML：手写注释 + `model` 原样保留，仅追加 block。
- [x] `undo` TOML：只删 block，注释保留。
- [x] `undo` JSON：移除 entry，兄弟保留。
- [x] 无 entry undo：从备份回滚。
- [x] `atomic_write` 0600 权限。
- [x] `--dry-run` 不写盘。
- [x] 单测 `tests/test_mcp_setup.py`（12 例）全过。

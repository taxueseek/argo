# 升级指南（UPGRADE）

Argo 的两条升级路径都会拉新码，但**不会自动重启长驻进程**——这一页说清升级后要做什么。

## 升级路径

- **install.sh（推荐）**：`curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash`。对已有仓库执行 fetch + `pull --ff-only`，失败（本地有改动）时保留当前版本继续。
- **手动**：`git -C <仓库目录> pull --ff-only origin main`，然后 `uv sync --frozen` 更新 venv（install.sh 会自动做这步，手动用户别漏）。

## 升级后必做：重启长驻进程

新代码落盘不等于新代码生效：

1. **MCP 宿主里的 argo 连接器**：DSH / 其他宿主拉起的 argo MCP 进程要重启（重开宿主会话或在宿主里重连连接器）。argo MCP 有代码指纹热重载，但只对「活着且允许重载」的进程生效——进程已经带着旧缓存 schema 崩溃过、或宿主缓存了旧 tools/list 的，热重载救不了。
2. **argo daemon**：daemon 检出旧代码时会自愈重启，但被你手动 stop 过、或卡在旧代码崩溃循环里的，手动拉起一次。

## 缓存库迁移（2026-11-17 起）

历史包袱：v2.0.0（2026-07-19）到 c685513（2026-09-19）之间写出的缓存库，`meta.schema_version='2'` 但缺后来加的列；v2.10.0（4026fab）起版本早退会让这类库跳过全部列迁移，升级后 `find_similar`/写入报 `no such column: mode`（或 domain）。

- **现在**：`SCHEMA_VERSION` 已 bump 3，旧库在下次打开时**自动补列**，无需删库、无需手动 SQL。
- 如果升级后仍见 `no such column`：说明进程还在跑旧代码——先按上面两步重启，再跑一次查询。

## 升级后自检

```
argo doctor          # 源健康度审计（基于真实使用日志）
argo paths --check   # 路径与状态目录自检
```

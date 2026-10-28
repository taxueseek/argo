# 发版操作（scripts/release.py）

一条链：改号 → 校验 → 提交打标签 → 发 npm → 轮询核验。内容段落要人写，脚本负责卡口。

## 三步

```bash
# 1. 改号：要求工作区干净、在 main、目标版本大于当前、标签不存在
python3 scripts/release.py bump 2.10.1

# 2. 填内容（脚本会列出缺哪几块，`check` 全绿前不让发）
#    - docs/RELEASE_NOTES_v2.10.1.md（去掉 <!-- release-todo -->）
#    - 五语 README 的「最近更新」段、能力段、发布记录表行

# 3. 校验并发布
python3 scripts/release.py check
python3 scripts/release.py publish 2.10.1 --yes
```

`bump --dry-run` 只打印将改的每一处，不落盘。`publish --dry-run` 只打印将要执行的步骤。

## 脚本管什么，不管什么

管（确定性）：

- 11 处机器声明：pyproject / package.json / dsh 插件包 / 插件市场 / ZCode 插件清单 / server.json（顶层 + packages）/ `__version__` / SKILL.md / `ARGO_MCP_VERSION` / 面板 server 与 UI
- 五语 README 徽标、安装段「当前版本」散文、能力段标题
- package.json 的 files 白名单补新发布说明；缺发布说明时生成带 `<!-- release-todo -->` 的骨架
- `check`：release_check（结构）+ 内容清单 + `gen_engine_catalog.py --check`（目录计数）
- `publish`：全量 pytest → commit → tag → push main/tag → npm 发双包 → 轮询 registry 与 GitHub Release

不管（要判断）：

- 发布说明正文、五语「最近更新」、能力段新段落、发布记录表行——这些要写和翻，脚本只检查在不在

## 已知环境事实

- **npm 由本地脚本发**：CI 的 release 工作流里 npm 步骤因无 `NPM_TOKEN` 会跳过，GitHub Release 与资产照常。要改回 CI 发，配 `NPM_TOKEN` 即可。
- **npm CDN 传播 3-4 分钟**：`publish` 里已做轮询（默认 300s，`--wait` 可调），期间查不到新版本是正常现象，**不要重发**。
- **解释器**：用 `python3.14` 跑（3.15 没有 pytest 与 PyYAML）。脚本检测到缺 PyYAML 会提示。

## 发到一半失败怎么办

- `bump` 中途失败：rules 的每处替换都先校验出现次数，失败即停；已改的文件可用 `git checkout -- <路径>` 回退（树是干净的，好回滚）。
- `check` 不过：按提示补内容，别绕过。
- `publish` 在 npm 之前失败：重跑同一条命令即可（已提交的会跳过 commit，已存在的 tag 会复用）。
- `publish` 在 npm 阶段失败：registry 已有该版本的包会自动跳过；单包失败时重跑只补没发出去的那个。

# 发版操作（scripts/release.py）

一条链：改号 → 校验 → 提交打标签 → 发 npm → 轮询核验。内容段落要人写，脚本负责卡口。

## 三步

```bash
# 1. 改号：要求工作区干净、在 main、目标版本大于当前、标签不存在
python3 scripts/release.py bump 2.10.1

# 2. 填内容（脚本会列出缺哪几块，`check` 全绿前不让发）
#    - docs/CHANGELOG.md（去掉 <!-- release-todo -->）
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

## Release 资产（scripts/build_release_assets.sh）

资产清单是 **wheel + sdist 两件套**，每项配一个独立 `.sha256`：

```
argo_search-<ver>-py3-none-any.whl      装好的包，pip / uvx 用
argo_search-<ver>.tar.gz                sdist，无 wheel 的平台或源码构建用
argo_search-<ver>-py3-none-any.whl.sha256
argo_search-<ver>.tar.gz.sha256
```

**为什么没有运行时源码包**：GitHub 每个 Release 页面自动生成 Source code
(tar.gz) 与 (zip)，那是完整仓库快照，「要源码」的需求已被平台覆盖。同类项目
（psf/requests、pandas、astral-sh/uv）的 Release 资产都是 wheel + sdist 两件
套，没有一个手动传运行时源码包。曾存在一个 `argo-v<ver>.tar.gz`（运行时子集，
不含 tests），2026-11-10 删除——它服务的需求没有安装路径能触达。

**为什么 sdist 不含 tests/**：sdist 服务「从源码构建」，tests/ 解包后约 4 MB、
占 sdist 三分之一；想跑测试的人用 `git clone`（能拿到完整仓库、测试依赖与
CI 配置）。`MANIFEST.in` 里 `recursive-exclude tests *` 负责排除。

**为什么逐资产一个 .sha256 而不是合并清单**：合并清单（`SHA256SUMS.txt`）是
单点——它损坏则所有校验都不可用；且校验时必须先下清单再逐项对。独立文件可以
边下边校，一个损坏不影响其余。格式照 astral-sh/uv：`<sha256>  <filename>`。

这三条由 `tests/test_release_assets_contract.py` 机械锁住。

## 已知环境事实

- **npm 由本地脚本发**：CI 的 release 工作流里 npm 步骤因无 `NPM_TOKEN` 会跳过，GitHub Release 与资产照常。要改回 CI 发，配 `NPM_TOKEN` 即可。
- **npm CDN 传播 3-4 分钟**：`publish` 里已做轮询（默认 300s，`--wait` 可调），期间查不到新版本是正常现象，**不要重发**。
- **解释器**：用 `python3.14` 跑（3.15 没有 pytest 与 PyYAML）。脚本检测到缺 PyYAML 会提示。
- **build 模块**：`build_release_assets.sh` 打 wheel/sdist 需要 `.venv` 里有
  `build`（CI 的 release 工作流会 `uv pip install build`）。缺失时脚本会走
  setuptools 分支，可能打不出 wheel——本机要手跑一次构建时先装。

## 发到一半失败怎么办

- `bump` 中途失败：rules 的每处替换都先校验出现次数，失败即停；已改的文件可用 `git checkout -- <路径>` 回退（树是干净的，好回滚）。
- `check` 不过：按提示补内容，别绕过。
- `publish` 在 npm 之前失败：重跑同一条命令即可（已提交的会跳过 commit，已存在的 tag 会复用）。
- `publish` 在 npm 阶段失败：registry 已有该版本的包会自动跳过；单包失败时重跑只补没发出去的那个。

## 双语发布说明与国际化宣发规范

GitHub Release 面向全球开发者，而当前生态已具备 18 种语言检索与海外开源受众。后续每次发版须遵循以下双语结构规范：

### 1. `docs/RELEASE_NOTES_v*.md` 结构规范

每个版本的发布说明直接作为 GitHub Release 的正文，必须包含以下三个核心分区：

1. **头部：English Highlights (TL;DR)**
   - 供海外开发者、Claude/Cursor/MCP 社区快速获知更新内容
   - 包含 3-5 条关键要点（What's new、Why it matters、Breaking changes/Key upgrades）
2. **主体：这次更新有什么（通俗版，中文）**
   - 保持既有的「以前 vs 现在」通俗大白话风格与定位说明
   - 包含详细的功能改进、新增源、验证说明与兼容性
3. **尾部：社区广播模板（Broadcast Snippets）**
   - 提供 280 字符的英文社交媒体（X/Twitter、Reddit）摘要与中文开发者群广播文案，便于发版后一键复制宣发

### 2. 骨架自动生成

`python3 scripts/release.py bump <version>` 已自动集成该双语模板骨架。改号后只需在此骨架中填入英文 Highlights 与中文详情，去掉 `<!-- release-todo -->` 即可通过预检。


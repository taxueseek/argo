# Argo v2.18.0

## Highlights (English / TL;DR)

- **What's New**:
  - **Agent-First CLI & Unified URL Dispatch**: Added `--agent` single switch and `--intent` high-level intent routing (`scripts/intent_router.py`) with Fail-Fast `--envelope` conflict checking; unified specialized URL dispatch (`scripts/fetch_dispatch.py` for WeChat, X tweets, arXiv papers, and PDFs) with tiered `--help` and pipe-friendly stdin support (`S_ISSOCK` Node libuv socketpair compatibility).
  - **Exact Zhihu Answer Fetch & Verified X Keyword Search (Community PRs #54 & #55)**: `argo fetch` now reads exact Zhihu answers (`/question/{qid}/answer/{aid}`) via verified browser body first and falls back to open-platform paginated summaries with `--answer-offset` / MCP `answer_offset`; X keyword search adds auto-routing (`fxtwitter` / `twitter_cli` / `twitter` browser), `from:` + time-window verification, and single-tweet FxTwitter fallback within the shared timeout budget (total sources 313→314).
  - **Local-Seek Precision Read & Workbench Integration**: Split `local-seek` into modular `seek_inspect.py` / `seek_git.py` with `--outline` span ranges, `--symbol-body` one-hop source reads, `--agent` switch, and zero-hit `handoff_hint`; wired Argo Workbench directly to `--intent`, `/argo/api/local-inspect` (with path sandbox), and `/argo/api/note-study`.
- **Contributors**:
  - Thanks to **@OMSociety** (山海学社OMSociety) for four accepted PRs with full original commit authorship preserved:
    - [#52](https://github.com/taxueseek/argo/pull/52): Preserve image metadata (`author`, `tags`, `similarity`, `image_url_preview`) under `--fields agent`.
    - [#53](https://github.com/taxueseek/argo/pull/53): Create the state directory before acquiring its lock in DSH Workbench and isolate concurrency tests to temp directories.
    - [#54](https://github.com/taxueseek/argo/pull/54): Exact Zhihu answer fetching with verified expanded body and paginated summary fallback (`--answer-offset` / `answer_offset`).
    - [#55](https://github.com/taxueseek/argo/pull/55): Verified X keyword search routing (`twitter_cli` / `fxtwitter` / `twitter`) and single-tweet FxTwitter fallback.
- **Key Improvements**:
  - **Reliability & Source-Side Attribution**: Eliminated silent failures and added mechanical switch-default parity gates (`test_switch_defaults_gate.py`); completed upstream error classification and HTTP status attribution, recovered transparency gaps, and moved quota pre-checks ahead of all search modes; hardened Windows/EPIPE Workbench I/O and empty-PDF explicit failure guards.
  - **L2 Change-Aware Test Selection**: Landed `scripts/affected_tests.py` incremental test selection in `pre-commit` alongside `badcase_ledger.py` auto-demotion proposals and `test_help_covers_dispatch.py`.
- **Breaking Changes**: None. All CLI, MCP, sub-skill, and DSH plugin interfaces remain backward-compatible.

---

# Argo v2.18.0 发布说明（中文）

**版本**：2.18.0
**定位**：Agent 入口收口 + 统一抓取分发 + `local-seek` 精读与工作台直连 + 社区 4 项高质量 PR 合入（知乎指定回答精确抓取、X 关键词核验与单帖补读、图片元数据保留、工作台状态锁目录修复；源 313→314）。

---

## 这次更新有什么（通俗版）

### 1. 社区贡献合入（感谢 @OMSociety / 山海学社OMSociety，保留原始提交署名）

- **知乎指定回答精确抓取（PR [#54](https://github.com/taxueseek/argo/pull/54)）**：
  - 对 `https://www.zhihu.com/question/{问题ID}/answer/{回答ID}` 链接，`argo fetch` 与 MCP 首轮优先读取经核验的已展开回答正文，不可核验时按开放平台游标分页查摘要并做问题与回答双身份校验；新增 `--answer-offset`（MCP：`answer_offset`）显式续查，保留 `--offset` 正文切片语义，并拆出 `scripts/fetch_cli_output.py` 维持模块体积纪律。
- **X 关键词核验检索与单帖补读（PR [#55](https://github.com/taxueseek/argo/pull/55)，源 313→314）**：
  - X 关键词检索支持自动选路与 `engine=twitter_cli` / `fxtwitter` / `twitter` 固定单通道；对简单 `from:` 与时间窗查询按返回作者与发布时间核验（含起点、不含终点）；`twitter_syndication` 单帖主通道临时失败时共用剩余预算向 FxTwitter 补读并核验 ID；MCP 同步暴露 `platforms` 与 `engine_outcomes`。
- **`--fields agent` 保留图片元数据（PR [#52](https://github.com/taxueseek/argo/pull/52)）**：
  - `scripts/search_output.py` 白名单补齐 `author`、`tags`、`similarity` 与 `image_url_preview`，让 Agent 在精简输出档也能读到 pixiv 与以图搜图的完整元数据。
- **DSH 工作台状态锁目录初始化修复（PR [#53](https://github.com/taxueseek/argo/pull/53)）**：
  - `acquireStateFileLock` 在独占创建锁文件前先 `mkdirSync` 父目录，修复干净环境下首次 `mutateWatches` 静默返回 `null` 的问题，并将并发测试隔离至临时目录。

### 2. Agent 入口、统一 URL 分发与工作台增强

- **`--agent` 单开关与 `--intent` 高层意图路由**：
  - 新增 `scripts/intent_router.py` 接入 `engines_boost` 真实垂类选源，入口 Fail-Fast 拦截 `--envelope` 冲突，并补齐 intent 引擎注册表门禁与 `bin/argo` 零依赖快启动。
- **统一 URL 专用分发与工作流管道**：
  - 新增 `scripts/fetch_dispatch.py` 统一自动分发微信、X 推文、arXiv 论文与 PDF（空正文 PDF 显式失败，不再假成功）；升级分层 `--help` 与 `test_help_covers_dispatch.py` 门禁；`cli_io.py` 兼容 Node libuv `S_ISSOCK` socketpair 管道输入。
- **`local-seek` 精读能力与 DSH 工作台直连**：
  - `sub-skills/local-seek` 拆出 `seek_inspect.py` 与 `seek_git.py`，新增 `--outline` 起止行跨度、`--symbol-body` 一跳直读符号源码、`--agent` 单开关与零命中 `handoff_hint`。
  - DSH 工作台直连 `--intent` 意图路由、`/argo/api/local-inspect`（带路径沙箱保护）与 `/argo/api/note-study` 管道沉淀，并加固 Windows 与 EPIPE 跨平台 I/O。

### 3. 工程质量与可观测性

- **错误归因与配额前置**：补齐源头错误分类与 HTTP 状态码归因、恢复输出透视空转，并前置全模式配额预检。
- **开关默认值门禁与 L2 增量选测**：新增 `tests/test_switch_defaults_gate.py` 机械对拍开关默认值；落地 `scripts/affected_tests.py` 变更感知增量选测与 `badcase_ledger.py` 坏例自动降级提案；快门禁清单扩展至 83 项（4 片并行）。

---

## 社区贡献致谢（Contributors）

- **@OMSociety（山海学社OMSociety）**：贡献 4 项 PR（[#52](https://github.com/taxueseek/argo/pull/52)、[#53](https://github.com/taxueseek/argo/pull/53)、[#54](https://github.com/taxueseek/argo/pull/54)、[#55](https://github.com/taxueseek/argo/pull/55)），全部以 `--no-ff` 保留原始提交与作者署名合入主干。

---

## 验证

- `bash scripts/pre-commit`（83 条快门禁 4 片并行 + 314 引擎派生件一致性校验 + `ego-search` 13 用例 + DSH 插件 139 项 Node 测试）全绿
- `.venv/bin/python -m pytest tests/ --splits 4`（全量 4 片并行回归）100% 通过
- `.venv/bin/python scripts/release.py check`（结构、五语 README、`ENGINE_CATALOG.md` 与 `gen_readme_art.py --check`）全绿

---

## 兼容性

- **无破坏性变更**：CLI、MCP、子技能与 DSH 插件接口均向后兼容；`twitter_cli` 按运行环境凭据按需启用。

---

## 社区广播模板（Broadcast Snippets）

**X / Twitter (280 chars)**:

Argo v2.18.0: `--agent` switch & `--intent` routing, unified `fetch_dispatch` (WeChat/X/arXiv/PDF), modular `local-seek` `--symbol-body`, plus 4 community PRs from @OMSociety (exact Zhihu answer fetch, verified X search & fallback, agent image metadata, DSH lock fix; 314 sources).

**开发者群广播**:

Argo v2.18.0 发布：新增 `--agent` 单开关与 `--intent` 意图路由、统一 URL 分发层（微信/推文/论文/PDF）、`local-seek` `--symbol-body` 一跳直读与 DSH 工作台直连；并完整吸纳社区贡献者 @OMSociety 的 4 项 PR（#52/#53/#54/#55：知乎指定回答精确抓取、X 关键词核验与单帖补读、agent 档图片元数据保留、工作台状态锁目录初始化修复，源增至 314 个），全部保留原始提交署名。83 项快门禁、139 项插件 Node 测试与全量 pytest 回归全绿。详见 docs/RELEASE_NOTES_v2.18.0.md。

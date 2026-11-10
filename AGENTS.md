# AGENTS.md — Argo Agent 路由表

> 本文件**只写路由，不写规则**。它告诉 agent「干什么活之前先翻哪份文档、
> 什么决定不能改」。规则本身在各份被引用的文档里，此处不复制第二份真源。
>
> 维护要求：新增一份 `references/` 文档或新增一类「不可回退的决定」时，
> 同一次改动更新本文件对应行。漏更新的后果是 agent 按旧路由走，而路由错了
> 比没有路由更坏。

## 先读哪份

| 你要做的事 | 先读 | 然后跑什么 |
|-----------|------|-----------|
| 新增或改搜索源 | `engines/specs/*.yaml`（唯一真源）+ `docs/ADDING_NEW_ENGINE.md` | `python3 scripts/sync_backends.py && python3 scripts/sync_backends.py --check` → `python3 -m pytest tests/ -q` |
| 改路由（域/语言/引擎组合） | `references/engines.md` + `scripts/route_domains.py` 文件头 | `scripts/matrix_search_eval.py --offline` → `pytest tests/test_route_*.py` |
| 改排序或融合 | `references/architecture.md` 的量化公式节 | `scripts/replay_eval.py --check` → `pytest tests/test_ranking_*.py` |
| 改状态词表（25+ 台状态机） | `scripts/engine_states.py` 文件头 | `pytest tests/test_engine_states.py tests/test_state_registry.py tests/test_state_literal_drift.py` |
| 改 SKILL.md / 常驻文档 | `references/operations.md` 工程纪律节 | `pytest tests/test_context_budget.py tests/test_usage_doc_gates.py` |
| 改 MCP 工具面 | `scripts/mcp_tools.py`（单一真源）+ `references/operations.md` | `pytest tests/test_mcp_*.py` |
| 改抓取链 | `references/usage.md` 的降级链节 | `pytest tests/test_fetch_*.py` |
| 改发版或依赖 | `docs/RELEASING.md` + `pyproject.toml`（真源） | `pytest tests/test_release_*.py tests/test_dependency_source_gate.py` |
| 写提交 | `docs/COMMIT_STANDARD.md` | `scripts/check_commit_report.py --last` |
| 提交前自检 | — | `bash scripts/pre-commit`（14 个 pytest 文件 + 2 个 node 测试，~13s） |
| 全量回归 | — | `python3 -m pytest tests/ -q`（约 5 分钟，不放进 hook） |

## 不可回退的决定

这些是**拍过板的产品决定**，不是实现细节。PR 不得改它们的默认值；确需改动，
先在 PR 描述里说明理由并等 owner 拍板。改动这些开关的默认值会让既有用户的
行为静默变化，而这类变化在搜索工具里表现为「结果莫名其妙变了」。

**默认关闭的能力开关**（口径唯一真源：`references/usage.md` 的开关总表）：

- `ARGO_CONCEPT_ROUTER`：概念语义路由。默认走纯词面 TF-IDF，开启后多一层向量
- `ARGO_LOCAL_IMAGE`：本地图片检索。需要 macOS + swiftc + numpy
- `ARGO_RRF_WEIGHTED`：加权 RRF 融合。标为**逃生开关**，正常路径不启用
- `ARGO_SEMANTIC_EVIDENCE`：可选语义证据层。需配置 key
- `ARGO_ADAPTIVE_TAIL`：尾宽分层。三轮 A/B 未证实收益，保留用于复现
- `ARGO_GENERAL_FALLBACK_STATIC` / `ARGO_GENERAL_FALLBACK_LEGACY`：静态兜底链。
  用于逐位复现历史行为或固定 A/B 臂
- `ARGO_DAEMON`：search 自动拉起 daemon。显式 opt-in
- `ARGO_NO_CACHE`：跳过缓存读取。仅改动调试用（与 `ARGO_ROUTE_CACHE` 不同：
  后者默认开，只在测试环境默认关，不是产品决定）
- `ARGO_ALLOW_PRIVATE_URLS`：SSRF 防护放行。**安全边界，非必要不动**
- `ARGO_ALLOW_RECOMPUTE`：调度器执行子进程。**安全边界，非必要不动**
- `ARGO_PIXIV_ALLOW_R18`：pixiv 放行 R-18 作品
- `ARGO_SAUCENAO_API_KEY` / `ARGO_SERPAPI_API_KEY`：反搜图引擎，仅显式反搜时需要

**已停用的引擎**：`docs/ENGINE_CATALOG.md` 的「已停用」清单（当前 15 个）。
重新启用需走 `docs/ADDING_NEW_ENGINE.md` 的准入流程，不是把 `enabled` 改回 true。

**出口纪律**：SSRF 防护（`content_security` / `url_safety`）与 recompute
执行器的默认值不得放宽。这两处是安全边界，放宽需要 owner 明确拍板。

## 工程纪律（唯一来源）

以下每条都在 `references/operations.md` 末尾有完整说明，此处只列结论：

1. **代码看本仓库**，引擎声明看 `engines/specs/*.yaml`（外置 specs 优先覆盖
   `config.yaml` 同名引擎）
2. **宿主入口用 `scripts/link_source.py` 建 symlink 指回来源**，禁止 rsync 或
   多副本；禁止在产品代码里写死主机 skill 路径
3. **新增搜索源只改 spec**，然后 `scripts/sync_backends.py` 派生，不手改 `backends/*`
4. **状态值一律引用 `engine_states` 常量**，不写裸字面量（有机械门禁
   `tests/test_state_literal_drift.py` 拦）
5. **改完先跑秒级检查**，全量回归按需（`/hunt`、`/ui` 专项验收才跑）

## 提交纪律

高风险提交（新增引擎、改路由、改状态词表、动发版或依赖链）**必须**在提交信息
里写清「跑了什么、在 commit 上跑的、没跑什么」。三段固定为 `跑了` / `在 commit` /
`未跑`；确实没漏写 `未跑: 无`，不能整段省略。

口径与示例见 `docs/COMMIT_STANDARD.md`，机械检查是
`scripts/check_commit_report.py`。

**理由**：`f702ee2` 把 SKILL.md 压到 8049B、`4a71693` 让 deepseek_search 成死源，
两笔都过了 pre-commit hook 却在全量回归里红——跑了的门禁全绿，没跑的那条门
没人知道它该跑。省略不写是默认行为，而默认行为正是这类事故的成因。

## 回退测试要求

新增测试必须验证它真能抓到退步：把旧实现放回去，新测试要挂。「测试存在」
不等于「测试有效」。做法与示例见 `docs/COMMIT_STANDARD.md` 的「回退测试要求」节。

## 坏例复发处理

`scripts/badcase_ledger.py` 从本机使用日志归集坏例，并按根因聚合出 `seen_days`
（在多少个不同日期出现过）。同一根因跨 **3 个不同日期**出现即触发升级判定
（`check --promote-days 3`），此时它该从错题本挪进硬规则——通常是补一条测试或
一条 lint。词表见 `scripts/engine_states.py` §27，导出格式见 `lessons` 子命令。

`external_ranking`（活网排名漂移）不参与升级判定：它不是 argo 的问题。

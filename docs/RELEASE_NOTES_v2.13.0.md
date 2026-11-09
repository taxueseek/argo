# Argo v2.13.0 发布说明

**版本**：2.13.0
**定位**：文档分层收口 + 22 台状态机单点真源 + 调度尾宽修复。把公开仓的文档从 87 份精简到 26 份，把散落的状态词收进一个真源文件，修掉 deep 档 10 秒空等缺陷。

---

## 这次更新有什么（通俗版）

### 1. 文档不再有死链与重复

**以前**：`docs/` 累积到 87 份，发版说明 23 份（其中 10 份已被 README 引用但文件不存在），引擎文档 60 份（内容与 `engines/specs/*.yaml` 完全重复），过程报告 18 份（写于 2026-09，代码已迭代到 v2.12）。改一个引擎要判断改哪份，看文档要区分「当时的还是现在的」。

**现在**：

- `docs/engines/` 60 份删除：字段（准入时间、状态、cost_tier、环境变量、最近验证）全部在 `engines/specs/*.yaml`（真源）与 `docs/ENGINE_CATALOG.md`（生成）里
- 18 份过程报告归档到 `docs-local/archive/`（不进公开仓，但保留本机可追溯性），新增 `docs-local/archive/README.md` 作索引
- 新增 `docs/CHANGELOG.md`：合并 23 份 RELEASE_NOTES 的「定位 + 关键变更 + 默认关闭开关」，按版本倒序
- README 版本表 27 处 RELEASE_NOTES 引用改为指向 CHANGELOG，消除 10 处死链
- `docs/` 从 87 份降到 26 份

### 2. 状态词不再散落：22 台状态机单点真源

**以前**：状态词（`closed/open/half_open/disabled`、`skipped_*`）散在 6 个模块，`half_open` 与 `half-open` 混用。最危险的是精排降级集合在 4 处各写一份手抄 `frozenset`，新增一个 `skipped_*` 状态时漏改任何一处，会导致「既不全量精排也不走本地保底，顺序静默退回 RRF 原始序」。

**现在**：

- `scripts/engine_states.py` 从 12 台扩到 22 台状态机，34 个模块引用，各文件字面量残留从 19 处降到 0
- 派生的降级/惩罚/可路由集合一律由全集减去例外得到，不再手抄
- 修掉 `health_check --category` 必崩（`TypeError: multiple values for argument 'available'`）
- daemon 状态从「靠中文文案反推」变成机器可读四态（running/busy/idle/stopped）
- 缓存命中路径补发 `Stage.DONE`，常驻进程热路径能收到终态

### 3. deep 档不再空等：10 秒降到 2.2 秒

**以前**：deep 档全量并行分支缺尾宽约束，4 个引擎 0.5s 交付后空等 biorxiv 9.5s，而结果条数与 fast 档完全相同——9.5s 全花在等一个零贡献的慢源。

**现在**：给该分支补 `tail_grace_s` 与 `tail_grace_fn`，deep 档中位 10,000ms → 2,324ms，条数全部不变。

### 4. 引擎声明从双源变单源

**以前**：`config.yaml` 内联 165 个引擎声明 + `engines/specs/` 123 个，双源并存，改一个引擎要判断改哪份。

**现在**：165 个声明全部迁到 `engines/specs/*.yaml`，`config.yaml` 的 `engines:` 变成空壳。`config.py` 的合并逻辑从「双源覆盖」变成「单向 specs→config」，删掉 3523 行。

### 5. 依赖收口到 uv

**以前**：只有 `pyproject.toml` + `requirements*.txt`，无锁文件，不同机器装出不同版本。

**现在**：`uv.lock`（3196 行钉版）+ `.python-version` + `pyproject.toml` 真源，新增 `test_dependency_source_gate.py` 机械对账 requirements 与 pyproject 漂移。

---

## 验证

- `bash scripts/pre-commit` 全绿（16 个测试文件 + node 测试）
- 全量 pytest exit 0（含 5,000+ 用例）
- 新增契约用例：精排降级=全集减 ok、探针 fail-open、数值降权集合、coverage 大写契约、阈值单一来源、同名函数消失、缓存命中发 DONE、health_check 两条路径不重复传参与不丢 available、桥的三种 daemon 状态来源
- 体积门禁：search.py 998→991、search_rank.py 983→976（上限同步下调）

## 兼容性

- 无破坏性变更，v2.12.0 的配置与用法继续有效
- `docs/engines/` 删除后，`engine_validate.py --write-doc` 仍可生成单引擎文档，只是不再默认入库
- `docs-local/archive/` 已被 gitignore，不影响公开仓
- 新增 `docs/CHANGELOG.md`，README 版本表链接已更新

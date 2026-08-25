# 设计：本地数据与网络数据的分层融合（深度研究 L1）

> 状态：P0-1 已实施（2026-08-25）。决策点：常规层 `--include-local` 默认关闭；recompute 执行器用本机受限子进程 + 审批。
> 目标：常规查询保持轻量双通道；深度研究才启用「本地数据入账 + 可复算 + 攻击验证 + 任务板」完整栈。

## 1. 问题重定义

上一轮审查发现 argo 深度研究只能吃 URL/文本，用户本地一手数据（CSV/XLSX/文献 PDF）无法入账，也无代码执行语义。经讨论确认：**这个缺口只属于深度研究层，常规层不需要**。

- 常规查询验收标准：快 + 来源对得上。本地检索（local-seek）与联网检索（argo_search）独立并列即可，交叉验证成本只在结论承重时值得付。
- 深度研究验收标准：结论可核查。本地一手数据与网络公开证据需要血缘合并、相互印证、数值重算。

## 2. 分层架构

| 层 | 通道 | 本地+网络关系 | 新增组件 |
|---|---|---|---|
| L0 常规 | argo_search + argo_local_search | 独立并列，Agent 自选；可选 `--include-local`（默认关） | `search.py` 一个参数 + seek 结果并尾 |
| L1 深度 | argo_research / wide_research | 血缘合并 + 可复算 + 攻击验证 | 账本扩展 / recompute 执行器 / claim 验证 / 任务板 |

分流规则（写进 SKILL.md）：
- 单跳、来源明确 → 两个常规工具任选或都用
- 多跳、结论承重、涉本地数据文件 → argo_research + 工作包（file_inputs / recompute）
- 中间地带（如「我的笔记怎么说 + 网上怎么说」单跳对照）→ `--include-local`（默认关）

## 3. P0-1 本地数据入账（file:// 通道）

现状：来源账本只认 http(s) URL（wide_research 防本地注入）。

- 账本条目：`{type: file, path, sha256, mtime, role}`；工作包增 `file_inputs: [{path, kind, role}]`（kind: csv|xlsx|pdf|json|md；role: 定义|基线|数据）
- 白名单制：仅工作包显式声明的文件可入账（`--forensics-dir` 或 `file_inputs`），其余 fail-closed 拒绝
- 隐私：内容不入账，只存哈希、摘要字段与引用行号；dossier 输出 `local_sources` 章节
- 门禁挂钩：`no_primary_sources` 把本地一手文件计入一手命中（防「有原始数据却判 no sources」假阴性）

## 4. P0-2 recompute 可复算契约

工作包可选字段：

```json
{
  "id": "wp-rev",
  "question": "公司 2025 年度营收同比增速是多少",
  "query": "公司名 2025 年报 营收 同比",
  "file_inputs": [{"path": "~/data/company_2025.xlsx", "role": "原始年报数据"}],
  "recompute": {
    "script": "读取 xlsx 营收列，计算 (2025-2024)/2024",
    "expect": "0.23 左右",
    "budget": {"timeout_s": 30, "max_mem_mb": 512}
  }
}
```

执行器 `scripts/recompute.py`（新组件）：
- 只读挂载 file_inputs → 本机受限子进程（决策确认：非 Docker）
- Fail-closed：默认禁运行，需 `--allow-exec` 或 MCP 参数显式放行；输入只读、断网（网络命名空间/权限不可用则跳过联网工具）、超时硬杀、无写目录
- 输出 `{exit_code, stdout, stderr, elapsed_ms}`；改产物进 dossier `recomputed_values`
- 闭环：检索数字与重算结果对照——对齐标 `recomputed=true`；冲突命中 `fact_conflicts` 门禁（结论上限 medium）
- 安全：审批制（变异操作须显式确认）；执行记录进 trace

## 5. P0-3 claim 级攻击验证（非对称验证）

- 从 dossier key_findings 提取 load-bearing claims（含数字/判断）
- 每个 claim 生成攻击子任务：验证器只见「主张 + 证据 + 交付约束」，不见生成全轨迹（防同源锚定）；任务 = 独立源类三角 + 数字/日期/公式原子核验 + 反例搜索
- 输出 `claim_audit: [{claim, verdict: confirmed|contested|rejected, basis, repair_hint}]`
- contested/rejected → conclusion_cap 降级；repair_hint 直接生成下一轮工作包
- 复用现有 verify/evidence 抓取能力，新增验证提示词模板 + 调度

## 6. P2 任务板 + 介入（协调层）

- wide_research 轨道状态机显式化：`board: [{id, objective, state: pending|active|completed|blocked|cancelled, depends_on, returned}]`，输出进 dossier
- 失败只失效后代：轨道失败 → 依赖轨道 `blocked`，已完成的独立进展保留
- 介入：研究会话执行中侧信道输入（MCP `intervene` / `--intervene-file` 指令文件，下一轮次边界注入）；支持暂停/取消单轨道、补充 file_inputs、改优先级
- 预算可见：有界并发预算按轨道分配到板

## 7. L0 常规层明确不做

不融合重排、不加 checkpoint/resume/回放、不加 recompute、不加任务板。

可选开关 `--include-local`（默认关）：`argo_search` 把 seek 结果并入输出尾部（source=local_files），不做交叉评分，零成本扩展口。

## 8. 实施顺序与验收

| 步 | 内容 | 验收 |
|---|---|---|
| 1 ✅ | P0-1 账本扩展 + work_packages schema | dossier 含 local_sources；白名单外文件拒绝；`no_primary_sources` 计入（已实施） |
| 2 ✅ | P0-2 recompute 执行器 + 门禁闭环 | 只读/断网/超时/审批四组测试；recompute_skipped / recompute_conflict 门禁触发测试（已实施） |
| 3 | P0-3 claim 验证模板 + 调度 | claim_audit 三态判定测试 |
| 4 | P2 任务板 + intervene | 轨道状态机测试；失败仅后代 blocked |
| 5 | --include-local（默认关） | 开关默认不改变现有输出 |

与「深度研究不写判断稿」一致：recompute 只产数值事实，判断仍归 Agent。

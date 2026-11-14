# 深度研究模板

仅在需要拟定研究方案、组织工作包或写判断稿时读取。取证仍走 `argo_research`。

## A. 研究契约

```markdown
# 研究契约

- **决策/用途**：
- **核心问题**：
- **工作包**：
- **范围**：对象、地域、时间窗、纳入与排除。
- **时效性**：资料截点。
- **用户资料**：标为「用户提供」，不与公开证据混称。
- **停止条件**：覆盖、交叉验证、信息饱和的可检查标准。
```

## B. 工作包

```json
{
  "id": "wp-1",
  "question": "待回答的可验证问题",
  "query": "可选检索词，缺省用 question",
  "priority_sources": [],
  "depends_on": []
}
```

`priority_sources`：该子查询的引擎优先序列，显式指定优先于机器启发；新闻/实体/带时间窗的取证可写 `["deepseek_search", ...]`，语义与场景口径见 research-protocol.md 的 priority_sources 节。

## C. 判断稿骨架

```markdown
# [主题] 研究判断

> 研究截点：YYYY-MM-DD。取证包见 dossier。本文区分事实 / 推断 / 建议。

## 执行摘要
直接回答核心问题，写结论强度和关键限制。

## 研究问题与边界

## 关键发现
### 发现一
事实：… [n]
推断：…

## 结论与建议
建议：…（条件、权衡、下一步核验）

## 局限与未解
```

## D. 知乎协同研究工作包范式（取证仍统一走 `argo_research`）

以下三类高频问题可直接套用对应工作包模板；`zhihu_questions` 与 `zhihu_kb` 均属 `explicit_only` 源，仅在明确写入 `priority_sources` 或显式传 `--engine` 时触发（拉取某问题下的多方回答摘要传 `"q:<问题ID>"`，避免裸 URL 触发 `known-url` 拦截）：

### 1. 选型对账型（官方账面 × 社区实测 × 私有约束）

```json
[
  {"id": "wp-kb", "question": "用户或团队既有哪些硬约束与预算红线", "query": "scope:personal 选型 预算 约束", "priority_sources": ["zhihu_kb"]},
  {"id": "wp-spec", "question": "官方规格、国标或一手技术文档怎么界定关键指标", "query": "<对象> 官方参数 规格 标准", "priority_sources": ["zhihu_global", "bocha"]},
  {"id": "wp-field", "question": "过来人实测踩坑、长期衰减与隐性成本是什么", "query": "<对象> 实测 踩坑 后悔 缺点", "priority_sources": ["zhihu", "zhihu_questions"]}
]
```

### 2. 观点交锋型（母问题锚定 → 正反切面拆解）

```json
[
  {"id": "wp-hub", "question": "社区讨论最集中的母问题与争议焦点是什么", "query": "<议题>", "priority_sources": ["zhihu_questions", "zhihu_hot"]},
  {"id": "wp-facts", "question": "截至当前的一手客观进展与基础数据是什么", "query": "<议题> 官方 进展 数据", "priority_sources": ["zhihu_global", "deepseek_search"], "depends_on": ["wp-hub"]},
  {"id": "wp-procon", "question": "支持方与反对方的算账口径与前提条件分别是什么", "query": "<议题> 收益 成本 争议", "priority_sources": ["zhihu"], "depends_on": ["wp-hub"]}
]
```

### 3. 动态追踪型（两段式：先 `db:realtime` 锁定新事件 → 再深挖内行解读）

```json
[
  {"id": "wp-news", "question": "观察窗口内有哪些新发布、版本变化或关键事件", "query": "db:realtime <领域或产品>", "priority_sources": ["zhihu_global", "deepseek_search"]},
  {"id": "wp-deep", "question": "针对上述新变化，一线开发者或内行实测给出了什么反馈", "query": "<事件词> 实测 拆解 影响", "priority_sources": ["zhihu", "zhihu_questions"], "depends_on": ["wp-news"]}
]
```

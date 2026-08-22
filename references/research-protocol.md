# 深度研究协议

取证归 `argo_research`，判断归 Agent。不要把搜索摘要写成研究报告。

深度研究只走这一条路径。不要再装独立的「专业深度研究」skill。

## 两层产品

| 层 | 谁做 | 产物 |
|---|---|---|
| 取证 | `scripts/research.py` / `argo_research` | **dossier**：来源、覆盖、缺口、门禁、待核验 URL |
| 判断 | 读完本文件的 Agent | **判断稿**：事实 / 推断 / 建议 / 未知 分开写 |

机器输出的 `kind` 必须是 `dossier`。`key_findings` 是各工作包的检索头条，不是结论。

## Agent 流程

1. 立研究契约：决策、对象、时间窗、地域、口径、停止条件。意图会改结论时，先问一个最关键的问题。
2. 把核心问题拆成互斥的工作包。每个包是可验证主张，不是搜索扩词。定义或基线未定时，先做前置包。
3. 把工作包交给 `argo_research`（`--work-packages` / MCP `work_packages`）。有依赖就写 `depends_on`，机器按阶段取证，不一次并行。
4. 没有工作包时，机器只做扩词检索，仍返回 dossier，不当成「问题分解」。
5. 读 dossier 的 `quality_gate_results`。`passed=false` 时必须降级表述，禁止把低置信结论写成事实。
6. `fetch_required=true` 时先 `argo_fetch` / `--verify` 再下判断。社交帖只当叙事。
7. 按「问题—证据—解释—影响」写判断稿。重大数字保留时间、地域、单位、口径。冲突写原因，不靠「多数来源」。

## 工作包交接

```json
[
  {
    "id": "wp-def",
    "question": "固态电池的技术路线如何分类",
    "query": "固态电池 硫化物 氧化物 分类",
    "priority_sources": ["arxiv", "semantic_scholar"],
    "depends_on": []
  },
  {
    "id": "wp-risk",
    "question": "硫化物路线的量产瓶颈是什么",
    "depends_on": ["wp-def"]
  }
]
```

`question` 必填。`query` 缺省用 `question`。`depends_on` 缺省为空。回传必须能回答问题，禁止只回链接清单。

独立工作包可以并行；共享定义、口径或基线的包必须分阶段。不要用「至少五个维度」当并行门槛。

## 结论标签

| 标签 | 含义 |
|---|---|
| 事实 | 可回源核验的陈述或数据，就近给来源、时间和口径 |
| 推断 | 基于多项事实的解释，写清假设和置信度 |
| 建议 | 面向用户决策的下一步，写清条件和风险 |
| 未知 | 证据不足或冲突未解，写清最有效的补证动作 |

## 机器门禁（可判定）

`quality_gate_results` 是谓词结果，不是打印出来的空勾选。

| id | 失败条件 | 结论上限 |
|---|---|---|
| `no_sources` | 可用 URL 为 0 | low |
| `uncovered_dimensions` | 有工作包/扩词维度 `NOT_COVERED` | low |
| `fetch_required_unverified` | 高后果且未 `--verify` / 无 verify 记录 | low |
| `fact_conflicts` | `fact_alignment` 检出未处理冲突 | medium |
| `no_primary_sources` | 有 source_grades 但零一手命中 | medium |

过不了就降级，不要编造覆盖。topic profile 里的 `quality_gates` 字符串仍给 Agent 做判断稿自检，不替代本表。

## 禁止

- 把 snippet、营销文或二手转述写成事实
- 用一堆低质量来源冒充交叉验证
- 并行取证后不做集中综合
- 把 dossier 的 `key_findings` 直接当研究报告交付
- 另开第二套「深度研究」入口

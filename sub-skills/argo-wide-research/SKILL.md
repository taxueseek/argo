---
name: argo-wide-research
version: 2.15.0
description: 多轨并行深度研究与取证编排。当问题需要多视角对比、跨语言覆盖、引用来源账本、可核验的综合报告时使用；产出带质量门的取证报告，结论按证据封顶。适用于 wide research、多轨研究、对照研究、事实核查报告、争议问题核证。单代理取证用 argo research，日常一问用 argo search，均不是本技能的活。
---

# Argo Wide Research — 多轨编排取证

把一个复杂问题拆成互补轨道，并行派 worker 取证，汇总成带来源账本与质量门的综合报告。产出是**可核验的研究件**，不是判断稿。

## 与另两条通道的分工

- 日常一问 → `argo search`（单发）
- 单代理取证 → `argo research`（扩词/工作包 → dossier）
- 多视角、跨语言、要引用账本 → 本技能（多 worker 编排）

## 编排流程

### 1. 规划轨道

轨道之间**互补不重叠**，每轨一个独立视角。常用拆法：

- **按证据类型**：一手来源 / 量化数据 / 争议观点与反驳
- **按语言市场**：中文源轨 / 英文源轨（跨语言问题必拆，中文结论须有英文源交叉或标注单源）
- **按利益相关方**：官方口径 / 第三方评测 / 用户侧反馈

轨道可带 `depends_on` 声明依赖（被依赖轨进入后续阶段再跑），无依赖的默认并行。

### 2. 派发与执行（数字单源 protocol.md）

- DSH 宿主：直接走 `wide_research` 工具（runner 已固化 worker 白名单：只调 argo 原子命令 + 读文件，不会递归套娃）。
- 其余宿主：走 `scripts/wide_research.py stage/merge` 确定性管道（URL 归一去重、整轨门禁、conclusion_cap），worker 用通用/只读子代理并显式约束其只调 `argo search`、`argo fetch` 与读文件工具。
- 人工编排仅作两管都不可用时的兜底。轨道数、并发 worker 数、每轨信源上限一律以 `references/research-protocol.md` 为准，本文不复述（防止 skill、协议与 DSH runner 三方数字漂移）。

给 worker 的输入三件：轨道描述（要什么证据）、约束（禁止只报叙事不报来源）、返回格式（见下）。

### 3. worker 返回结构（逐轨收齐）

每轨按此结构收：

```
trackId: 轨道 id
summary: 该轨发现的一句话概括
findings: 具体发现（每条带证据强度）
sources: 来源账本（仅 http(s) URL 入账；每条含 url + 用途）
disagreements: 与其他信息冲突之处（没有写空）
gaps: 该轨没查到/没覆盖的
```

### 4. 来源账本规则

- 仅 `http(s)` URL 入账，非 http(s) 链接一律不进账本
- 同一 URL 在多轨重复出现只记一次，标注覆盖轨
- SERP 跳转链（baidu/s、sogou/link）不算来源，须由 worker 先解析成正文 URL

### 5. 综合报告

结构：`executiveSummary`（一段）→ `report`（按主题不按轨道组织，轨道只是取证维度）→ `tracks`（各轨发现附录）→ `sources`（合并账本）→ `caveats`（局限）→ `unansweredQuestions`（没答上的）→ `warnings`。

## 质量门（结论封顶）

收齐全部轨道后自检，按三档封顶结论强度：

| 检查 | 判定 |
|---|---|
| 账本无可用来源 / 无轨完成 / 全轨失败（任一） | `passed=false`，结论封顶 **low**，只许陈述「未能取证」 |
| 有来源但存在警告（单源孤证、轨道间矛盾未消解、跨语言无交叉） | 结论封顶 **medium**，逐条声明不确定性 |
| 账本充分、多轨交叉印证、无未消解矛盾 | 结论封顶 **high**，可下明确结论 |

`passed=false` 或 medium 以下时**必须降级表述**：先核验账本来源再下结论，未经核验的推断标注「推断」。宁缺毋滥。

# Argo v2.14.0

## English Highlights (TL;DR)

- **Smart Self-Introspecting Parsers**: Zero-config parser inference eliminates manual Python parsing code for new search engines. New sources declare their field paths, and the engine infers extraction rules automatically.
- **Engine Capacity Expanded to 300 Sources**: Added 9 new engines including Mwmbl, Kaggle, HuggingFace Papers, grep.app (code search), OpenAIRE (academic research), Mojeek, Repology, DBpedia, and 4get, plus reviving ClinicalTrials, CoinGecko, and fxTwitter.
- **Local Search Enhancements**: Adaptive root discovery across heterogeneous machines, FTS5 index slimming gate, morphology & acronym clustering, and pure standard library fallback.
- **Badcase Ledger & Recurrence Gate**: Full attribution tracking for retrieval badcases with strict `Seen N×` gate to prevent regressions.

---

# Argo v2.14.0 发布说明（中文）

**版本**：2.14.0
**定位**：解析器层收口 + 引擎扩容 + 错题本机制落地。把「新引擎接入要手写解析」变成「声明字段即可推断」，源从 290 增到 300；同时把「失败可归因、经验可复用」从口号变成可执行机制。

---

## 这次更新有什么（通俗版）

### 1. 新引擎接入不再需要手写解析器

**以前**：接一个新源，除了写声明还要写一段 Python 解析器——把各站不同的返回结构转成 argo 统一的字段。同一个站改一次版，解析器就静默失效，用户看到的是「这个源返回 0 条」，分不清是没结果还是解析坏了。

**现在**：

- 新增解析器注册表（`scripts/engines_parsers.py`），`argo doctor` 能审计每个源的解析路径与健康度
- 落地「智能自省推断解析器」：声明字段路径后自动推断解析规则，零配置、不需要 `output_map`
- 解析器契约对齐：分值与清洗机制统一，消除各站各写一套的漂移
- `argo doctor` 补源审计，解析层失效能主动报出来而不是等用户遇到

### 2. 源从 290 增到 300

**新增 9 个源声明**：Mwmbl、Kaggle、HuggingFace Papers、grep.app（官方 MCP 代码检索）、OpenAIRE（学术聚合）、Mojeek、Repology、DBpedia、4get；另有 clinicaltrials / coingecko / fxtwitter 三源复活。

其中 8 个默认可用，4get 默认关（`enabled: false`，作为匿名搜索的备用通道）。

**收录 300 个源**，开箱可用 250 个，需自备密钥 32 个，需装后端工具 3 个，已停用 15 个。

### 3. 本机检索四项改进

- 消除特定机器路径硬编码，落地多环境自适应资产发现（换台机器不用再改代码）
- FTS5 索引瘦身门禁、词形归一与缩写增强、邻近行聚簇
- 跨平台纯标库保底（不依赖 FTS5 的环境也能跑）与公网/本机搜索隔离
- 成本可观测、候选可配、意图预判、条数上限

### 4. 本地意图信号收口

两级信号驱动与分面通道隔离落地；异常与私有标识符信号收口到单一真源。此前英文查询会白跑日文源（`cnii` 重复注册导致语言独占摘除从未生效）。

### 5. 错题本机制：失败可归因、经验可复用

这是本次最该说清的一件。argo 此前有一本「坏例账本」，把真实使用中发生的失败收集起来，但它只做到「记录」——同一类问题反复出现，靠人回想「这个是不是以前犯过」。

**现在补了三层**：

- **归因**：每条坏例入账时按证据判一个根因（12 类，从「路由选错源」到「上游改版」），判不出就标「未归因」而不是硬猜
- **复发判定**：同一根因跨 3 个不同日期出现，就触发「该挪进硬规则」的信号——事不过三。活网排名漂移不算（那不是 argo 的问题）
- **导出**：一条命令导出错题本清单，可直接读

**为什么这件事重要**：外部项目（magpie）的实践显示，全 AI 闭环里最值钱的不是「AI 能写代码」，而是**犯过的错写下来、下次启动一定读到、犯满三次就升成硬规则**。argo 此前缺的正是这一环。

### 6. 提交检查清单与 Agent 路由表

- `AGENTS.md`：仓库根新增 agent 路由表，只写「干什么活之前读哪份文档」与「哪些决定不可回退」，不复制规则本身
- `docs/COMMIT_STANDARD.md`：提交信息格式唯一真源。高风险提交（新增引擎、改路由、改状态词表、动发版或依赖链）必须写「跑了什么 / 在哪个 commit 上跑的 / 没跑什么」
- **为什么加这个**：argo 的提交历史里真实发生过两起「过了 pre-commit 门禁却在全量回归里红」的事故——跑了的门全绿，没跑的那条门没人知道它该跑

### 7. 状态机收口

`engine_states.py` 从 22 台扩到 27 台状态机，隐式状态机普查补齐 3 台（恢复链等级、结论上限、验证结论），补登坏例根因（第 27 台）。新增机械门禁 `test_state_literal_drift.py`：AST 扫描代码，已登记的状态值再退回裸字面量即红。

---

## 验证

- `bash scripts/pre-commit` 退出码 0
- 全量 pytest：**5200 passed, 43 skipped, 62 subtests passed**，0 失败（4 分 53 秒）
- `python3 scripts/release.py check` 全绿
- 新增契约用例：坏例归因与 Seen N× 门（9 条，含 4 条回退测试）、提交清单检查（40 条）、AGENTS.md 路由一致性（6 条）
- **三处新判定都做了回退验证**：把旧实现放回去，新测试必须挂。「测试存在」不等于「测试有效」

## 兼容性

- 无破坏性变更，v2.13.0 的配置与用法继续有效
- 解析器层是新增能力，既有源的解析路径不变
- 坏例账本的老数据库自动补列（新增 `root_cause` 字段），不重建、不丢历史记录
- `AGENTS.md` 与 `docs/COMMIT_STANDARD.md` 是新增文档，不影响运行时

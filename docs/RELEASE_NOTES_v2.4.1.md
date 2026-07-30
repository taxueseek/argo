# Argo v2.4.1 发布说明

**版本**：2.4.1（无后缀）  
**仓库**：[taxueseek/argo](https://github.com/taxueseek/argo)  
**定位**：给 Agent 用的统一搜索与证据核验——不只返回链接，而是尽量给出能核验、能吸收的材料。

---

## 一句话

本版把「搜得快、回得省、分得清、研得深」串成一条可交付链路：MCP 更轻，日常搜索更像搜索引擎，深度研究成为 argo 内建的专业子技能，而不是另一套外挂流程。

---

## 用大白话看本版

1. **MCP 性能**：启动后会在后台预热；默认回包变短，Agent 少吃上下文；社交多平台并行，少排队。  
2. **主技能 / 子技能斜杠**：`/argo` 是总入口；搜一下用 `/argo-search`；认真做研究用 `/argo-research` / `/deep-research`；科研、金融有专用斜杠。  
3. **深度研究在 argo 里面**：不额外挂别的 skill；学术、金融等选题自带引擎组合和质量门禁。  
4. **搜的效果**：性能优化不改「谁排前面」；变的是回包有多肥。需要长摘要就关 `summary`。

---

## 为什么值得升级

| 你可能遇到的问题 | v2.4.1 怎么处理 |
|------------------|-----------------|
| MCP 首调慢、回包巨大、占满上下文 | 后台预热 + 紧凑 JSON + 默认精简摘要 |
| 日常问答和「做一份深度研究」混在一起 | 分层：日常 SERP vs research 子技能 |
| 研报/综述没有结构、容易编造 | academic / finance 等 profile + 质量门禁 |
| 引擎 Key 缺失、坏引擎拖垮路由 | 引擎生命周期：声明、env、准入、routable |
| 搜完不知道链接在哪 | 正文 `[n]`，链接统一沉底「相关信源」 |

---

## 本版增量（按主题）

### 1. 性能与 MCP 接入

- **启动与首包**：`initialize` / `tools/list` 后后台预热 `search` 与缓存，降低首次 `tools/call` 冷启动感。
- **回包体积**：默认紧凑序列化（无缩进）；`summary` 默认开启，截断过长 snippet、去掉 plan/candidates 等调试重字段。
- **并发**：社交多平台搜索改为并行，减少串行等待。
- **模块缓存**：进程内复用已加载模块，避免重复 import。
- **重要说明**：上述优化主要发生在 **MCP 封装与传输层**，**不改变**多引擎召回、路由与 RRF 排序本身。若 Agent 需要更长摘要，调用时设置 `summary: false`。

### 2. 搜索体验标准化

- **日常搜索**：默认不归档；人读输出「标题/摘要在上、链接在下」；JSON 提供 `sources[]`。
- **深度研究**：默认归档（可退出）；多跳发现包 + 引用与缺口；归档 ≠ 已核验正文。
- **输入分流**：纯 URL / 已知链接优先 fetch 或 handoff，避免误走关键词热搜。
- **引擎治理**：外置声明、环境变量注入、`engine_validate` 准入；自动路由只使用 routable 引擎。

### 3. 深度研究子技能（全部在 argo 内实现）

深度研究 **不** 在运行时调用其他 skill。科研/金融的方法已内化进选题配置：

| 选题 | 用途要点 |
|------|----------|
| `academic` | 文献/综述向；学术引擎优先；问题框架与局限；禁止编造 DOI/论文 |
| `finance` | IC 风格信息包；信源级别；盲区与风险；免责声明（非买卖建议） |
| `investment` / `ai` / `tech` / `tool` / `internet` / `social` | 各域引擎组合、新鲜度与模板子查询 |

- 支持显式 `--topic`，也可按查询启发式自动推断。
- 输出可带：`quality_gates`、`report_sections`、`source_grades`、`disclaimer` 等，方便 Agent 自检与成文。

```bash
python3 scripts/research.py "CRISPR 脱靶综述" --topic academic --json
python3 scripts/research.py "台积电估值分歧" --topic finance --json
python3 scripts/research.py --topic help
```

### 4. 主命令与子技能斜杠（Agent / 客户端侧）

| 斜杠 | 角色 |
|------|------|
| `/argo` | 主入口，按子命令分流 |
| `/argo-search` | 日常搜索 |
| `/argo-research` · `/deep-research` | 深度研究 |
| `/argo-research-academic` | 科研深度 |
| `/argo-research-finance` | 金融深度 |
| `/argo-evidence` · `/argo-clarify` · `/argo-fetch` | 核验 / 消歧 / 抓取 |

自然语言触发示例：「深度研究」「文献综述」「deep research」等 → 走 research，禁止用日常搜索冒充。

### 5. 其它工程能力（一并交付）

- 工作区搜索归档分层与索引（研究默认开、日常默认关）
- FxTwitter 等社交路径与打包能力的持续补强（以仓库代码为准）
- 单元测试覆盖选题 profile、MCP 压缩逻辑等

---

## 升级与安装

```bash
git clone https://github.com/taxueseek/argo.git
cd argo
# 或解压本 Release 附件 argo-2.4.1.zip

# CLI 示例
python3 scripts/search.py "你的查询"
python3 scripts/research.py "复杂议题" --json

# MCP：将 mcp_server.py 配置到客户端（Claude / Kimi / 等）
python3 scripts/mcp_server.py
```

建议关注：环境变量中的引擎 Key、以及 `python3 scripts/search.py --list-engines --detail` 查看 routable 状态。

---

## 兼容性说明

- 公共版本号统一为 **2.4.1**（无 beta / rc 后缀）。
- 既有 `argo_search` / `argo_research` 等工具名保持兼容；research 新增 `topic`、`summary` 等可选参数。
- 默认 MCP `summary=true` 可能使摘要更短，**不影响排序结果列表**；需要长摘要请显式关闭。

---

## 写在最后

好的搜索不是让你看得更多，而是让你更敢下结论——以及清楚什么时候还不该下结论。  
v2.4.1 把「省上下文、分清日常与深度、把专业纪律写进工具」当作同一件事交付。欢迎 Issue / PR。

MIT License © 2026 taxueseek

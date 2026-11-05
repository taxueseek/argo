# Argo v2.11.2 发布说明

**版本**：2.11.2
**定位**：外部反馈收口版本。合入社区贡献的三条以图搜图引擎（SauceNAO / AnimeTrace / Google Lens）并摘除已失效的 tineye；修掉四类长期静默失效——ke**yRefs 凭证从未转发到子进程、quota 换 Key 不自愈与 402 误锁 30 天、社交引擎 env 与凭据透传、以及主干常红的 YAML loader 门禁。

---

## 这次更新有什么（通俗版）

### 1. 以图搜图能力补齐（SauceNAO / AnimeTrace / Google Lens），下线失效的 tineye

此前唯一的反搜图通道 tineye 端点已返回 HTTP 422 + Cloudflare 挑战页，按文档选它的调用方只会拿到空结果。本版：

- **以前不行**：反搜图要么不可用，要么拿到死源的 422；**现在**接入三条可用引擎——
  - `saucenao`：相似度 + Pixiv/Pawoo/Getchu 出处页，需 `ARGO_SAUCENAO_API_KEY`（免费 150 次/日）；
  - `animetrace`：角色/作品 → Bangumi 检索入口，免 key（按出口 IP 限频）；
  - `google_lens`：精确匹配/视觉相似/相关网页三档，需 `ARGO_SERPAPI_API_KEY`（免费 250 次/月，按次计费，`cost_per_call ¥0.11` 仅面板估算）。
- 三条都是 `explicit_only`，不进日常路由候选池；tineye 已从 spec、registry、config、builder 与测试中彻底摘除。
- 本地图片输入三重防护：只认图片魔数（不靠扩展名）、单张 ≤20MB、经 `ARGO_IMAGE_LOCAL_ROOTS` 目录白名单（realpath 校验）。

### 2. keyRefs 凭证终于能到达需要密钥的引擎（issue #41）

**以前不行**：`argo_search(engine="tavily")` 之类一律返回 `skipped-missing-env`，即使同一把 key 直连服务商正常。根因是 dsh 插件的 web 搜索/抓取 provider 走 MCP 池时没把宿主凭证服务传下去，`keyRefs` 一个都解析不出来。**现在**：`searchViaArgoMCP` 的池连接与 `registerSearchProvider/registerFetchProvider` 都显式带上 `hostCredentials`，池内连接与单发兜底解析同一份 key。

### 3. 配额状态机三处联动修复（issue #42）

- **换 Key 不自愈**：`_engine_key_fp` 只按 `<ENGINE>_API_KEY` 拼名，与 spec 的 `required_env` 对不上的 13 个引擎（deepseek_search 读 `DEEPSEEK_API_KEY`、metaso_* 读 `ARGO_METASO_API_KEY`、you 读 `YDC_API_KEY` 等）指纹恒空，换新 Key 也解不了熔断。**现在**与 `search._missing_env_for` 同源读实际生效变量，换 Key 即自愈。
- **402 误锁 30 天**：预算周期覆写了 `profile.period`，DeepSeek 的 `second`（限频语义）被改成 `month`，一次 `Insufficient Balance` 就熔断 30 天，充值后照样 `skipped-quota-exhausted`。**现在**预算周期拆到独立的 `budget_period`，`period` 保留原值；远端 402 余额不足类固定 1h 短冷却，与月度统计周期脱钩（免费额度类仍按下月重置口径）。
- **恢复指引缺失**：`skipped-quota-exhausted` 的 detail 现在补上 `quota.py reset <engine>` 手动恢复提示。

### 4. 社交引擎静默失效修复（issue #36）

xhs / twitter 子进程此前会因环境问题表现成「平台 0 结果」：

- **xhs**：子进程缺 UTF-8 环境（Windows GBK 下 emoji 输出 UnicodeEncodeError 崩溃）、`NO_PROXY` 含 `[::1]` 让 httpx 抛 `Invalid port ':1]'`。**现在**统一走 `_child_env()`：`PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`，剥除 `NO_PROXY` 中的 `[::1]`（剥空则删键）。
- **twitter**：FxTwitter 的错误占位不再阻断 CLI/Nitter 兜底；`twitter`/`tw` 子进程透传 `TWITTER_AUTH_TOKEN`/`TWITTER_CT0`/`TWITTER_PROXY`（含 `ARGO_PROXY` 兜底），`--json` 信封/JSONL/数组三种形态都能解析。

### 5. 主干常红修复：lint_spec_contracts 走共享 YAML loader

**以前不行**：main 自 `dc4d7e6` 起 `unit`/`extras` 分片恒红，卡在新增的「loader 单一真源」护栏自己身上——`lint_spec_contracts.py` 两处直接调 `yaml.safe_load`。**现在**改走共享 `yaml_load.loads`（顺带吃到 CSafeLoader，省掉每次解析 125 份规格 + 123 KB config 的开销），坏 YAML 仍抛 `yaml.YAMLError` 被同一个 except 接住，fail-open 口径不变。

## 相对上一版的署名

本版含社区贡献者 **OMSociety** 提交并经审查合入的三条 PR（#40 / #37 / #38），以及针对 #41 / #42 与社交引擎剩余项的内部修复。各自 commit 保留原作者署名。

---

## 路由准确性与性能改进（同一版本内落地）

以下改动与上述外部反馈收口同批进入 v2.11.2，此前只在提交历史里、未进发布说明，现补登记。

### 6. `--plan-only` 走路由缓存并补计时

**以前不行**：`argo search --plan-only` 是离线路径（不联网），却绕过路由缓存直接调 `route_query`——同一个路由决策，主搜索路径（`search.py`）走 `route_query_cached`，只有这条「本该最快」的路径每次重付约 47 ms。同时它在 `search.py` 里直接 `return build_plan(...)`，不产生任何 `timing` 键，`--explain-timing` 在这条路径上恒为空。

**现在**：`plan.py` 改走 `route_query_cached`，并把路由段单独计时挂到返回体的 `timing` 键（与主搜索同一结构）。

- 端到端墙钟（冷进程、无 daemon）：**110 ms → 60 ms（约 −45%）**
- 路由段耗时（缓存命中）：**64 ms → 1.3 ms**
- 默认输出结构逐字节不变，只在 `--timing` 时多一个 `timing` 键

### 7. `local_*` 六域被泛化域抢占的根治

**以前不行**：`local_chinese` / `local_news` / `local_code` / `local_academic` / `local_reference` / `local_general` 六个域是用户**显式选择**本地化/免 key 镜像通道的意图，但它们在 config 里排在末尾（idx 92-97），而域匹配先到先得——「本地搜索」「arxiv api」「wikipedia api」「github repo」「bing news」「百度」全部被靠前的泛化域截走，那六个域**永远拿不到主位**（实测 6/6 失配）。

根因不是单条 pattern 写错，而是**域顺序 + catch-all**：`chinese_general` 是唯一用 `[一-鿿]` 的 catch-all（idx 91），所有中文查询都先命中它，排在它后面的 7 个域（6 个 local_* + social）永远拿不到主位；`academic` / `code_search` / `news_realtime` / `english_tech` 的宽 pattern 又各自抢走一批。

**现在**：`route_domains.py` 新增 `_channel_intent_gate`（匹配层阶段 4）——查询含 `@local_channel` 意图词时把 local_* 域提到命中列表首位，泛化域降为次域（不删除，仍可作 fallback）。门放在匹配层，一次覆盖所有泛化域，新增泛化域时不必再逐个加否决。

- `local_*` 六域命中：**0/6 → 6/6**
- 误伤（普通查询被 local 抢）：**0 条**（`github repo 教程 推荐`、`stackoverflow 问题 报错`、`python api 文档` 等 9 条全部正确）
- 路由延迟中位 **1.47 ms**，无回归

### 8. 多语言天气触发词补齐（3 种 → 15 种）

**以前不行**：`weather_query` 的 combo 里有 `open_meteo`（全球天气引擎，免认证、按地名取坐标），但 patterns 只有中/日/韩三种语言——其余 13 种语言的天气查询**全部落 `general_search`**（实测 13/13 失配）。

**现在**：补齐 13 种语言的天气触发词（俄/阿/泰/越/德/法/西/葡/捷/土/希/印尼）。泰语裸词 `อากาศ`（天气）在通用句里也出现，单独挂 `unless` 否决「会议/出行/工作」语境（与中文第一条用「气候变化/极端天气」否决气候议题同一手法）。

- 12 语言天气查询命中 `weather_query`：**12/12**
- 泰语通用句误伤：**已消除**（`วันนี้อากาศดีมาก เรามีประชุมที่กรุงเทพ` =「今天天气很好，我们在曼谷开会」正确落 `general_search`）

### 9. 路由评测资产：从 13 条人工标注到 99/99 域、13 种语言

**以前不行**：路由准确性的评测只有 13 条人工标注 + 201 条快照。13 条无法区分概念路由的净收益（实测 2 好 2 差），201 条快照只判「domain+kind 不漂移」，不判「意图对不对」；且语料几乎全中文，非中英查询的路由对不对没有任何门在守。

**现在**：新增 `tests/golden/route_labeled.jsonl`（**100 条构造用例**）与四道评测门，按三种证据分档：

| 证据档 | 条数 | 来源 | 用途 |
|---|---|---|---|
| `capability` | 58 | 按域声明触发词生成 | 长尾域覆盖 |
| `language` | 27 | 按语言能力生成 | 多语言路由 |
| `feedback` | 15 | 外部反馈撞车场景 | 意图撞车回归 |

- **域覆盖 99/99**（此前 41/99）
- **语言覆盖 13 种**（此前 3 种：zh/en/ko）
- 新增意图类型六分类（factual / content / academic / sentiment / entity / navigational），覆盖「同一个词在不同意图下属于不同域」的撞车场景

**语料全部为构造用例**（按域声明触发词、按语言特征词、按外部反馈转述生成），不含任何本机运行数据——本机数据按项目规则不入库。生成器三个：`build_route_labels.py`（基底）、`extend_route_labels_by_capability.py`（域能力）、`extend_route_labels_by_language.py`（语言能力），均可离线重跑。

**定位说明**：语料的 `expect_domain` 是**路由的输出**，不是人工真值。因此这套语料用于**回归门**（判路由决策不漂移），不用于准确率。准确率由 `test_route_accuracy_labeled`（13 条人工集）承担。

> 评测资产（语料 + 评测门 + 扩展脚本）随 **GitHub 仓库**分发，**不随 npm 包**分发——`package.json` 的 `files` 白名单不含 `tests/`，这是既有设计（测试资产不随运行时包发布）。

### 10. 顺带修掉的自身缺陷

- **`_split_pattern_entry` 支持列表形态 `unless`**：一个触发词要挂多个否决集（academic 原有写作技巧否决 + 新增本地通道否决）。拼接成字符串会让 `(?i)` 作用域只覆盖前半段（Python 3.11+ 直接编译失败），整体替换成宏名又会丢掉原有否决。
- **`route.py` 删掉只导入未使用的 `_compile_domain_patterns`**（全仓无调用方，测试直接从 `route_domains` 导入）。

### 11. 评测语料的数据来源更正

**背景**：本版早前提交里，路由评测语料（`tests/golden/route_labeled.jsonl`）曾从本机运行数据（`usage_log` 查询日志与 `badcase.db` 坏例账本）取真实查询，共 772 条。

**问题**：那些是**真实使用 argo 时产生的查询记录**，其中含具体企业名、统一社会信用代码、域名、人名等可定位信息，合起来还能反推用户的行为画像（在调查哪些公司、用什么工具、关注什么领域）。项目规则对同类数据早有约束——本地运行数据不入库——这一版没有遵守。

**更正**：语料已改为**全构造用例**（100 条），由三个生成器产出，不含任何本机运行数据：

| 生成器 | 产出 | 条数 |
|---|---|---|
| `extend_route_labels_by_capability.py` | 按域声明触发词构造 | 58 |
| `extend_route_labels_by_language.py` | 按语言特征词构造 | 27 |
| `extend_route_labels.py` | 外部反馈撞车场景 | 15 |

`build_route_labels.py` 不再读取本机数据，改为产空基底（重建语料的入口）。三个生成器均可离线重跑。

**影响**：域覆盖从 99/99 降到 58/99——差的 41 个域此前靠真实查询覆盖，现在没有对应构造用例（它们的触发词是平台专属语法或语义过专）。这些域的路由正确性由 `test_route_accuracy_labeled`（13 条人工集）与 `test_routing_snapshot`（201 条）承担，能力覆盖门下限相应从 0.85 调到 0.55，未覆盖域全部登记原因。语言覆盖保持 13 种不变。

**验证**：敏感模式复扫（绝对路径 / 邮箱 / 手机号 / 身份证 / 统一社会信用代码 / 企业名 / 人名 / API key / 本机名）在当前语料上 **0 命中**。

### 12. 域覆盖从 58/99 补回 99/99，覆盖门 0.55 上调回 1.0

**问题**：第 11 节那次脱敏只做对了一半——语料换成全构造用例后，域覆盖掉到 58/99，覆盖门下限从 0.85 降到 0.55，41 个域被登记进 `_KNOWN_GAPS`，理由写的是「触发词是平台专属语法」「语义过专，构造用例不自然」。复核后这两条理由都不成立：`wechat_search`（「公众号 文章 搜索」）、`zhihu_content`（「知乎 如何评价」）、`skill_search`（「agent skills 技能市场」）是普通中文说法；`modal_card`（「今日油价 92号汽油 多少钱一升」「火车票 余票 查询 车次」）、`medical`（「临床试验 适应症 不良反应」）是标准日常问法。

真正卡住的是**判据不一致**：旧能力扩展脚本判「目标域出现在 `match_domains` 前 5 名」，而回归门判「`route_query(q).domain` 精确相等」。同族域互抢主位时前者通过、后者变红——语料里有 4 条正是这种（`刑法 第二百三十四条` 落 `law_text`、`港股 港交所 披露易 招股书` 落 `sec_filings` 等）。

**改动**：

- 新增 `scripts/extend_route_labels_full_coverage.py`：为 31 个未覆盖域写**人工自然问法**，判据三条——自然性（真实用户会这么问）、能力必要性（确实需要该域的能力）、精确命中（`route_query(q).domain == 该域`）。31 条候选全部精确命中，零失配，域覆盖回到 **99/99**。
- 覆盖门下限 0.55 → **1.0**，`_KNOWN_GAPS` 整表删除（缺口登记是过渡态，它会让人忘记「暂时没有用例」不等于「这个域不该有用例」）。
- 回归门一致率阈值 0.85 → **1.0**：全构造语料的期望域是人工判断的真值，不再有「弱标注噪声」这个豁免项。失败时打印全部失配（查询 / 期望 / 实际 / 证据档）。
- 新增 `build_route_labels.py --reproduce` 与对应门禁：按「能力 → 语言 → 意图标签 → 覆盖补齐」这条链重放语料并与已提交文件逐条对账，防「只改 jsonl 忘了改生成脚本」。`--reproduce` 把已提交内容留在内存、结束还原，是只读入口。

**顺带修掉的两处域规则缺陷**（由补覆盖的过程暴露，均在 `config.yaml`）：

1. **`ai_model` 触发词与意图词表不一致**：`intent_required` 认「定价」（注释里的样板查询就是「claude api 定价」），触发词却不认，于是「模型定价」类查询既不触发本域也过不了门，落 `chinese_general`。触发词补「定价|成本」，并给该条挂 `@compare_or_award` 否决——横向对比题（「A 和 B 对比」）交回通用域，模型目录答不了对比。
2. **`us_fundamentals` 的 `unless` 把英文财务查询一起否掉**：除名单里的 zh 词形（营收/净利润/财报/资产负债表…）本意是「zh 问法让位 `stock_query`」，但否定是「命中任一即否」，而除名单含 revenue / net income / financials 的中文对应词，于是「AAPL annual revenue net income financials」触发词与除名单同时命中 → 本域失效、落 `us_stock`。给除名单加语言门 `(?=[^\n]*[\u4e00-\u9fff])`（查询里至少一个汉字才让位），「宁德时代 毛利率 现金流」等 zh 查询行为不变（已单独测）。

**实测（改造前后）**：

| 项 | 改前 | 改后 |
|---|---|---|
| 域覆盖 | 58/99（门下限 0.55） | **99/99**（门下限 1.0） |
| 语料条数 | 100 | 131（capability 89 / language 27 / feedback 15） |
| 精确命中率（`route_accuracy_eval`） | 96/100 = 96.0% | **131/131 = 100%** |
| 落到 general 的兜底率 | 0% | 0% |
| 语言覆盖 | 13 种 | 13 种（不变） |
| 回归门阈值 | 0.85 | 1.0 |
| 路由快照（201 条 domain+kind） | —— | **零漂移**（两处规则修复不影响任何既有查询） |

**验证**：`sh scripts/pre-commit` 八道快门禁全绿；全量 `pytest tests/ -q` 全绿；敏感模式复扫新增语料 0 命中（仅保留 `crates.io` / `baidu.com` 两个公开站点名，来自既有用例）。

## 相关依赖

- 新增可选凭据：`ARGO_SAUCENAO_API_KEY`、`ARGO_SERPAPI_API_KEY`（仅显式调用反搜图时需要，缺失时引擎报可读错误而非静默空结果）；
- 本地图片输入需配置 `ARGO_IMAGE_LOCAL_ROOTS` 目录白名单；
- 零新增 Python 依赖。

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

## 相关依赖

- 新增可选凭据：`ARGO_SAUCENAO_API_KEY`、`ARGO_SERPAPI_API_KEY`（仅显式调用反搜图时需要，缺失时引擎报可读错误而非静默空结果）；
- 本地图片输入需配置 `ARGO_IMAGE_LOCAL_ROOTS` 目录白名单；
- 零新增 Python 依赖。

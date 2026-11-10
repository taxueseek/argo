# Argo 变更日志

> 单一时间线，按版本倒序。每版保留「定位 + 关键变更 + 默认关闭开关」三段，细节见提交历史与 `references/`。
> 源数口径以 `docs/ENGINE_CATALOG.md`（生成）与 `argo search --list-engines`（本机）为准。

## 未发布

**定位**：Release 资产瘦身。调研同类项目（psf/requests、pandas、astral-sh/uv）的 Release 资产后，把三处冗余或不一致的产入口径收掉。

**关键变更**：
- **去掉运行时源码包**：`build_release_assets.sh` 不再产出 `argo-v<ver>.tar.gz`。GitHub 每个 Release 页面自动生成 Source code (tar.gz/zip)，「要源码」的需求已被平台覆盖；同类项目都是 wheel + sdist 两件套，没有一个手动传运行时源码包。那个包服务的需求没有安装路径能触达，保留它意味着每版多约 2 MB 下载、多一处版本号同步、多一个可能与 sdist 不一致的副本
- **sdist 排除 tests/**：`MANIFEST.in` 加 `recursive-exclude tests *`。tests/ 解包后约 4 MB、占 sdist 三分之一，而 sdist 服务「从源码构建」不服务「跑测试」——想跑测试的人用 `git clone`。sdist 从 3.1 MB 降到 2.2 MB
- **校验和改为逐资产独立 `.sha256`**：不再产出合并的 `SHA256SUMS.txt`（照 astral-sh/uv 格式）。合并清单是单点，损坏则所有校验失效；独立文件可以边下边校，一个损坏不影响其余
- **新增 `tests/test_release_assets_contract.py`** 锁住以上三条，三处都做了回退验证（把旧写法加回来，对应用例必须挂）。首次写的断言漏了「带路径前缀」的变体写法，回退时没抓到，已改为正则匹配
- 资产总下载从 7.4 MB 降到约 4.7 MB

**默认关闭**：无新增

---

## v2.14.0

**定位**：解析器层收口 + 引擎扩容 + 错题本机制落地。把「新引擎接入要手写解析」变成「声明字段即可推断」，源从 290 增到 300；把「失败可归因、经验可复用」从口号变成可执行机制。

**关键变更**：
- 修掉恢复链 L0 门控失效：`max_level="L0"` 与任何未登记值此前都通过 `_TIER.get(..., 3)` 落到最高档，放行 L3 换引擎与 L4 跨语言；现在等级词表与成本序收进 `engine_states.py`，未登记值 fail-closed 返回空计划。补 `tests/test_p0_v25.py` 两条红转绿契约。
- 抓取降级链词表与代码实际值对齐：登记 `http_mobile` / `tls_spoof` / `jina_reader` / `md_variant` / `llms_txt` / `robots_blocked` / `http_direct` / `full` 八个此前未进词表的值，修正词表写 `tls` 而代码写 `tls_spoof` 的失配；`fetch_v3` 与四个抓取侧脚本改引用常量，`is_degraded_fetch_method` 的判据集合也收进词表。
- 验证结论状态机收口：`pass/fail/skipped/error` 与阶段名 `health/quality` 由 `engine_validate.py`（产出）、`engine_admission.py`（落库）共用同一常量，消除 `skipped` 改名漏一处即静默拉黑引擎并落库的隐患；结论上限 `conclusion_cap` 三态由 Python 三处与 JS `wide-research` 共用同一词表。
- 跨侧状态词表投影：`scripts/gen_native_tools.py` 除 `NATIVE_TOOLS` 外新增导出 `STATES`（plugin / daemon / conclusionCap），与 `engine_states.py` 逐值对拍；`py_bridge.py` 的 CLI 回退分支不再写死 `running/stopped`。此前改 Python 词表不会让任何 JS 或桥侧测试变红。
- 新增机械门禁 `tests/test_state_literal_drift.py`：AST 扫 `scripts/*.py` 状态字段上下文，已登记状态值再退回裸字面量即红（含同名异义白名单与负向验证）。它与 `test_engine_states.py`、`test_native_tools_sync.py`、`test_zcode_bridge_daemon_state.py` 一并挂进 `scripts/pre-commit` 快门禁——此前改 `engine_states.py` 不跑全量 pytest 就能提交。
- 文档：`references/architecture.md` 的状态机登记表从 22 台更新到 25 台（22 台 + 3 台新登记），补三道门禁的说明。

- **坏例账本加归因层**：`scripts/badcase_ledger.py` 每条坏例入账时按证据判 `root_cause`（词表为 `scripts/badcase_causes.py`，12 个值；由该模块自行注册进 `engine_states.STATE_REGISTRY` 成为第 27 台状态机），判不出归 `unattributed` 不硬猜。新增 `root_cause_stats()` 按根因聚合出 `seen_days`（在多少个不同日期出现过）与首末日期。老库自动补列不重建——历史坏例是证据，重建等于丢掉。
- **Seen N× 升级判定**：`check` 增加 `--promote-days`（默认 3），同一根因跨 3 个不同日期出现即非零退出并点名「该挪进硬规则」。判定与条数门相互独立，报告里分开列。`external_ranking`（活网排名漂移）排除在升级判定外——它不是 argo 的问题，算成复发会制造长期噪音（2026-10-28 issue #35 误报根因）。新增 `lessons` 子命令导出 LESSONS.md 格式清单。
- **提交检查清单契约**：新增 `docs/COMMIT_STANDARD.md`（提交信息格式唯一真源）与 `scripts/check_commit_report.py`。高风险提交（新增引擎、改路由、改状态词表、动发版或依赖链）须写「跑了 / 在 commit / 未跑」三段，`未跑: 无` 合法但整段省略不合法。挂进 pre-commit 时**只警告不硬拦**——hook 慢于 20s 就会被 `--no-verify` 绕过，一道会被绕过的硬门比没有门更坏。
- **AGENTS.md 路由表**：新增仓库根 `AGENTS.md`，只写路由不写规则。含「先读哪份」表、「不可回退的决定」清单（12 个默认关能力开关 + 已停用引擎 + 出口安全边界）、工程纪律结论、提交纪律与坏例复发处理。规则本身仍在各份被引用文档里，不复制第二份真源。
- **三道机械门禁**：`tests/test_badcase_ledger.py` 补 9 条（含 4 条回退测试）、新增 `tests/test_check_commit_report.py`（26 条）、`tests/test_agents_routing.py`（6 条）。三者一并挂进 `scripts/pre-commit`。AGENTS.md 的门禁会机械对拍「usage.md 的默认关开关是否都登记进不可回退清单」，漏登即红。
- **回退测试**：三处新判定都做了回退验证——把旧实现放回去，新测试必须挂（旧 `check()` 无 `recurrence_ok` 字段直接 KeyError；旧 `check_commit_report` 恒回 ok 导致 4 条断言失败；删掉 AGENTS.md 或抽空开关清单都会红）。「测试存在」不等于「测试有效」。

**默认关闭**：无新增

---

## v2.13.0

**定位**：文档分层收口 + 22 台状态机单点真源 + 调度尾宽修复。把 `docs/` 从 87 份精简到 26 份，把散落的状态词收进一个真源文件，修掉 deep 档 10 秒空等缺陷。

**关键变更**：
- 文档分层收口：删除 `docs/engines/` 60 份（与 `engines/specs/*.yaml` 真源重复）；18 份过程报告归档 `docs-local/archive/`（不进公开仓，保留本机可追溯性）；新增 `docs/CHANGELOG.md` 合并 23 份 RELEASE_NOTES；README 27 处死链改为指向 CHANGELOG
- 22 台状态机单点真源：`scripts/engine_states.py` 从 12 台扩到 22 台，34 个模块引用，各文件字面量残留从 19 处降到 0；修掉精排降级集合 4 处手抄导致的高危静默退化、`health_check --category` 必崩、daemon 状态靠文案反推、缓存命中路径无终态事件
- 调度尾宽修复：deep 档全量并行分支补 `tail_grace_s` 与 `tail_grace_fn`，中位 10,000ms → 2,324ms，条数不变（公开版同查询同样 10s 空等）
- 引擎声明单源：165 个引擎声明从 `config.yaml` 迁到 `engines/specs/*.yaml`，`config.py` 合并逻辑从双源覆盖变单向 specs→config，删 3523 行
- 依赖收口到 uv：`uv.lock`（3196 行钉版）+ `.python-version` + `pyproject.toml` 真源，新增 `test_dependency_source_gate.py` 机械对账漂移
- 图像源扩容：V&A（vam）与 Wallhaven 过质量门禁准入，met_museum 迁 v1.1 复活（源 288→290）

**默认关闭**：无新增

---

## v2.12.0

**定位**：统一设置中心首发；社区贡献 pixiv 正向搜图合入，内置 trace.moe 场景识别（源 286→288）；fetch 对微信公众号文章不再零处理；调度与路由算法内部优化。

**关键变更**：
- `argo settings` 零服务设置页与写回（密钥状态、客户端接入片段、开关现值）；`argo settings set/unset/list` 命令行直改，值走 stdin 不进 argv；`argo ui` 同义别名
- pixiv 双通道正向搜图（匿名 Web + App API），默认只交付全年龄结果；trace.moe 动画场景识别（免 key，100 次/小时）
- fetch 对公众号文章前置分流（host+path 精确匹配，r.jina.ai 代理 URL 不误分流）
- 调度与路由内部优化：对冲宽限窗、尾宽分层、查询信号与 TF-IDF 路由、场景桶收缩

**默认关闭**：`ARGO_PIXIV_ALLOW_R18`（R18 放行）、`ARGO_TRACEMOE_PROXY`（出口代理覆盖）

---

## v2.11.2

**定位**：外部反馈收口版本。合入三条以图搜图引擎并摘除已失效的 tineye；修掉四类长期静默失效——keyRefs 凭证从未转发到子进程、quota 换 Key 不自愈与 402 误锁 30 天、社交引擎 env 与凭据透传、以及主干常红的 YAML loader 门禁。

**关键变更**：
- 接入 SauceNAO / AnimeTrace / Google Lens 三条 `explicit_only` 反搜图引擎；tineye 彻底摘除
- dsh 插件 `searchViaArgoMCP` 池连接与 provider 注册显式带上 `hostCredentials`，凭证能到达需要密钥的引擎（#41）
- `_engine_key_fp` 与 `search._missing_env_for` 同源读实际生效变量，换 Key 即自愈；预算周期拆到独立 `budget_period`，`period` 保留原值；远端 402 余额不足类固定 1h 短冷却（#42）
- xhs / twitter 子进程统一走 `_child_env()`（UTF-8 环境、剥除 `NO_PROXY` 中的 `[::1]`）；twitter 透传 `TWITTER_AUTH_TOKEN` / `TWITTER_CT0` / `TWITTER_PROXY`（#36）
- `lint_spec_contracts` 改走共享 `yaml_load.loads`，解除主干常红
- `--plan-only` 走路由缓存并补计时（端到端 110ms→60ms）；`_channel_intent_gate` 让 local_* 六域命中 0/6→6/6；多语言天气触发词 3 种→15 种
- 路由评测资产：`tests/golden/route_labeled.jsonl` 100 条构造用例，域覆盖 99/99、语言覆盖 13 种

**默认关闭**：`ARGO_SAUCENAO_API_KEY` / `ARGO_SERPAPI_API_KEY`（仅显式反搜图时需要）

---

## v2.10.2

**定位**：语义路由三步走（P0-P1 相关性锚点加权 + P2 静态概念向量表）与路由准确率收口。标注定向 76.9%→100%，路由快照 61% 决策从 tfidf 打分换概念直判；熔断器工厂模块别名分裂修复；ZCode 面板三页版入库。

**关键变更**：
- `test_route_accuracy_labeled` 13 条人工标注集域定向与主源族定向双双 13/13
- `backends/engine_concept_vectors.json` + `concept_vectors.bin` 静态概念向量表入仓，`ARGO_CONCEPT_ROUTER=1` 开启（默认关）
- 熔断器工厂改调用期解析，模块别名分裂消除
- 摘要标签卫生 `snippet_clean.py` 与口径去重 `caliber_dedupe.py` 落地
- ZCode 面板三页版入库（看板/密钥/诊断三 tab，CSS 变量化 + 深色模式）
- conftest 新增 `ARGO_*` 环境变量每用例快照恢复层

**默认关闭**：`ARGO_CONCEPT_ROUTER`（概念语义路由）

---

## v2.10.1

**定位**：路由精度与边界收口、空链与双跑缺陷修复。集中收口 3 个社区反馈（Issue #29、#34、#35）：引入声明式商业意图否决宏消除医药商业误路由、接入免 Key 海外视频检索并归一化 YouTube URL、消除本机检索零命中双跑税与工具缺失静默、支持无外链 spec 与空占位符死链守卫、引擎状态自洽并清理孤儿文件、解耦夜间漂移门并杜绝基线自动滚降。

**关键变更**：
- `scripts/route_domains.py` 引入命名否决宏 `@commercial_market`，`protein_search` / `chem_search` / `medical` 统一接入 `unless`（#34.1）
- `video` 域与 `--video` 强制决策接入 `parallel_free`，自动附加 `site:youtube.com`；`url_canon.py` 追加 `vl` 与 `si` 追踪参数（#34.3）
- `local_seek.py` 修复零命中退出码处理，消除 100% 触发的子进程二次重跑税（近 800ms）（#34.4）
- spec 支持 `url_kind: none`；`_format_url_template` 空占位符返回空并清空非 HTTP 原生字段（#29）
- 未收录引擎正确返回 `status="unknown", enabled=False`；清理孤儿 `_cisa_kev.yaml` / `_osv.yaml`（#29、#34.6）
- `ci_drift_gate.py` 结果体积漂移降为纯信息项；nightly.yml 移除自动覆盖基线并直推 `main` 的步骤（#35）

**默认关闭**：无新增

---

## v2.10.0

**定位**：能力面扩张。三条主线：源从 272 增到 284（秘塔五档、Sciverse、NCPSSD、DSH 插件目录与技能索引）、视频检索与抓取一条线走通、抓取链重写前沿质控与礼貌节奏；另有一批交付质量改造：通用选源实测排序、DeepSeek 英文条件对冲、响应体统一解码、安装器 venv 化、发布一致性预检。

**关键变更**：
- 新增秘塔五档（metaso_scholar/doc/image/video/podcast，均 `explicit_only`）、Sciverse 题录档、NCPSSD 中文社科、DSH 插件目录三源、技能索引两源
- 新增 `video` 域（第 99 个域）；`--video` / `--min-duration` / `--max-duration`；B 站字幕与 YouTube timedtext、可选 ASR
- `scripts/crawl_frontier.py` + `crawl_policy.py`：同主机最小间隔 / 并发上限、429 指数退避、robots `Crawl-delay`
- `scripts/charset_decode.py` 三级解码（头声明 > meta 嗅探 > 猜测链），GBK / Big5 / Shift_JIS 不再乱码
- `scripts/general_pool.py` + `source_selection.py`：通用选源实测排序；英文通用查询接入 DeepSeek 条件对冲（¥0.005/次）
- 安装器 venv 化：`install.sh` / `install.ps1` 依赖安装改走 venv 主路，三个入口自动发现 `.venv`
- `scripts/release_check.py` 13 处版本声明一致性预检；新增 `server.json` MCP Registry 清单
- `wikipedia` 补 `output_map`（此前永远空结果）；`reddit` 迁 RSS

**默认关闭**：`ARGO_VIDEO_ASR_CMD`、`ARGO_PAID_HEDGE_DEFER`、`ARGO_GENERAL_FALLBACK_LEAD`、`ARGO_GENERAL_FALLBACK_LEGACY`、`ARGO_GENERAL_FALLBACK_STATIC`

---

## v2.9.9

**定位**：安全纵深 + 交付质量。三条主线：内容与网络出口的安全加固（高风险正文隔离、逐跳重定向校验、DNS 重绑定封堵）、调度与学习重构（早停弃置的慢源交付不再丢弃、场景分桶学习）、路由精度的六族真实场景扫描收口；另有 `argo engine add` 一键接入与 ZCode 插件包两个能力扩展。

**关键变更**：
- `risk_score ≥ 0.5` 的正文被隔离：清洗后正文替换原始正文、标 `content_quarantined`、不进证据分
- 重定向逐跳校验（`ARGO_MAX_REDIRECTS` 默认 5）；DNS 解析一次锁定给本次 TCP 连接，堵住 DNS 重绑定窗口
- 早停弃置的引擎转入后台收割（`ARGO_TAIL_COMPLETE_S` 默认 3 秒）；学习器记真实成绩
- 六族真实场景扫描收口学术/财务/企业/法律/医药误路由
- `argo engine add` 一键接引擎（写 spec→设 env→验证→同步→改 combo→补白名单→补语料）
- 场景分桶学习：自适应学习按 `(engine, domain, lang)` 分桶聚合
- ZCode 插件包入库；DSH 设置页新增路由观测卡片与工具推荐组合

**默认关闭**：`ARGO_TAIL_COMPLETE_S`（默认 3，0=关）、`ARGO_CONTENT_RISK_GATE`（默认开）、`ARGO_CONTENT_RISK_THRESHOLD`（默认 0.5）、`ARGO_MAX_REDIRECTS`（默认 5）

---

## v2.9.8

**定位**：路由精度审计与反馈闭环。把 v2.9.7 以来积累的分发、质量、重构改动一并收进本版，并集中修掉 issue #18 / #19 暴露的路由与夜间任务问题。

**关键变更**：
- `latest` 只有在 `latest news / on / about / in` 等明确新闻搭配下才触发实时新闻
- 人民网默认 feed、SERP 缓存页按 query token 覆盖率识别为「答非所问」，整页丢弃并触发恢复链
- `local_bing` 年份表垃圾页被覆盖率门拦下；`cross_validation` 改用 CJK 二元组覆盖率
- 审计并收窄 16 类误触发（CJK 构词成分、ASCII 子串、跨域同形、域缺口）
- `argo stats` 空日志也返回稳定字段形状（`None` 表示无样本）；夜间工作流按 `samples` 判断
- 补 53 个零用例域的路由用例，加「每域至少一条路由用例」门禁

**默认关闭**：无新增

---

## v2.9.0

**定位**：搜索源优化——新增 16 个源、替换部分质量不佳的源，净 237 → 253（免配置 198 → 219）；两个常青源修活；插件与 CLI 搜索能力首次完全持平（19 个 MCP 工具）；多语言小语种源 +14 并按语言自动配源；本地使用日志规范化（仅本机）并新增 `argo stats` 读出口；74 个功能开关成表可查。

**关键变更**：
- 新增 14 个小语种本地新闻源、CORE 开放获取全文源、iPlant 植物智、sov2ex 全文搜索、TinEye 反搜图、Bing RSS 稳定出口、Exa 免 key 匿名通道；PubMed 修复
- 百度与搜狗修活：引擎层逐条解析跳转链换真实 URL
- CLI 18 个命令与 MCP 19 个工具一一对应；一致性由门禁锁定
- 导入链 252ms → 35ms（−79%）；重排序 n=200 场景 33ms → 1.5ms
- 新增 `argo stats` 本地使用日志（四个 JSONL 流，单流自动回缩 1MB/2000 行，总开关 `ARGO_USAGE_LOG=0`）
- `references/usage.md` 升级：74 个 `ARGO_*` 开关按六类成表

**默认关闭**：`ARGO_USAGE_LOG=0`（本地使用日志总开关）

---

## v2.8.9

**定位**：搜索更快也更省——默认输出体积 −66%、重复查询整次耗时 −30%、慢网轮等待 −36%；拿到手的内容更完整——召回 +35%、抓取时延 −30%、正文结构保真 0/6 → 6/6；新增 5 个外媒与核查源（全部免密钥，源 232 → 237、领域 90 → 92）；修复六处「数字互相矛盾」的记账缺陷；新增离线重跑对比工具。

**关键变更**：
- 默认输出 15.5KB → 5.2KB（−66%）；`--envelope` 显式打开，`--archive` 自动保留
- 路由决策缓存：重复查询 168ms → 118ms（−30%）
- 串行查询主源 0.8 秒宽限窗补发备选源：8 条慢查询 21.0s → 13.4s
- 正文结构保留标题层级、列表、表格结构（HTML → Markdown），六类典型页面 0/6 → 6/6
- 新增 `guardian_rss` / `france24` / `dw_news` / `factcheck_org` / `full_fact` 五个免密钥源；下线死源 gdelt
- 修复六处记账缺陷：缓存命中率口径、部分成功被当失败、全引擎超时报「跑了 0 个引擎」、深度模式报出「用时超过总预算」、每次启动白付 15ms、DSH 插件连接池四处收口
- 新增 `scripts/replay_eval.py` 离线重跑对比工具（录制 → 重跑 → 对比）

**默认关闭**：`--envelope`（完整输出封套）、`ARGO_ROUTE_CACHE=0`（路由决策缓存）、`--no-timing`（阶段耗时账）

---

## v2.8.8

**定位**：修复两个真实用户报告的问题——按文档推荐名配置密钥后引擎静默失效（issue #12）、抓取链不走代理导致需代理站点必然失败（issue #13）；搜索源 218 → 232（免配置开箱 184 → 194）、领域 89 → 90；冷启动与热路径全面提速（命令冷启动 2.1s → 0.55s）；Windows / Linux / macOS 三平台惯例路径适配，支持 Python 3.9；新增「直答」与「网页观察」两个子命令。

**关键变更**：
- 16 处密钥读取统一走别名链，推荐名/旧名任一就绪都出结果，缺 key 显式报错；新增源码级一致性门禁（#12）
- 新增统一出口调度层：`ARGO_PROXY` / config 按域规则 / 标准代理环境变量三条路都生效，`NO_PROXY` 全程尊重（#13）
- 命令冷启动 2.1s → 0.55s；配置跨进程缓存 50–82ms → 16–18ms
- 搜索源 218 → 232：新增 `weibo_hot` / `douyin_hot` / `csdn` / `wallstreetcn` / `weather_cn` / `osv` / `cisa_kev` / `federal_register` / `unpaywall` / `opencitations` / `skillsmp` / `clawhub` / `parallel_free` / `seltz`
- 69 个引擎补归类，`web_general` 兜底占比 41% → 20%；新增 `security` 领域
- 新增 `argo answer` 直答（带引用合成答案）与 `argo watch` 网页观察模式
- 三平台惯例路径：Linux 走 XDG、Windows 走 `LOCALAPPDATA`/`APPDATA`、macOS 走 `~/Library`；新增 `argo paths` 自省命令

**默认关闭**：`ARGO_PROXY`（代理默认跟随标准环境变量）、`ARGO_FETCH_PARALLEL=0`（parallel_free 抓取级）

---

## v2.8.7

**定位**：搜索源 168 → 218（+50，其中 184 个免配置开箱可用），业务域 72 → 89；抓取链新增三条「拿正文」的直出通道；修掉一批真实场景里会空手而归、答非所问或整串失效的问题。

**关键变更**：
- 新增 50 个数据源：金融财经（finviz / seeking_alpha / sec_edgar / eastmoney / fred / worldbank / eurostat / nbs_stats / fx_rate / jin10 / em_global_news / ths_hot / cninfo）、法律法规（flk_law / egov_law / kor_law / gov_regulations）、标准合规（std_samr / openstd）、安全情报（nvd / crt_sh / deps_dev）、学术科研（openalex / europepmc / openreview / biorxiv / semantic_scholar / datacite / zenodo / google_patents / clinicaltrials / openfda / pubchem / uniprot / rcsb_pdb / gbif / obis）、新闻舆情（people_daily / google_news / gdelt / zhihu_hot）、生活文娱（douban_movie / bangumi / netease_music / met_museum / nasa_images / steam / zdic / wikisource / sspai / weread）、能源交通空间（energy_charts / carbon_intensity / aviation_weather / noaa_swpc / tle_mirror / gbfs_nyc / nhtsa_vpic / usda）、其他（polymarket / coingecko / huggingface / docker_hub / openverse / opencorporates / un_comtrade / who_don）
- 抓取链新增三条直出通道：站点根 `/llms.txt` 探测、页面 `.md` 直出、r.jina.ai 阅读器级
- stackexchange 引擎一套接 180+ 问答站；doi 引擎 DOI 号直出结构化论文元数据
- `--engine "a,b,c"` 逗号多引擎修复；宏观数据域零结果修复
- 新增排序金标度量（MRR/nDCG 地板锁死）+ 融合增益消融门禁

**默认关闭**：`ARGO_TINYFISH_API_KEY`（TinyFish 三引擎）、`ARGO_FETCH_JINA=0`（jina 阅读器级）、`ARGO_FETCH_MD_VARIANT=0`（.md/llms.txt 探测）

---

## v2.8.6

**定位**：搜索更快更准、长尾拖尾大幅收敛；知乎三源语义分工并解锁全部额度；新源接入从「改多处配置」变成「声明一次」，死源由门禁自动显形。

**关键变更**：
- 首引擎宽限窗竞速（hedged racing）：主引擎 2 秒宽限窗内出结果且合格 → 只付 1 次调用
- 地理查询拖尾 −56%（12.4s→5.5s）：OSM 的 6s 超时声明修复为硬上限
- live 多语言金标命中率 69%→100%：实体型引擎查询规范化
- 知乎三源语义分工：站内搜索（5000/天）、全网搜索（5000/天）、个人数据（10000/天，新增）
- 声明式语言分发：引擎语言能力进元数据，路由的语言重排/ja-ko 过滤由元数据派生
- 可达性门（WARN）：enabled 引擎必须被 ≥1 条分发路径可达
- 新增 `references/academic-query.md` 学术检索协议（17 学术源查询构造、多库去重、相关性五因子排序、引用网络挖掘、18 条反模式）
- 新增 `zhihu_user` 个人数据引擎；小红书 REDSkill 技能检索域
- YAML 同名键静默覆盖修复：新增 `test_config_dup_keys` 门

**默认关闭**：无新增

---

## v2.8.5

**定位**：DSH 插件工具原生化 + MCP 默认关闭 + Windows 全平台兼容（社区贡献）+ 配额自愈闭环 + 抓取降级链全局 deadline + 审查加固。

**关键变更**：
- `argo_search` / `argo_fetch` 以原生一等工具注册，默认即可用；工具 schema 由 `gen_native_tools.py` 从唯一真源自动生成
- DSH 插件三形态接入：MCP（默认关）、原生一等工具（默认入口）、原生 `web_search` seam
- Windows 全平台兼容（PR #11，感谢 HiSeax）：临时文件统一走 `tempfile.gettempdir()`、HTML 引擎显式 `encoding="utf-8"`、CLI 解释器运行时解析、Skill 链接退化为 junction、新增 PowerShell 一键安装
- 配额自愈闭环：HTTP 200 响应体里的业务错误封套被识别透出；识别为配额耗尽 → 路由全模式排除该引擎 → 到配额周期边界惰性自愈
- 抓取降级链全局 deadline（`ARGO_FETCH_DEADLINE_S` 默认 60s）；429/503 明确停止信号；tinyfish 免费渲染层；客户端形态分流
- 密钥热读：`~/.config/argo/env` 作为 env 文件真源；本地状态目录单一真源 `scripts/argo_paths.py`；热生效基建 `scripts/hot_state.py`

**默认关闭**：MCP 14 工具（DSH profile patch 开启）、`nativeTools` 全部 13 个（默认仅 search/fetch）、`ARGO_FETCH_TINYFISH=0`（TinyFish 渲染层）

---

## v2.8.4

**定位**：深度研究本地数据融合 L1 + 多客户端 MCP 一键接入 + 结构化搜索增强 + Keenable 接入 + 安全加固。

**关键变更**：
- 工作包可带 `file_inputs`（本地一手数据，白名单制）与 `recompute`（可复算执行器：只读挂载输入 → 受限子进程执行 → 输出 `{ok, exit_code, stdout, elapsed_ms}`）
- `argo mcp inject --all` 一键注入所有已安装客户端（原子写 + 写前自动备份 + 0600 权限）；`argo mcp undo --all` 精确移除
- 查询归一化 + 复杂度门控：低复杂度查询只允许低成本放宽（L1/L2），禁掉高价多源 / 跨语言（L3/L4）
- 接入 Keenable 通用网页搜索引擎（`ARGO_KEENABLE_API_KEY`）
- recompute 断网承诺真正成立：meta_path 拦截 `subprocess/multiprocessing/ctypes/pty/pexpect` 导入 + 覆盖 os 进程 / 执行入口
- 主机路径单真源化：`scripts/seek_locator.py` 统一发现

**默认关闭**：`ARGO_ALLOW_RECOMPUTE=1`（recompute 数值重算器，默认 fail-closed）

---

## v2.8.3

**定位**：多语言路由修复 + anysearch 进程化 + weighted RRF 融合强化。

**关键变更**：
- anysearch 从子进程改为进程内 builder（`_build_anysearch_engine`），省掉每次 subprocess 启动（约 0.2-0.3s 纯开销）；网络失败自动重试降错
- 语言门控：日/韩查询不再误入中文泛内容域；中文引擎双层过滤
- anysearch 多语言兜底：ja/ko 宏观返回日语/韩语结果；德法西意走 anysearch
- Bing `mkt` 市场码：`local_bing` 追加市场码
- `_engine_weight` 升级为静态基础权重 × 动态可靠性因子（熔断与高错误引擎自动降权）

**默认关闭**：无新增

---

## v2.8.2

**定位**：Windows 全平台可用 + 证据语义统一（dossier 与 wide_research 同一套门禁）。

**关键变更**：
- 移除 npm `os` 限制，Windows 直接安装；全链路 UTF-8 防线（Node 入口注入 `PYTHONUTF8=1`，Python 入口 `-X utf8` 重启动）
- 工具探测改 `shutil.which`（跨平台），无 rg 时经 Git 自带 grep 兜底；Chrome 自动发现新增 `CHROME_PATH` + Windows Chrome/Edge 五个常见安装路径
- DSH 插件两种装法：`dsh plugin --profile web add "github:taxueseek/argo"`（仅 10 个 MCP 搜索工具）与 `dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"`（搜索工具 + wide_research 并行研究编排）
- `wide_research` 输出新增 `quality_gate_results`（`passed` / `conclusion_cap`），与 dossier 同一套语义；支持 `depends_on` 依赖分阶段
- 取证协议化：机器只产出 dossier（来源 / 覆盖 / 缺口 / 门禁 / 待核验 URL），判断稿由 Agent 按 `references/research-protocol.md` 写
- npm 包补 `engines/`、`data/`；安装脚本新增 `ARGO_PIN` 固定 commit

**默认关闭**：无新增

---

## v2.8.0

**定位**：从「帮你搜到」升级为「帮你核到」。新增证据闭环、求职搜索 v3、天气双源、通用搜索增强。

**关键变更**：
- 证据闭环：金融/医疗/法律问题结果自带 `fetch_required`；每条结果标注 `fetch_suggested`；`--verify` 一键核验并记住已核实链接（URL→证据分缓存）
- `argo job` 升级到 v3：结构化字段（岗位名称、公司、地点、薪资、发布时间）、增量监控（指纹去重）、新增 Ashby ATS 免 key 后端
- 天气双源并行：wttr.in 和 Open-Meteo，支持地理编码与空气质量查询
- 新增 Parallel 搜索（`PARALLEL_API_KEY`）与 You.com 搜索（`YDC_API_KEY`）两个免 Key 可选引擎
- 稳定性修复：`health_check` 崩溃修复、日韩股票查询错配修复

**默认关闭**：无新增

---

## v2.7.x 及更早

| 版本 | 说明 |
|------|------|
| **v2.7.3** | 引擎层接入 HTTP 客户端（UA 轮换 / 重试 / 重定向跟随，arxiv 从 5s 超时变 2s 内 10 条）；TF-IDF 强语义注入激活 25 个此前永远选不中的垂直引擎；70 域缓存 TTL 全覆盖；国际引擎中文查询 URL 编码修复 |
| **v2.7.2** | 新增 ego-search 子技能（默认关闭）；搜索保底与多意图路由；日韩文查询不再混入中文引擎 |
| **v2.7.1** | SSRF 防护（URL 白名单 + IP 段检查）；路由健康状态语义漂移根因修复；深度研究 local_first 浪费修复 |
| **v2.7.0** | 内建 `bocha` / `bocha_ai` 原生引擎，`modal_card` 域统一识别火车票 / 油价 / 贵金属 / 万年历 / 星座等实时卡片 |
| **v2.6.2** | 网络环境感知 / 加权 RRF 与语义缓存 / 自适应引擎禁用 / 内容安全与查询变体 / 三大垂直引擎；含 v2.6.1 路由修复 |
| **v2.6.1** | v2.6.0 修复版：路由误伤修复（`capital of` 不再抢 fact_check）；版本同步 |
| **v2.6.0** | 多语言搜索（检测 / 引擎参数 / 跨语言回退）；影视·体育·地理·组织·媒体等垂直补全；约 120+ 源 |
| **v2.5.1** | 金融/宏观/化学等垂直答案源加厚；引擎分层 + combo 预算 |
| **v2.5.0** | 安装脚本 + npx；查询改写与路由拆开；路由热路径缓存；MCP 紧凑响应 |
| **v2.4.0** | 路由低分回退与社交误吸过滤；缓存 depth / 柔性命中；熔断与负缓存；`engine_outcomes` |
| **v2.2–v2.3** | 证据两阶段、中文信源表、content_signals、fetch 栈、引擎扩充 |
| **v2.1** | 社交引擎层（多平台 UGC） |
| **v1.x** | 统一命名为 Argo，多引擎路由与双层缓存成型 |

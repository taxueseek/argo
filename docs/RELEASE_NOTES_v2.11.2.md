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

## 相关依赖

- 新增可选凭据：`ARGO_SAUCENAO_API_KEY`、`ARGO_SERPAPI_API_KEY`（仅显式调用反搜图时需要，缺失时引擎报可读错误而非静默空结果）；
- 本地图片输入需配置 `ARGO_IMAGE_LOCAL_ROOTS` 目录白名单；
- 零新增 Python 依赖。

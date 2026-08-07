# Argo v2.7.2 发布说明

**版本**：2.7.2
**定位**：以「重新定义问题 → MECE → 量化」为纲的检索能力扩展版。在 v2.7.1（安全加固）基础上，新增登录态专业搜索子技能（ego-search），并完成 P0/P1/P2 三轮路由与编排系统性优化：备用源无缝切换、多意图路由、语言引擎单一入口、MCP 服务层拆分与观测遥测。

---

## 一句话

v2.7.2 = 登录态专业搜索（ego-search 子技能）+ 检索路由四层优化（备用源/多意图/语言体系/遥测）+ 服务层结构重构。497 测试全绿，10 个 MCP 工具契约不变。

---

## 新增能力：ego-search 登录态专业搜索子技能

**问题**：主系统 120+ API/HTML 引擎覆盖不到登录墙内容（知乎/小红书/微博/X/公众号）、JS 渲染与 SPA 页面、反爬与 Cloudflare 保护页、需要交互的动态搜索、同源接口数据。

**新增**：`sub-skills/ego-search/` 子技能（约 2900 行），基于真实 Chromium 浏览器运行时的登录态专业搜索：

- **双运行时**：ego lite（`ego-browser`）与 WebBridge（用户 Chrome/Edge 扩展桥）互补，任一可用即可执行 `search` / `fetch` / `act` / `api` 四模式
- **登录态继承**：真实浏览器会话继承登录态，抓登录墙正文；同源 API 数据直取（如知乎 `search_v3`）
- **专业模式闸门**：默认关闭，需用户明确要求后 `enable`，状态持久化
- **缓存硬隔离**：登录态结果 `cache_eligible=false`，`SearchCache` 在 set/set_engine/set_fetch 入口硬拒绝（`is_login_partition_payload` / `assert_cacheable`）
- **分析层融合**：`merge` 命令把 public + login 两路结果按 canonical URL 去重、冲突并列，可一起喂 evidence 与 RRF
- **安全**：URL 守卫（SSRF 防护，默认禁私网/本机）、登录墙质量信号（`auth_wall_suspected` / `login_likely_ok`）

测试：`test_cache_login_isolation.py` / `test_ego_search_*.py` / `test_envelope_login_state.py`（8 个文件，全量新增）。

---

## 检索优化 P0：备用源无缝切换 + 登录态意图标注

**问题**：16 个垂直域（微信公众号/财联社/同花顺/HN/StackOverflow/V2EX 等）主引擎故障时只能失败重试，整个查询静默降级。

**修复**：各垂直域 fallback 统一为 anysearch 免费通用源；主源故障时无缝切换，不再整查询失败。`route.py` 增加登录态意图标注，登录墙站点查询可被识别并引导至浏览器态路径。测试：`test_route_fallback_breaker.py`（+241 行，覆盖备用源切换与意图标注）。

---

## 检索优化 P1：多意图路由 + 统一健康度视图 + 免费源单一真源

- **多意图路由**：definition / fact / news / compare / social 意图并存，各意图独立并行度与引擎预算（`_INTENT_PARALLELISM`）
- **统一健康度视图**：`engine_status.py` 提供全引擎统一健康度视角，health_probe 语义修正
- **免费源单一真源**：`engine_policy.py` 免费源作为单一真源治理，`recovery.py` 跨层恢复增强

测试：`test_p1_absorb.py`（+97 行）。

---

## 检索优化 P2：语言体系 + MCP 拆分 + 观测遥测

### P2-1 语言引擎单一入口

**问题**：语言引擎追加逻辑散落三处（主路径 / TF-IDF 回退 / 兜底回退），行为不一致；日韩查询会混入中文域噪声引擎（bocha/byted/wechat_sogou 等）。

**修复**：`route.py` 新增 `_select_language_engines`（单一入口）→ `_merge_language_engines`（幂等合并）→ `_add_language_engines`（兼容包装）三层结构，三处合并点统一调用。日/韩查询自动剔除中文噪声引擎。测试：`TestLanguageEngineUnified`（8 例）。

### P2-2 URL 语言参数断言

**问题**：多语言索引依赖引擎 URL 语言参数（Bing setlang / Google hl / Yandex lang / Wikipedia uselang），但无测试锁定，`_lang_param` 注入点行为漂移不可见。

**修复**：重写 `TestEngineLangParamWiring`，用真实 URL 捕获断言 setlang=ja-JP/ko-KR/en-US/zh-Hans/cyrillic、hl=zh-CN/iw、lang、uselang 全参数矩阵。测试：+5 例。

### P2-3 显式语言覆盖

**问题**：「用日文搜」等显式语言意图此前被忽略，查询落入默认语言路由。

**修复**：`_LANG_OVERRIDE_MAP` + `_detect_lang_override`，支持 en/ja/ko/zh/cyrillic 五语显式覆盖（中文简体、日文平假名、英文、韩文、俄文等写法）；override 分支进入语言引擎选择单一入口，与主语言同等参与噪声剔除与 must_keep。测试：`TestLangOverride`（9 例）。

### P2-4 MCP 服务层拆分

**问题**：`mcp_server.py` 970 行逼近 1k 红线，传输 / 工具 schema / 执行逻辑混在一个文件。

**修复**：拆分为四模块——`mcp_tools.py`（10 工具 schema 唯一真源，161 行）、`mcp_handlers.py`（执行逻辑，643 行）、`mcp_transport.py`（JSON-RPC 传输，138 行）、`mcp_server.py`（薄入口，115 行）。10 工具 schema 快照一致；后台预热扩展：新增 local-seek 模块导入预热（为本地搜索进程内化铺路）。

### P2-6 观测遥测

**新增**：`telemetry.py`（append-only JSONL，失败静默，`ARGO_TELEMETRY` 可关）。merge 持久化 `dual_sourced_count` / `conflicts`；recovery 记录跨语言回退触发与命中率。测试：`test_telemetry.py`（+152 行）。

**遗留项**（非阻塞，中风险批次）：P2-5「合一本地搜索路」（seek.py 暴露 import 接口 + 进程内调用 + 共享缓存）未实施，`argo_local_search` 仍走 subprocess；待 P2-6 遥测数据支撑后单独实施。

---

## 验证

```bash
python3 -m pytest tests/ -q
# 497 passed, 12 skipped, 19 subtests passed
```

- MCP 契约：10 工具 schema 与 2.7.1 逐名一致（`argo_search` / `argo_local_search` / `argo_research` / `argo_evidence` / `argo_clarify` / `argo_crawl` / `argo_fetch` / `argo_screenshot` / `argo_pdf` / `argo_social_search`）
- 语言路由：`pytest tests/test_multilingual.py` 60 passed（含 P2-1/P2-2/P2-3 全量）
- 遥测：`pytest tests/test_telemetry.py` 全绿
- 相对 2.7.1：35 文件变更，+5115/−1020，纯超集，无功能回退

### ego-search 运行时状态（本机）

```bash
python3 sub-skills/ego-search/scripts/ego_search.py status
# pro_mode: false（默认关闭）；ego-browser 0.4.5.9 与 WebBridge 均在线
```

# Argo v2.15.0

## Highlights (English / TL;DR)

- **What's New**: Zhihu full chain (question drill-down, curated answers, Keychain credentials, vote-quality weighting); evidence-layer `upstream_authority` passthrough; keyless 3-tier semantic fallback (local 2-gram heuristics — semantic evidence now works without any API key); MCP generic connector (`type: mcp`, first integration grep.app); route zero-based domain scoring. Sources 304→311, domains 306→313.
- **Key Improvements**: single-file research-report pipeline with upstream structure self-healing; runtime state lifecycle (tombstones + observation); silent-catch ratchet 187→160; fail-closed env placeholder allowlist; CLI child-env minimization; performance — time-window-free searches no longer parse all 307 specs; fixes: mp.weixin fetch branch ordering, MCP explicit-null params, settings-page `/dev/tty` blocking, schemastery symlink install.
- **Breaking Changes**: env placeholders in engine specs are now **fail-closed** — any `{ENV}` used by a spec must be declared in `required_env`/`allowed_env` (or the alias table) or it silently resolves to empty. All bundled specs are verified compliant (0 residual); custom spec owners must add declarations. `argo-zcode` plugin removed from the repo (official marketplace never carried it; use the local marketplace source).

---

# Argo v2.15.0 发布说明（中文）

**版本**：2.15.0
**定位**：知乎全链 + 证据层权威透传 + MCP 连接器 + 路由零基域评分。这版把「路由命中之后」的能力做厚：证据层拿到上游权威档、无 key 用户走本地语义兜底、任意 MCP server 可声明式接入为搜索源；同时把工程债还掉一批（裸吞棘轮 187→160、状态补生命周期、单真源收口）。

---

## 这次更新有什么（通俗版）

- **知乎一次查透**：新增 `zhihu_questions` / `zhihu_kb` 引擎——问题自动下钻、精选回答摘要、Keychain 凭证自省、结果按点赞质量加权；上游 `AuthorityLevel` 全程透传。调用：`argo search "..." --engine zhihu_questions`
- **没有 API key 也能用语义层**：Gemini Decision 三级阶梯降级到最后一级本地启发式（CJK/日韩 2-gram、同句极性窗口），不再因缺 key 整层关闭
- **取证可信度看得见上游**：排序层的上游权威档（0-4）原样透传到证据层的 `credibility.authority.upstream_level`——只透传不赋权，不改变本仓评分
- **任意 MCP server 接入为搜索源**：`type: mcp` 通用适配器（stdio 子进程 / Streamable HTTP 双传输），内容层声明式（JSON 走标准 output_map、纯文本走分块正则），首个连接器 `grepapp_mcp`（GitHub 代码字面搜索）
- **路由评分零基重做**：域评分声明式下放、多语种概念表与脱敏遥测飞轮；标注路由集 13/13 全绿
- **研报一条龙**：单文件标准化落盘、上游结构自愈、摘录修复与去重
- **新垂类源**（均 explicit_only，点名调用）：`ai_labs`（AI 实验室一手博客聚合）、`eng_blogs`、`cisa_alerts`、`lobsters`
- **修了几个真的**：mp.weixin 专用抓取分支被 robots 闸挡住 100% 不可达（闸门顺序）、MCP 可选参数显式 null 炸 TypeError、settings 页无 fd 时阻塞 /dev/tty、软链直连安装下 DSH 面板消失（schemastery）、cn_ai_news 迁 AIHOT v1（issue #49）、fxtwitter 搜索端点复测恢复

## 验证

- 全量 pytest 绿（2 skipped）；标注路由集 13/13；相关性/排序 golden 绿
- `grepapp_mcp` 线上准入 contract/health/quality 三段全过（q=1.0）
- 派生件一致性（`sync_backends.py --check`）、目录计数（`gen_engine_catalog.py --check`）、发版契约（`release.py check`）全绿

## 兼容性

- **占位符 fail-closed**：engine spec 里出现的 `{ENV}` 必须在 `required_env`/`allowed_env` 或别名表声明，否则静默置空（防 Confused Deputy 越权读密钥）。仓内全部 spec 已验证零残留；自定义 spec 的维护者需补声明
- **`argo-zcode` 插件从主仓剥离**：官方市场从未上架，本地市场换源一行配置即可
- 新增垂类源均为 `explicit_only`，不占自动路由预算，点名调用即可
- CLI/JSON 输出新增 `upstream_authority` / `authority_level` 字段（增量，不破坏既有消费方）

## 社区广播模板（Broadcast Snippets）

**X / Twitter (280 chars)**:

Argo v2.15.0: Zhihu full chain, evidence-layer authority passthrough, keyless semantic fallback, and a generic MCP connector — plug any MCP server in as a search source with one YAML file. 311 sources, 313 domains. Apache-2.0.

**开发者群广播**:

Argo v2.15.0 发布：证据层拿到上游权威档（只透传不赋权）、无 key 语义兜底（本地 2-gram）、`type: mcp` 通用连接器（一份 YAML 接入任意 MCP server，首接 grep.app）、知乎全链与研报单文件链路。注意兼容性：engine spec 的 `{ENV}` 占位符改为 fail-closed，自定义 spec 需补 allowed_env 声明。源 304→311。详见 docs/RELEASE_NOTES_v2.15.0.md。

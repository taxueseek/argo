# Argo v2.17.0

## Highlights (English / TL;DR)

- **What's New**:
  - **Native Query Operator Pushdown & Browser Auth Debounce**: `query_operators.py` compiles provider-native parameters (including Tavily native `language`); `browser_auth.py` and `chrome_cdp.py` add 4-state login debouncing and CDP network capture; `ego-search` adds OS-file-locked cross-process pacing (`pacing.py`), site cookie/shadow-restriction probes (`site_profiles.py`), and unifies auth state constants with `engine_states.py`.
  - **Deterministic Evidence Grounding & Isolated Source Discovery**: Added `evidence_grounding.py` (NFKC folding, ≥3-digit anti-hallucination gate, entity-window anchoring, sha1 excerpt hash) wired into `numeric_verify.py`, `CoverageStallTracker` in `research_gates.py`, and `argo engine add --probe-domain / --from-har / --discover-gaps / --verify-draft` with HAR sanitization and isolated subprocess smoke verification.
  - **Workbench Daily Hotspots & DSH v0.2.2-rc.5 Alignment**: Added daily real-time hotspot recommendations with zero-LLM interest re-ranking and configurable refresh schedule in Argo Workbench, `route-guard.js` TCP loopback security fence, React `PanelBoundary` fault isolation, route-aware `web_search` decorator for DSH `v0.2.2-rc.5`, and `helve` paid source (`explicit_only`; total sources 312→313; thanks to Owen Gallagher / `@helve-dev-mcp` via PR #50).
- **Contributors**:
  - Thanks to Owen Gallagher (`@helve-dev-mcp`) for contributing the initial Helve search engine spec in [#50](https://github.com/taxueseek/argo/pull/50).
- **Key Improvements**:
  - **Circuit-Breaker Self-Healing & Profile Ratchet**: Fixed disabled-engine cooldown recovery loop in `route_combo.py`, added `engine_fingerprint.py` environment-change auto-reset, backfilled 18 domain profiles with an empty-profile ratchet (`AUTO_EMPTY_PROFILE_BASELINE=182`), switched OpenAlex links to direct DOIs, and tightened `nan`/`inf` JSON body serialization in `engines_fields.py`.
  - **Engineering Gates & Data-Driven README Art**: Removed `# skip` exemptions from fast gates, switched `scripts/pre-commit` to 4-way parallel pytest (~12s total) including `sub-skills/ego-search/tests` and all 136 DSH plugin Node tests, extended HTTP egress attribution gates to `sub-skills/`, and replaced hand-edited README SVGs with `scripts/gen_readme_art.py` checked at release time.
- **Breaking Changes**: None. All CLI, MCP, sub-skill, and DSH plugin interfaces remain backward-compatible.

---

# Argo v2.17.0 发布说明（中文）

**版本**：2.17.0
**定位**：机制收口 + 登录态与证据锚定升级 + 查询原生下推 + 工作台每日热榜与安全加固。将 v2.16.0 之后的点状修补升级为硬机制约束，同步补齐登录态防风控、确定性数字锚定、工作台每日热点推荐与跨进程并发可靠性。

---

## 这次更新有什么（通俗版）

- **新源接入与画像机制收口（源 312→313）**：
  - 按 `paid + explicit_only` 纪律纳入 `helve` 搜索源（基于外部 PR [#50](https://github.com/taxueseek/argo/pull/50)，感谢外部贡献者 Owen Gallagher / `@helve-dev-mcp`）；补齐 18 个源的域画像并建立空画像棘轮（`AUTO_EMPTY_PROFILE_BASELINE=182`），杜绝新源反复空画像。
  - OpenAlex 证据链接切换为 DOI 直链并完善摘要拼接；4 个源补齐 `item_upstream_authority` 透传。
- **查询操作符原生下推与熔断自愈**：
  - `query_operators.py` 落地引擎原生参数下推（含 Tavily 原生 `language` 下发，不再静默丢失）。
  - 修复 `route_combo.py` 熔断超冷却后无法触发 `half_open` 探测翻转的死环，并新增 `engine_fingerprint.py`（环境变更自动复位熔断与健康回归差分）。
  - `engines_fields.py` 收敛 body 模板值类型唯一出口，`nan`/`inf` 不再产出非法 JSON。
- **登录态治理与双套单真源收敛**：
  - 主仓 `browser_auth.py` 与 `chrome_cdp.py` 落地登录态四态防抖与 CDP 网络流捕获。
  - `ego-search` 新增 `pacing.py`（无守护 OS 文件锁跨进程槽位、同站节奏锁与站点级终态冷却）与 `site_profiles.py`（Cookie 探针与静默限权识别），并与主仓 `engine_states.py` 收敛为同一份登录态词表单真源（含 `ImportError` 兜底对齐测试）。
- **确定性证据锚定与新源协议探测**：
  - 新增 `evidence_grounding.py`（NFKC 折叠、≥3 位防编造数字门禁、近实体窗口锚定与 sha1 摘录哈希）接入 `numeric_verify.py`；`research_gates.py` 新增覆盖高水位线停滞传感器。
  - `argo engine add` 新增 `--probe-domain`（Discourse/MediaWiki/OpenSearch/llms.txt/RSS 探测）、`--from-har`（HAR 脱敏合成 Spec）、`--discover-gaps` 与 `--verify-draft` 隔离子进程烟测。
- **DSH 插件与搜索工作台加固**：
  - 完整对齐 DSH `v0.2.2-rc.5`（含路由感知 `web_search` 装饰器、HMR 重注册修复、双形态入口收敛与 `resolveArgoBin` 候选链）。
  - 工作台新增每日实时热榜、零 LLM 兴趣重排与顶栏更新时间选择器，并修复跨进程热榜刷新互踩、失败误盖章与并发状态真值问题。
  - 新增 `route-guard.js` TCP 回环安全围栏（拒信伪造 `X-Forwarded-For`）与 `PanelBoundary` 前端容错边界。
- **工程门禁与对外证据图数据驱动**：
  - 彻底取消快门禁 `# skip` 豁免层（加反向断言拦截），`scripts/pre-commit` 升级为 4 片并行 pytest + `sub-skills/ego-search/tests` + 全量 136 项 DSH 插件 Node 测试（实测 ~12s）。
  - `assets/readme/hero.svg` 与 `proof-routes.svg` 改为由 `scripts/gen_readme_art.py` 从运行时真源生成并接入 `release.py check`。

## 验证

- `.venv/bin/python -m pytest tests/ -q`（全量回归，耗时 5m50s）100% 通过
- `bash scripts/pre-commit`（74 条快门禁 4 片并行 + 313 引擎派生件一致性校验 + `ego-search` 13 用例 + DSH 插件 136 项 Node 测试）全绿
- `scripts/release.py check`（结构、五语 README、`ENGINE_CATALOG.md` 与 `gen_readme_art.py --check`）全绿

## 兼容性

- **无破坏性变更**：CLI、MCP、子技能与 DSH 插件接口保持向后兼容；`helve` 默认 `explicit_only` 不占自动路由预算。

## 社区广播模板（Broadcast Snippets）

**X / Twitter (280 chars)**:

Argo v2.17.0: Native query operator pushdown, 4-state browser auth & ego-search OS-locked pacing, deterministic numeric grounding, isolated HAR/protocol source discovery, Workbench daily hotspots + TCP loopback guard, and 313 sources. Apache-2.0.

**开发者群广播**:

Argo v2.17.0 发布：落地引擎原生查询参数下推、登录态四态防抖与 `ego-search` 跨进程防风控节奏锁（与主仓状态词表单真源统一）、确定性数字证据锚定、`argo engine add` 协议探测与 HAR 脱敏隔离烟测；DSH 工作台新增每日实时热榜与零 LLM 兴趣重排、TCP 回环安全围栏并全面对齐 DSH `v0.2.2-rc.5`（共 313 源）；全量回归与 136 项插件测试全绿。详见 docs/RELEASE_NOTES_v2.17.0.md。

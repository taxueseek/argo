---
name: ego-search
description: >-
  ego-search 是 argo 的浏览器态搜索增强子技能，基于真实 Chromium 浏览器运行时，提供
  登录态继承、JS 渲染、反爬穿透、动态交互与同源接口数据直取（api 模式）能力。专业搜索
  模式默认关闭，需用户明确要求并执行开启指令后启用。当 argo
  的 120+ API/HTML 引擎覆盖不到时启用：登录墙后的内容（知乎/小红书/微博/X/公众号）、
  JS 渲染与 SPA 页面、反爬与 Cloudflare 保护页、需要交互（翻页/展开/滚动加载）的动态搜索、
  SPA/XHR 接口数据直取、以及需要真实登录态才能搜到的私有内容。输出对齐 argo 统一 JSON
  schema，可直接进入 evidence 评分与 RRF 融合。Triggers include 浏览器搜索、登录后才能
  搜到、JS 渲染页面、SERP 补充、动态内容抓取、登录态抓取、API 数据直取、接口数据。
metadata:
  version: "1.6.0"
  date: "2026-08-07"
  upstream: ego lite（ego-browser）+ WebBridge 扩展桥（双运行时）
  parent: argo
  pro_mode: 默认关闭，手动开启（enable/disable）
  cache: 登录态结果 cache_eligible=false，禁止写入 argo 公共缓存
  architecture: dual_runtime_complete
  security: URL 守卫 + 任务空间收尾 + 专业模式闸门 + 登录墙质量信号
triggers:
  - 专业搜索模式
  - 开启专业搜索模式
  - 浏览器搜索
  - 登录态
  - 登录墙
  - JS渲染
  - SERP
  - 动态内容
  - 反爬
  - 浏览器抓取
  - API数据直取
  - 接口数据
---

# ego-search（argo 子技能）

ego-search 是 argo 的**登录态专业搜索**子技能（v1.4 完全态）：

- **双运行时保留**：ego lite（`ego-browser`）与 WebBridge（用户浏览器扩展桥）**互补，不互相替代**
- **任一可用即可**：`search` / `fetch` / `act` / `api` 在至少一条运行时在线时可用
- **与常规检索隔离**：登录态结果 `cache_eligible=false`，禁止进公共 SearchCache
- **汇总可融合**：输出带 `merge_with_public_ok=true`，分析层可把 public + login 两路结果一起喂 evidence

## 完全态架构

```text
常规检索（argo public）          登录态专业搜索（本技能）
  cache: argo public                 partition: login
  cache_eligible: 可写             cache_eligible: false
         \                              /
          \                            /
           └── 汇总分析（evidence / RRF 可选）──┘
                 按 source / search_partition 区分，再综合判断
```

| 运行时 | 何时用 | 优势 |
|--------|--------|------|
| **ego**（默认优先） | `ego-browser` 可用 | 任务空间隔离、Agent 专用浏览器、learnings |
| **webbridge** | 无 ego 或 auto 降级 | 用户 Chrome/Edge 现成登录态、扩展桥 |

```bash
# 探测双运行时 + 专业模式
python3 sub-skills/ego-search/scripts/ego_search.py status
python3 sub-skills/ego-search/scripts/ego_search.py status --fix   # 幂等启 WebBridge 桥

# 显式指定运行时（任一安装即可）
python3 .../ego_search.py search "查询" --runtime auto        # 默认：有 ego 用 ego，否则 WebBridge
python3 .../ego_search.py search "查询" --runtime webbridge
python3 .../ego_search.py fetch "https://..." --runtime ego

# 同站登录态保温
python3 .../ego_search.py fetch "https://www.zhihu.com/..." --site zhihu.com

# 汇总分析：public 常规 JSON + login 专业 JSON
python3 .../ego_search.py merge --public /tmp/public.json --login /tmp/login.json
```

## 专业搜索模式（默认关闭）

本子技能涉及真实浏览器**登录态继承**，出于安全与可靠性考虑**默认关闭**。开启后
`search`/`fetch`/`act`/`api` 与直接浏览器操作才可用；未开启时调用会被拒绝并提示开启命令。

```bash
# 开启（必须由用户明确要求「开启专业搜索模式」后执行）
python3 sub-skills/ego-search/scripts/ego_search.py enable
# 关闭
python3 sub-skills/ego-search/scripts/ego_search.py disable
# 查看状态
python3 sub-skills/ego-search/scripts/ego_search.py status
```

- **纪律**：Agent 不得自行开启——只有用户明确表达开启意图时才运行 `enable`。
- 未开启时，同样不得用 heredoc 直连浏览器运行时绕开闸门。
- 状态持久化于 `~/.local/state/ego-search/pro-mode.json`，开启后长期生效，直到手动关闭。

## 能力基础（MECE 对照）

| 能力 | ego | WebBridge | 说明 |
|------|:---:|:---------:|------|
| 登录态搜索/取正文 | ✅ | ✅ | 任一可用即可 |
| 任务空间隔离 | ✅ | session 标签组 | ego 更强隔离 |
| 已知 URL 同源 API | ✅ browserFetch | ✅ page fetch | api 模式 |
| 语义快照/复杂交互 | ✅ heredoc 全 helper | ✅ snapshot/@e | 复杂操作用原版运行时能力 |
| 被动 network 发现 | — | ✅ network | 需原生扩展能力时走 WebBridge |
| 写入公共 SearchCache | ❌ | ❌ | 一律 cache_eligible=false |

**边界**：

| 在范围内 | 不在范围内 |
|----------|------------|
| 双运行时 search/fetch/act/api | 放弃任一侧只留单后端 |
| 登录分区 + 分析层融合 | 登录结果写入 public 缓存 |
| auto 择优与失败降级 | 与常规 argo 召回混进同一 cache key |

## 定位与升级决策

主系统 API/HTML 检索覆盖不足时，升级到 **ego-search**：

| 场景 | 常规检索 | ego-search 补什么 |
|------|----------|-------------------|
| 登录墙内容（知乎/小红书/微博/X/公众号/会员站） | 拿不到正文 | 任务空间继承登录态，搜并抓取 |
| JS 渲染 / SPA / 懒加载页面 | 可能空壳 | 真实浏览器执行 JS 后提取 |
| 反爬 / Cloudflare 保护 | HTTP 失败 | 真实浏览器直接过 |
| 需要交互的动态搜索（翻页/「加载更多」/表单筛选） | 单次请求不够 | `act` 或 `ego-browser` heredoc 多步 |
| 搜索引擎实时 SERP（Google/Bing/百度） | 可能被反爬 | 真实 SERP 结构化提取 |
| 深嵌套 iframe 页面 | 常挂 | 快照专长 |
| 已知同源 API URL | DOM 不全 | `api` + `browserFetch` |
| 未知 XHR 尚未拿到 URL | — | 不在 CLI 内；heredoc 自探或先拿 URL 再 `api` |

**决策顺序**：

```text
常规检索（快、省 token）
  → 结果不足 / 正文拿不到 / 需要登录态
    → ego-search search|fetch|act|api（pro-mode 开启）
      → 更复杂多步 → ego-browser nodejs heredoc
高后果结论 → 证据核验
```

## 依赖与环境

- **官方项目**：ego lite → <https://lite.ego.app/>（官网）；WebBridge → <https://www.kimi.com/zh-cn/help/kimi-webbridge/kimi-webbridge-introduction>（Kimi WebBridge 官方帮助中心）。
- **浏览器运行时**（macOS 应用）与 **运行时命令 `ego-browser`**（`~/.local/bin`）。
- 首次使用前确认：`command -v ego-browser`。未安装时按 `references/install.md` 完成安装。
- 运行时依赖由安装包自管理，本子技能不重复携带安装脚本。
- 登录态来自运行时 onboarding（可导入 Chrome 数据）；不同站点登录态由用户在运行时中维护。

## 快速开始（ego_search.py CLI）

```bash
# 首次使用：先开启专业搜索模式（需用户确认；开启后长期生效）
python3 sub-skills/ego-search/scripts/ego_search.py enable

# 浏览器态搜索（真实 SERP，输出 argo JSON schema）
python3 sub-skills/ego-search/scripts/ego_search.py search "AI agent 浏览器自动化" --engine bing --n 8

# 强制登录态站点搜索（如知乎/小红书，需登录态）
python3 sub-skills/ego-search/scripts/ego_search.py search "site:zhihu.com AI 搜索" --engine bing

# 浏览器态正文提取（JS 渲染/反爬/登录墙页面）
python3 sub-skills/ego-search/scripts/ego_search.py fetch "https://example.com/article" --focus 关键词

# 同源 API 数据直取（登录态站点接口）
python3 sub-skills/ego-search/scripts/ego_search.py api "https://www.zhihu.com/api/v4/search_v3?t=general&q=AI代理&limit=5" --origin "https://www.zhihu.com"

# 指定任务空间名（同一目标的多轮操作复用同名空间）
python3 sub-skills/ego-search/scripts/ego_search.py search "竞品分析" --task-space "竞品调研"
```

输出约定：**JSON 到 stdout**（机器可读，可管道给 `evidence.py` / 解析喂 RRF），日志到 stderr。

### 登录态 provenance（强制）

所有 `search` / `fetch` / `act` / `api` 输出均带：

| 字段 | 值 | 含义 |
|------|-----|------|
| `login_state_used` | `true` | 使用了浏览器登录态 |
| `auth_partition` | `login` | 认证分区标签（不写 cookie） |
| `cache_eligible` | `false` | **禁止**写入 argo 公共 `SearchCache` / `set_fetch` |
| `search_partition` | `login` | 与常规 `public` 检索分区隔离 |
| `merge_with_public_ok` | `true` | **允许**在汇总分析时与 public 结果一并送入 evidence |
| `runtime` | `ego` \| `webbridge` | 实际使用的运行时 |

公共库路径 `~/.cache/unified-search/cache.db` 在 `set` / `set_engine` / `set_fetch` 入口会硬拒绝
此类载荷。登录态结果默认不缓存 body。

### 与常规搜索的隔离与融合

| 阶段 | 规则 |
|------|------|
| **检索** | public（argo）与 login（本技能）分开跑、分 cache、分 partition |
| **缓存** | login 永不进 argo 公共缓存；两路互不 soft-hit |
| **汇总** | Agent 可将两路 `results` / 正文一并交给 `evidence` 或报告生成；用 `source` / `runtime` / `search_partition` 区分权重与可信语境 |

### 安全与登录态长期可用

| 控制 | 默认 | 说明 |
|------|------|------|
| 专业模式闸门 | 关 | 需用户 `enable` 后才跑登录态检索 |
| URL 守卫 | 拦 | `fetch`/`api`/`navigate` 仅 http(s)，默认禁本机/字面私有 IP；`EGO_SEARCH_STRICT_SSRF=1` 全量 DNS；`ARGO_ALLOW_PRIVATE_URLS=1` 放行 |
| 任务空间收尾 | 关空间 | ego 路径默认 `completeTaskSpace keep:false`，防标签堆积 |
| `--keep-space` / `--site` | 见右 | 多轮保温；`--site zhihu.com` → `site:zhihu.com` 并默认 keep |
| 登录墙质量信号 | 开 | fetch/act 输出 `quality.auth_wall_suspected` / `login_likely_ok` |
| WebBridge session | 不自动 close_session | 避免误关用户标签 |
| 公共缓存 | 拒写 | `cache_eligible=false` + SearchCache 硬守卫 |
| 分析融合 | `merge` | 与 public 结果隔离缓存、汇总时用 `merge --public --login` |
| 降级 | auto | 任一安装即可；ego 优先，失败降 WebBridge |

**登录态稳定建议**：

1. 同站多轮：`--site zhihu.com`（或 `--task-space site:zhihu.com --keep-space`）  
2. 单次取证：默认即可（跑完关空间）  
3. `quality.login_likely_ok=false` → 在 ego App 或用户 Chrome 人工登录后重试  
4. 不要把登录态 body 写入 public cache；汇总用 `merge`  

### 与原版 ego-browser 技能的关系（不是「超过」）

| 维度 | 原版 ego-browser skill | ego-search |
|------|------------------------|------------|
| 通用交互面 | **完整**（点选/填表/截图/handoff/learnings） | 故意收窄 |
| 登录态专业搜索 CLI | 需手写 heredoc | **更强**（双运行时 + schema + 隔离） |
| 安全/缓存/融合 | 弱文档约束 | **更强**（闸门+SSRF+provenance） |

ego-search **在搜证管线维度更强**，**在通用浏览器自动化维度仍弱于原版**。复杂操作继续用原版；见 `references/original-ego-upgrade.md`。

### search 模式

打开指定搜索引擎的真实结果页，`js()` 提取结构化 SERP，输出：

```json
{
  "query": "AI agent 浏览器自动化",
  "engine": "ego_browser_bing",
  "source": "ego-browser",
  "url": "https://cn.bing.com/search?q=...",
  "results": [
    {"title": "...", "url": "https://...", "snippet": "..."}
  ],
  "count": 8,
  "fetch_method": "browser",
  "login_state_used": true,
  "auth_partition": "login",
  "cache_eligible": false
}
```

- `--engine`：`bing`（默认，最稳）/ `baidu`（中文）/ `google`（需网络可达，可能弹验证）。
- `--n`：结果条数（默认 8）。
- 注意：SERP 本身是**检索入口**，不是正文证据——高后果结论必须 `fetch` 打开结果页取正文后再下判断（与 argo「SERP 链禁止当正文来源」纪律一致）。

### fetch 模式

浏览器态正文提取。优先 `article`/`main`/`[role=main]` 等语义容器，回退 `document.body.innerText`，
输出 `{ url, title, content, word_count, fetch_method: "browser" }`，正文可管道给 argo 质量信号分析。

- `--focus 关键词`：若传，只返回包含该关键词附近的段落（减少 token）。

### api 模式

浏览器上下文**同源 API 数据直取**（v1.1.0）。
先打开目标站点页面（继承登录态），再用 `browserFetch` 从该页面上下文请求同源接口，
拿回结构化 JSON/文本。覆盖 DOM 提取拿不到的场景：

- **SPA/XHR 懒加载截断**：页面只渲染前几屏，全量数据在接口里 → 直取接口拿全量。
- **登录态私有数据**：已登录站点的个人/搜索接口 → 页面 cookies 随请求携带。
- **站点内 API 搜索**：直接调站点搜索 API（如知乎 `search_v3`），比解析 SERP 更干净。

```json
{
  "api_url": "https://www.zhihu.com/api/v4/search_v3?t=general&q=...",
  "page_url": "https://www.zhihu.com/",
  "page_title": "(1 条消息) 首页 - 知乎",
  "data": { "paging": {...}, "data": [...] },
  "data_type": "json",
  "fetch_method": "browser_api"
}
```

- `--origin`：登录态上下文页面 URL。**默认取 API URL 同源**（scheme://host）；
  跨子域场景（如 `cn.bing.com` 请求 `www.bing.com`）须显式指定，否则 browserFetch 受
  同源约束失败。
- `data_type`：`json` / `text` / `error`（跨域、网络错误等归一化为 error，脚本不崩）。
- 大响应截断：`data` 超 100KB 时截断并标记 `truncated: true`、`data_type` 加
  `_truncated` 后缀。
- **定位**：api 模式输出是「原始数据」，供 Agent 分析判断，**不是 SERP 证据**，
  高后果结论仍须走 `argo_evidence` 核验。

### act 模式

把「搜索 → 点开结果 → 抓正文」等常见链式动作一次跑完。
支持 `--engine bing|baidu|google`（与 search 同表，默认 bing）。
输出含 `query` / `results` / `detail` 与登录态 provenance。
更复杂的多步交互（登录、表单、翻页、对比多个页面）用 `ego-browser nodejs` heredoc；
`ego_search.py` 只覆盖高频确定路径。

## 与 argo 主系统配合

1. **证据流水线**：ego-search 的输出与 argo schema 对齐，`results` 可直接喂
   `python3 scripts/evidence.py "查询词" --stdin --json` 做 Selection×Absorption 评分；
   正文可复用 `content_signals` / `content_security` 检查。
2. **RRF 融合**：`source=ego-browser` / `engine=ego_browser_<engine>` 可参与多源去重；
   envelope 会标 `login_state_used=true`。**不得**把该路结果写入公共 SearchCache。
3. **分层查询纪律**：事实类问题保持 argo 的 2–3 条子查询纪律（来源要求 / 对比 / 主体），
   ego-search 用于其中「登录态/动态内容」这一路，不替代 argo 引擎的全量召回。
4. **防污染**：内容安全引擎照常；**缓存隔离**靠 `cache_eligible=false` + SearchCache 硬守卫。
5. **缓存策略**：默认不缓存登录态 body；若未来做短缓存，必须独立库
   `~/.cache/ego-search/`，键含 `auth_partition`，禁止 soft-hit 公共 combo。

## 直接浏览器操作

复杂交互路径用浏览器运行时命令 `ego-browser nodejs` heredoc 直接编排（本子技能继承完整
运行时）。核心纪律：

```bash
ego-browser nodejs <<'EOF'
// 同名任务空间跨轮次复用；同一用户目标不新建空间
const task = await useOrCreateTaskSpace('竞品调研')
await openOrReuseTab('https://example.com', { wait: true, timeout: 20 })
cliLog(await snapshotText())   // 语义快照，带 [ref=N, loc=..., url=...]
EOF
```

**Helpers 速查**：

- 任务空间：`useOrCreateTaskSpace` / `listTaskSpaces` / `claimTaskSpace` / `handOffTaskSpace` / `takeOverTaskSpace` / `completeTaskSpace`
- 导航：`openOrReuseTab` / `gotoAndWait` / `pageInfo` / `listTabs` / `switchTab`
- 观察：`snapshotText` / `captureScreenshot` / `drainEvents`
- 动作：`click` / `fillInput` / `typeText` / `pressKey` / `scrollToBottomUntil` / `hover` / `uploadFile`
- 等待：`wait` / `waitForElement` / `waitForNetworkIdle`
- 提取：`js`（页面内 JS）/ `cdp`（浏览器协议）
- 输出：`cliLog`（唯一输出通道；`-e` 模式下输出到 stderr）

**关键纪律**：

1. **任务空间隔离 + 登录态继承**：Agent 在独立空间操作，不抢用户标签页；登录态默认继承，可访问已登录站点。
2. **跨轮次复用**：Node 运行时每次 heredoc 退出即释放，后续轮次用 `useOrCreateTaskSpace(同名)` 或
   `takeOverTaskSpace`（用户确认继续后）恢复。
3. **归属权**：用户接管空间时（"user is controlling"）是硬停——问用户并等待，不重试不抢回；
   `handOffTaskSpace` 交还用户后，只有用户明确确认（Ask 的 Continue）才 `takeOverTaskSpace`。
4. **收尾**：任务完成必须 `completeTaskSpace(name, { keep: false })` 关掉空间；
   仅当用户明确要求保留页面/需人工操作/结果无法用 URL 交付时才 `{ keep: true }`。
5. **`js()` 用法**：页面内逻辑包成单个 IIFE 一次返回；`js()` 返回求值结果而非 JSON 字符串，
   不要包 `JSON.parse`；模板字符串里正则反斜杠要双写或 `String.raw`。
6. **输出通道**：heredoc 模式 `cliLog` 输出到 stdout；`ego-browser nodejs -e "..."` 模式输出到 **stderr**。

**三种工作流**：普通 DOM 页用语义流（`snapshotText` + `@N`/`loc=` 引用）；canvas/富编辑器
（Google Docs/Notion/Figma 等）用视觉流（截图 + 坐标 + 键盘）；需要浏览器态/紧凑数据提取用
直接 DOM/CDP 流（`js`/`cdp`）。

## 文件结构

```
sub-skills/ego-search/
├── SKILL.md               # 本文件
├── scripts/
│   ├── ego_search.py      # CLI：双运行时路由 + 闸门 + provenance + merge
│   ├── runtime.py         # ego / WebBridge 探测
│   ├── webbridge_adapter.py
│   ├── safety.py          # URL 守卫
│   ├── quality.py         # 登录墙/空页信号
│   └── merge.py           # public+login 分析融合
└── references/
    ├── install.md
    └── original-ego-upgrade.md
```

## 参考

- 浏览器运行时细节：`references/install.md`（依赖安装）

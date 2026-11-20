# ego-search（argo 登录态专业搜索子通道）

> 本能力作为 `argo ego-search` 子命令接入（默认关闭 pro-mode，由主 Skill 按需调用），不再作为独立顶层 Skill 触发。

ego-search 是 argo 的**登录态专业搜索**通道（v1.6 完全态）：

- **双运行时保留**：ego lite（`ego-browser`）与 WebBridge（用户浏览器扩展桥）**互补，不互相替代**
- **任一可用即可**：`search` / `fetch` / `act` / `api` 在至少一条运行时在线时可用
- **与常规检索隔离**：登录态结果 `cache_eligible=false`，禁止进公共 SearchCache
- **汇总可融合**：输出带 `merge_with_public_ok=true`，分析层可把 public + login 两路结果一起喂 evidence
## 专业搜索模式（默认关闭）

本子技能涉及真实浏览器**登录态继承**，出于安全与可靠性考虑**默认关闭**。开启后
`search`/`fetch`/`act`/`api` 与直接浏览器操作才可用；未开启时调用会被拒绝并提示开启命令。

```bash
# 开启（必须由用户明确要求「开启专业搜索模式」后执行）
argo ego-search enable
# 关闭
argo ego-search disable
# 查看状态
argo ego-search status
```

- **纪律**：Agent 不得自行开启——只有用户明确表达开启意图时才运行 `enable`。
- 未开启时，同样不得用 heredoc 直连浏览器运行时绕开闸门。
- 状态持久化于 `~/.local/state/ego-search/pro-mode.json`，开启后长期生效，直到手动关闭。
## 定位与升级决策

主系统 API/HTML 检索覆盖不足时，升级到 **ego-search**：

| 场景 | 常规检索 | ego-search 补什么 |
|------|----------|-------------------|
| 登录墙内容（知乎/小红书/微博/X/公众号/会员站） | 拿不到正文 | 任务空间继承登录态，搜并抓取 |
| JS 渲染 / SPA / 懒加载页面 | 可能空壳 | 真实浏览器执行 JS 后提取 |
| 反爬 / Cloudflare 保护（尽力而为） | HTTP 失败 | 真实浏览器尝试，重度防护可能失败 |
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
argo ego-search enable

# 探测运行时与专业模式（--fix 幂等启 WebBridge 桥）
argo ego-search status [--fix]

# 浏览器态搜索（真实 SERP，输出 argo JSON schema）
#   --runtime auto(默认: 有 ego 用 ego，否则 webbridge) | ego | webbridge
argo ego-search search "AI agent 浏览器自动化" --engine bing --n 8

# 强制登录态站点搜索（如知乎/小红书，需登录态）
argo ego-search search "site:zhihu.com AI 搜索" --engine bing

# 浏览器态正文提取（JS 渲染/反爬/登录墙页面）
argo ego-search fetch "https://example.com/article" --focus 关键词

# 同源 API 数据直取（登录态站点接口）
argo ego-search api "https://www.zhihu.com/api/v4/search_v3?t=general&q=AI代理&limit=5" --origin "https://www.zhihu.com"

# 声明式同源 API 预设（zhihu / xueqiu / bilibili / weibo / xiaohongshu）
argo ego-search api --preset zhihu --query "AI 搜索" --n 8

# 清除站点冷却期（人工登录恢复后解除 auth_failed / account_restricted 冷却）
argo ego-search status --clear-cooldown zhihu.com

# 主动体检围墙站账号是否被静默限权（L1 探针：healthy / account_restricted / auth_failed）
argo ego-search status --check-health xiaohongshu

# 指定任务空间名（同一目标的多轮操作复用同名空间）
argo ego-search search "竞品分析" --task-space "竞品调研"
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
## 按需读取

复杂交互、heredoc 全 helper 速查、任务空间归属权纪律 → `references/browser-runtime.md`；安装细节 → `references/install.md`；升级到原版运行时 → `references/original-ego-upgrade.md`。

## 参考

- 浏览器运行时细节：`references/install.md`（依赖安装）

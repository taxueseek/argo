# Argo v2.8.5 发布说明

**版本**：2.8.5
**定位**：DSH 插件工具原生化 + MCP 默认关闭 + Windows 全平台兼容（社区贡献）+ 配额自愈闭环 + 抓取降级链全局 deadline + 审查加固。
**相对上一版的改进**：插件接入从「挂 MCP」变成「原生工具默认可用」；MCP 从「默认挂载」变成「按需开启」；Windows 从「能跑」变成「开箱即跑」；配额用尽自动切换备用源；抓取不再无限叠加超时。

---

## 这次更新有什么（通俗版）

### 1. DSH 插件工具原生化：不挂 MCP 也能用上 argo 搜索

**以前**：DeepSeek Harness（DSH）里要用 argo 的搜索/抓取，得先挂载 14 个 MCP 工具——常驻连接、占配置、开启有负担。

**现在**：

- `argo_search` / `argo_fetch` 以**原生一等工具**注册（`ctx.tools.register`），默认即可用，不依赖 MCP 连接——CLI 单发执行（`mcp_server.py --call` / `bin call`），与 MCP 同引擎、同压缩、同守卫
- 工具 schema 由 `scripts/gen_native_tools.py` 从唯一真源（`scripts/mcp_tools.py`）自动生成，Python 与 JS 两侧措辞/参数零漂移（`tests/test_native_tools_sync.py` 门禁把关：改 schema 不重新生成，测试直接红）
- 除 `argo_research` 外全部 13 个工具都可通过 `nativeTools` 配置按需启用为原生工具（默认只开 search/fetch，零常驻 token 开销；未知工具名 loud fail）

### 2. MCP 默认关闭，三形态按需开启

**以前**：DSH 主包默认挂 argo MCP，工具描述全部进系统提示词，不用也在花 token。

**现在**：DSH 插件三形态接入、互为冗余，按宿主能力自动降级：

1. `mcp__argo__*`：profile 按需挂载的 stdio MCP，完整 14 工具面（**默认关**，要用在 profile patch 里开）
2. 原生一等工具 `argo_search` / `argo_fetch`：CLI 单发，不依赖 MCP 连接（**默认入口**）
3. 原生 `web_search` seam：注册 "argo" provider，内置 web_search 经 argo 引擎链路由（默认启用）

MCP 开关回归默认关闭——需要完整工具面时一条 profile patch 打开，平时零常驻开销。

### 3. Windows 全平台兼容（社区贡献 PR #11，感谢 HiSeax）

**以前**：不少路径与工具写死 macOS 假设——`/tmp` 直拼、`python3` 可执行名、locale 编码读 YAML、`os.symlink` 要权限，Windows 上要么崩要么静默失效。

**现在**（Windows 10/11 + Python 3.13 实测）：

- 临时文件统一走 `tempfile.gettempdir()`（Chrome 用户目录、截图输出）
- HTML 引擎解析映射显式 `encoding="utf-8"` 读取（修复 Windows GBK 下静默失效）
- CLI 引擎解释器运行时解析：`python3` → `python` → `sys.executable`
- Skill 链接在无开发者模式时自动退化为 junction（`mklink /J`，无需管理员权限）
- 新增 PowerShell 一键安装 `scripts/install.ps1`（与 install.sh 对齐）
- local-seek 的 `--spotlight` 在无 mdfind 平台自动退化为 rg 正文搜索

### 4. 配额自愈闭环：引擎用尽自动切备用源，周期结束自动回归

**以前**：远端配额耗尽（如火山 10406 Free quota exhausted）以 HTTP 200 + 空结果静默通过，路由继续把流量打向死引擎，等人工改配置。

**现在**：

- HTTP 200 响应体里的业务错误封套（火山 `ResponseMetadata.Error`、知乎 `Code/Message`）被识别透出，不再「配额用完当没结果」
- 识别为配额耗尽 → `quota.mark_remote_exhausted` 标记 → 路由全模式排除该引擎、备用源自然接管
- 到配额周期边界惰性自愈，恢复后引擎自动回归；充值后可 `python3 scripts/quota.py reset <engine>` 提前恢复
- 配额/鉴权类错误不再计入自适应学习分数（防止「配额期连败毒化分数、恢复后永远翻不了身」的死锁）与熔断统计

### 5. 抓取降级链全局 deadline：延迟有一等公民约束

**以前**：抓取降级是「延迟换成功率」，各级独立超时加法无上限（8+8+8+12+8+15≈59s+），可能击穿 MCP 客户端工具超时。

**现在**：

- 全局 deadline（`ARGO_FETCH_DEADLINE_S`，默认 60s，0 关闭）：每级升级前检查剩余预算，耗尽即停链返回当前最优结果并打 `deadline_exhausted` 标记
- 429/503 明确停止信号：收到后不再升级重链，不无视服务器指示放大目标负载
- tinyfish 免费渲染层（markdown 直出、含 JS 执行，`ARGO_FETCH_TINYFISH=0` 关闭，需 `TINYFISH_API_KEY`）：HTTP/指纹层失败时优先于本地 Chrome，爬取场景（need_html）自动跳过
- 客户端形态分流：抖音类站点移动 UA 首发 + per-host 身份记忆（TTL 24h），减少风控试错
- `{url}.md` AI 友好变体探测：头部文档站命中即跳过整条反爬降级链（`ARGO_FETCH_MD_VARIANT=0` 关闭）

### 6. 密钥与状态基建（对用户透明，但值得知道）

- **密钥热读**：`~/.config/argo/env` 作为 env 文件真源（os.environ 优先），改文件即生效无需重启；search 引擎与 fetch 渲染层同真源，修复「search 能用、fetch 渲染层静默禁用」的分裂
- **本地状态目录单一真源**（`scripts/argo_paths.py`）：11 个模块各自拼 `~/.cache/unified-search` 的分裂拼法收敛为一处，`ARGO_STATE_DIR` 可整体隔离（测试/只读环境）
- **热生效基建**（`scripts/hot_state.py`）：代码/配置变更指纹自检 + `os.execv` 自重启（stdio 跨 exec 继承不断连），密钥/配置/注册表 mtime 热读；`ARGO_NO_AUTORELOAD=1` 关闭
- **缓存层降级不拖垮调用方**：目录不可写/数据库只读时缓存自动降级为 no-op（只该变慢，不该变错）

### 7. 审查加固（本轮 code review 修复）

- `mcp_handlers` 三处内联夹取统一走 `_clamp_int`（修复传 0 时语义不一致）
- 配额错误关键词单一真源 `_QUOTA_ERROR_KEYWORDS`（熔断分类与自适应跳过共用，防口径漂移）
- 语言感知排序的移尾形状提取 `_move_to_tail`
- DSH 共享 MCP 连接的 `request` 内建超时并同步清理 pending entry（修复超时后 entry 泄漏到进程关闭）

---

## 升级方式

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash

# Windows（PowerShell，本版新增）
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.ps1 | iex"

# 已装过：进仓库目录拉取即可
git pull --ff-only
```

依赖不变：Python 3.10+（PyYAML），可选 curl_cffi（TLS 指纹伪造层）。已装 DSH 插件的，`dsh plugin add github:taxueseek/argo` 更新即得原生工具形态；MCP 想开回完整工具面在 profile patch 里挂 `mcp-argo` 即可。

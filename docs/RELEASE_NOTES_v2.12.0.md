# Argo v2.12.0 发布说明

**版本**：2.12.0
**定位**：统一设置中心首发（issue #44 兑现）；社区贡献 pixiv 正向搜图合入，内置 trace.moe 场景识别（源 286→288）；fetch 对微信公众号文章不再零处理；调度与路由算法内部优化。

---

## 这次更新有什么（通俗版）

### 1. 统一设置中心：一个入口配好所有客户端（issue #44，感谢 @zooyoo 提议）

**以前不行**：DSH 插件有设置面板，其他客户端（Cursor / Claude Code / Codex 等 MCP 场景）只能手工编辑 env 文件，同一把 key 在各客户端之间来回填。

**现在能行**：

- `argo settings`——生成并打开本地设置页：密钥状态（只露尾 4 位）、各客户端接入片段、常用开关现值；
- `argo settings --edit`——编辑表单页：页面内改值、导出变更 JSON，`argo settings apply` 写回；
- `argo settings set/unset/list`——命令行直改：值走 stdin 读入，不进 argv 与 shell history，明文不落任何输出；写回 `~/.config/argo/env` 后全客户端热读取，无需重启；
- `argo ui` 是同义别名；Agent 对话里说「argo 设置」也能唤起同一套入口。

**实现取舍**：没有做成常驻 Web 服务（当初讨论的方向），而是零服务——不开端口、没有鉴权凭证生命周期要管理、页面是单文件零依赖 HTML，改动统一走同一条写回通道（env_store：行级补丁保留注释、原子写 + 0600 + 单份 .bak）。写回面越小越安全。引擎开关的写回暂不做：`config.yaml` 处于派生链上游，直接改它要连动再生成派生件，收益低风险高，等真实需求再评估。

### 2. pixiv 正向关键词搜图（社区 PR#45，感谢 @OMSociety）

**以前不行**：`image_search` 域以开放图库为主，按角色名、作品名找插画与同人图没有合适源。

**现在能行**：单引擎双通道——未配凭据走匿名 Web Ajax（免登录）；配置 `ARGO_PIXIV_REFRESH_TOKEN` 后走 App API，认证失败直接报错不静默回退。结果标 `channel`（web/app）让下游知道实际通路；默认只交付全年龄结果（`ARGO_PIXIV_ALLOW_R18=true` 放行）；支持搜索目标档、日期区间、过滤 AI 作品与排序；图片直链保持原图宽高比（i.pximg.net 下载需带 Referer，spec 注释已写明）。`explicit_only` 不进自动路由。

### 3. trace.moe 动画场景识别（内置）

输入形态特殊：query 必须是**公网可访问的图片 URL**。返回图中场景所属动画作品、集数与时间点（AniList ID）与相似度；url 槽填 AniList 作品页作为下一步查证入口，不假称图源页。免 key（免费档 100 次/小时，按出口 IP 限频），`ARGO_TRACEMOE_PROXY` 可覆盖出口代理——共享机场节点的出口常被别的客户端打到 429，该 API 直连可达，默认直连。本地图片在入口返回可读错误。`explicit_only`。

### 4. fetch 对微信公众号文章不再零处理

**以前不行**：`argo fetch` 抓 mp.weixin.qq.com 文章时按普通页面处理，直接被反爬挑战页挡死，返回壳页或失败。

**现在能行**：fetch 对公众号文章走前置分流（判定按 host+path 精确匹配，r.jina.ai 代理 URL 与 query 夹带微信链接的页面不会被误分流）。

### 5. 调度与路由算法内部优化

对冲宽限窗、尾宽分层、查询信号与 TF-IDF 路由、场景桶收缩等内部算法更新（含引擎准入原子性放宽）。本版 52 项相关单元测试全绿；这些属先验设计改进，尚无线上 A/B 数据，行为异常时可按 `references/usage.md` 行为调参表的既有开关回退对照。

### 6. 插件与文档收口

- DSH 插件 specs 与入口解耦（fast-adapt 审计 100/100）；
- 版本真源收敛到 package.json / SKILL.md，DSH 插件主包（`mcp-argo`，仅 MCP 工具面）与子包（`@taxueseek/argo-dsh`，含设置页与原生工具）的装法说明修正（#43 后续）；
- env_store 修正：末行无换行时追加新键不再粘连、`.bak` 备份改 0600 权限。

## 验证

- 本机全量 pytest 通过（Python 3.14，含新增 pixiv 26 例、tracemoe 6 例）；
- pixiv 匿名 Web 与 OAuth App 两通道实机搜索通过，返回图片带 Referer 可正常下载；tracemoe 实机识别返回作品/集数/时间点；
- CI gates / unit 四分片 / smoke 全绿（PR#45 附带分片数据）。

## 兼容性

- 源 286→288（pixiv、tracemoe），均 `explicit_only`，不改变自动路由候选池；
- 新增可选凭据：`ARGO_PIXIV_REFRESH_TOKEN`（pixiv App 通道；不配走匿名 Web，无需注册）；
- 新增可选开关：`ARGO_PIXIV_ALLOW_R18`、`ARGO_TRACEMOE_PROXY`（见 `references/usage.md` 行为调参表）；
- 零新增 Python 依赖；curl_cffi 仍为可选依赖（tracemoe/SauceNAO 共用）。

## 署名

- **@zooyoo**：统一设置中心的需求与形态建议（issue #44）；
- **@OMSociety**：pixiv 引擎全量实现与 26 例测试（PR#45），各 commit 保留原作者署名。

# Argo v2.10.0 发布说明

**版本**：2.10.0
**定位**：能力面扩张。三条主线：源从 272 增到 284（秘塔五档、Sciverse、NCPSSD、DSH 插件目录与技能索引）、视频检索与抓取一条线走通、抓取链重写前沿质控与礼貌节奏；另有一批交付质量改造：通用选源实测排序、DeepSeek 英文条件对冲、响应体统一解码、安装器 venv 化、发布一致性预检。

---

## 这次更新有什么（通俗版）

### 1. 源 272 → 284

五类新源，全部按既有声明的接入纪律走（spec + 派生件 + 目录校验 + 可达性门）：

- **秘塔五档**（`metaso_scholar` / `metaso_doc` / `metaso_image` / `metaso_video` / `metaso_podcast`）：与既有 metaso 网页档同源，五档各自声明 `output_map` 与 `url_template`；计费 3 积分/次且与返回条数无关，故条数取满；图片档上游不给许可字段与来源页，链接是图床直链，这三条写进 spec 注释。均 `explicit_only`，不参与自动路由。
- **Sciverse 题录档**（`sciverse`）：POST `/meta-search`，支持年份 / 期刊 / 作者 / 学科过滤与排序，`url` 由 doi 经 `url_template` 生成；覆盖外文为主，不含 CNKI / 万方 / CSSCI 中文核心；证据句与页码不在本档。`explicit_only`，计价未公布。
- **NCPSSD 中文社科**（`ncpssd`）：上游只吃表单 POST（GET 返 HTML、POST 空体被 WAF 493），声明式 http 引擎表达不了，用 CLI 桥 `scripts/ncpssd_bridge.py` 代发；`url` 取 `pdfurl`，无全文回落详情页。`cost_tier: free`，`explicit_only`。
- **DSH 插件目录三源**（`dshopen` / `deepseek1024` / `dsh_hub`）：894 / 13855 / 18933 条，前两者走声明式 JSON，dshopen 上游无搜索 API 故本地全量匹配；排序做了四处修正（IDF 压掉满语料词、标题取中文 `title.zh`、摘要带分类前缀、域级 `score_floors` 保底）。
- **技能索引两源**（`explainx` / `skills_sh`）：12075 条与 57k+ 条，首次带来「有多少人在用」的安装量字段；既有三技能源（skillsmp / clawhub / redskill）同步补上安装量。

配套：`docs/ENGINE_CATALOG.md`、五语 README、插件清单的计数全部由 `gen_engine_catalog.py` 从真源校正，不再手改。

### 2. 视频检索与抓取

- 新增 `video` 域（第 99 个域），只按显式视频意图命中；`--video` 强制 B 站 + anysearch 并只保留视频结果
- `--min-duration` / `--max-duration`：B 站按 duration 档位下推，档位表达不了的范围翻页后精确过滤；视频与时长过滤前移到 rerank 池截断之前，避免候选被 top-N 截掉
- 修掉根因：B 站 CLI 少传 `--json`，`social_meta` 在 JSON 解析前就不存在；现在补上并透传，envelope 识别 `content_type=video`、`duration_seconds`、播放量 / 作者 / 发布时间，摘要前置 UP 主 / 时长 / 播放量
- 抓取：B 站 view/player API 取简介与字幕，YouTube oEmbed + timedtext，ASR 走 `ARGO_VIDEO_ASR_CMD` 可选兜底；抖音等平台让路给通用抓取链

### 3. 抓取前沿质控与礼貌节奏

- 新增 `scripts/crawl_frontier.py`：复用 `url_canon` 归一化（保留原 scheme），过滤静态资产与坏路径，限定同主机 + 路径范围，叶子页上提到栏目；`extract_links` 全量提取并按归一化去重
- 新增 `scripts/crawl_policy.py`：每主机最小间隔 / 并发上限、429 指数退避（成功减半）、robots `Crawl-delay`（`robots_guard` 复用同一解析缓存）
- `crawl.py` 重写：抓取额度提到 200KB 以拿到完整 HTML，全量跟进链接（原前 5），HTML 嗅探，正文哈希去重但仍跟进重复页链接；新增 `--query` / `--full` / `--scope`，结果按入队顺序稳定输出
- `watch.py` 新增噪声感知块级 diff：日期 / 价格 / 计数 / 相对时间占位后比较，输出 `change_type=noise|substantive|unknown`，只保留最新一帧正文
- 实测：blog.rust-lang.org 抓 10 页 12.1s，0 资产混入、0 重复、正文 2000 字（原 500）

### 4. 响应体统一解码与结构化元数据

- 新增 `scripts/charset_decode.py`：头声明 > meta 嗅探 > 猜测链三级解码，抓取链与 `fetch_v3` 统一走它，GBK / Big5 / Shift_JIS 页面不再乱码
- `article_meta` 补 JSON-LD：`@type`、author、datePublished、publisher 从结构化数据读取，正文元数据不再只靠 meta 标签

### 5. 通用选源实测排序 + DeepSeek 英文对冲 + 权威早停

- 新增 `scripts/general_pool.py` 与 `scripts/source_selection.py`：`general_pool` 的 `_general_fallback` 与 `tfidf_lead_combo` 共用同一套排序，此前后者写死 anysearch/octen，换分支就换源
- issue #23 的条数与体积回归收口：覆盖地板（kept ≥ 3）+ 语言门 + 词面下限，只做门槛不参与打分
- 英文通用查询接入 DeepSeek 条件对冲：¥0.005/次，第二位常驻，`ARGO_PAID_HEDGE_DEFER=1` 降到第三位；预算内自动启用
- 权威等级回填到单引擎与对冲路径，恢复 DeepSeek 结构化字段价值；权威早停（authority ≥ 3 可少一条、无权威要求明确过半）
- 英文新闻路由补口：election / foreign policy 进 `global_event`

### 6. 安装器 venv 化

- `install.sh` / `install.ps1` 依赖安装改走 venv 主路：PEP 668 下 `--user → 全局 → 清华镜像` 的三段回落必败，装完仍缺 PyYAML
- 三个入口（CLI / MCP / npm bin）自动发现 `.venv`，GUI 宿主不继承 shell 变量也能命中
- `ARGO_PYTHON` 仍可显式指定解释器

### 7. 发布与注册

- 新增 `scripts/release_check.py`：13 处版本声明、五语 README 徽标与发布表、发布说明文件与 npm `files` 白名单一把校验，进 pre-commit 快门禁
- 新增 `server.json`：MCP Registry 清单，按 npm 分发（`argo-search`，`npx` 启动 stdio），README 补 `mcp-name` 归属锚点
- `packages/marketplace.json` 解除 ignore 入库，zcode 一致性门在 fresh checkout 与 CI 上不再炸
- DSH 插件 `client.js` 补 JS 体积门禁（1,599 行登记祖父）

### 8. 带病源救活与功能撤出

- `wikipedia` 补 `output_map`（此前缺 `query.search` 路径，永远空结果）；`reddit` 迁 RSS（`.json` 已被 CF 挑战页取代，RSS 按 IP 一次一发 429，qps 限 1/分钟）
- 飞书文档认证直取按用户拍板撤出：`fetch_feishu.py` 删除、`fetch_v3` 第零级挂载与 `feishu_md` 方法回退、开关总表删 `ARGO_FETCH_FEISHU` 行并回退计数、祖父上限随动撤回；撤出后 `scripts/` 零 feishu 残留

---

## 验证

- 全量 `python3.14 -m pytest -q` 退出码 0（4781 用例，少量平台性 skip）
- CI 在 main 上双平台绿；nightly 2026-10-27 恢复成功（issue #23 已关）
- 抓取实测：10 页 12.1s、0 资产混入、0 重复、正文 2000 字
- NCPSSD 真机冒烟：3 条中文社科题录通过；秘塔五档按 spec 逐档验证
- `release_check.py` 一致；`gen_engine_catalog.py --check` 一致（284 个源）

## 兼容性

- 无破坏性变更，v2.9.9 的配置与用法继续有效
- 新增开关：`ARGO_VIDEO_ASR_CMD`、`ARGO_PAID_HEDGE_DEFER`、`ARGO_GENERAL_FALLBACK_LEAD`、`ARGO_GENERAL_FALLBACK_LEGACY`、`ARGO_GENERAL_FALLBACK_STATIC`
- 新增 CLI：`--video`、`--min-duration`、`--max-duration`、`crawl --query/--full/--scope`
- 安装路径变化：依赖装进 `.venv`，入口自动发现；`ARGO_PYTHON` 可覆盖
- 分发面变化：MCP Registry 清单按 npm 声明；PyPI 未发布，不再作为安装方式出现
- 仓库版本号、五语 README、DSH 插件对照表、ZCode 插件清单统一到 2.10.0

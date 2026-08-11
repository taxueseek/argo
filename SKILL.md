---
name: argo
description: Argo 阿尔戈 — 统一搜索与证据核验。多语言检测与跨语言回退；约 120+ 引擎 TF-IDF 路由 + RRF；影视/体育/地理/组织/媒体/金融/宏观/化学等垂直源；垂直结构化模态卡（火车票/油价/贵金属/万年历/星座/手机/汽车/挂号）；日常 combo 预算与深度研究 boost；recovery 防污染；Selection×Absorption；MCP（含 argo_local_search）。入口：install.sh / npx github:taxueseek/argo / mcp_server.py。
version: 2.7.3
triggers:
  - 搜索
  - 查一下
  - 搜一下
  - 核实
  - 查证
  - 可信度
  - search for
  - look up
  - fact check
engines:
  - anysearch
  - archive_org
  - arxiv
  - baidu_baike
  - baidu_hot
  - bilibili
  - bilibili_hot
  - bocha
  - byted
  - clinicaltrials
  - cls_telegraph
  - cninfo
  - coingecko
  - courtlistener
  - crates
  - crossref
  - dblp
  - devto
  - docker_hub
  - douban_book
  - duckduckgo
  - eastmoney
  - em_flow
  - em_global_news
  - europepmc
  - eurostat
  - exa
  - finviz
  - fred
  - free_dictionary
  - fx_rate
  - fxtwitter
  - gbif
  - github
  - google_scholar
  - gutenberg
  - hackernews
  - huggingface
  - itotii
  - itunes
  - jin10
  - juejin
  - know_your_meme
  - marginalia
  - searchmysite
  - lieu
  - opensky
  - electricity_maps
  - usda
  - tatoeba
  - figshare
  - tencent_kline
  - qq_music
  - cnii
  - dnb
  - doaj
  - eu_opendata
  - europeana
  - fr_opendata
  - gov_policy
  - hal
  - hatena_bookmark
  - kor_law
  - ndl
  - open_meteo
  - qiita
  - local_arxiv
  - local_baidu
  - local_bing
  - local_bing_news
  - local_brave
  - local_crossref
  - local_ddgs_images
  - local_ddgs_news
  - local_ddgs_books
  - local_ddgs_videos
  - local_duckduckgo
  - local_github
  - local_gitlab
  - local_google_news
  - local_mojeek
  - local_npm
  - local_openstreetmap
  - local_pubmed
  - local_search
  - local_semantic_scholar
  - local_sogou
  - local_stackoverflow
  - local_startpage
  - local_wikipedia
  - local_wikiquote
  - local_wiktionary
  - local_yahoo
  - local_yandex
  - mdn
  - models_dev
  - moegirl
  - musicbrainz
  - nasa_cmr
  - nbs_stats
  - npm
  - octen
  - open_library
  - openalex
  - openfda
  - openverse
  - polymarket
  - pubchem
  - pypi
  - qweather
  - rcsb_pdb
  - reddit
  - rfc_editor
  - seeking_alpha
  - semantic_scholar
  - sina_quote
  - stackoverflow
  - steam
  - tavily
  - tencent_quote
  - ths_hot
  - toutiao_hot
  - twitter
  - uniprot
  - urban_dictionary
  - usgs
  - v2ex
  - wayback_cdx
  - wechat_sogou
  - weibo
  - wenshu
  - weread
  - wiby
  - wikidata
  - wikipedia
  - worldbank
  - xiaohongshu
  - zh_wikipedia
  - zhihu
  - zhihu_global
  - zhihu_hot
---
## Argo v2.7.0

### 本版你多了什么（通俗）

**按语言、按领域选路，查询越用越准。** 多语言检测与引擎参数；影视 / 体育 / 地理 / 组织 / 媒体与金融宏观化学等垂直源；**垂直结构化模态卡**（火车票 / 油价 / 贵金属 / 万年历 / 星座 / 手机 / 汽车 / 挂号——单一语义识别自动路由到模态卡引擎，其余引擎 0 参与）；空结果恢复防串味；日常 combo 预算、研究 boost 不锁死。详见 `docs/RELEASE_NOTES_v2.7.0.md`。

| 你问的 | 大概走哪类 | 体感 |
|--------|------------|------|
| 日文 / 韩文 / 俄语提问 | 语言检测 + local_bing 等 | 少塞中文专用源 |
| 电影导演 / 主演 | IMDb 等影视域 | 实体答案感 |
| 球星 / 俱乐部 | TheSportsDB 等体育域 | 球员球队直达 |
| 地标在哪 / 组织创办 | OSM / Wikidata | 地理与实体 |
| 专辑 / 艺人 | iTunes 等媒体域 | 音乐条目 |
| A 股 / 美股行情 | 新浪腾讯 / Finviz | 快照 + early-stop |
| CPI / GDP / 分子式 | 宏观 / PubChem | 数据与化合物 |
| 深度研报 / 综述 | research + vertical boost | 更全且不易整条空 |

### 安装与接入（摘要）

任选其一（详见仓库 [README](https://github.com/taxueseek/argo)）：

```bash
# 1) 一键安装（推荐，不依赖 npm）
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash

# 2) MCP：从 GitHub 拉最新（不走 npm 包版本）
pip3 install pyyaml
npx -y github:taxueseek/argo

# 3) 源码 / Release 包
git clone https://github.com/taxueseek/argo.git && cd argo && pip3 install pyyaml
python3 scripts/mcp_server.py
```

MCP 推荐：`npx -y github:taxueseek/argo`，或 `python3 ~/.local/share/argo/scripts/mcp_server.py`（路径用本机占位）。

Skill 入口请用符号链接：`python3 scripts/link_source.py --to ~/.claude/skills/argo`。

### 能力增量（相对早期版本）

| 能力 | 说明 |
|------|------|
| 约 120+ 搜索源 | `config.yaml` 真源：local_* / 金融 / 影视 / 体育 / 地理 / 组织 / 媒体 / 宏观 / 化学 / 社交 / 学术 / 代码 |
| 多语言搜索 | 统一语言检测 + 引擎语言参数 + 语言补充源 + 跨语言回退 + 中英基线偏好 |
| 垂直域补全 | film / sports / geo / org / media 与金融宏观化学等；recovery 防无关垂直污染 |
| 能力族体系 | `engine_families.py`：MECE 能力族，同族可互换、按族去重与回填 |
| 知乎全网搜索 | `zhihu_global`：SearchDB=all 真全网 + site 语法；AuthorityLevel/互动/时效信号 |
| MCP 工具 | search / research / evidence / clarify / fetch / crawl / screenshot / pdf / social_search / local_search 等 |
| 引擎分层 + combo 预算 | 日常精简、研究放宽（`engine_policy`） |
| 垂直答案源 | 行情 / 影视 / 体育 / 宏观 / 化合物等优先可吸收事实 |
| 查询改写 | 口语 → 检索友好；**不污染**路由域匹配 |
| 路由热缓存 | 重复路由接近亚毫秒 |
| 矩阵回归 | `matrix_search_eval.py` / `regression_p0p1.py` |
| MCP 紧凑响应 | 控制 snippet 与内部字段，省 token |
| 熔断 + 负缓存 + 柔性命中 | 失败可观测，热查询更快 |
| 证据两阶段 | Selection × Absorption + 共识 / 时效 |


### 问题重定义（第一性原理）

| 旧问题（错误） | 新问题（正确） |
|---|---|
| 怎么返回更多搜索结果？ | 怎么让 Agent **吸收到可核验的证据**？ |
| 域名权威高 = 可信 | 权威只是 **Selection 门槛**；还要 **Absorption 证据密度** |
| 被引用/被搜到 = 事实 | 被检索到 ≠ 进入答案；口径未对齐前禁止合并数字 |
| 单次泛查询够用 | 事实核查需要 **分层查询**（来源要求 / 对比 / 时间 / 主体） |

**一句话**：Argo 的产出是「证据候选 + 可信度分解」，不是「链接清单」。

### MECE 证据流水线

```
Query
  ├─ Clarify（意图是否可执行）
  ├─ Search Selection（引擎召回 + 域名权威 + SERP 剔除）
  ├─ Absorption（数字/定义/对比/披露密度；fetch 后 quality）
  ├─ Freshness（发布年；忽略「2015年以来」历史对比年）
  └─ Consensus（多可吸收域名佐证；社交仅叙事）
```

四块互不重叠、合起来覆盖「能不能用这条结果」。

### 量化公式（evidence v2.2）

```
selection  = authority_score（SERP/跳转链 ≤ 0.15）
absorption = evidence_density（has_numbers/definition/comparison/howto/disclose − qa）
freshness  = 发布年/URL年/完整日期
final      = 0.40·selection + 0.35·absorption + 0.15·freshness + 0.10·engine_score
```

搜索结果内嵌快评字段：`selection` / `absorption` / `credibility_fast` / `evidence_flags`。
完整交叉验证：`python3 scripts/evidence.py --stdin --json`。

### Agent 执行纪律

1. **高后果问题**：search → evidence（或看 `credibility_fast`）→ fetch 高分 URL → 再下结论  
2. **数字**：必须标注口径（全市场/主动/持仓市值 vs 占比）；冲突时并列  
3. **SERP 链**（baidu/s、sogou/link）：禁止当正文来源  
4. **社交帖**：叙事/舆情，不进事实真值  
5. **分层查询**：事实类 deep 至少 2–3 条子查询（来源 / 对比数据 / 关键主体）

### 能力清单

- **TF-IDF 语义路由**：二元组 + boost_keywords + boost_combos，< 5ms 延迟
- **Exa 语义搜索引擎**：embedding 匹配 + 内容摘要，中英文开放式调研首选（v2.3 新增）
- **搜狗微信搜索引擎**：weixin.sogou.com 公众号文章搜索，无需登录（v2.3 新增）
- **成本感知评分**：free=1.0 / low=0.7 / api=0.5 / paid=0.3 四档 cost_factor
- **预算模式**：fast / auto / deep / budget 四档配额追踪 + 自动降级
- **渐进式多源**：engines_combo 先快后全 + 并行模式
- **AnySearch 垂直域**：19 个垂直域结构化搜索，匿名兜底
- **双层缓存**：L1 LRU + L2 SQLite + gzip，分级 TTL
- **RRF 融合**：多引擎 Reciprocal Rank Fusion 去重合并
- **Bocha Reranker**：语义精排后处理
- **Selection×Absorption**：SERP 降权 + 证据密度 + 中文信源表（v2.2）
- **内容安全引擎**：抓取内容注入检测 + 编码归一化 + 语义意图分析 + 脱敏（70+ 中英模式）
- **查询变体生成**：无 LLM 六策略（问句化/概念扩展/反方观点/范围调整/缩写互换），深度研究多路召回
- **Wayback 快照回退**：HTTP 失败自动查 Wayback 最新快照兜底
- **加权 RRF（WG-RRF）**：按引擎来源质量加权融合（权威源 1.2-1.4、社交源 0.7-0.8），权威结果自然上浮
- **语义缓存**：minhash 字符 n-gram 近重复查询软命中（「苹果 2025 营收」可命中「苹果 2025 年营收」缓存），阈值 0.7 + 长度约束
- **自适应 TTL**：内容稳定的查询自动延长 TTL（×2），频繁变化的保持短 TTL，兼顾命中率与新鲜度
- **网络环境感知**：利用 adaptive 延迟数据推断网络环境——慢网自动放大超时预算（1.8×，避免误杀正常引擎）并偏好本地源，快网收紧超时（0.8×，更快响应）；路由层同族内快源前置（分数差 ≥0.15 才重排，缓存键稳定）
- **自适应引擎禁用**：引擎连续失败（熔断 open 达 3 次）自动进入 disabled 状态，后续搜索完全跳过（不再 half-open 探测白等超时）；有成功信号或新环境自动恢复。按当前网络环境个性化——DDG 不可用时自动跳过，恢复后自动启用
- **新垂直引擎**：GDELT 全球事件（事件/舆情/地理维度）、OpenCorporates 公司注册（尽调/反欺诈）、Google Patents 专利搜索（技术尽调/IP）
- **自适应学习**：success × latency × cost 三维评分，SQLite 持久化
- **社交引擎**：Twitter/Reddit/小红书/B站/微博 5 大平台原生搜索
- **引擎层 HttpClient 接入（v2.7.10）**：HTTP/HTML 类引擎 GET 请求统一走 `http_client`（UA 轮换 + Cookie 积累 + 429/503 Retry-After 尊重 + 指数退避重试 + 重定向跟随），修复了 `follow_redirects` 无效死参数（301/302 跟随到最终页）；实测 arxiv 类 UA 敏感引擎从 urllib 超时空返回（5s/0 结果）变为 2s 内 10 条有效结果；POST 引擎保留 urllib；`ARGO_ENGINE_HTTP_CLIENT=0` 可整体回退（灰度/诊断），测试默认走回退路径
- **TF-IDF 强语义注入（v2.7.10）**：TF-IDF 高分推荐（≥0.6）从 catch-all 域放宽到所有域，marginalia/wiby/searchmysite/lieu/open_meteo/usda/gov_policy/cnii/ndl/qiita 等 25 个垂直新引擎不再被正则域压制——「独立博客 长尾」路由到 marginalia、「营养成分 热量」路由到 usda、「国务院 政策」路由到 gov_policy；通用引擎（byted/bocha/octen 等）黑名单跳过，域主源保留备位不锁死；注入位置在 primary 扶正之后，串行 early-stop 下真正执行
- **env 占位缺失过滤（v2.7.10）**：`{GITHUB_TOKEN}` 等未配置的 env 占位替换为空并过滤空/认证前缀残留头（`token `、`Bearer ` 后无凭据）——github 引擎无 token 时从 401 恢复为匿名 API（0.55s 3 条结果），不再把字面量 `token {GITHUB_TOKEN}` 发给服务器
- **SSRF 防护（v2.7.1）**：fetch/crawl/http_client 统一拦截内网/私有地址（scheme 白名单 + 主机名黑名单 + DNS 解析 IP 段 + 重定向逐跳校验）；`ARGO_ALLOW_PRIVATE_URLS=1` 显式放行
- **路由健康语义修复（v2.7.1）**：健康过滤只作用于 local_* 子引擎；sub-skills 与 scripts 顶层模块名隔离（health_check 不再被劫持）；health_probe 只探测 local_*，消除慢源误报
- **macro_data 国家分流（v2.7.1）**：非美国宏观查询（中国GDP/日本通胀）worldbank 前置不再被 primary 扶正覆盖，避免 FRED 美国数据冒充
- **深度研究 local_first（v2.7.1）**：决策树先本地聚合，结果不足才升级全量；修复「先全量再本地」的浪费顺序
- **实时索引引擎（v2.7.3）**：`realtime_index` 免 Key 实时索引源，结构化 YAML 输出，结果带 `published_at` 发布时间维度；不参与日常 combo，显式 `--engine realtime_index` 指定。**部署要求**：需本机安装 `realtime-index` CLI（或把 spec 的 cmd 改为其绝对路径），未安装时引擎自动禁用、强制指定会回退通用引擎
- **时间窗过滤（v2.7.3）**：`--since`/`--until`（`7d` 或 `2026-08-01`）下推到支持时间窗的引擎，CLI 与 MCP 均支持；per-engine 缓存按时间窗隔离
- **时间能力补强（v2.7.4）**：时间窗从「下推支持引擎」扩展到「融合后兜底过滤」——任意引擎组合按 `published_at` 剔除明确超窗条目（宽松策略，无时间字段保留），返回包带 `time_filtered: N`；相对值归一化为绝对日期（`7d` 与等价日期共享缓存键，相对窗跨天不串旧数据）；`--until 2026-08-01` 含当天；`--sort newest|oldest` 按发布时间重排（最早出处用 `oldest`）；recovery 恢复路径保留时间窗不丢约束；`wayback_cdx` 输出标准 `published_at`（CDX 最早快照时间戳，最早出处召回前置）
- **时间窗按引擎能力生效（v2.7.5）**：时间窗的缓存键隔离与后过滤只对**带发布时间能力**的引擎组合生效（`realtime_index` / `wayback_cdx` / `local_search` 聚合 / news 类子引擎）；不带时间字段的引擎（octen/anysearch/bocha 等）忽略时间窗且结果相同，不再隔离缓存键——`--since 7d` 与 `--since 30d` 命中同一缓存，命中率提升；组合内无时间能力引擎时不重复告警「未实际过滤」（已知常态）
- **ddgs CLI 增强（v2.7.5）**：local-search 的 ddgs 引擎全部改 `-o json` 结构化输出（淘汰脆弱文本行解析，news 的 date 归一化为 `published_at`）；`use_lang_region` 按查询主语言下推 `-r region`（zh→cn-zh）；新增 `local_brave` / `local_yahoo` 两个免 key 快源（实测 ~1s）；ddgs 引擎 timeout 15→8s 与外层并行超时对齐
- **local-search 超时健壮性（v2.7.5）**：`as_completed` 超时不再整链崩溃——未完成引擎标记 timeout 记入 errors，已完成引擎结果保留（此前 ddgs 慢后端超时会拖垮整个 local_search 聚合）；`_check_cli_available` 结果缓存 60s（`ddgs --help` 冷启动 ~190ms/次不再重复付费）
- **TF-IDF profile 补全（v2.7.5）**：`aviation_weather` / `cn_ai_news` / `datacite` / `firecrawl` / `fxtwitter` / `sec_edgar` / `train` / `weather` / `zenodo` 9 个引擎补齐代表文档，语义路由不再永久选不中；韩文路由不再引用已禁用的 `local_google`（落 local_bing + local_duckduckgo）
- **ddgs CLI 失败识别与重试（v2.7.6）**：ddgs 9.14.4 起失败信号（`DDGSException`/`No results`/`ConnectError` 等）在 rc=0 时打到 stdout、输出文件 0 字节，旧逻辑只在 rc≠0 分支检查导致静默吞错（实测 yahoo 间歇丢结果）；现在无论 rc 先识别错误信号，失败/超时自动重试 1 次（yahoo 成功率 ~60%→84%、images ~80%→96%），持续失败返回明确错误而非误导消息
- **Anna's Archive 电子书引擎（v2.7.6）**：新增 `local_ddgs_books`（ddgs books 子命令），author/publisher/info 拼入 snippet，vertical 类别路由；默认关闭，需用时在 `sub-skills/local-search/config.yaml` 置 `enabled: true`
- **独立索引补充引擎（v2.7.7）**
- **多语言与开放数据引擎（v2.7.8）**
- **小型网络与垂直数据源（v2.7.9）**：新增 9 个免 key 引擎——小型网络域补全 `searchmysite`（人工审核个人站索引）与 `lieu`（webring 专用搜索），与 marginalia/wiby 构成「去商业化探索」四件套（how to/自己搭/原理类查询命中）；实时航班 `opensky`（主要都会区 ADS-B）、电网碳强度 `electricity_maps`、营养成分 `usda`（官方 DEMO_KEY）、双语例句 `tatoeba`（400+ 语言对）、科研数据集 `figshare`、腾讯 K 线 `tencent_kline`（前复权日 K）、`qq_music` 曲库搜索：新增 13 个免 key 引擎——中文政策库（国务院文件带发布日期）、日文三源（CiNii Research 学术/NDL 书目/Hatena 书签）、日文技术社区（Qiita）、韩国判例库（官方公开 demo 账号）、欧陆开放数据（data.gouv/EU ODP）、多语学术（DOAJ 开放获取期刊/HAL 法国仓储/DNB 德语书目）、Europeana 文化遗产、Open-Meteo 全球天气（地名→坐标→当前天气）：新增 `marginalia`（非大厂代理的独立爬虫索引，专挖长尾非商业页面）与 `wiby`（老式手工网页索引）两个免 key JSON 引擎，与主流引擎结果互补；语义路由 profile 已配（长尾/独立/复古类查询命中）
- **CLI 引擎声明式接入（v2.7.3）**：`output_format: yaml` 通用 YAML 解析 + `filter_args` 条件参数，新增 CLI 桥接引擎零 Python；模板 `engines/_template_cli.yaml`
- **裸命令引擎路径校验修复（v2.7.3）**：PATH 中的裸命令 CLI 引擎不再被配置校验误判为不存在而禁用（`shutil.which` 兜底）

### 用法

```bash
# 自动路由（推荐）
python3 scripts/search.py "查询词"

# JSON 输出（供 Agent 消费）
python3 scripts/search.py "查询词" --json

# 解释路由决策（含 TF-IDF 分数）
python3 scripts/search.py "查询词" --explain

# 强制引擎
python3 scripts/search.py "查询词" --engine anysearch
python3 scripts/search.py "查询词" --engine byted
python3 scripts/search.py "查询词" --engine arxiv
python3 scripts/search.py "查询词" --engine eastmoney
python3 scripts/search.py "查询词" --engine zhihu
python3 scripts/search.py "知乎热榜" --engine zhihu_hot
python3 scripts/search.py "查询词" --engine bocha
python3 scripts/search.py "查询词" --engine exa
python3 scripts/search.py "查询词" --engine wechat_sogou
python3 scripts/search.py "查询词" --engine hackernews
python3 scripts/search.py "查询词" --engine stackoverflow
python3 scripts/search.py "查询词" --engine google_scholar
python3 scripts/search.py "查询词" --engine v2ex
python3 scripts/search.py "查询词" --engine realtime_index   # 实时索引源，结构化输出带发布时间

# 本地零成本优先（local_search 聚合）
python3 scripts/search.py "查询词" --local-first
python3 scripts/search.py "查询词" --local-first --mode fast

# 预算模式
python3 scripts/search.py "查询词" --mode fast     # 免费优先，自动前置 local_search
python3 scripts/search.py "查询词" --mode auto     # 成本感知（默认）
python3 scripts/search.py "查询词" --mode deep     # 质量优先
python3 scripts/search.py "查询词" --mode budget   # 配额控制

# 跳过缓存 / 搜索深度 / 列出引擎 / 配额状态
python3 scripts/search.py "查询词" --no-cache
python3 scripts/search.py "查询词" --depth fast|balanced|deep
python3 scripts/search.py --list-engines
python3 scripts/quota.py stats

# 时间窗过滤（7d 相对值或 YYYY-MM-DD 绝对日期；支持引擎下推 + 融合后兜底过滤）
python3 scripts/search.py "rust async" --engine realtime_index --since 7d
python3 scripts/search.py "rust async" --since 2026-08-01 --until 2026-08-05
# 时间方向排序：newest 找最新动态，oldest 找最早出处
python3 scripts/search.py "rust async" --sort oldest --json
# MCP argo_search 同样支持 since/until/sort 参数

# AnySearch 垂直域搜索
python3 scripts/search.py "AAPL" --domain finance --sub_domain finance.us_stock

# TF-IDF 路由测试
python3 scripts/tfidf_router.py "查询词"

# ── 深度研究（问题分解→多源采集→综合报告）──
python3 scripts/research.py "CRISPR脱靶效应AI预测方法综述"
python3 scripts/research.py "CVE-2024-6387 生产环境影响" --depth deep --json
python3 scripts/research.py "React vs Vue 2025 生产环境对比" --sub-queries 5

# ── 来源可信度评估 ──
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json

# ── 意图消歧 ──
python3 scripts/clarify.py "Python 吞苹果 兼容吗" --explain
python3 scripts/clarify.py "苹果股价" --json
```

### 答案型垂直源（v2.5.1–v2.6.0）

| 引擎 | 方向 | 说明 |
|------|------|------|
| sina_quote / tencent_quote / em_flow | A 股行情 | 快照与资金流，日常优先 |
| finviz | 美股 | 美股域专线 |
| imdb | 影视 | 导演 / 主演 / 影片实体 |
| thesportsdb | 体育 | 球员 / 球队 / 赛事 |
| local_openstreetmap | 地理 | 地标与地点 |
| wikidata | 组织 / 实体 | 机构与结构化实体 |
| itunes / musicbrainz | 媒体音乐 | 专辑 / 艺人 |
| fred / worldbank / nbs_stats / eurostat / fx_rate | 宏观与汇率 | 数据问句直达 |
| pubchem | 化学 | 化合物 / 分子式 |
| gbif / rfc_editor | 物种 / 标准 | 窄域直达 |
| weread / douban_book / cninfo | 图书与公告 | 按域启用 |
| bocha_ai | 垂直结构化模态卡 | 火车票 / 油价 / 贵金属 / 万年历 / 星座 / 手机参数 / 汽车 / 医疗挂号等结构化卡片（见下方「垂直结构化模态卡」） |

日常 `depth=fast` 会收紧 combo；`research` / `deep` 再放开长尾（如 seeking_alpha、archive 等）。

### 垂直结构化模态卡（modal_card，v2.7 新增）

一类查询需要**实时结构化卡片**（而非网页列表），统一由 `modal_card` 域识别并路由到 `bocha_ai` 引擎（失败自动回落 `bocha` web 搜索）：

- 火车票 / 高铁：`火车票`、`高铁票价`、`车次`、`高铁时刻`
- 油价：`今日油价`、`成品油`、`汽油/柴油价格`
- 贵金属：`今日金价`、`银价`、`铂金`、`钯金`
- 黄历万年历：`黄历`、`万年历`、`宜忌`、`农历查询`、`黄道吉日`
- 星座生肖：`今日运势`、`星座运势`、`生肖运程`、`属相运势`
- 手机参数：`手机参数`、`手机配置`、`手机对比`、`手机报价`
- 汽车：`汽车报价`、`车型价格`、`落地价`、`汽车行情`
- 医疗挂号：`挂号`、`医院预约`、`三甲医院`、`门诊时间`

未命中的普通查询不受影响（如「北京天气」仍走 `weather_query`、「今日A股」仍走 `stock_query`）。卡片结果为结构化值，标题无法提取时用 `card_type` 兜底参与 RRF 去重。

### 引擎全景（摘要；全量约 120+，以 `config.yaml` / `--list-engines` 为准）

| 引擎 | cost_tier | 特点 | 延迟 |
|------|-----------|------|------|
| anysearch | free | 垂直领域通用 | ~2.7s |
| zhihu | free | 知乎观点 | ~700ms |
| zhihu_hot | free | 知乎热榜（hot_list，日约 100 次） | ~500ms |
| eastmoney | free | 金融数据 | ~400ms |
| arxiv | free | 学术论文 | ~1s |
| duckduckgo | free | 快速事实 | ~500ms |
| octen | free | 中文网页 | ~800ms |
| semantic_scholar | free | 学术+引用 | ~1s |
| openalex | free | 2.5亿+论文 | ~2s |
| crossref | free | DOI/引用元数据 | ~2s |
| github | free | 代码搜索 | ~1s |
| wikipedia | free | 百科事实 | ~500ms |
| imdb | free | 影视实体（导演/主演） | ~1s |
| thesportsdb | free | 体育实体（球员/球队） | ~1s |
| bocha | low | 中文网页(AI友好) | ~1s |
| bocha_ai | low | 垂直结构化模态卡(实时值) | ~12s |
| byted | low | 字节搜索 | ~1s |
| tavily | paid | 国际搜索 | ~2s |
| exa | api | 语义搜索(embedding+内容摘要) | ~6s |
| wechat_sogou | free | 搜狗微信搜索(公众号文章) | ~2s |
| hackernews | free | Hacker News(科技新闻+讨论) | ~2s |
| marginalia | free | Marginalia 独立索引(长尾非商业页面) | ~1s |
| wiby | free | Wiby 手工网页索引(复古非商业化站点) | ~1s |
| gov_policy | free | 中国政府网政策库(国务院文件带日期) | ~0.3s |
| qiita | free | Qiita(日本技术社区) | ~0.6s |
| cnii | free | CiNii Research(日文学术) | ~0.4s |
| ndl | free | NDL 国立国会图书馆(日本书目) | ~0.3s |
| kor_law | free | 韩国判例库(判例全文) | ~0.8s |
| hatena_bookmark | free | Hatena Bookmark(日本书签) | ~1.2s |
| dnb | free | DNB 德国国家图书馆(德语书目) | ~3s |
| doaj | free | DOAJ 开放获取期刊(80+语种) | ~0.4s |
| europeana | free | Europeana 欧洲文化遗产 | ~0.9s |
| hal | free | HAL 法国学术仓储 | ~0.9s |
| eu_opendata | free | EU Open Data Portal(24语言) | ~0.6s |
| open_meteo | free | Open-Meteo 全球天气(地名→天气) | ~1s |
| fr_opendata | free | 法国政府开放数据目录 | ~2.9s |
| searchmysite | free | SearchMySite 个人站索引(独立博客) | ~0.8s |
| lieu | free | Lieu webring 搜索(小众站点) | ~1.6s |
| opensky | free | OpenSky 实时航班(都会区 ADS-B) | ~0.8s |
| electricity_maps | free | Electricity Maps 电网碳强度 | ~0.7s |
| usda | free | USDA 营养成分库(官方 DEMO_KEY) | ~1.5s |
| tatoeba | free | Tatoeba 双语例句(400+语言对) | ~1.6s |
| figshare | free | Figshare 科研数据集 | ~1.1s |
| tencent_kline | free | 腾讯 K 线(前复权日K) | ~0.5s |
| qq_music | free | QQ 音乐曲库搜索 | ~0.9s |
| stackoverflow | free | Stack Overflow(编程问答) | ~2s |
| google_scholar | free | Google Scholar(学术论文) | ~3s |
| v2ex | free | V2EX(中文技术社区) | ~2s |
| ths_hot | free | 同花顺热点(强势股+题材归因) | ~2s |
| cls_telegraph | free | 财联社电报(实时财经快讯) | ~2s |
| em_global_news | free | 东财全球资讯(7×24快讯) | ~2s |
| realtime_index | free | 实时索引源（需本机安装 realtime-index CLI，未安装自动禁用） | ~1s |
| local_brave / local_yahoo | free | ddgs brave/yahoo 后端（免 key 快源） | ~1s |
| **Bocha Reranker** | low | 语义精排（后处理） | ~500ms |

### Exa 语义搜索引擎（v2.3 新增）

Exa 是基于向量 embedding 的语义搜索引擎，核心能力：
- **语义匹配**：不使用关键词，而是理解查询意图，在 embedding 空间中找到最相关的页面
- **内容摘要**：每条结果自带 `text` 字段（页面正文摘要），减少 fetch 环节
- **中英文均可**：中文搜索质量超预期（embedding 模型对中文友好）

**免费额度**：1000 次/月，超出后需升级付费计划。

### 搜狗微信搜索引擎（v2.3 新增）

通过搜狗微信搜索（weixin.sogou.com）直接抓取公众号文章，无需登录、无需 API key。
返回字段：`title`、`url`、`snippet`、`account`（公众号名）

> 注意：搜狗微信搜索受验证码限制（`weixin.sogou.com/link` 跳转常被拦截），此引擎
> 只能拿到标题列表；需要公众号全文时用下面的 `argo article`。

### 微信公众号文章全文抓取（`argo article`，v2.7.3+）

按 URL 抓取微信公众号文章全文，弥补 `wechat_sogou` 只能搜标题、且跳转被验证码拦截的缺口。

```bash
# 纯文本输出（标题/作者/正文/图片列表）
argo article "https://mp.weixin.qq.com/s/XXXX"

# JSON 输出（供 Agent 消费：title/author/publish_time/char_count/image_count/images/content）
argo article "https://mp.weixin.qq.com/s/XXXX" --json
```

实现原理：公众号 robots.txt 禁爬，但文章页对真实浏览器开放。`scripts/article.py`
用手机 UA 直连（urllib 不遵守 robots），解析 `js_content` 节点提取正文纯文本与
`mmbiz.qpic.cn` 图片 URL 列表。实测同一文章页连续抓取 3/3 成功，单篇约 2-3 秒。

失败处理：返回 `{"ok": false, "error": ...}`；遇到微信「环境异常」反爬页会明确报错，
稍后重试即可（触发频率低）。

与 `wechat_sogou` 的分工：搜狗引擎负责「按关键词发现公众号文章标题」，
`argo article` 负责「按 URL 取全文」，两者组合覆盖公众号检索全链路。

### 招聘岗位多平台聚合搜索（`argo job`，v2.7.3+，v2 于 2026-08-11 升级）

求职搜索 = 检索（招聘平台）+ 判定（地区）+ 呈现（去重排序），脚本只做这三件事。
猎聘/智联/BOSS 直聘等平台反爬强，免费 HTML 引擎抓不动；用 AI 搜索 API 检索
（均走环境变量 key）：

```bash
argo job "工艺工程师" --city 成都                  # 严格：只留成都岗位
argo job "工艺工程师" --city 昆山 --engine exa     # 县级地区
argo job "会计" --city 新加坡                     # 海外：自动启用免 key 国际源
argo job "电工" --city 苏州 --loose                # 宽松：异地保留但命中置顶
argo job "焊工" --city 上海 --platforms zhipin,liepin
argo job "remote engineer" --engine free          # 仅免 key 国际源（远程岗位）
```

地区识别**完全数据驱动，零硬编码**（技能可安全分享）：
- `data/regions_cn.json`：全国 31 省 / 342 地市 / 3056 区县（民政部公开区划数据）
- `data/countries.json`：主要国家与城市词表（中英文）
- 任意省/市/县/海外国家/城市直接作 `--city`：省级展开全省地市+区县，
  地市级展开该市+区县，区县级关联所属市和省；海外国家展开主要城市

默认行为（严格模式）三级判定：标题/URL 命中（L1）→ 摘要头部命中（L2）→ 剔除；
政府人社局源（hrss/rsj/rlsbj 域名）信任不误剔；`--loose` 切换宽松（全部保留，命中置顶）。
v2 修复 v1 假阳性：摘要尾部（公司简介/福利文本）含地区词不再误判命中，
非白名单 URL 一律剔除，资讯/攻略页（工资待遇/就业前景）与「职位已关闭」岗位剔除，
距今超 365 天标记 `[过期]` 垫底。实测 2026-08-11 五城市严格模式全部结果
L1/L2 命中（昆山 54 条 / 上海 49 条 / 深圳 57 条 / 北京 56 条 / 成都 50 条）。

后端（v2 全量启用，国内 5 API 后端 + 海外免 key 源）：

| 后端 | 域名过滤 | 特点 |
|------|---------|------|
| exa（EXA_API_KEY） | includeDomains 严格 | 单请求覆盖全部白名单，语义准 |
| tavily（TAVILY_API_KEY） | include_domains 严格 + days=90 | 时效过滤实测生效 |
| byted（WEB_SEARCH_API_KEY） | site: 逐平台 + URL 后置校验 | 混入非白名单域名由校验剔除 |
| bocha（BOCHA_API_KEY） | 宽查询 + 白名单校验 | 中文站点发现强（鱼泡/人社局/卓博） |
| octen（OCTEN_API_KEY） | 宽查询 + 白名单校验 | 高速语义搜索 |
| remotive / himalayas / jobicy / arbeitnow / greenhouse | 免 key 公开 JSON | 远程/海外岗位，海外城市自动启用 |

平台白名单：核心 6 平台（BOSS/猎聘/智联/前程无忧/597/今日招聘）+ 扩展域
（卓博 jobcn、鱼泡 yupao、中华英才 chinahr、智通 job5156、58 同城、国聘 iguopin、
新职业 24365 ncss、校园就业联盟 91job、JobsDB、JobStreet、苏州人社局 hrss.suzhou.gov.cn），
政府人社局域名（hrss/rsj/rlsbj 开头）通用识别。

常用参数：`--engine {all,free,exa,tavily,byted,bocha,octen,remotive,...}`
（默认 all = 五个 API 后端并行，海外城市自动加 free 免 key 源）、
`-n` 每后端条数（默认 5）、`--platforms zhipin,liepin,zhaopin,51job,597,jrzp`、
`--loose`、`--json`（默认文本输出）。地区索引一次性构建，查表 O(1)。
测试：`tests/test_job.py`（29 项，含三城市 live 精确率回归）。

### Python 解释器选择（v2.7.3+）

macOS 默认 `python3` 指向系统 3.9（`/usr/bin/python3` 在 PATH 靠前），
而 argo 脚本使用 3.10+ 语法（`X | None` 联合类型）。`bin/argo` 启动时自动探测：
`ARGO_PYTHON` 环境变量优先 → `python3.14/3.13/3.12/3.11/3.10` 依次验证
yaml/requests 依赖 → 兜底当前解释器。如需强制指定：`ARGO_PYTHON=/opt/homebrew/bin/python3.14 argo ...`

### Hacker News 搜索（v2.3 新增）

通过 Algolia API 搜索 Hacker News，覆盖科技新闻和讨论。
返回字段：`title`、`url`、`snippet`（score/comments/author）

### Stack Overflow 搜索（v2.3 新增）

通过 Stack Exchange API 搜索编程问答，覆盖技术问题和解决方案。
返回字段：`title`、`url`、`snippet`（score/answers/tags）

### Google Scholar 搜索（v2.3 新增）

通过 HTTP 页面解析搜索 Google Scholar，覆盖学术论文。
返回字段：`title`、`url`、`snippet`（论文摘要）

### V2EX 社区搜索（v2.3 新增）

搜索 V2EX 中文技术社区讨论。
返回字段：`title`、`url`、`snippet`

```bash
# 强制使用某个引擎
python3 scripts/search.py "查询" --engine hackernews
python3 scripts/search.py "查询" --engine stackoverflow
python3 scripts/search.py "查询" --engine google_scholar
python3 scripts/search.py "查询" --engine v2ex

python3 scripts/search.py "查询词" --engine v2ex
python3 scripts/search.py "查询词" --engine ths_hot
python3 scripts/search.py "查询词" --engine cls_telegraph
python3 scripts/search.py "查询词" --engine em_global_news

# 查看配额
python3 scripts/quota.py stats

### 社交平台引擎（v2.1 新增）

| 引擎 | cost_tier | 特点 | 认证 |
|------|-----------|------|------|
| twitter | free | Twitter/X 推文搜索 | 可选（nitter 兜底） |
| reddit | free | Reddit 帖子+评论 | 无需认证 |
| xiaohongshu | free | 小红书笔记+评论 | xhs login |
| bilibili | free | B站视频+弹幕 | 无需认证 |
| weibo | free | 微博帖子+话题 | 无需认证 |

社交引擎统一输出 `social_meta` 字段，包含作者、互动数据（点赞/评论/转发）、平台元信息。

### 垂直内容引擎（v2.4 新增）

新增 7 个免认证垂直引擎，覆盖网络梗/俚语与全网热榜两个新方向：

| 引擎 | cost_tier | 特点 | 数据源 |
|------|-----------|------|--------|
| itotii | free | 中文流行语/网络梗词条 | geng.itotii.com WordPress REST |
| urban_dictionary | free | 英文俚语定义+例句 | api.urbandictionary.com |
| know_your_meme | free | 英文 meme 溯源（作者+条目） | knowyourmeme.com HTML 解析 |
| zh_wikipedia | free | 中文维基百科 | zh.wikipedia.org MediaWiki |
| baidu_hot | free | 百度实时热搜榜 | top.baidu.com HTML 解析 |
| toutiao_hot | free | 今日头条热榜 | toutiao.com hot-board JSON |
| bilibili_hot | free | B站热搜词 | api.bilibili.com search/square |

路由规则：`meme_slang` 域（梗/俚语/黑话/流行语/玩梗等触发词）→ itotii + urban_dictionary + know_your_meme；
`hot_trending` 域（热搜/热榜/热门话题等触发词）→ baidu_hot + toutiao_hot + bilibili_hot + ths_hot。
三个热榜引擎与既有 ths_hot 同型：拉取当前榜单，查询词仅作路由触发，返回 top-N 热搜词条。
调用示例：`python3 scripts/search.py "这是什么梗" --engine itotii`、`python3 scripts/search.py "今天热搜"`。

### 广义垂直引擎（v2.5 新增）

在 v2.4 的梗/热榜细分类之外，新增 9 个免认证的广义垂直引擎，覆盖媒体、图书、包管理、
实体、词典、百科、加密七大方向，全部免 API key：

| 引擎 | cost_tier | 特点 | 数据源 |
|------|-----------|------|--------|
| itunes | free | 音乐/影视/播客/应用 | itunes.apple.com/search |
| openverse | free | 开放版权图片/素材 | api.openverse.org/v1/images |
| coingecko | free | 加密货币/币种 | api.coingecko.com/api/v3/search |
| wikidata | free | 结构化知识实体 | www.wikidata.org wbsearchentities |
| crates | free | Rust 包/库（需 UA） | crates.io/api/v1/crates |
| musicbrainz | free | 音乐人/作品元数据（限速 1rps） | musicbrainz.org/ws/2/artist |
| open_library | free | 图书（作者·年份） | openlibrary.org/search.json |
| free_dictionary | free | 英英词典（词义+例句） | api.dictionaryapi.dev |
| moegirl | free | 萌娘百科（ACG 词条） | zh.moegirl.org.cn HTML 解析 |

### P0/P1 扩展引擎（v2.6 新增）

复活学术双引擎 + 新增 15 个免认证垂直源，补齐包管理、文档、模型、中文百科/社区、医学、游戏与预测市场：

| 引擎 | cost_tier | 特点 | 数据源 |
|------|-----------|------|--------|
| openalex | free | 2.5 亿+论文索引（复活） | api.openalex.org/works |
| crossref | free | DOI/引用元数据（复活） | api.crossref.org/works |
| europepmc | free | 生物医学文献（SS 429 后备） | ebi.ac.uk/europepmc |
| dblp | free | CS 文献（偶发 SSL 抖动） | dblp.org/search/publ/api |
| baidu_baike | free | 百度百科词条 | baike suggest + OpenAPI |
| pypi | free | Python 包精确查询 | pypi.org/pypi/{name}/json |
| npm | free | JS 包搜索 | registry.npmjs.org |
| huggingface | free | ML 模型检索 | huggingface.co/api/models |
| mdn | free | Web 官方文档 | developer.mozilla.org API |
| juejin | free | 中文技术文章 | api.juejin.cn search |
| docker_hub | free | 容器镜像 | hub.docker.com v2 search |
| devto | free | 英文技术文 | dev.to/api/articles/search |
| clinicaltrials | free | 临床试验 | clinicaltrials.gov v2 |
| openfda | free | 药品标签 | api.fda.gov/drug/label |
| archive_org | free | 互联网档案馆 | archive.org advancedsearch |
| steam | free | 游戏商店 | store.steampowered.com |
| polymarket | free | 预测市场（叙事，非真值） | gamma-api public-search |

路由（先匹配先命中）：
`package_search` → pypi + npm + crates + docker_hub + github；
`web_docs` → mdn + stackoverflow；
`ml_models` → huggingface + github；
`cn_tech_community` → juejin + v2ex + devto；
`medical` → clinicaltrials + openfda + local_pubmed；
`cn_encyclopedia` / `entity_search` → baidu_baike + 维基系；
`academic` → arxiv + openalex + crossref + europepmc + dblp + SS；
`game_search` → steam；`prediction_market` → polymarket；`web_archive` → archive_org。

实现：字段解析支持点分路径与根数组；baidu_baike / pypi / clinicaltrials / openfda / juejin 为专用 builder；
其余声明式 `output_map`。调用示例：
`python3 scripts/search.py "httpx" --engine pypi`、
`python3 scripts/search.py "Fetch API" --engine mdn`、
`python3 scripts/search.py "bert" --engine huggingface`。

### 标准化：单一真源（v2.6，v2.7.3 修正）

磁盘上只保留**一份** argo 代码（本仓库）。引擎注册在仓库内派生；宿主入口用符号链接指回真源。**禁止** rsync/多副本；**禁止**在产品代码里写死主机 skill 路径。

**引擎声明的第二真源（外置 specs）**：`engines/specs/*.yaml` 是合法的外置引擎声明目录（`config.py._merge_external_engines` 启动时合并，外置**优先覆盖**同名引擎）。当前 10 个引擎声明在外置目录：`aviation_weather` / `cn_ai_news` / `train` / `weather` / `datacite` / `firecrawl` / `fxtwitter` / `realtime_index` / `sec_edgar` / `zenodo`。新增引擎可任选 config.yaml 或 engines/specs/（外置适合需独立版本管理的试验性引擎）；`sync_backends.py --check` 会校验两源合并后的注册表一致性。

| 层 | 真源 / 工具 | 说明 |
|----|-------------|------|
| 代码 | 本仓库根目录 | 唯一可改业务代码的位置 |
| 引擎声明 | `config.yaml` → `engines:` | type / enabled / label / cost_tier / qps / … |
| 注册表派生 | `python3 scripts/sync_backends.py` | → `backends/*`；`--check` 只校验 |
| 宿主入口 | `python3 scripts/link_source.py` | 把**调用方声明的**路径 symlink 到真源；目标仅来自 `--to` / `ARGO_LINK_TARGETS` / 本机 `installs.local.yaml`（gitignore） |
| MCP | 客户端配置 `…/scripts/mcp_server.py` | 路径由宿主配置，README 用 `/path/to/argo` 占位 |

曾分散在各 skill 副本里的引擎（`octen` / `finviz` / `jin10` / `models_dev` / `qweather` / `wenshu` / `seeking_alpha` / `zhihu_global`）已并回本仓库，与 P0/P1 同一套声明与 `_BUILDERS`。

**新增一个搜索源的标准流程**：

```bash
# 1. 只在本仓库改 config.yaml engines（必要时 engines.py 注册 builder）
# 2. 派生 backends
python3 scripts/sync_backends.py && python3 scripts/sync_backends.py --check
# 3. （可选）本机入口指回真源——路径由你声明，脚本无默认地址
#    cp installs.local.yaml.example installs.local.yaml  # 填写 link_targets
python3 scripts/link_source.py --check
# 4. 回归
python3 scripts/search.py "httpx" --engine pypi --json
python3 -m pytest tests/ -q
```

需要新解析逻辑时才在 `scripts/engines.py` 的 `_BUILDERS` 注册新 builder，否则纯声明即可。

### 三大增强工具（v2.0 新增）

| 工具 | 功能 | 适用场景 | Token 开销 |
|------|------|---------|-----------|
| `research` | 问题分解→多源并行采集→综合报告+引用+知识缺口 | 学术综述、事实核查、竞品分析、技术选型 | ~700/次 |
| `evidence` | 权威性+时效性+交叉验证的综合可信度评分 | 高后果决策、学术引用、新闻真伪 | ~300/次 |
| `clarify` | 歧义检测+意图分类+推荐路由策略 | 歧义查询、意图不明确、多语言混合 | ~200/次 |

#### research — 深度研究

```bash
# 自动分解问题，多源采集
python3 scripts/research.py "你的复杂查询"

# 控制子查询数量和搜索深度
python3 scripts/research.py "查询" --sub-queries 5 --depth deep

# JSON 输出供 Agent 消费
python3 scripts/research.py "查询" --json
```

输出包含：`key_findings`（按子查询分组的关键发现）、`citations`（引用列表）、`gaps`（知识缺口）、`source_distribution`（来源统计）。

**科研方法论增强**：
- `coverage_map`：各子查询覆盖状态（COVERED / PARTIAL / NOT_COVERED），即维度覆盖地图
- `verification_records`：claim-to-source 验证记录表（主张 / 来源 / 证据强度 / 核验方法 / 可核验性）
- `blind_spots`：显式盲区（未覆盖维度 + 单来源维度），不把「没搜到」写成「不存在」
- 证据强度分层：primary / secondary / tertiary / unknown（对照 topic profile 的 source_grades）

**Verify 阶段（cross_verification）**：研究报告内嵌 Selection×Absorption 可信度评分与多源交叉验证——
- `corroboration_level`：佐证强度（strong / moderate / weak / minimal / insufficient）
- `cross_score`：交叉验证分（0-1）
- `top_sources`：按可信度 final 排序的前 5 来源
- `conflicts`：同维度内低证据层级来源混入标记（blog/forum/social）
- `unverified_count`：无结果可核实的维度数

**事实对齐（fact_alignment）**：深度研究报告内嵌跨源事实交叉标记（v2.2 fact_align 现已接入 research 报告）——
- 从 title+snippet 抽取结构化事实：版本号 / 百分比 / 金额 / 日期 / 法规号（MECE 正交类型）
- `fact_conflicts`：同类型出现 ≥2 个不同值 → 标记冲突（如营收 1024.66 vs 1094），提示 Agent 谨慎，不盲目采信单源
- `fact_corroborated`：同值出现在 ≥2 个域名 → 标记印证，提升可信度
- `stats`：facts_extracted / conflicts / corroborated 计数
- 仅 auto/deep 且结果 ≥3 时启用；fast 跳过（控制延迟）

```bash
# 固定工具预算：子查询上限，超出后停止派发并标记 budget_exhausted
python3 scripts/research.py "查询" --budget 3

# 决策树路由：先零成本本地源，结果不足 3 条自动升级通用/垂直源
python3 scripts/research.py "查询" --route-strategy local_first
python3 scripts/research.py "查询" --route-strategy cost_aware  # fast 模式自动本地优先（默认）
python3 scripts/research.py "查询" --route-strategy full         # 全量（deep 研究）
```

**预算语义**：`--budget N` 限制实际执行的子查询数（每个子查询计 1 次工具调用），超限时报告标记 `budget.exhausted=true` 并输出基于已完成查询的最佳部分答案，未覆盖维度见 `blind_spots` / `gaps`。

#### evidence — 可信度评估（v2.2 Selection×Absorption）

```bash
# 对搜索结果进行可信度评分
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json

# 高后果模式（共识微调）
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json --high-stakes
```

输出包含：
- `credibility.final` / `selection` / `absorption`
- `authority`（含 `is_serp`）
- `freshness`（忽略「YYYY年以来」历史对比年）
- `evidence_density`（has_numbers / has_comparison / …）
- `cross_validation`（可吸收域名数）

中文信源覆盖与降权表：`backends/source_types_cn.json`。

#### clarify — 意图消歧

```bash
# 分析查询歧义和意图
python3 scripts/clarify.py "有歧义的查询" --explain --json
```

输出包含：`ambiguities`（歧义词+可能含义+置信度）、`intents`（意图分类）、`recommended_strategy`（推荐策略：clarify_first/deep_research/split_search/direct_search）。

#### social-sentiment — 社交舆情研究（v2.1 新增）

```bash
# 跨平台舆情分析
python3 scripts/research.py "iPhone 16 用户评价" --mode social-sentiment --platforms xiaohongshu,reddit,twitter

# JSON 输出
python3 scripts/research.py "AI Agent 产品口碑" --mode social-sentiment --json
```

输出包含：`platform_breakdown`（各平台帖子数）、`engagement_totals`（互动数据汇总）、`top_topics`（高频讨论话题）、`cross_platform_posts`（代表性内容）。

### MCP 服务

十个工具同时暴露为 MCP server（JSON-RPC over stdio），可被 Grok/Claude/Kimi 等客户端直接调用：

```bash
# 启动 MCP 服务
python3 scripts/mcp_server.py

# 本地测试
python3 scripts/mcp_server.py --test
```

MCP 工具名：`argo_search`、`argo_local_search`（本地文件/记录搜索，非联网）、`argo_research`（含 social-sentiment 模式）、`argo_evidence`、`argo_clarify`、`argo_crawl`、`argo_fetch`（mode=extract 结构化提取）、`argo_screenshot`、`argo_pdf`、`argo_social_search`（mode=sentiment 舆情聚合）。

### 成本感知路由公式

```
score = quality × cost_factor

cost_factor:
  free  = 1.0   (anysearch/zhihu/eastmoney/arxiv/duckduckgo/octen/local_*...)
  low   = 0.7   (bocha/bocha_ai/byted)
  api   = 0.5   (exa — 有限额度的免费引擎)
  paid  = 0.3   (tavily)
```

### 预算模式

| 模式 | 说明 | 触发条件 |
|------|------|---------|
| fast | 免费引擎优先，禁用付费 | 简单查询 |
| auto | 成本感知评分（默认） | 普通查询 |
| deep | 质量优先，忽略成本 | 深度研究 |
| budget | 配额控制，用完降级 | 配额紧张 |

### Local Search 子技能与 SearXNG 替代策略

local-search 是 argo 内置的「零成本聚合后端」，不依赖独立的 SearXNG 服务：

- **33 个本地引擎，29 个默认启用**：覆盖 web_general、chinese、academic、news、code、reference、vertical 七大类，
  统一通过 HTML/RSS/JSON/XML/CLI 解析公开页面；其中 10 个走 ddgs CLI（text 五后端 bing/yandex/brave/yahoo/duckduckgo + news/images/videos/books），
  结构化 JSON 输出、按查询语言自动下推 region 参数、失败自动重试 1 次。
- **引擎注册表**（`sub-skills/local-search/engine_registry.py`）：唯一真源，加载
  `config.yaml` + `parse_maps.yaml`；新增引擎只需改 YAML。
- **健康探针**（`sub-skills/local-search/local_health_check.py`）：canary 查询 + 反爬/拦截检测，
  状态缓存 5 分钟；连续 2 次失败或单次 >8s 标记 unavailable，成功 1 次恢复。
  fast/budget 模式下只检查实际要用的引擎，避免全量探针拖慢响应。
- **智能路由**（`sub-skills/local-search/smart_router.py`）：根据查询特征自动选择最优本地引擎组合。
- **统一 schema**：输出与 argo 完全一致，直接参与 RRF 融合与 Bocha reranker。

**SearXNG 替代说明**：当 SearXNG 未启用或不可用时，argo 在 `fast`/`budget` 模式
下会自动将 `local_search` 加入 `engines_combo` 首位，实现同等零成本聚合效果，无需运行
SearXNG 实例。强制使用本地聚合：

```bash
python3 scripts/search.py "查询词" --local-first
```

### local-seek 子技能（本机文件搜索，2026-08-05 收编）

local-seek 是原创的本机文件搜索技能（2026-08-03 设计落地，独立演进至今），2026-08-05 收编为 argo 子技能，物理位于 `sub-skills/local-seek/`：

- **三层渐进式**：L1 定位（`--count`/`--filename`）→ L2 上下文（`--context`）→ L3 精读（`--lines`），工具输出即答案，零探索成本。
- **路由**：rg（正文）→ fd（文件名）→ mdfind（Spotlight 全盘兜底）；中文查询「精确优先、2-gram 扩展兜底」（`--exact` 关闭）。
- **扩展能力**：`--structural` 结构搜索（裸 except/空 catch/装饰函数等）、`--git-log`/`--git-blame`、`--outline`、`--domains`。
- **MCP 接入**：`argo_local_search` 工具探测 `sub-skills/local-seek/scripts/seek.py` 后 subprocess 调用，结果包装为 argo 风格（`file://` URL + `source=local_files`），与 `argo_search`（网络）互补。

```bash
python3 sub-skills/local-seek/scripts/seek.py "查询词" --path ~/notes --count
```

### ego-search 子技能（登录态专业搜索，完全态 dual runtime / 1.6.0）

ego-search 是 argo 的**登录态专业搜索**子技能，物理位于 `sub-skills/ego-search/`：

- **双运行时（都保留）**：ego lite（[lite.ego.app](https://lite.ego.app/)）+ WebBridge（[Kimi WebBridge 官方帮助中心](https://www.kimi.com/zh-cn/help/kimi-webbridge/kimi-webbridge-introduction)）；**任一可用**即可；`--runtime auto|ego|webbridge`。
- **与常规检索隔离**：`search_partition=login`、`cache_eligible=false`，禁止写公共 SearchCache。
- **汇总融合**：`merge --public a.json --login b.json`（分析层；不去污染缓存）。
- **登录稳定**：`--site host` 粘性空间 + 默认 keep；fetch 挂 `quality.login_likely_ok`。
- **安全**：URL 守卫、专业模式闸门、任务空间默认收尾。
- **专业模式默认关**：`enable` / `disable` / `status`。

```bash
python3 sub-skills/ego-search/scripts/ego_search.py status
python3 sub-skills/ego-search/scripts/ego_search.py search "AI 搜索" --runtime auto
python3 sub-skills/ego-search/scripts/ego_search.py fetch "https://www.zhihu.com/..." --site zhihu.com
python3 sub-skills/ego-search/scripts/ego_search.py merge --public /tmp/p.json --login /tmp/l.json
```

### 文件结构

```
argo-v2/
├── SKILL.md              # 本文件 — 技能注册文档
├── config.yaml           # 引擎配置 & 路由规则
├── backends/
│   ├── domain_profiles.json   # TF-IDF 领域文档（sync 派生/校验）
│   ├── engine_registry.yaml   # 引擎注册表文档（sync 派生）
│   └── quota_profiles.json    # 配额配置（sync 派生）
├── scripts/
│   ├── sync_backends.py   # 注册表派生 + 一致性校验（单一真源 config.yaml）
│   ├── link_source.py     # 宿主入口 symlink 到真源（目标由调用方声明）
│   ├── config.py         # 配置加载器
│   ├── search.py         # CLI 入口 & 执行编排
│   ├── route.py          # 三层路由决策
│   ├── engine_families.py # 能力族分类（16 族，MECE）
│   ├── engines.py        # 引擎适配层
│   ├── cache.py          # 双层缓存
│   ├── adaptive.py       # 自适应学习
│   ├── tfidf_router.py   # TF-IDF 语义路由
│   ├── quota.py          # 配额管理
│   ├── search_types.py   # 统一类型系统
│   ├── research.py       # [新] 深度研究工具
│   ├── evidence.py       # [新] 可信度评估工具
│   ├── clarify.py        # [新] 意图消歧工具
│   ├── crawl.py          # [新] 站点爬取工具
│   ├── extract.py         # 结构化提取工具
│   ├── fetch.py           # 页面抓取工具（urllib）
│   ├── content_signals.py # [v2.0] 内容质量信号系统
│   ├── focus_extract.py   # [v2.0] BM25 聚焦提取
│   ├── pdf_extract.py     # [v2.0] PDF 结构化提取
│   ├── mcp_server.py      # MCP 服务层（10 工具）
│   └── social_engines/    # [v2.1] 社交平台引擎
│       ├── twitter_engine.py
│       ├── reddit_engine.py
│       ├── xiaohongshu_engine.py
│       ├── bilibili_engine.py
│       └── weibo_engine.py
├── sub-skills/
│   ├── local-search/     # 本地引擎子技能（聚合 local_bing/local_baidu 等私有搜索 API）
│   ├── local-seek/       # 本机文件搜索子技能（原生原创；MCP argo_local_search 封装 seek.py）
│   └── ego-search/       # 浏览器态搜索增强子技能（真实 Chromium + 同源接口数据直取）
└── tests/
```

### 抓取三工具（argo_fetch / argo_screenshot / argo_pdf）

| 工具 | 功能 | 适用场景 | 依赖 |
|------|------|---------|------|
| `argo_fetch` | HTTP→反检测浏览器自动降级 + BM25 聚焦 + 质量信号 | 反爬网站、CF 保护页、JS 渲染页 | 可选：master_fetch |
| `argo_screenshot` | 页面截图（全页/视口） | 布局验证、网页快照、多模态分析 | playwright |
| `argo_pdf` | PDF→Markdown（表格+目录+元数据） | 论文/报告/白皮书解析 | pdfplumber 或 PyMuPDF |

#### argo_fetch — 智能页面抓取

```bash
# 自动模式（HTTP 优先，失败升级浏览器）
argo fetch "https://example.com"

# BM25 聚焦提取（只返回相关段落）
argo fetch "https://example.com/long-article" --focus "关键词"

# 强制使用反检测浏览器
argo fetch "https://cloudflare-protected.com" --use-browser
```

输出包含 Hound 质量信号：
```json
{
  "content_ok": true,
  "page_type": "article",
  "source_type": "docs-site",
  "is_official": true,
  "is_stale": false,
  "content_age_days": 45,
  "quality_score": 0.85,
  "fetch_method": "http"
}
```

**降级触发条件**：HTTP 失败 / 内容 < 50 字符 / 检测到 CF 挑战 / 检测到 JS shell

**Wayback 快照回退**：HTTP 失败或内容为空时，自动尝试 Wayback Machine 最新快照（CDX API 查 → 抓快照），命中后 `fetch_method=wayback` 并附 `snapshot_url` / `snapshot_ts`。尽力而为，失败不阻塞主流程。

**内容安全引擎（content_security）**：任何抓取内容先过安全检测再交给 Agent——
- **多语言感知**：复用 `lang_detect.detect_language` 判定内容主语言，按语系加载注入模式（英语通用 + 中/日/韩/俄/阿拉伯/希伯来/泰/希腊专有），对齐 argo 9 大语系能力，避免全语系扫描的低误报高性能
- 注入检测：70+ 中英日韩俄阿希泰希模式（指令覆盖 / 角色操纵 / 系统提示泄露 / 越狱 / 数据外泄 / 身份冒充 / XSS）
- 编码归一化：零宽字符、RTL 覆盖、Unicode 同形字（仅拉丁内容启用，避免西里尔/希腊正文误报）、base64 片段、URL 编码剥离
- 语义意图分析：命中词数判意图（跨语种稳健，权威×覆盖 / 提取×秘密组合）
- 风险评分（0-1）+ 目标脱敏（检测与脱敏共用模式表，不漂移）
- 输出字段：`content_security.content_clean` / `risk_score` / `threat_count` / `threat_types` / `redactions` / `content_lang`

```bash
# 单独调用安全引擎
python3 scripts/content_security.py "待检测文本" --json
python3 scripts/content_security.py --stdin < content.txt
```

#### argo_screenshot — 页面截图

```bash
argo screenshot "https://example.com"
argo screenshot "https://example.com" --full-page --output /tmp/page.png
```

#### argo_pdf — PDF 结构化提取

```bash
argo pdf "https://example.com/paper.pdf"
argo pdf "https://example.com/paper.pdf" --pages "1-5"
argo pdf "/local/file.pdf" --password "secret"
```

### 内容质量信号系统（v2.2 增强）

所有抓取结果自动附带质量信号；搜索快评另附 `credibility_fast`：

| 信号 | 类型 | 说明 |
|------|------|------|
| `content_ok` | bool | 内容是否可信可用（quality_score > 0.3 且 word_count > 50） |
| `page_type` | string | article/list/forum/qa/docs/js_shell/auth_wall/paywall |
| `source_type` | string | gov/edu/github/news/blog/forum/qa/docs-site/ecommerce |
| `is_official` | bool | 是否官方来源（.gov/.edu/github/厂商docs） |
| `is_stale` | bool | 是否过期（> 365 天） |
| `content_age_days` | int | 内容年龄（天） |
| `quality_score` | float | 0-1（长度 0.2 + 密度 0.2 + 结构 0.2 + **证据密度 0.3** + 标题 0.1） |
| `has_numbers` / `has_definition` / `has_comparison` / `has_howto` | bool | 证据块（GEO 吸收信号） |
| `absorption_score` | float | 证据密度综合分 |
| `selection` / `credibility_fast` | float | 搜索结果内嵌两阶段快评 |

### 输出 JSON Schema

```json
{
  "query": "string",
  "engine": "string",
  "engines": ["string"],
  "engines_combo": ["string"],
  "cached": false,
  "cache_level": "L1 | L2",
  "domain": "string | null",
  "elapsed_ms": 0,
  "tfidf_scores": [{"engine": "string", "score": 0.0}],
  "results": [
    {
      "title": "string",
      "url": "string",
      "snippet": "string",
      "score": 0.0,
      "source": "string"
    }
  ],
  "count": 0,
  "engines_used": ["string"],
  "errors": ["string"]
}
```

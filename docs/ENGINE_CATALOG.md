# Argo 搜索源使用文档（自动生成）

> 本文件由 `scripts/gen_engine_catalog.py` 生成，**勿手改**。
> 改引擎声明后重新生成：`python3 scripts/gen_engine_catalog.py`；
> 过期会被 `tests/test_engine_catalog.py` 拦下。

## 一、总量与口径

- **收录 255 个源**（config.yaml + `engines/specs/*.yaml` 声明合并后的总数）
- **开箱可用 221 个**：不需要你配任何密钥或装额外工具，自动路由就会用上
- **需自备密钥 21 个**：`bocha`、`bocha_ai`、`byted`、`em_miaoxiang`、`exa`、`keenable`、`octen`、`parallel`、`qweather`、`seltz`、`tavily`、`tinyfish`、`tinyfish_news`、`tinyfish_paper`、`unpaywall`、`weread`、`you`、`zhihu`、`zhihu_global`、`zhihu_hot`、`zhihu_user`（没配也不影响搜索，路由会跳过）
- **需装后端工具 3 个**：`reddit`、`twitter`、`xiaohongshu`（装好并登录后即可用）
- **已停用 10 个**：`brave`、`europeana`、`felo`、`jikan`、`local_goodreads`、`metaso`、`realtime_index`、`searxng`、`soilgrids`、`wolframalpha`
- **显式专用 12 个**：`bing_rss`、`datagov_uk`、`doi`、`gdelt`、`opencitations`、`tineye`、`tinyfish`、`tinyfish_news`、`tinyfish_paper`、`twitter_syndication`、`unpaywall`、`zbmath`（设计上不进自动路由，按 `--engine` 或交接提示调用）

自己核一遍（口径不同，别混用）：

```bash
argo search --list-engines | wc -l                  # 有实现的源（可实例化）
argo search --list-engines --detail | wc -l         # 收录总数（含已停用）
argo search --list-engines --routable-only | wc -l  # 本机此刻真能路由的
argo search --list-engines --detail                 # 逐源状态/密钥/依赖
```

本文件的分档是**声明口径**（照引擎自己的声明算，换台机器也一样）；最后一行是**本机口径**——它把已配好密钥、已装好后端工具的源也算进来，再扣掉当前被熔断/封锁的源。两个数字不相等是正常的；想知道本机此刻到底哪些就绪，看 `--detail` 里的 `missing_env`（空 = 已配）。

## 二、费用与密钥：哪些白用、哪些要钱

- **免费档 242 个**（含已停用）：无需密钥，或只需免费注册的密钥；其中 12 个要自备密钥（免费额度）：em_miaoxiang、keenable、qweather、tinyfish、tinyfish_news、tinyfish_paper、unpaywall、weread、wolframalpha、zhihu、zhihu_hot、zhihu_user
- **计费档 13 个**（下表逐个列出，档位取自各源自己的 `cost_tier` 声明）

| 引擎 | 档位 | 是否进自动路由 | 需自备密钥 |
|---|---|---|---|
| `felo` | 付费 | 是 | ARGO_FELO_API_KEY |
| `bocha` | 低价计费 | 是 | ARGO_BOCHA_API_KEY |
| `bocha_ai` | 低价计费 | 是 | ARGO_BOCHA_API_KEY |
| `brave` | 低价计费 | 否，仅显式调用 | ARGO_BRAVE_API_KEY |
| `byted` | 低价计费 | 是 | ARGO_BYTED_API_KEY |
| `metaso` | 低价计费 | 否，仅显式调用 | ARGO_METASO_API_KEY |
| `exa` | 按调用计费 | 是 | ARGO_EXA_API_KEY |
| `octen` | 按调用计费 | 是 | ARGO_OCTEN_API_KEY |
| `parallel` | 按调用计费 | 是 | PARALLEL_API_KEY |
| `seltz` | 按调用计费 | 是 | SELTZ_API_KEY |
| `tavily` | 按调用计费 | 是 | ARGO_TAVILY_API_KEY |
| `you` | 按调用计费 | 是 | YDC_API_KEY |
| `zhihu_global` | 按调用计费 | 是 | ARGO_ZHIHU_ACCESS_SECRET |

**结论**：付费档里有 1 个进了自动路由（felo），用之前先确认额度；低价/按量计费的源有 10 个在自动路由路径上，多数带免费额度或已配密钥。额度记在本地配额表（`backends/quota_profiles.json` 的 limit / period，用量存在本机状态库），用尽后该源在语义路由里被降权；本地表统计的是 argo 自己的调用，若同一密钥还被别的工具用，实际额度以服务商侧为准。想彻底避开计费源，用 `--engine` 显式指定免费源，或走 `--mode budget`。

## 三、特别能力（不是普通网页搜索）

| 能力 | 怎么用 | 说明 |
|---|---|---|
| 垂直结构化卡 | 直接问「北京到上海高铁」「今天油价」「黄金价格」「2026 年历」「上海车牌摇号」等 | 火车票 / 油价 / 贵金属 / 万年历 / 星座 / 手机 / 汽车 / 挂号等有标准答案的问题，直接给答案而不是一堆链接 |
| 抽取型取证 | `argo search "<推文URL或ID>" --engine twitter_syndication` | 按 URL 取单条推文，免登录、零密钥；关键词查询会诚实返回空（该通道没有搜索端点） |
| 学术与数据集 | 问「XX 论文」走学术域（arXiv/OpenAlex/Crossref/EuropePMC/DBLP/Semantic Scholar）；问「数据集/开放数据」走数据集域（DataCite/Zenodo） | 论文与数据集分开走，互不串味 |
| 美股申报原文 | 问「苹果 10-K」「SEC filing」「招股书」 | SEC EDGAR 官方全文检索（免密钥），直出申报文件 |
| 深度研究 | `argo research "问题"` | 问题分解 → 多源采集 → 综合报告；注意它不套娃，worker 里不会再用研究工具 |
| 证据核验 | `argo evidence "query"`（或管道喂搜索结果） | 对结果打分、标注事实/推断/未知 |
| 公众号全文 | `argo article "<链接>"` | 标题 / 正文 / 图片全量抽取 |
| 招聘聚合 | `argo job "岗位"` | BOSS / 猎聘 / 智联 / 前程无忧 / 597 / 今日招聘 |
| 抓取与爬站 | `argo fetch "<url>"` / `argo crawl "<url>"` / `argo extract "<url>"` | 四级降级抓取（md 变体 → HTTP → TLS 指纹 → 浏览器）；crawl 走 sitemap/BFS；extract 取表格/Meta/JSON-LD |
| 批量预检 | `argo preflight "url1" "url2" …` | 开工前判定哪些能拿、哪些要登录、哪些已死，三档结论 go / go_with_skips / stop |
| 本地文件搜索 | `argo search "词" --include-local` | 并入本机文件/笔记命中（source=local_files，不参与融合评分） |
| 可复算 | MCP `argo_recompute` / `--allow-recompute` | 数值重算，默认关闭，需要显式授权 |
| 网页截图 / PDF | MCP `argo_screenshot` / `argo_pdf` | 长尾取证能力，默认不挂载 MCP 时用不到 |

## 四、默认关闭的能力怎么打开

- **MCP 19 工具面**（与 CLI 能力面对齐）：默认关（工具定义会常驻注入上下文）。DSH 用户在 profile patch 里取消 `mcp-argo` 段注释后重启；其他客户端见 README「MCP 接入」。
- **原生工具**：默认只注册 `argo_search` / `argo_fetch`；`nativeTools` 配置可按需放开全部 13 个（`argo_research` 除外）。
- **需密钥的源**：把密钥写进 `~/.config/argo/env`（600 权限）或环境变量，下表中「需自备密钥」一列即变量名。
- **显式专用源**：不进自动路由，用 `argo search "词" --engine <引擎名>` 调用。

## 五、逐源清单（按能力族）

状态含义：**可直接用** = 自动路由会用上；**需自配密钥 / 需装后端工具** = 配好后即可用；**被上游封锁** = 源站当前拒绝；**已停用** = 配置层面关闭。

### 全网搜索（28）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `anysearch` | 可直接用 | 免费 | 2000/天 | — | 域 ai_model、域 book_search、域 chinese_general、域 claim_check、域 code_search、域 crypto_search、域 dataset_search、域 earth_science、域 energy_grid、域 english_tech、域 financial_news、域 fund_query、域 game_search、域 hackernews_search、域 image_search、域 japan_law、域 kor_law、域 law_text、域 legal、域 lifecycle_search、域 local_code、域 local_general、域 macro_data、域 medical、域 meme_slang、域 outbreak_health、域 package_intel、域 prediction_market、域 redskill_search、域 rfc_search、域 sec_filings、域 security_search、域 semantic_discovery、域 shopping、域 skill_search、域 soil_agri、域 sports_search、域 stackoverflow_search、域 stock_query、域 ths_hot_search、域 trade_stats、域 transport_rt、域 us_legal、域 us_stock、域 v2ex_search、域 vehicle_data、域 wechat_search、域 wenshu_query、域 world_news、域 zhihu_content、深度研究 boost、语义画像命中、通用兜底链 | 通用搜索主力，进程内 JSON-RPC（HttpClient），零 token |
| `firecrawl` | 可直接用 | 免费 | 1000/月 | — | 通用兜底链 | Firecrawl 云搜索（search+全文markdown，JS渲染/学术/PDF垂直，keyless 免费层 1000 credits/月） |
| `lieu` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | webring 专用搜索（只索引加入 webring 的小众站点，HTML 解析） |
| `local_baidu` | 可直接用 | 免费 | 不限 | — | 语义画像命中、通用兜底链 | 百度本地 |
| `local_bing` | 可直接用 | 免费 | 不限 | — | 深度研究 boost、通用兜底链 | Bing本地 |
| `local_search` | 可直接用 | 免费 | 不限 | — | 域 chinese_general、域 local_academic、域 local_chinese、域 local_code、域 local_general、域 local_news、域 local_reference、语义画像命中 | Local Search 聚合 |
| `local_sogou` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 搜狗本地 |
| `marginalia` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | Marginalia 独立爬虫索引（非大厂代理，专挖长尾非商业页面，JSON 免认证） |
| `parallel_free` | 可直接用 | 免费 | 不限 | — | 通用兜底链 | Parallel 免费搜索（官方免费 MCP 端点 search.parallel.ai，无账号无 key；excerpts 长文摘录省 fetch；与按量计费的 parallel REST 通道分立，作其缺位时的补位） |
| `searchmysite` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 人工审核准入的个人独立站索引（非商业博客，HTML 解析） |
| `uapi` | 可直接用 | 免费 | 不限 | — | 语义画像命中、通用兜底链 | UAPI 聚合搜索 |
| `wechat_sogou` | 可直接用 | 免费 | 不限 | — | 域 chinese_general、域 wechat_search | 搜狗微信搜索引擎（公众号文章，免登录） |
| `wiby` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | Wiby 老式手工网页索引（专收非商业化页面，JSON 免认证） |
| `bing_rss` | 显式专用 | 免费 | 不限 | — | 显式调用（--engine） | Bing 网页搜索 RSS 出口（免 key 免 HTML 解析；local_bing 改版/风控时的稳定备胎） |
| `bocha` | 需自备密钥 | 低价计费 | 10000/月 | ARGO_BOCHA_API_KEY | 域 chinese_general、域 local_chinese、域 modal_card、语义画像命中 | 博查搜索 API（中文，freshness 按查询时效动态化） |
| `brave` | 已停用 | 低价计费 | 不限 | ARGO_BRAVE_API_KEY | 已停用 | Brave Search API |
| `byted` | 需自备密钥 | 低价计费 | 不限 | ARGO_BYTED_API_KEY | 域 chinese_general、域 chinese_tech_deep、域 fact_check、域 financial_news、域 legal、域 local_news、域 news_realtime、域 weather_query、语义画像命中 | 字节搜索 API，中文通用/news |
| `exa` | 需自备密钥 | 按调用计费 | 1000/月 | ARGO_EXA_API_KEY | 域 english_tech、域 semantic_discovery、域 us_stock、深度研究 boost | Exa 语义搜索（embedding 匹配 + 内容摘要，新户 $20 赠金 + 每月 $10 赠金） |
| `felo` | 已停用 | 付费 | 不限 | ARGO_FELO_API_KEY | 语义画像命中 | Felo AI 搜索 API |
| `keenable` | 需自备密钥 | 免费 | 不限 | ARGO_KEENABLE_API_KEY | 域 english_tech | Keenable 通用网页搜索（ranked results，含 published_at，按 credits 计费） |
| `metaso` | 已停用 | 低价计费 | 不限 | ARGO_METASO_API_KEY | 已停用 | 秘塔搜索 API（中文 AI 搜索） |
| `octen` | 需自备密钥 | 按调用计费 | 不限 | ARGO_OCTEN_API_KEY | 域 chinese_general、域 chinese_tech_deep、域 english_tech、域 news_realtime、深度研究 boost | Octen AI 高速搜索（需 OCTEN_API_KEY；支持 broad-search） |
| `parallel` | 需自备密钥 | 按调用计费 | 不限 | PARALLEL_API_KEY | 域 chinese_tech_deep | Parallel AI 批量搜索（excerpts 长文摘录，结果自带正文省 fetch） |
| `searxng` | 已停用 | 免费 | 不限 | — | 已停用 | SearXNG 直连（已废弃，由 T3 替代） |
| `tavily` | 需自备密钥 | 按调用计费 | 1000/月 | ARGO_TAVILY_API_KEY | 语义画像命中 | Tavily AI 搜索 API（免费层 1000 次/月，按 credit 计费；与 exa 同为 api 档） |
| `tinyfish` | 需自备密钥 + 显式专用 | 免费 | 不限 | ARGO_TINYFISH_API_KEY | 显式调用（--engine） | TinyFish 实时网页搜索（免费，浏览器渲染，结果含原文摘要，X-API-Key 认证） |
| `you` | 需自备密钥 | 按调用计费 | 不限 | YDC_API_KEY | 域 news_realtime | You.com 网页+新闻搜索（时效性强，官方一手源，web/news 合并） |
| `zhihu_global` | 需自备密钥 | 按调用计费 | 5000/天 | ARGO_ZHIHU_ACCESS_SECRET | 域 chinese_general、域 news_realtime、域 zhihu_content | 知乎开放平台全网搜索（SearchDB=all 全网索引 + Filter host== 站点限定；需 ZHIHU_ACCESS_SECRET） |

### 学术文献（27）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `arxiv` | 可直接用 | 免费 | 不限 | — | 域 academic、域 local_academic、域 scholar_search、域 tech_deep、深度研究 boost、语义画像命中 | arXiv 论文搜索 |
| `biorxiv` | 可直接用 | 免费 | 不限 | — | 域 academic、深度研究 boost | bioRxiv/medRxiv 预印本（DOI 单篇详情 / 最近 3 天列表） |
| `cinii` | 可直接用 | 免费 | 不限 | — | 域 academic | CiNii Research 日本学术总库（论文/博士论文/科研项目，免 key） |
| `cnii` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 日本国立情报学研究所学术总库（论文/博士论文/科研项目，JSON-LD） |
| `core` | 可直接用 | 免费 | 不限 | — | 域 academic | CORE 开放获取全文（含 PDF 直链，覆盖机构仓储灰色文献） |
| `crossref` | 可直接用 | 免费 | 不限 | — | 域 academic、深度研究 boost | Crossref DOI 元数据 API（免认证，礼貌池 mailto） |
| `datacite` | 可直接用 | 免费 | 不限 | — | 域 dataset_search | DataCite 科研数据集搜索（Dryad/Figshare/Dataverse/OSF 等仓储，免认证） |
| `dblp` | 可直接用 | 免费 | 不限 | — | 域 academic | DBLP 计算机科学文献（免认证，偶发 SSL 抖动） |
| `doaj` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 开放获取期刊全球总库（80+ 语种，元数据统一英文） |
| `europepmc` | 可直接用 | 免费 | 不限 | — | 域 academic、域 tech_deep | Europe PMC 生物医学文献（免认证，SS 429 后备） |
| `figshare` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 科研数据集检索（论文/数据/图表） |
| `google_scholar` | 可直接用 | 免费 | 不限 | — | 域 scholar_search、深度研究 boost | Google Scholar（学术论文搜索，HTTP 页面解析） |
| `hal` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 法国全国科研机构开放仓储（Solr JSON） |
| `k10plus` | 可直接用 | 免费 | 不限 | — | 域 book_search | K10plus 联合目录（德国最大图书馆联合目录，SRU，免认证） |
| `local_arxiv` | 可直接用 | 免费 | 不限 | — | 域 patent_search、语义画像命中 | arXiv本地 |
| `local_crossref` | 可直接用 | 免费 | 不限 | — | 经 local_search 展开 | Crossref本地 |
| `local_pubmed` | 可直接用 | 免费 | 不限 | — | 域 academic、域 medical | PubMed本地 |
| `local_semantic_scholar` | 可直接用 | 免费 | 不限 | — | 经 local_search 展开 | Semantic Scholar本地 |
| `openalex` | 可直接用 | 免费 | 不限 | — | 域 academic、域 chem_search、域 tech_deep、深度研究 boost | OpenAlex 2.5亿+论文索引（免认证，礼貌池 mailto） |
| `openreview` | 可直接用 | 免费 | 不限 | — | 域 academic、深度研究 boost | OpenReview 顶会论文（含评审可见性与 PDF，AI/ML 研究为主） |
| `semantic_scholar` | 可直接用 | 免费 | 不限 | — | 域 academic、域 local_academic、域 patent_search、域 tech_deep、深度研究 boost、语义画像命中 | Semantic Scholar API（备选） |
| `zenodo` | 可直接用 | 免费 | 不限 | — | 域 dataset_search | Zenodo 科研数据集搜索（CERN 托管，免认证，DOI 可引用） |
| `doi` | 显式专用 | 免费 | 不限 | — | 显式调用（--engine） | DOI 内容协商（doi.org 官方解析器，Accept 头直出 CSL JSON 结构化元数据，keyless 免 key 免限次，查询词传裸 DOI） |
| `opencitations` | 显式专用 | 免费 | 不限 | — | 显式调用（--engine） | OpenCitations 引用计数（DOI → 被引次数，免 key 无限次；查询词传裸 DOI） |
| `tinyfish_paper` | 需自备密钥 + 显式专用 | 免费 | 不限 | ARGO_TINYFISH_API_KEY | 显式调用（--engine） | TinyFish 学术论文搜索（免费，含作者/发表处/年份/被引/pdf_url） |
| `unpaywall` | 需自备密钥 + 显式专用 | 免费 | 不限 | ARGO_UNPAYWALL_EMAIL | 显式调用（--engine） | Unpaywall 开放获取定位（DOI → 是否有合法免费全文及链接；需 ARGO_UNPAYWALL_EMAIL） |
| `zbmath` | 显式专用 | 免费 | 不限 | — | 显式调用（--engine） | zbMATH Open 数学文献索引（含 MSC 分类号，免 key） |

### 其他垂直（26）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `aviation_weather` | 可直接用 | 免费 | 不限 | — | 域 aviation_weather | 航空气象（METAR 实时例行天气报告，按 ICAO 机场代码查询） |
| `clawhub` | 可直接用 | 免费 | 不限 | — | 域 skill_search | ClawHub 技能生态聚合搜索（OpenClaw 原生 + skills.sh 条目，免认证，下载量口径） |
| `coingecko` | 可直接用 | 免费 | 不限 | — | 域 crypto_search、语义画像命中 | CoinGecko 币种搜索（免认证） |
| `ddgs_images` | 可直接用 | 免费 | 不限 | — | 域 image_search | 通用图片搜索（免 Key，覆盖全网图片；无版权标注，发布前须自行核对） |
| `electricity_maps` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 全球电网分区目录（zones 免 key） |
| `eu_opendata` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 欧盟开放数据目录（24 语言元数据） |
| `fr_opendata` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 法国政府开放数据目录（data.gouv.fr） |
| `gbfs_nyc` | 可直接用 | 免费 | 不限 | — | 域 transport_rt | GBFS 共享单车站点（NYC Citi Bike，通用规范，免认证） |
| `google_patents` | 可直接用 | 免费 | 不限 | — | 域 patent_search | Google Patents 专利搜索（技术尽调/竞品分析，免认证） |
| `itotii` | 可直接用 | 免费 | 不限 | — | 域 meme_slang、语义画像命中 | itotii 梗百科（中文流行语/网络梗溯源，WordPress REST，免认证） |
| `know_your_meme` | 可直接用 | 免费 | 不限 | — | 域 meme_slang、语义画像命中 | Know Your Meme（英文 meme 词条溯源，HTML 解析） |
| `models_dev` | 可直接用 | 免费 | 不限 | — | 域 ai_model | models.dev AI 模型目录（全量缓存，免认证） |
| `openfoodfacts` | 可直接用 | 免费 | 不限 | — | 域 soil_agri | Open Food Facts 全球食品成分库（200+ 国家，多语言，免 key） |
| `polymarket` | 可直接用 | 免费 | 不限 | — | 域 prediction_market | Polymarket 预测市场搜索（叙事源，非事实真值） |
| `rfc_editor` | 可直接用 | 免费 | 不限 | — | 域 rfc_search | RFC/互联网标准文档检索（datatracker.ietf.org 免认证） |
| `skillsmp` | 可直接用 | 免费 | 不限 | — | 域 skill_search | SkillsMP Agent 技能聚合目录（200万+ 开源技能独立索引，免费 keyless REST API，stars 排序） |
| `steam` | 可直接用 | 免费 | 不限 | — | 域 game_search | Steam 商店搜索（免认证） |
| `train` | 可直接用 | 免费 | 不限 | — | 域 modal_card、域 transport_rt | 火车余票查询（免 Key：12306 官方接口，车次时刻+余票） |
| `urban_dictionary` | 可直接用 | 免费 | 不限 | — | 域 meme_slang、语义画像命中 | Urban Dictionary（英文俚语定义与例句，官方 API） |
| `weather` | 可直接用 | 免费 | 不限 | — | 域 weather_query | 天气查询（免 Key：wttr.in 主用 + Open-Meteo 兜底，当前+未来预报） |
| `weather_cn` | 可直接用 | 免费 | 不限 | — | 域 weather_query | 中国天气网城市实况（城市联想取 cityid → sk JSON，免认证，两步） |
| `wikimedia_commons` | 可直接用 | 免费 | 不限 | — | 域 image_search | Wikimedia Commons 通用图库（免 key，CC 素材，原图直链+许可+宽高） |
| `datagov_uk` | 显式专用 | 免费 | 不限 | — | 显式调用（--engine） | data.gov.uk 英国政府开放数据目录（CKAN，免 key） |
| `qweather` | 需自备密钥 | 免费 | 不限 | ARGO_QWEATHER_KEY | 域 weather_query | 和风天气实时天气（需 QWEATHER_KEY） |
| `realtime_index` | 已停用 | 免费 | 不限 | — | 已停用 | 实时索引数据源（免 Key，结构化输出，带发布时间维度与时间窗过滤） |
| `tineye` | 显式专用 | 免费 | 不限 | — | 显式调用（--engine） | TinEye 反向图片搜索（query 须为公网图片 URL，免 key；argo 首个反搜图能力） |

### 媒体 / 图书（22）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `artic` | 可直接用 | 免费 | 不限 | — | 域 art_museum | 芝加哥艺术博物馆馆藏（免认证，含 IIIF 图像） |
| `bangumi` | 可直接用 | 免费 | 不限 | — | 域 anime_encyclopedia | Bangumi 番剧仓库（动画/漫画/游戏条目元数据，官方开放 API，免认证） |
| `cleveland` | 可直接用 | 免费 | 不限 | — | 域 art_museum | 克利夫兰艺术博物馆（CC0 开放图像，免认证） |
| `deezer` | 可直接用 | 免费 | 不限 | — | 域 media_search | 音乐艺人/专辑（Deezer，免认证） |
| `douban_book` | 可直接用 | 免费 | 不限 | — | 域 book_search | 豆瓣读书搜索（评分/出版社/年份/价格，免认证） |
| `douban_movie` | 可直接用 | 免费 | 不限 | — | 域 film_search | 豆瓣电影搜索（中文片名/年份/类型，免认证，suggest 接口） |
| `gutenberg` | 可直接用 | 免费 | 不限 | — | 域 book_search | Project Gutenberg 公版书全文检索（gutendex.com 免认证） |
| `imdb` | 可直接用 | 免费 | 不限 | — | 域 film_search、语义画像命中 | IMDb suggestion API（电影/剧集/人物，免认证） |
| `itunes` | 可直接用 | 免费 | 不限 | — | 域 film_search、域 media_search、语义画像命中 | iTunes Search API（音乐/专辑、播客节目与单集（含集数/时长/发布日期）；中文 country=cn，免认证） |
| `listenbrainz` | 可直接用 | 免费 | 不限 | — | 域 media_search | 音乐收听趋势榜（ListenBrainz，免认证） |
| `local_imdb` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | IMDb suggestion API（local-search 垂直别名，与 imdb 同源） |
| `met_museum` | 可直接用 | 免费 | 不限 | — | 域 art_museum | Met Museum 藏品库（艺术/博物馆藏品，两跳搜索，免认证） |
| `musicbrainz` | 可直接用 | 免费 | 不限 | — | 域 media_search、语义画像命中 | MusicBrainz 音乐人/作品元数据（免认证，限速 1rps） |
| `nasa_images` | 可直接用 | 免费 | 不限 | — | 域 astro_space、域 earth_science | NASA 图像视频库（公开航天影像，免认证，匿名约 30 req/h） |
| `netease_music` | 可直接用 | 免费 | 不限 | — | 域 media_search | 网易云音乐搜索（中文曲库/专辑元数据，免认证，非官方接口） |
| `open_library` | 可直接用 | 免费 | 不限 | — | 域 book_search、语义画像命中 | Open Library 图书搜索（免认证） |
| `openverse` | 可直接用 | 免费 | 不限 | — | 域 image_search、语义画像命中 | Openverse 开放版权图库（免认证，CC 素材） |
| `qq_music` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | QQ 音乐曲库搜索（歌手/歌曲/专辑） |
| `tvmaze` | 可直接用 | 免费 | 不限 | — | 域 film_search | 电视剧元数据（TVMaze，免认证，含首播/语言/评分） |
| `jikan` | 已停用 | 免费 | 不限 | — | 域 anime_encyclopedia | 动漫元数据（Jikan/MyAnimeList，免认证，含评分与集数） |
| `local_goodreads` | 已停用 | 免费 | 不限 | — | 经 local_search 展开 | Goodreads本地 |
| `weread` | 需自备密钥 | 免费 | 不限 | ARGO_WEREAD_API_KEY | 域 book_search | 微信读书图书搜索（中文书目/评分/在读，需 WEREAD_API_KEY） |

### 百科 / 实体（18）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `baidu_baike` | 可直接用 | 免费 | 不限 | — | 域 cn_encyclopedia、域 company_search、域 entity_search、域 org_entity | 百度百科词条（suggest + OpenAPI 卡片，免认证） |
| `dnb` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 德语出版物最全书目（SRU/RDF XML） |
| `free_dictionary` | 可直接用 | 免费 | 不限 | — | 域 dictionary_search、语义画像命中 | dictionaryapi.dev 英英词典（免认证，响应为数组） |
| `local_wikipedia` | 可直接用 | 免费 | 不限 | — | 经 local_search 展开 | Wikipedia本地 |
| `local_wikiquote` | 可直接用 | 免费 | 不限 | — | 经 local_search 展开 | Wikiquote本地 |
| `local_wiktionary` | 可直接用 | 免费 | 不限 | — | 经 local_search 展开 | Wiktionary本地 |
| `moegirl` | 可直接用 | 免费 | 不限 | — | 域 anime_encyclopedia、语义画像命中 | 萌娘百科（zh.moegirl.org.cn 搜索页 HTML 解析，免认证） |
| `ndl` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 全日本出版物书目总汇（OpenSearch RSS） |
| `opencorporates` | 可直接用 | 免费 | 不限 | — | 域 company_search | OpenCorporates 全球公司注册（尽调/反欺诈，免认证） |
| `ror` | 可直接用 | 免费 | 不限 | — | 域 org_entity | ROR 研究机构标识（含域名映射，免认证） |
| `stackexchange` | 可直接用 | 免费 | 300/天 | — | 域 stackoverflow_search | StackExchange API 族（StackOverflow 等 180+ 问答站结构化检索，匿名 300/天/IP，免费 key 可提额 10000/天） |
| `tatoeba` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 400+ 语言对真实句子语料（翻译/语言学习） |
| `wikidata` | 可直接用 | 免费 | 不限 | — | 域 cn_encyclopedia、域 entity_search、域 film_search、域 geo_places、域 org_entity、域 sports_search、语义画像命中 | Wikidata 实体搜索（wbsearchentities，免认证，限流较严） |
| `wikipedia` | 可直接用 | 免费 | 不限 | — | 域 art_museum、域 astro_space、域 dictionary_search、域 entity_search、域 fact_check、域 film_search、域 geo_places、域 local_reference、域 medical、域 org_entity、域 species_search、域 sports_search、域 web_archive、语义画像命中、通用兜底链 | Wikipedia API（T2 替代） |
| `wikisource` | 可直接用 | 免费 | 不限 | — | 域 local_reference | 维基文库（古文/公版文献全文检索，中文引文溯源，免认证） |
| `zdic` | 可直接用 | 免费 | 不限 | — | 域 dictionary_search | 汉典（中文字词典：字义/音韵/字源，HTML 解析，免认证） |
| `zh_wikipedia` | 可直接用 | 免费 | 不限 | — | 域 anime_encyclopedia、域 cn_encyclopedia、域 entity_search、域 geo_places、域 org_entity、域 sports_search | 中文维基百科（MediaWiki API，与 en.wikipedia 同构） |
| `europeana` | 已停用 | 免费 | 不限 | — | 语义画像命中 | 欧洲 27 国文化遗产聚合（官方公开 demo key） |

### 社区 UGC（16）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `bilibili` | 可直接用 | 免费 | 不限 | — | 域 social、语义画像命中 | B站 |
| `csdn` | 可直接用 | 免费 | 不限 | — | 域 cn_tech_community | CSDN 搜索（so.csdn.net v3 JSON，免认证；标题与摘要的高亮标签、URL 追踪参数在引擎内清洗） |
| `fxtwitter` | 可直接用 | 免费 | 不限 | — | 域 social | FxTwitter 公开 API：X/Twitter 推文搜索（零认证，含互动元数据） |
| `hackernews` | 可直接用 | 免费 | 不限 | — | 域 hackernews_search、域 social | Hacker News（Algolia API，科技新闻+讨论） |
| `hatena_bookmark` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 日本技术圈书签搜索（RDF RSS，带收藏日期） |
| `juejin` | 可直接用 | 免费 | 不限 | — | 域 chinese_tech_deep、域 cn_tech_community | 掘金技术文章搜索（免认证） |
| `qiita` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | Qiita 日本最大技术社区（匿名 60 req/h） |
| `redskill` | 可直接用 | 免费 | 不限 | — | 域 redskill_search、域 skill_search | 小红书 REDSkill 排行榜与全量技能检索（47650 技能，data.json 本地缓存，免认证） |
| `sspai` | 可直接用 | 免费 | 不限 | — | 域 cn_tech_community | 少数派搜索（中文效率/数码/软件文章，免认证） |
| `v2ex` | 可直接用 | 免费 | 不限 | — | 域 cn_tech_community、域 social、域 v2ex_search | V2EX（中文技术社区，官方 API 候选池 + 本地相关性过滤） |
| `weibo` | 可直接用 | 免费 | 不限 | — | 域 social、语义画像命中 | 微博 |
| `reddit` | 需装后端工具 | 免费 | 不限 | — | 域 social、语义画像命中 | Reddit |
| `twitter` | 需装后端工具 | 免费 | 不限 | — | 域 social、语义画像命中 | Twitter/X |
| `twitter_syndication` | 显式专用 | 免费 | 不限 | — | 显式调用（--engine） | X/Twitter 单条推文（syndication 通道，免登录零 key，含正文/作者/时间/媒体计数） |
| `xiaohongshu` | 需装后端工具 | 免费 | 不限 | — | 域 social、语义画像命中 | 小红书 |
| `zhihu` | 需自备密钥 | 免费 | 5000/天 | ARGO_ZHIHU_ACCESS_SECRET | 域 shopping、域 social、域 zhihu_content、域 zhihu_hot_list、语义画像命中 | 知乎搜索，中文观点/评测 |

### 代码 / 包 / 文档（15）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `crates` | 可直接用 | 免费 | 不限 | — | 域 package_search、语义画像命中 | crates.io Rust 包搜索（免认证，需 UA） |
| `deps_dev` | 可直接用 | 免费 | 不限 | — | 域 package_intel | deps.dev 包依赖（npm/pypi/go/maven/cargo；版本/弃用/发布时间） |
| `devto` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | DEV.to 技术文章搜索（免认证） |
| `docker_hub` | 可直接用 | 免费 | 不限 | — | 域 package_search | Docker Hub 镜像搜索（免认证） |
| `endoflife` | 可直接用 | 免费 | 不限 | — | 域 lifecycle_search | 产品生命周期（版本/最新/支持期/EOL，endoflife.date 200+ 产品） |
| `github` | 可直接用 | 免费 | 1000/天 | — | 域 code_search、域 local_code、域 ml_models、域 package_search、域 tech_deep、深度研究 boost、语义画像命中 | GitHub 代码搜索 API |
| `huggingface` | 可直接用 | 免费 | 不限 | — | 域 ai_model、域 code_search、域 ml_models | Hugging Face 模型搜索（免认证） |
| `local_github` | 可直接用 | 免费 | 1000/天 | — | 语义画像命中 | GitHub本地 |
| `local_gitlab` | 可直接用 | 免费 | 不限 | — | 经 local_search 展开 | GitLab本地 |
| `local_npm` | 可直接用 | 免费 | 不限 | — | 经 local_search 展开 | NPM本地 |
| `local_stackoverflow` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | StackOverflow本地 |
| `mdn` | 可直接用 | 免费 | 不限 | — | 域 code_search、域 web_docs | MDN Web Docs 搜索（en/官方 API，免认证） |
| `npm` | 可直接用 | 免费 | 不限 | — | 域 package_search | npm 包搜索（registry.npmjs.org，免认证，支持 keywords: 等限定符按生态收窄） |
| `pypi` | 可直接用 | 免费 | 不限 | — | 域 package_search | PyPI 包查询（/pypi/{name}/json 精确解析，免认证） |
| `stackoverflow` | 可直接用 | 免费 | 300/天 | — | 域 stackoverflow_search、域 web_docs | Stack Overflow（Stack Exchange API，编程问答） |

### 本地新闻流（14）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `aljazeera` | 可直接用 | 免费 | 不限 | — | 域 world_news | 半岛电视台阿语（半岛阿语版，阿拉伯世界一手视角，免 key） |
| `antara` | 可直接用 | 免费 | 不限 | — | 域 world_news | 安塔拉通讯社（印尼国家通讯社（ANTARA），免 key） |
| `ct24` | 可直接用 | 免费 | 不限 | — | 域 world_news | 捷克电视台24（ČT24，捷克公共电视新闻，免 key） |
| `elpais` | 可直接用 | 免费 | 不限 | — | 域 world_news | 西班牙国家报（El País，西语世界发行量最大的日报，免 key） |
| `faz` | 可直接用 | 免费 | 不限 | — | 域 world_news | 法兰克福汇报（FAZ，德国全国性日报，免 key） |
| `folha` | 可直接用 | 免费 | 不限 | — | 域 world_news | 巴西页报（Folha de S.Paulo，巴西最大日报，免 key） |
| `hurriyet` | 可直接用 | 免费 | 不限 | — | 域 world_news | 自由报（Hürriyet，土耳其主流日报，免 key） |
| `lefigaro` | 可直接用 | 免费 | 不限 | — | 域 world_news | 费加罗报（Le Figaro，法国主流日报，免 key） |
| `matichon` | 可直接用 | 免费 | 不限 | — | 域 world_news | 泰国民族报（Matichon，泰国主流日报，免 key） |
| `nhk` | 可直接用 | 免费 | 不限 | — | 域 world_news | NHK 新闻（日本放送协会主要新闻，公共广播一手源，免 key） |
| `tass` | 可直接用 | 免费 | 不限 | — | 域 world_news | 塔斯社（俄罗斯国家通讯社（ТАСС），俄语一手新闻，免 key） |
| `vnpress` | 可直接用 | 免费 | 不限 | — | 域 world_news | VnExpress（越南最大网络新闻，免 key） |
| `yna` | 可直接用 | 免费 | 不限 | — | 域 world_news | 韩联社（韩国国家通讯社（연합뉴스），韩半岛一手新闻，免 key） |
| `ynet` | 可直接用 | 免费 | 不限 | — | 域 world_news | Ynet 新闻（以色列最大新闻站，希伯来语一手源，免 key） |

### 快讯 / 电报（13）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `cls_telegraph` | 可直接用 | 免费 | 不限 | — | 域 cls_telegraph_search、域 em_news_search、域 global_event、域 jin10_flash | 财联社电报（全市场实时快讯，v1 API+本地签名零key） |
| `cn_ai_news` | 可直接用 | 免费 | 不限 | — | 域 chinese_tech_deep | 中文 AI 垂直资讯检索（模型/产品/行业/论文，含发布时间与上游来源） |
| `em_global_news` | 可直接用 | 免费 | 不限 | — | 域 em_news_search、域 global_event | 东财全球资讯（7×24 财经快讯） |
| `google_news` | 可直接用 | 免费 | 不限 | — | 域 global_event、域 news_realtime | Google News RSS（多语言新闻，免认证，支持时间窗与 site: 限定） |
| `jin10` | 可直接用 | 免费 | 不限 | — | 域 cls_telegraph_search、域 jin10_flash | 金十数据财经快讯（免认证） |
| `local_bing_news` | 可直接用 | 免费 | 不限 | — | 经 local_search 展开 | Bing News本地 |
| `local_google_news` | 可直接用 | 免费 | 不限 | — | 经 local_search 展开 | Google News本地 |
| `people_daily` | 可直接用 | 免费 | 不限 | — | 域 news_realtime | 人民网搜索（权威综合中文新闻，官方接口，免认证） |
| `wallstreetcn` | 可直接用 | 免费 | 不限 | — | 域 financial_news | 华尔街见闻快讯（lives 直播流 JSON，免认证；全量流 + 本地关键词过滤） |
| `em_miaoxiang` | 需自备密钥 | 免费 | 不限 | ARGO_EASTMONEY_APIKEY | 域 financial_news | 东财妙想搜索（官方研报/公告/政策，需 EASTMONEY_APIKEY） |
| `gdelt` | 显式专用 | 免费 | 不限 | — | 显式调用（--engine） | GDELT 全球新闻事件流（75+ 语言跨国媒体扫描，带域名/语言/国家标注） |
| `seltz` | 需自备密钥 | 按调用计费 | 20000/月 | SELTZ_API_KEY | 语义画像命中 | Seltz 搜索（英文主力，news 语料；结果带发布日期与正文摘录；中文无召回价值已用 langs 排除） |
| `tinyfish_news` | 需自备密钥 + 显式专用 | 免费 | 不限 | ARGO_TINYFISH_API_KEY | 显式调用（--engine） | TinyFish 实时新闻搜索（免费，含 publisher 与发布日期） |

### 法律判例（11）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `courtlistener` | 可直接用 | 免费 | 不限 | — | 域 us_legal | CourtListener 美国判例全文检索（匿名可用，2026-05 起约 5 次/分限额） |
| `egov_law` | 可直接用 | 免费 | 不限 | — | 域 japan_law | 日本法令检索（e-Gov 官方全文，免认证） |
| `federal_register` | 可直接用 | 免费 | 不限 | — | 域 us_legal | 美国联邦公报全文检索（行政法规/提案/通告原文，免 key；一手法规源） |
| `flk_law` | 可直接用 | 免费 | 不限 | — | 域 law_text、域 legal | 国家法律法规数据库（法律/行政法规/司法解释全文，权威法条源，免认证） |
| `gov_policy` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 中国政府网政策文件库（国务院文件，带发布日期） |
| `gov_regulations` | 可直接用 | 免费 | 不限 | — | 域 law_text、域 legal | 中国政府网规章库（部门规章/地方政府规章，免认证） |
| `kor_law` | 可直接用 | 免费 | 不限 | — | 域 kor_law、语义画像命中 | 韩国国家法令信息中心判例全文检索（官方公开 demo 账号，韩文 XML） |
| `nhtsa_vpic` | 可直接用 | 免费 | 不限 | — | 域 vehicle_data | 车辆厂商/车型本体（NHTSA vPIC，免认证） |
| `openstd` | 可直接用 | 免费 | 不限 | — | 域 standards | 国家标准全文公开系统（GB 全文预览入口，HTML 解析，免认证） |
| `std_samr` | 可直接用 | 免费 | 不限 | — | 域 standards | 全国标准信息公共服务平台（国标检索，标准号/状态/日期，免认证） |
| `wenshu` | 可直接用 | 免费 | 不限 | — | 域 law_text、域 legal、域 wenshu_query | 中国裁判文书网（反爬较强，尽力而为） |

### 热榜（11）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `baidu_hot` | 可直接用 | 免费 | 不限 | — | 域 hot_trending | 百度热搜（实时热搜榜，HTML 解析，免认证） |
| `bilibili_hot` | 可直接用 | 免费 | 不限 | — | 域 hot_trending | B站热搜（search/square 热搜词，免认证） |
| `douyin_hot` | 可直接用 | 免费 | 不限 | — | 域 hot_trending | 抖音热榜（iesdouyin word_list JSON，免认证） |
| `dw_news` | 可直接用 | 免费 | 不限 | — | 域 intl_news_flash | 德国之声英语版 RSS（德语区/欧洲公共广播视角，条目数最多的一档） |
| `france24` | 可直接用 | 免费 | 不限 | — | 域 intl_news_flash | France 24 英语版 RSS（法语区一手的国际新闻实时流，免 key） |
| `guardian_rss` | 可直接用 | 免费 | 不限 | — | 域 intl_news_flash | 卫报世界版 RSS（一手国际新闻实时流；摘要密度最高的一档，免 key） |
| `ths_hot` | 可直接用 | 免费 | 不限 | — | 域 hot_trending、域 ths_hot_search | 同花顺热点（当日强势股+题材归因，独家能力） |
| `toutiao_hot` | 可直接用 | 免费 | 不限 | — | 域 hot_trending | 今日头条热榜（hot-board JSON，免认证） |
| `weibo_hot` | 可直接用 | 免费 | 不限 | — | 域 hot_trending | 微博热搜榜（side/hotSearch JSON，免登录，需 Referer） |
| `zhihu_hot_app` | 可直接用 | 免费 | 不限 | — | 域 hot_trending | 知乎热榜匿名通道（App JSON，免密钥；开放平台 hot_list 的免费替代） |
| `zhihu_hot` | 需自备密钥 | 免费 | 100/天 | ARGO_ZHIHU_ACCESS_SECRET | 域 hot_trending、域 zhihu_hot_list | 知乎开放平台热榜 hot_list（日配额约 100；需 ZHIHU_ACCESS_SECRET；原 zhihu skill 迁入） |

### 地球 / 空间（10）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `carbon_intensity` | 可直接用 | 免费 | 不限 | — | 域 energy_grid | 英国电网碳强度与发电结构（国家电网官方，免认证） |
| `energy_charts` | 可直接用 | 免费 | 不限 | — | 域 energy_grid | Energy-Charts 欧洲发电结构/可再生占比（Fraunhofer ISE，免认证） |
| `gdacs` | 可直接用 | 免费 | 不限 | — | 域 earth_science | GDACS 全球多灾种预警（洪水/台风/野火/地震，EU JRC 官方，免认证） |
| `local_openstreetmap` | 可直接用 | 免费 | 不限 | — | 域 geo_places、语义画像命中 | Nominatim 地理编码/地点搜索（OSM，免认证，须带 User-Agent） |
| `nasa_cmr` | 可直接用 | 免费 | 不限 | — | 域 earth_science | NASA CMR 地球科学数据目录（MODIS/遥感，cmr.earthdata.nasa.gov 免认证） |
| `noaa_swpc` | 可直接用 | 免费 | 不限 | — | 域 astro_space | 空间天气（NOAA SWPC，Kp 指数/太阳活动区，官方免认证） |
| `satnogs` | 可直接用 | 免费 | 不限 | — | 域 astro_space | 卫星目录（SatNOGS DB，NORAD ID/发射信息，免认证） |
| `tle_mirror` | 可直接用 | 免费 | 不限 | — | 域 astro_space | TLE 轨道根数镜像（第三方，Celestrak 不可达时替代，免认证） |
| `usgs` | 可直接用 | 免费 | 不限 | — | 域 earth_science | USGS 地震目录（最近 30 天 M2.5+，earthquake.usgs.gov 免认证） |
| `soilgrids` | 已停用 | 免费 | 不限 | — | 域 earth_science、域 soil_agri | 全球土壤属性（ISRIC SoilGrids，逐点栅格，免认证） |

### 生物 / 蛋白（9）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `gbif` | 可直接用 | 免费 | 不限 | — | 域 species_search | 全球生物多样性物种检索（学名/俗名，api.gbif.org 免认证） |
| `iplant` | 可直接用 | 免费 | 不限 | — | 域 species_search | iPlant 植物智（中文植物名/俗名 → 学名 + 分类，中科院植物所，免认证） |
| `obis` | 可直接用 | 免费 | 不限 | — | 域 species_search | OBIS 海洋物种观测（IOC/UNESCO 官方，2.29 亿条记录，免认证） |
| `rcsb_pdb` | 可直接用 | 免费 | 不限 | — | 域 protein_search | RCSB PDB 蛋白质结构检索（search.rcsb.org v2 免认证） |
| `uniprot` | 可直接用 | 免费 | 不限 | — | 域 protein_search | UniProt 蛋白质/基因组检索（rest.uniprot.org 免认证） |
| `usda` | 可直接用 | 免费 | 不限 | — | 域 soil_agri、语义画像命中 | 美国农业部食品营养成分（官方 DEMO_KEY） |
| `who_don` | 可直接用 | 免费 | 不限 | — | 域 medical、域 outbreak_health | WHO 疫情暴发通报（世卫官方公共卫生事件，免认证 OData） |
| `who_gho` | 可直接用 | 免费 | 不限 | — | 域 medical、域 outbreak_health | WHO GHO 全球卫生指标（世卫官方统计目录，免认证） |
| `worms` | 可直接用 | 免费 | 不限 | — | 域 species_search | WoRMS 海洋物种权威命名（分类学标准，免认证） |

### 行情 / 资金（9）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `cninfo` | 可直接用 | 免费 | 不限 | — | 域 company_search、域 financial_news | 巨潮资讯网官方公告（A股公告第一官方源，免认证） |
| `eastmoney` | 可直接用 | 免费 | 不限 | — | 域 financial_news、域 fund_query、域 stock_query、域 us_stock、语义画像命中 | 东方财富，金融数据首选 |
| `em_flow` | 可直接用 | 免费 | 不限 | — | 域 stock_query | 东财资金流向（个股主力/北向/板块，push2.eastmoney.com 免认证） |
| `finviz` | 可直接用 | 免费 | 不限 | — | 域 us_stock | Finviz 美股快照（HTML，免认证） |
| `sec_edgar` | 可直接用 | 免费 | 不限 | — | 域 sec_filings | SEC EDGAR 美国证监会官方全文检索（公司/财报/申报，免认证） |
| `seeking_alpha` | 可直接用 | 免费 | 不限 | — | 域 us_stock | Seeking Alpha 美股分析（HTML，反爬较强） |
| `sina_quote` | 可直接用 | 免费 | 不限 | — | 域 stock_query | 新浪实时行情快照（现价/涨跌/成交量，免认证） |
| `tencent_kline` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 腾讯财经前复权日 K 线（A股+港股+美股） |
| `tencent_quote` | 可直接用 | 免费 | 不限 | — | 域 stock_query | 腾讯实时行情（qt.gtimg.cn 免认证，含换手率/市盈率/五档） |

### 宏观数据（6）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `eurostat` | 可直接用 | 免费 | 不限 | — | 域 macro_data | 欧盟统计局宏观数据（EU 国家 GDP/人均GDP/失业率/人口，ec.europa.eu SDMX 免认证） |
| `fred` | 可直接用 | 免费 | 不限 | — | 域 macro_data | FRED 宏观时序数据（CPI/失业率/国债收益率/GDP/M2，免认证） |
| `fx_rate` | 可直接用 | 免费 | 不限 | — | 域 macro_data | 实时汇率（open.er-api.com 免认证） |
| `nbs_stats` | 可直接用 | 免费 | 不限 | — | 域 macro_data | 国家统计局分省/全国宏观数据（GDP/CPI/PPI/人口，data.stats.gov.cn V2 免认证） |
| `un_comtrade` | 可直接用 | 免费 | 不限 | — | 域 trade_stats | UN Comtrade 双边贸易（国家+HS 码+年份+流向；preview 免 key） |
| `worldbank` | 可直接用 | 免费 | 不限 | — | 域 macro_data | 世界银行宏观指标（GDP/通胀/失业/人口，api.worldbank.org 免认证） |

### security（4）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `cisa_kev` | 可直接用 | 免费 | 不限 | — | 域 security_search | CISA 已知被在野利用漏洞目录（官方确认已被实际攻击利用的 CVE；全量 1.35MB 本地过滤） |
| `crt_sh` | 可直接用 | 免费 | 不限 | — | 域 security_search | crt.sh 证书透明度日志（子域名/证书情报，免认证，响应较慢） |
| `nvd` | 可直接用 | 免费 | 不限 | — | 域 security_search | NVD 漏洞情报（CVE 编号/描述/CVSS 与 KEV 标记；免认证 5 请求/30 秒） |
| `osv` | 可直接用 | 免费 | 不限 | — | 域 security_search | OSV.dev 开源漏洞库（按包查已知漏洞与影响版本区间，免 key；生态 PyPI/npm/Go/crates 等） |

### 体育（4）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `jolpica` | 可直接用 | 免费 | 不限 | — | 域 sports_search | F1 车手积分榜/赛程（Jolpica，Ergast 继任，免认证） |
| `openf1` | 可直接用 | 免费 | 不限 | — | 域 sports_search | F1 车手/车队/赛程数据（OpenF1，免认证，社区维护） |
| `openligadb` | 可直接用 | 免费 | 不限 | — | 域 sports_search | 德甲/欧洲联赛赛程比分（OpenLigaDB，免认证） |
| `thesportsdb` | 可直接用 | 免费 | 不限 | — | 域 sports_search、语义画像命中 | TheSportsDB 球员/球队/赛事（公开 test key，免认证） |

### 垂直结构化卡（4）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `open_meteo` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 全球天气（城市/地名 → 坐标 → 当前天气，免认证） |
| `opensky` | 可直接用 | 免费 | 不限 | — | 语义画像命中 | 全球 ADS-B 实时航班（主要都会区 bbox，免 key） |
| `bocha_ai` | 需自备密钥 | 低价计费 | 10000/月 | ARGO_BOCHA_API_KEY | 域 modal_card | 博查 AI 搜索（统一语义识别 + 垂直结构化模态卡：天气/股票/汇率/油价/火车/万年历/贵金属/星座/医疗等） |
| `wolframalpha` | 已停用 | 免费 | 不限 | ARGO_WOLFRAM_APPID | 已停用 | WolframAlpha 计算知识引擎 |

### 化学 / 药学（3）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `clinicaltrials` | 可直接用 | 免费 | 不限 | — | 域 medical | ClinicalTrials.gov v2 临床试验（免认证） |
| `openfda` | 可直接用 | 免费 | 不限 | — | 域 medical | openFDA 药品标签（免认证） |
| `pubchem` | 可直接用 | 免费 | 不限 | — | 域 chem_search | 化学/药学化合物检索（分子式/分子量/IUPAC/SMILES，pubchem.ncbi.nlm.nih.gov 免认证） |

### verification（2）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `factcheck_org` | 可直接用 | 免费 | 不限 | — | 域 claim_check | FactCheck.org（美国事实核查：声明级真伪判定，摘要密度仅次于卫报） |
| `full_fact` | 可直接用 | 免费 | 不限 | — | 域 claim_check | Full Fact（英国事实核查：与 FactCheck.org 分属英美两法域口径） |

### 归档 / 历史（2）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `archive_org` | 可直接用 | 免费 | 不限 | — | 域 web_archive | Internet Archive 高级搜索（免认证） |
| `wayback_cdx` | 可直接用 | 免费 | 不限 | — | 域 web_archive | Wayback Machine CDX 历史快照检索（web.archive.org 免认证） |

### 个人数据（1）

| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |
|---|---|---|---|---|---|---|
| `zhihu_user` | 需自备密钥 | 免费 | 10000/天 | ARGO_ZHIHU_ACCESS_SECRET | 域 zhihu_user_data | 知乎个人数据（本人创作内容/收藏/关注；Access Secret 直查本人无需 OAuth，user_data 池 10000/天） |

## 六、分发域清单（自动生成）

共 93 个业务域；命中即按域内的组合取源（多意图时按配置顺序取，窄意图域排在宽泛域之前）。自己看某个域为什么选这些源，看 `config.yaml` 的 `domains` 段。

| 域 | 主源 | 组合 |
|---|---|---|
| `redskill_search` | `redskill` | `redskill`、`anysearch` |
| `skill_search` | `skillsmp` | `skillsmp`、`clawhub`、`redskill`、`anysearch` |
| `wechat_search` | `wechat_sogou` | `wechat_sogou`、`anysearch` |
| `hackernews_search` | `hackernews` | `hackernews`、`anysearch` |
| `stackoverflow_search` | `stackoverflow` | `stackoverflow`、`stackexchange`、`anysearch` |
| `scholar_search` | `google_scholar` | `google_scholar`、`arxiv` |
| `v2ex_search` | `v2ex` | `v2ex`、`anysearch` |
| `ths_hot_search` | `ths_hot` | `ths_hot`、`anysearch` |
| `cls_telegraph_search` | `cls_telegraph` | `cls_telegraph`、`jin10` |
| `em_news_search` | `em_global_news` | `em_global_news`、`cls_telegraph` |
| `fund_query` | `eastmoney` | `eastmoney`、`anysearch` |
| `sec_filings` | `sec_edgar` | `sec_edgar`、`anysearch` |
| `us_stock` | `finviz` | `finviz`、`exa`、`seeking_alpha`、`anysearch`、`eastmoney` |
| `stock_query` | `sina_quote` | `sina_quote`、`tencent_quote`、`em_flow`、`eastmoney`、`anysearch` |
| `macro_data` | `fred` | `fred`、`worldbank`、`nbs_stats`、`eurostat`、`fx_rate`、`anysearch` |
| `chem_search` | `pubchem` | `pubchem`、`openalex` |
| `species_search` | `gbif` | `gbif`、`iplant`、`wikipedia`、`obis`、`worms` |
| `rfc_search` | `rfc_editor` | `rfc_editor`、`anysearch` |
| `protein_search` | `uniprot` | `uniprot`、`rcsb_pdb` |
| `us_legal` | `courtlistener` | `courtlistener`、`federal_register`、`anysearch` |
| `earth_science` | `usgs` | `usgs`、`gdacs`、`anysearch`、`nasa_cmr`、`nasa_images`、`soilgrids` |
| `security_search` | `nvd` | `nvd`、`anysearch`、`osv`、`cisa_kev`、`crt_sh` |
| `lifecycle_search` | `endoflife` | `endoflife`、`anysearch` |
| `package_intel` | `deps_dev` | `deps_dev`、`anysearch` |
| `trade_stats` | `un_comtrade` | `un_comtrade`、`anysearch` |
| `astro_space` | `noaa_swpc` | `noaa_swpc`、`satnogs`、`tle_mirror`、`nasa_images`、`wikipedia` |
| `energy_grid` | `energy_charts` | `energy_charts`、`carbon_intensity`、`anysearch` |
| `transport_rt` | `gbfs_nyc` | `gbfs_nyc`、`train`、`anysearch` |
| `vehicle_data` | `nhtsa_vpic` | `nhtsa_vpic`、`anysearch` |
| `japan_law` | `egov_law` | `egov_law`、`anysearch` |
| `kor_law` | `kor_law` | `kor_law`、`anysearch` |
| `soil_agri` | `soilgrids` | `soilgrids`、`usda`、`openfoodfacts`、`anysearch` |
| `standards` | `std_samr` | `std_samr`、`openstd` |
| `law_text` | `flk_law` | `flk_law`、`anysearch`、`gov_regulations`、`wenshu` |
| `art_museum` | `met_museum` | `met_museum`、`wikipedia`、`artic`、`cleveland` |
| `dataset_search` | `datacite` | `datacite`、`zenodo`、`anysearch` |
| `financial_news` | `em_miaoxiang` | `em_miaoxiang`、`cninfo`、`wallstreetcn`、`byted`、`eastmoney`、`anysearch` |
| `aviation_weather` | `—` | `aviation_weather` |
| `weather_query` | `qweather` | `qweather`、`byted`、`weather_cn`、`weather` |
| `modal_card` | `bocha_ai` | `bocha_ai`、`bocha`、`train` |
| `jin10_flash` | `jin10` | `jin10`、`cls_telegraph` |
| `zhihu_hot_list` | `zhihu_hot` | `zhihu_hot`、`zhihu` |
| `hot_trending` | `baidu_hot` | `baidu_hot`、`toutiao_hot`、`bilibili_hot`、`weibo_hot`、`douyin_hot`、`ths_hot`、`zhihu_hot`、`zhihu_hot_app` |
| `patent_search` | `google_patents` | `google_patents`、`semantic_scholar`、`local_arxiv` |
| `crypto_search` | `coingecko` | `coingecko`、`anysearch` |
| `package_search` | `pypi` | `pypi`、`npm`、`crates`、`docker_hub`、`github` |
| `web_docs` | `mdn` | `mdn`、`stackoverflow` |
| `ml_models` | `huggingface` | `huggingface`、`github` |
| `ai_model` | `models_dev` | `models_dev`、`anysearch`、`huggingface` |
| `cn_tech_community` | `juejin` | `juejin`、`v2ex`、`sspai`、`csdn` |
| `legal` | `anysearch` | `anysearch`、`byted`、`flk_law`、`gov_regulations`、`wenshu` |
| `wenshu_query` | `wenshu` | `wenshu`、`anysearch` |
| `outbreak_health` | `who_don` | `who_don`、`who_gho`、`anysearch` |
| `medical` | `clinicaltrials` | `clinicaltrials`、`anysearch`、`openfda`、`local_pubmed`、`wikipedia`、`who_don`、`who_gho` |
| `game_search` | `steam` | `steam`、`anysearch` |
| `prediction_market` | `polymarket` | `polymarket`、`anysearch` |
| `web_archive` | `archive_org` | `archive_org`、`wayback_cdx`、`wikipedia` |
| `cn_encyclopedia` | `baidu_baike` | `baidu_baike`、`zh_wikipedia`、`wikidata` |
| `dictionary_search` | `free_dictionary` | `free_dictionary`、`wikipedia`、`zdic` |
| `anime_encyclopedia` | `moegirl` | `moegirl`、`bangumi`、`zh_wikipedia`、`jikan` |
| `book_search` | `weread` | `weread`、`douban_book`、`open_library`、`gutenberg`、`anysearch`、`k10plus` |
| `film_search` | `imdb` | `imdb`、`douban_movie`、`itunes`、`wikipedia`、`wikidata`、`tvmaze` |
| `sports_search` | `thesportsdb` | `thesportsdb`、`jolpica`、`anysearch`、`openf1`、`openligadb`、`wikipedia`、`wikidata`、`zh_wikipedia` |
| `geo_places` | `local_openstreetmap` | `local_openstreetmap`、`wikipedia`、`wikidata`、`zh_wikipedia` |
| `org_entity` | `wikidata` | `wikidata`、`wikipedia`、`baidu_baike`、`zh_wikipedia`、`ror`、`gleif` |
| `media_search` | `itunes` | `itunes`、`musicbrainz`、`netease_music`、`deezer`、`listenbrainz` |
| `image_search` | `openverse` | `openverse`、`wikimedia_commons`、`ddgs_images`、`anysearch` |
| `entity_search` | `wikidata` | `baidu_baike`、`wikidata`、`wikipedia`、`zh_wikipedia` |
| `zhihu_user_data` | `zhihu_user` | `zhihu_user` |
| `zhihu_content` | `zhihu` | `zhihu`、`zhihu_global`、`anysearch` |
| `academic` | `arxiv` | `arxiv`、`openreview`、`biorxiv`、`openalex`、`local_pubmed`、`core`、`cinii`、`crossref`、`europepmc`、`dblp`、`semantic_scholar` |
| `tech_deep` | `openalex` | `openalex`、`arxiv`、`semantic_scholar`、`github`、`europepmc` |
| `shopping` | `zhihu` | `zhihu`、`anysearch` |
| `semantic_discovery` | `exa` | `exa`、`anysearch` |
| `code_search` | `github` | `github`、`mdn`、`huggingface`、`anysearch` |
| `meme_slang` | `itotii` | `itotii`、`urban_dictionary`、`know_your_meme`、`anysearch` |
| `fact_check` | `wikipedia` | `wikipedia`、`byted` |
| `global_event` | `google_news` | `google_news`、`em_global_news`、`cls_telegraph` |
| `intl_news_flash` | `guardian_rss` | `guardian_rss`、`france24`、`dw_news` |
| `world_news` | `anysearch` | `anysearch`、`yna`、`tass`、`aljazeera`、`elpais`、`folha`、`lefigaro`、`faz`、`nhk`、`matichon`、`vnpress`、`antara`、`hurriyet`、`ynet`、`ct24` |
| `claim_check` | `factcheck_org` | `factcheck_org`、`full_fact`、`anysearch` |
| `news_realtime` | `byted` | `byted`、`zhihu_global`、`people_daily`、`google_news`、`octen`、`you` |
| `chinese_tech_deep` | `byted` | `byted`、`octen`、`juejin`、`cn_ai_news`、`parallel` |
| `english_tech` | `octen` | `octen`、`anysearch`、`exa`、`keenable` |
| `company_search` | `opencorporates` | `opencorporates`、`cninfo`、`baidu_baike` |
| `chinese_general` | `bocha` | `bocha`、`byted`、`anysearch`、`octen`、`zhihu_global`、`wechat_sogou`、`local_search` |
| `local_chinese` | `local_search` | `local_search`、`bocha` |
| `local_news` | `local_search` | `local_search`、`byted` |
| `local_code` | `local_search` | `local_search`、`github`、`anysearch` |
| `local_academic` | `local_search` | `local_search`、`arxiv`、`semantic_scholar` |
| `local_reference` | `local_search` | `local_search`、`wikipedia`、`wikisource` |
| `local_general` | `local_search` | `local_search`、`anysearch` |
| `social` | `zhihu` | `zhihu`、`hackernews`、`bilibili`、`v2ex`、`twitter`、`fxtwitter`、`reddit`、`xiaohongshu`、`weibo` |

## 七、怎么自己查当前状态

```bash
argo search --list-engines --detail | python3 -m json.tool | less   # 全部 255 个源的详情
argo search --list-engines --detail --routable-only              # 只看现在能用的
python3 scripts/matrix_search_eval.py --offline                   # 可达性门：有没有死源
python3 scripts/engine_validate.py --engine <名> --stage all       # 单个源的健康+质量双阶段体检
```


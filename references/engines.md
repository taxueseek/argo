# Argo 引擎全景（v2.8.0）

> 引擎声明真源为 `config.yaml`（外置 `engines/specs/*.yaml` 优先覆盖）。本页是 Agent 路由速查。
> 全量清单运行：`python3 scripts/search.py --list-engines`

## 通用引擎（API/免 Key）

| 引擎 | cost_tier | 特点 |
|------|-----------|------|
| anysearch | free | 匿名兜底，垂直域结构化 |
| byted / bocha / bocha_ai | low | 中文综合搜索（BYTED/BOCHA_API_KEY） |
| exa | api | 语义搜索引擎，embedding 匹配（EXA_API_KEY） |
| tavily | paid | 通用搜索（TAVILY_API_KEY） |
| octen | low | 高速语义搜索（OCTEN_API_KEY） |
| felo / metaso | low | AI 搜索（FELO/METASO_API_KEY） |
| duckduckgo | free | 通用搜索 |
| wechat_sogou | free | 微信公众号文章搜索（v2.3） |
| zhihu / zhihu_global / zhihu_hot | free | 知乎站内/全网/热榜（ZHIHU_ACCESS_SECRET） |

## 社交平台引擎

| 引擎 | cost_tier | 特点 | 认证 |
|------|-----------|------|------|
| twitter | free | Twitter/X 推文搜索 | 可选（nitter 兜底） |
| reddit | free | Reddit 帖子+评论 | 无需 |
| xiaohongshu | free | 小红书笔记+评论 | xhs login |
| bilibili | free | B站视频+弹幕 | 无需 |
| weibo | free | 微博帖子+话题 | 无需 |

社交引擎统一输出 `social_meta` 字段（作者/互动/平台元信息）。

## 垂直内容引擎（梗/热榜）

| 引擎 | 特点 | 数据源 |
|------|------|--------|
| itotii | 中文流行语/网络梗词条 | geng.itotii.com |
| urban_dictionary | 英文俚语定义+例句 | api.urbandictionary.com |
| know_your_meme | 英文 meme 溯源 | knowyourmeme.com |
| zh_wikipedia | 中文维基百科 | zh.wikipedia.org |
| baidu_hot / toutiao_hot / bilibili_hot / ths_hot | 各平台实时热搜榜 | 官方页面/API |

路由：`meme_slang` 域 → itotii + urban_dictionary + know_your_meme；`hot_trending` 域 → baidu_hot + toutiao_hot + bilibili_hot + ths_hot。

## 广义垂直引擎（媒体/图书/包管理/实体/词典/百科/加密）

itunes（音乐影视）、openverse（开放版权图片）、coingecko（加密）、wikidata（结构化实体）、crates（Rust 包）、musicbrainz（音乐元数据，1rps）、open_library（图书）、free_dictionary（英英词典）、moegirl（萌娘百科）。

## P0/P1 扩展引擎（学术/代码/文档/医学/游戏）

| 域 | 引擎 |
|----|------|
| 学术 | arxiv / openalex / crossref / europepmc / dblp / google_scholar / semantic_scholar / local_pubmed / rcsb_pdb / uniprot |
| 包管理/代码 | pypi / npm / crates / docker_hub / github / huggingface / models_dev / stackoverflow / juejin / devto / v2ex / qiita / mdn / rfc_editor |
| 百科/词典 | baidu_baike / wikipedia / zh_wikipedia / wikidata / wikiquote / wiktionary / free_dictionary / open_library |
| 医学 | clinicaltrials / openfda / pubchem |
| 游戏/预测 | steam / polymarket |
| 档案 | archive_org / wayback_cdx |
| 新闻 | hackernews / jin10 / cls_telegraph / cn_ai_news / cnii / gov_policy / em_global_news |
| 宏观/金融 | fred / worldbank / nbs_stats / eurostat / eastmoney / em_flow / tencent_quote / sina_quote / finviz / seeking_alpha / tencent_kline / fx_rate / gold_analyzer 系 |
| 影视/体育 | itunes / opensky / electricity_maps / usda / tatoeba / gbif / nasa_cmr / usgs / open_meteo / qweather / aviation_weather |
| 法律 | courtlistener / wenshu / kor_law |
| 长尾/独立 | marginalia / wiby / searchmysite / lieu |

路由（先匹配先命中）：`package_search` → pypi+npm+crates+docker_hub+github；`web_docs` → mdn+stackoverflow；`ml_models` → huggingface+github；`cn_tech_community` → juejin+v2ex+devto；`medical` → clinicaltrials+openfda+local_pubmed；`academic` → arxiv+openalex+crossref+europepmc+dblp+semantic_scholar；`game_search` → steam；`prediction_market` → polymarket；`web_archive` → archive_org。

## 本地零成本引擎（local_search 聚合，33 个）

web_general（duckduckgo/bing/yandex/brave/yahoo/mojeek/startpage）、chinese（baidu/sogou）、academic（arxiv/pubmed/semantic_scholar/crossref/wikipedia/wikiquote/wiktionary）、news（google_news/bing_news/ddgs_news）、code（github/gitlab/stackoverflow/npm）、reference、vertical（images/videos/books 等）。10 个走 ddgs CLI（text 五后端 + news/images/videos/books），结构化 JSON 输出，按查询语言自动下推 region，失败自动重试 1 次。

`--local-first` 强制本地聚合优先；fast/budget 模式自动前置 local_search。

## 招聘聚合（argo job）

免 key 后端：remotive / himalayas / jobicy / arbeitnow / greenhouse / ashby（Ashby ATS 免 Key，notion/openai 等）；平台白名单：BOSS/猎聘/智联/前程无忧/597/今日招聘 + 卓博/鱼泡/中华英才/智通/58/国聘/24365/91job/yingjiesheng/JobsDB/JobStreet/苏州人社局；人社局域名通用识别。

常用：`python3 scripts/job.py --engine free -n 5 --platforms zhipin,liepin,zhaopin,51job,597,jrzp --loose --json --fetch N --watch`（`--watch` 增量监控存快照到 data/jobs/）。

## 常用引擎调用示例

```bash
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
python3 scripts/search.py "这是什么梗" --engine itotii
python3 scripts/search.py "httpx" --engine pypi
python3 scripts/search.py "Fetch API" --engine mdn
python3 scripts/search.py "bert" --engine huggingface
python3 scripts/search.py "查询词" --engine ths_hot --engine cls_telegraph --engine em_global_news
```

## 外置引擎声明（engines/specs/*.yaml，启动时合并、优先覆盖）

aviation_weather / cn_ai_news / datacite / firecrawl / fxtwitter / realtime_index / sec_edgar / train / weather / zenodo（10 个）。

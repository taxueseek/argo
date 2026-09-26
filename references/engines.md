# Argo 路由速查与选源经验

> **全量引擎清单、费用、密钥、状态、域组合在 `docs/ENGINE_CATALOG.md`**（由
> `scripts/gen_engine_catalog.py` 从声明生成，有检查防漂移）——本页不重复清单，
> 只放「清单生成不出来」的东西：语义分工与选源判断。
>
> 声明来源：`config.yaml` ＋ `engines/specs/*.yaml`（外置 spec 启动时合并、优先覆盖）。
> 本机此刻哪些源就绪：`argo search --list-engines --detail`。

## 一、选源第一原则：先看语义，再看名字

同一个「搜索」在不同意图下要的不是同一批源。选错源的典型症状是**结果同质化**
（十条来自同一家）或**答非所问**（要数据集却给论文）。

### 知乎四源（同一密钥，四种语义）

| 源 | 搜的是 | 什么时候用 |
|---|---|---|
| `zhihu` | 知乎站内 UGC | 观点 / 经验 / 评测（「怎么看待」「如何评价」「哪个好」） |
| `zhihu_global` | 全网索引（中文为主） | 中文时事、泛资讯；支持 `site:域名` / `host:域名` 限定；英文召回弱 |
| `zhihu_user` | 本人创作 / 收藏 / 关注 | 「我的回答」「我的收藏」「我关注的人」——个人运营与素材回溯，不是内容搜索 |
| `zhihu_hot` | 官方热榜快照 | 只看热榜时（100/天） |

### 内容类型先分清（2026-09-12 拆开的三组）

- **论文 ≠ 数据集**：`academic` 域（arXiv / OpenAlex / Crossref / EuropePMC / DBLP /
  Semantic Scholar）管论文；`dataset_search` 域（DataCite / Zenodo）管数据集、开放数据。
  一句话里出现「数据集 / 开放数据 / dataset」才走后者。
- **行情 ≠ 申报原文**：`us_stock` / `stock_query` 管报价与资金流；`sec_filings` 域
  （SEC EDGAR）管 10-K / 10-Q / 8-K / 招股书这类申报文件原文。
- **推文关键词 ≠ 单条推文**：`fxtwitter`（在 `social` 域内）做 X 关键词搜索；
  单条推文按 URL 取正文走 `twitter_syndication` 通道（见下「URL 类查询」）。

## 二、URL 类查询：不是搜索，是交接

输入是 URL 时，搜索层不会硬搜——`classify_input_kind` 判为 `known-url` 后返回
`handoff_required`，并给建议工具：

- 普通网页 → `argo_fetch` / `argo_pdf`；
- **单条推文 URL → 提示 `argo_search(engine=twitter_syndication)`**（免登录、零密钥）。

想强行当关键词搜（用 URL 找相关讨论），加 `--input-kind url-seed`。

## 三、通用保底链的层次（`engine_policy.GENERAL_FREE_FALLBACK`）

`anysearch` → `local_bing` → `uapi` → `local_baidu` → `firecrawl` → `parallel_free` → `wikipedia`。
顺序有讲究：通用检索优先，本地零成本引擎居中，百科殿后；
`firecrawl` 排在自由额度型源（1000 credits/月）**末位**，日常 fast/auto 的 combo 预算
（2 / 3 个）够不到它，只在 deep / research 无截断时参战，共享免费层不会被烧穿。
`parallel_free`（2026-09-14 收录）同为末位补位：官方免费 MCP 端点
（search.parallel.ai，无账号无 key），与按量计费的 `parallel`（REST + key）分立——
key 缺失/额度耗尽/REST 失败时由它接住，自带 excerpts 长摘录省一次 fetch。
`duckduckgo` 2026-09-07 移出保底链（实测 45% 错误率 + 11 秒 0 条）；2026-09-26 两引擎（Instant Answer API + local_duckduckgo）随本机可达性门下线：出口对本域 TLS 层阻断，html/api 子域全灭，curl_cffi 三种指纹亦无法穿透，同类项目（ddgs 等）同样处于与该封锁的军备竞赛中，无客户端解法。

## 四、本地零成本引擎（`local_search` 聚合，25 个子引擎声明）

`local_bing` / `local_baidu` / `local_sogou` / `local_google` /
`local_yandex` / `local_mojeek` / `local_startpage` / `local_github` / `local_gitlab` /
`local_npm` / `local_stackoverflow` / `local_arxiv` / `local_pubmed` /
`local_semantic_scholar` / `local_crossref` / `local_wikipedia` / `local_wikiquote` /
`local_wiktionary` / `local_imdb` / `local_openstreetmap` / `local_bing_news` /
`local_google_news` / `local_goodreads` / `local_search`（聚合入口）。

`--local-first` 强制本地聚合优先；fast / budget 模式自动前置 `local_search`。
零密钥、零额度，语言相关的查询由它兜（`setlang` 按查询语言下推）。

## 五、选源反模式（都是踩过的坑）

1. **窄意图域必须排在宽泛域之前**：多意图查询按 `config.yaml` 域的顺序取主域。
   写新域时先看有没有更宽泛的域会先命中（`english_tech` / `chinese_general` 最容易抢）。
2. **抢词要收窄**：`dataset_search` 不写裸 `DOI`（论文也用 DOI），`sec_filings` 不写裸
   `财报`（那是 `financial_news` 的）。触发词只留能区分意图的那几个。
3. **引擎有「能力标签」才有话题信号**：`coverage` 写在 spec 里（不是注册表文档里）——
   互补回填要求主引擎带 coverage 才工作。
4. **付费档不进自动路由**：`paid` 档只有显式 `--engine` 才会用到；
   `low` / `api` 档有若干在自动路径上（见 catalog 的表），想完全避开用 `--mode budget`
   或显式指定免费源。
5. **别用「关键词」调抽取型引擎**：`twitter_syndication` 没有搜索端点，
   关键词查询诚实返回空——这是设计，不是故障。

## 六、常用调用

```bash
# 通用
python3 scripts/search.py "查询词" --json
python3 scripts/search.py "查询词" --engine anysearch          # 指定引擎
python3 scripts/search.py "查询词" --engine zhihu              # 知乎站内
python3 scripts/search.py "阿里 财报 研报" --engine eastmoney
python3 scripts/search.py "苹果 10-K" --json                   # 自动进 sec_filings 域
python3 scripts/search.py "climate dataset" --json              # 自动进 dataset_search 域

# 抽取 / 抓取
python3 scripts/search.py "https://x.com/user/status/1585841080431321088" \
    --engine twitter_syndication --json                        # 单条推文（免登录）
python3 scripts/fetch_v3.py "https://example.com"               # 单页正文（四级降级）
python3 scripts/batch_probe.py "url1" "url2" --probe            # 批量预检

# 深度研究 / 证据
python3 scripts/research.py "问题" --json
python3 scripts/search.py "查询词" --verify 3                   # 核验 top-3 正文

# 小红书技能榜（redskill，本地缓存免认证）
python3 scripts/redskill/redskill_engine.py rank use -n 10      # use|new|today|author
```

## 七、招聘聚合（`argo job`，独立于搜索）

免 key 后端：`remotive` / `himalayas` / `jobicy` / `arbeitnow` / `greenhouse` / `ashby`；
平台白名单：BOSS / 猎聘 / 智联 / 前程无忧 / 597 / 今日招聘 + 卓博 / 鱼泡 / 中华英才 /
智通 / 58 / 国聘 / 24365 / 91job / yingjiesheng / JobsDB / JobStreet / 苏州人社局。

```bash
python3 scripts/job.py "岗位 城市" --engine free -n 5 \
  --platforms zhipin,liepin,zhaopin,51job,597,jrzp --loose --json --fetch 5 --watch
```

`--watch` 增量监控，快照存 `data/jobs/`；`--engine free` 只走免 key 后端。

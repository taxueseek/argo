#!/usr/bin/env python3
"""
engine_families.py — 搜索源能力族分类（第一性原理重构）

问题重定义：
  旧：每个搜索源是独立个体，type/cost/coverage 散落，路由靠 domain 手写 combo，
      同类源（如 byted/bocha 都是全网搜索）无法互换、无法统一测试。
  新：搜索源按「检索能力」归族（MECE），同族源共享统一调用契约——
      任意组合、标准化输入输出、金标集按族检查、A/B 可替换。

能力族（MECE，互斥且穷尽）：
  web_general      全网网页检索（多语言）    byted / bocha / tavily / anysearch / octen / exa
  web_chinese      中文全网检索              bocha / byted / wechat_sogou / local_bing 等
  academic         学术文献                  arxiv / openalex / crossref / semantic_scholar / dblp / europepmc
  code             代码/包/文档              github / pypi / npm / mdn / stackoverflow / crates / gitlab / devto
  finance_market   行情/资金                 sina_quote / tencent_quote / em_flow / finviz
  finance_macro    宏观数据                  fred / worldbank / nbs_stats / eurostat / fx_rate
  news_flash       快讯/电报                 cls_telegraph / em_global_news / jin10 / em_miaoxiang
  social           社区 UGC                 zhihu / zhihu_global / v2ex / juejin / reddit / twitter / xiaohongshu / bilibili / weibo
  personal_data    个人数据（本人创作/收藏/关注） zhihu_user
  hot_trending     热榜                     baidu_hot / toutiao_hot / bilibili_hot / zhihu_hot / ths_hot
  knowledge        百科/实体                 wikipedia / baidu_baike / zh_wikipedia / wikidata / moegirl / free_dictionary
  science_chem     化学/药学                 pubchem / openfda / clinicaltrials
  science_bio      生物/蛋白                 uniprot / rcsb_pdb / gbif
  science_geo      地球/空间                 usgs / nasa_cmr
  legal            法律判例                  courtlistener / wenshu
  media_book       媒体/图书                 itunes / open_library / weread / douban_book / gutenberg / musicbrainz / openverse / imdb
  sports           体育赛事/球员球队           thesportsdb
  archive          归档/历史                 wayback_cdx / archive_org
  misc_vertical    其他垂直（游戏/预测/天气/标准等） steam / polymarket / qweather / rfc_editor / models_dev / itotii / urban_dictionary / know_your_meme / coingecko / docker_hub / huggingface / stackoverflow 等
  structured_card  垂直结构化模态卡（统一语义识别，跨域） bocha_ai

用途：
  - route 层组合时按 family 去重（同族至多 N 个，避免同质源堆叠）
  - 去重腾出的槽位用互补能力族引擎回填（complement_refill）
  - 金标集按族检查而非按引擎名（源可替换不破坏测试）
  - engine_status / list-engines 展示按族分组
  - research profile 的 vertical_engines 可按 family 扩展
"""

from __future__ import annotations

from typing import Any

# ── 引擎 → 能力族 映射（config.yaml 引擎声明的 family 字段优先，本表保底） ──

# 默认族：未声明 family 的引擎归 web_general（全网搜索是通用保底）
DEFAULT_FAMILY = "web_general"

# 引擎名 → family（显式覆盖表；config.yaml 声明 family 时以其为准）
_ENGINE_FAMILY_OVERRIDES: dict[str, str] = {
    # 全网搜索
    "byted": "web_general",
    "bocha": "web_general",
    "tavily": "web_general",
    "anysearch": "web_general",
    "octen": "web_general",
    "exa": "web_general",
    "uapi": "web_general",
    "searxng": "web_general",
    # zhihu_global 是真全网搜索（Filter host== 可搜非知乎站），非社区站内
    "zhihu_global": "web_general",
    # 中文全网（子集，语义上仍是 web_general，但标记中文能力）
    "wechat_sogou": "web_general",
    "local_bing": "web_general",
    "local_baidu": "web_general",
    "local_sogou": "web_general",
    "local_360": "web_general",
    "local_jisilu": "social",
    # 全网搜索引擎四兄弟（ecosia/qwant/shenma/so 显式_only 或中文移动端，
    # 能力上与 local_baidu/local_bing 同族）
    "ecosia": "web_general",
    "qwant": "web_general",
    "shenma": "web_general",
    "so": "web_general",
    # 批次十（2026-09-26）：bing_rss 是 local_bing 的 RSS 稳定出口；
    # tineye 反搜图不属于任何既有能力族，归 misc_vertical。
    "bing_rss": "web_general",
    "tineye": "misc_vertical",
    # 学术
    "arxiv": "academic",
    "openalex": "academic",
    "crossref": "academic",
    "semantic_scholar": "academic",
    "dblp": "academic",
    "europepmc": "academic",
    "google_scholar": "academic",
    "local_arxiv": "academic",
    "local_crossref": "academic",
    "local_semantic_scholar": "academic",
    "local_pubmed": "academic",
    # 代码
    "github": "code",
    "pypi": "code",
    "npm": "code",
    "crates": "code",
    "mdn": "code",
    "stackoverflow": "code",
    "gitlab": "code",
    "devto": "code",
    "docker_hub": "code",
    "huggingface": "code",
    "local_github": "code",
    "local_gitlab": "code",
    "local_npm": "code",
    "local_stackoverflow": "code",
    # 行情/资金
    "sina_quote": "finance_market",
    "tencent_quote": "finance_market",
    "em_flow": "finance_market",
    "finviz": "finance_market",
    "eastmoney": "finance_market",
    "cninfo": "finance_market",
    "seeking_alpha": "finance_market",
    # 宏观
    "fred": "finance_macro",
    "worldbank": "finance_macro",
    "nbs_stats": "finance_macro",
    "eurostat": "finance_macro",
    "fx_rate": "finance_macro",
    # 快讯
    "cls_telegraph": "news_flash",
    "em_global_news": "news_flash",
    "jin10": "news_flash",
    "em_miaoxiang": "news_flash",
    "local_bing_news": "news_flash",
    "local_google_news": "news_flash",
    # 社区 UGC
    "zhihu": "social",
    "v2ex": "social",
    "juejin": "social",
    "reddit": "social",
    "twitter": "social",
    "fxtwitter": "social",
    "xiaohongshu": "social",
    "bilibili": "social",
    "weibo": "social",
    "hackernews": "social",
    # 热榜
    "baidu_hot": "hot_trending",
    "toutiao_hot": "hot_trending",
    "bilibili_hot": "hot_trending",
    "zhihu_hot": "hot_trending",
    "ths_hot": "hot_trending",
    # 百科/实体
    "wikipedia": "knowledge",
    "zh_wikipedia": "knowledge",
    "baidu_baike": "knowledge",
    "wikidata": "knowledge",
    "moegirl": "knowledge",
    "free_dictionary": "knowledge",
    "local_wikipedia": "knowledge",
    "local_wiktionary": "knowledge",
    "local_wikiquote": "knowledge",
    # 化学/药学
    "pubchem": "science_chem",
    "openfda": "science_chem",
    "clinicaltrials": "science_chem",
    # 生物/蛋白
    "uniprot": "science_bio",
    "rcsb_pdb": "science_bio",
    "gbif": "science_bio",
    # 地球/空间
    "usgs": "science_geo",
    "nasa_cmr": "science_geo",
    "local_openstreetmap": "science_geo",
    "openstreetmap": "science_geo",
    # 法律
    "courtlistener": "legal",
    "wenshu": "legal",
    # 媒体/图书
    "itunes": "media_book",
    "open_library": "media_book",
    "weread": "media_book",
    "douban_book": "media_book",
    "gutenberg": "media_book",
    "musicbrainz": "media_book",
    "openverse": "media_book",
    "imdb": "media_book",
    "local_imdb": "media_book",
    # 体育
    "thesportsdb": "sports",
    # 归档/历史
    "wayback_cdx": "archive",
    "archive_org": "archive",
    # 其他垂直
    "steam": "misc_vertical",
    "polymarket": "misc_vertical",
    "qweather": "misc_vertical",
    "rfc_editor": "misc_vertical",
    "models_dev": "misc_vertical",
    "itotii": "misc_vertical",
    "urban_dictionary": "misc_vertical",
    "know_your_meme": "misc_vertical",
    "coingecko": "misc_vertical",
    # 火车票（官方接口，结构化行数据）
    "train": "misc_vertical",
    # 垂直结构化模态卡（统一语义识别，跨垂直域）
    "bocha_ai": "structured_card",
    # 本地聚合
    "local_search": "web_general",
    "tencent_kline": "finance_market",
    "qq_music": "media_book",
    "usda": "science_bio",
    "opensky": "structured_card",
    "electricity_maps": "misc_vertical",
    "tatoeba": "knowledge",
    "figshare": "academic",
    "searchmysite": "web_general",
    "lieu": "web_general",
    "cnii": "academic",
    "ndl": "knowledge",
    "kor_law": "legal",
    "hatena_bookmark": "social",
    "dnb": "knowledge",
    "doaj": "academic",
    "europeana": "knowledge",
    "hal": "academic",
    "eu_opendata": "misc_vertical",
    "open_meteo": "structured_card",
    "gov_policy": "legal",
    "qiita": "social",
    "fr_opendata": "misc_vertical",
    # ── 补录：此前未归类、靠 DEFAULT_FAMILY 静默落到 web_general 的引擎 ──────
    #
    # 2026-09-16：实测 222 个引擎里有 69 个从未被本表或 config 标注过，于是
    # 全部保底成 web_general（该族一度占 41%）。后果不是「统计计算方式难看」，而是
    # **这些源永远选不中**——route 按 family 组 combo，一个被误判为「全网搜索」
    # 的垂直源在通用查询里被同族上限挤掉，在它真正该服务的垂直域里又不被认作
    # 该族成员。实测：查「生物医学预印本」combo 里没有 biorxiv、查「SEC 监管
    # 文件」没有 sec_edgar、查「CVE 漏洞」没有 nvd，而它们其实都已接入且可用。
    #
    # 归族判据只有一条：**这个源回答的是哪一类问题**。判断依据取自各源 spec 的
    # desc（本表的注释即该源的自我描述摘要），不按数据形态（JSON/HTML）分。
    #
    # 学术文献
    "biorxiv": "academic",
    "openreview": "academic",
    "datacite": "academic",
    "zenodo": "academic",
    "tinyfish_paper": "academic",
    "k10plus": "academic",     # 德国最大联合目录：书目/馆藏
    # CORE 也归 academic 而非新建族：它回答的仍是「这个主题有哪些论文」，
    # 与 openalex/crossref 同一问题域；差别在**返回层**（可下载全文 PDF）
    # 而非能力域。单独建族会让 dedupe_by_family 失去「同族去重」的意义。
    "core": "academic",
    # 代码/包
    "deps_dev": "code",
    "endoflife": "code",
    # 百科/实体
    "ror": "knowledge",        # 研究机构标识
    "opencorporates": "knowledge",
    "wikisource": "knowledge",
    "zdic": "knowledge",       # 汉典：字义/音韵/字源
    # 媒体/图书/艺术
    "bangumi": "media_book",
    "jikan": "media_book",
    "tvmaze": "media_book",
    "douban_movie": "media_book",
    "deezer": "media_book",
    "listenbrainz": "media_book",
    "netease_music": "media_book",
    "local_goodreads": "media_book",
    "artic": "media_book",     # 芝加哥艺术博物馆馆藏
    "cleveland": "media_book",
    "met_museum": "media_book",
    "nasa_images": "media_book",
    # 生物/医药
    "obis": "science_bio",
    "worms": "science_bio",
    "iplant": "science_bio",   # 中文植物名 → 学名/分类（补 gbif 的中文盲区）
    "who_don": "science_bio",  # WHO 疫情暴发通报
    "who_gho": "science_bio",  # WHO 全球卫生指标
    # 地球/空间
    "soilgrids": "science_geo",
    "noaa_swpc": "science_geo",
    "satnogs": "science_geo",
    "tle_mirror": "science_geo",
    "gdacs": "science_geo",    # 全球多灾种预警
    "carbon_intensity": "science_geo",
    "energy_charts": "science_geo",
    # 法律/标准
    "egov_law": "legal",
    "flk_law": "legal",
    "gov_regulations": "legal",
    "openstd": "legal",        # 国标全文公开系统
    "std_samr": "legal",       # 全国标准信息公共服务平台
    "nhtsa_vpic": "legal",     # 车辆型式认证（法规型数据，非行情）
    # 安全情报（NVD 原被默认成全网搜索，实测 CVE 查询选不中）
    "nvd": "security",
    "crt_sh": "security",      # 证书透明度日志：子域名/证书情报
    # 宏观/贸易
    "un_comtrade": "finance_macro",
    "gdelt": "news_flash",     # 全球新闻事件数据库
    "people_daily": "news_flash",
    "sspai": "social",
    "redskill": "social",
    "zhihu_hot_app": "hot_trending",
    # 归档
    "marginalia": "web_general",   # 独立爬虫索引，确属全网搜索
    "wiby": "web_general",         # 老式手工网页索引，同上
    # 独立/新兴全网搜索（显式登记，避免再来一轮静默保底）
    "brave": "web_general",
    "felo": "web_general",
    "firecrawl": "web_general",
    "keenable": "web_general",
    "metaso": "web_general",
    "parallel": "web_general",
    "parallel_free": "web_general",
    # seltz 归 news_flash：它答的是新闻类问题（scope 只有 news/wikipedia/
    # people/companies 四个语料）。原先归 web_general 是误判——该族在
    # dedupe_by_family 里 max_per_family=2，它会与 octen/anysearch 争槽位
    # 而被静默挤掉，路由里永远轮不到。
    "seltz": "news_flash",
    "tinyfish": "web_general",
    "tinyfish_news": "news_flash",
    "you": "web_general",
    "google_news": "news_flash",
    "wolframalpha": "structured_card",
    "google_patents": "misc_vertical",
    "realtime_index": "misc_vertical",
    "gbfs_nyc": "misc_vertical",   # 共享单车站点
    "twitter_syndication": "social",
    # 本地新闻流（world_news）：每源服务一种本地语言，按查询语言互斥选源。
    # 声明在这里（而非只写在 spec）是因为 route._specs_snapshot() 在 engines
    # 模块未加载时返回空表，族/语言判定必须能从静态表回退，否则路由层看不见。
    "yna": "world_news", "tass": "world_news", "aljazeera": "world_news",
    "elpais": "world_news", "folha": "world_news", "lefigaro": "world_news",
    "faz": "world_news", "nhk": "world_news", "matichon": "world_news",
    "vnpress": "world_news", "antara": "world_news", "hurriyet": "world_news",
    "ynet": "world_news", "ct24": "world_news",
}

# 族 → 展示名
FAMILY_LABELS: dict[str, str] = {
    "web_general": "全网搜索",
    "academic": "学术文献",
    "code": "代码/包/文档",
    "finance_market": "行情/资金",
    "finance_macro": "宏观数据",
    "news_flash": "快讯/电报",
    "social": "社区 UGC",
    "hot_trending": "热榜",
    "knowledge": "百科/实体",
    "science_chem": "化学/药学",
    "science_bio": "生物/蛋白",
    "science_geo": "地球/空间",
    "legal": "法律判例",
    "media_book": "媒体/图书",
    "sports": "体育",
    "archive": "归档/历史",
    "structured_card": "垂直结构化模态卡",
    "personal_data": "个人数据（本人创作/收藏/关注）",
    "misc_vertical": "其他垂直",
    # 安全情报（CVE/漏洞/证书）：与「代码/包」区分开——回答的是「这个组件
    # 有没有已知漏洞」，不是「这个库怎么用」，路由与组合策略也不同。
    "security": "安全情报",
    # 声明核验（事实核查）：回答的是「这个具体声明被判定为真/假/误导」，
    # 既不是「发生了什么」（news_flash）也不是「X 是什么」（knowledge）。
    # 单列一族是为了不与新闻源争 max_per_family=2 的同族槽位。
    "verification": "声明核验",
    # 本地新闻流（各国本地语言的一手新闻 RSS）：与 hot_trending（中文热榜）
    # 和 news_flash（财经快讯）区分——回答的是「某国此刻发生了什么」。
    # 单列一族有三个理由：①同族 max_per_family=2 会把 20 个语言源互相挤掉；
    # ②语言由 langs 声明、按查询语言选源，需要族内可枚举；
    # ③它是实时流，不能参与通用 combo 回填（同 hot_trending）。
    "world_news": "本地新闻流",
    # 购物（2026-09-26 批次十）：电商站内搜索登录墙/强反爬，六源均为 DDG
    # site: 语法包装，回答「某商品在某平台的在售/价格」，不是通用网页检索。
    "shopping": "购物",
}


def family_of(engine: str, spec: dict[str, Any] | None = None) -> str:
    """返回引擎的能力族。

    优先级：config.yaml 引擎声明的 `family` 字段 > 本表显式覆盖 > 默认 web_general。
    spec 传入时优先读 spec["family"]（声明式，config 是来源）。
    """
    if spec and isinstance(spec, dict):
        f = spec.get("family")
        if isinstance(f, str) and f:
            return f
    return _ENGINE_FAMILY_OVERRIDES.get(engine, DEFAULT_FAMILY)


# ── 语言维度（2026-09-07 从 route.py 三张手写冻结表忠实推导收紧）──────────
# 语义：查询语言 lang 下，engine_langs 不含 lang 且不含 "*" → 该引擎对该
# 语言无召回价值（语言重排移尾 / ja-ko 组合过滤 / research 语言 boost /
# 可达性门统一从这里取）。config.yaml 引擎声明 `langs` 时以其为准；
# 未声明的引擎 = ["*"]（语言中立：anysearch/github/wikipedia/arxiv/local_* 等）。
ENGINE_LANGS: dict[str, list[str]] = {
    # 纯英文社区（对中文查询几乎零召回）
    "hackernews": ["en"], "reddit": ["en"], "twitter": ["en"],
    "nitter": ["en"], "lobsters": ["en"],
    # 中文专用（对 en/ja/ko 几乎零召回）
    "zhihu": ["zh"], "zhihu_global": ["zh"], "zhihu_hot": ["zh"],
    "zhihu_user": ["zh"], "zhihu_content": ["zh"],
    "bilibili": ["zh"], "v2ex": ["zh"], "xiaohongshu": ["zh"],
    "weibo": ["zh"], "baidu_baike": ["zh"], "moegirl": ["zh"],
    "cn_encyclopedia": ["zh"], "gov_policy": ["zh"], "cn_ai_news": ["zh"],
    "wechat_sogou": ["zh"], "baidu_hot": ["zh"], "toutiao_hot": ["zh"],
    "bilibili_hot": ["zh"], "juejin": ["zh"], "cnblogs": ["zh"],
    "wenshu": ["zh"], "kor_law": ["ko"],
    # 中英双语（ja/ko 无召回）：金融/中文 web API 源
    "bocha": ["zh", "en"], "bocha_ai": ["zh", "en"], "byted": ["zh", "en"],
    "octen": ["zh", "en"], "tencent_kline": ["zh", "en"],
    "eastmoney": ["zh", "en"], "sina_quote": ["zh", "en"],
    "tencent_quote": ["zh", "en"], "em_flow": ["zh", "en"],
    "em_global_news": ["zh", "en"], "em_miaoxiang": ["zh", "en"],
    "jin10": ["zh", "en"], "ths_hot": ["zh", "en"],
    "cls_telegraph": ["zh", "en"], "finviz": ["en"],
    # 本地新闻流：语言绑定（俄语查询用韩联社只会拿到韩语新闻，是错不是次优），
    # 路由据此在族内按查询语言互斥选源。
    "yna": ["ko"], "nhk": ["ja"], "tass": ["ru"], "aljazeera": ["ar"],
    "elpais": ["es"], "folha": ["pt"], "lefigaro": ["fr"], "faz": ["de"],
    "matichon": ["th"], "vnpress": ["vi"], "antara": ["id"],
    "hurriyet": ["tr"], "ynet": ["he"], "ct24": ["cs"],
    # 语言专用的国别垂直库：与上面同理——只在 spec YAML 里声明会被路由层
    # 漏掉（route._specs_snapshot() 在 engines 未加载时返回空表，回退到本表）。
    # 实测：cinii 漏登记时英文查询照样把它选进 academic 域预算窗口。
    "cinii": ["ja"],
    # 同样只存在于 config.yaml 而未登记本表的日语源（route_lang 查
    # ENGINE_LANGS 决定族内互斥与 must_keep，漏登记 = 在语言路由层隐形）：
    #   qiita  实测日文查询 5/5 全日文，是 ja 技术类查询的主力源；
    #   hatena_bookmark 日本技术圈书签聚合，同族。
    # ndl（日本国立国会图书馆书目）**故意不登记**：实测它对任何语言都返回
    # 0 条（上游 OpenSearch 正常 200，builder 已注册，故是解析层坏了）。
    # 登记一个恒返回空的源只会让路由把预算花在死源上。修好解析层再登记。
    "qiita": ["ja"], "hatena_bookmark": ["ja"],
}
ENGINE_LANGS_DEFAULT = ("*",)

# 书写系统标签 → 该语系下的具体语言码。
#
# 为什么需要这张表：lang_detect 对**没有足够特征**的文本只给到书写系统
# （西里尔/阿拉伯/希伯来/天城文/泰文），不给具体语言——`новости сегодня`
# 返回 cyrillic、`أخبار اليوم` 返回 arabic。而源声明的是具体语言（tass=ru、
# aljazeera=ar）。两边对不上时，该语言的本地源会被判成「不匹配」而永远
# 选不中（实测：俄语/阿语新闻查询 combo 里没有任何本地源）。
#
# 展开方向是单向的：查询给语系 → 认该语系下的具体语言源；查询给具体语言
# （ja/ko/zh 等）时不反向展开——那会把「zh 查询用日文源」这类错配放进来
# （lang_capability 记过同类错误：ja 展开成 (zh, ja) 会串味）。
_SCRIPT_FAMILY_LANGS: dict[str, tuple[str, ...]] = {
    "cyrillic": ("ru", "uk", "bg", "sr", "mk", "be"),
    "arabic": ("ar", "fa", "ur"),
    "hebrew": ("he",),
    "thai": ("th",),
    "devanagari": ("hi", "mr", "ne"),
    "greek": ("el",),
}

# 语言提示词：查询里出现这些实词时，按该语言处理。
#
# 为什么需要：lang_detect 对拉丁字母语言只给到 en/latin——`noticias de hoy`
# 判成 en、`berita hari ini` 判成 en、`tin tức` 判成 latin。这些语言共享
# 字母表，靠码位分不开（与 CJK 的汉字共享同一根因）。结果是西语/葡语/越南语/
# 印尼语/土耳其语查询在 world_news 族里匹配不到任何源，只能拿到英文通用源，
# 新接的 elpais/folha/vnpress/antara/hurriyet 永远选不中。
#
# 判据是**该语言的新闻/资讯类实词**（与 world_news 域 patterns 同一批词）：
# 这类词在查询里出现，说明用户就是在用那种语言问「有什么新闻」。方向仍然是
# 单向的——只在路由层做「查询语言 → 该语言源」的候选收窄，不改写查询、
# 不参与语言能力加权，判定失误的代价仅限于选源顺序。
_LANG_HINT_WORDS: dict[str, tuple[str, ...]] = {
    "ko": ("뉴스",), "ru": ("новости",), "ar": ("أخبار",),
    "es": ("noticias",), "pt": ("notícias",), "fr": ("actualités", "nouvelles"),
    "de": ("nachrichten",), "ja": ("ニュース",), "th": ("ข่าว",),
    "vi": ("tin tức",), "id": ("berita",), "tr": ("haberler", "haber"),
    "he": ("חדשות",), "cs": ("zprávy",),
}


def lang_hint_from_query(query: str) -> str:
    """从查询文本里的实词推断语言；无命中返回 ""。

    只服务「本地语言源选源」这一个场景，不替代 lang_detect：调用方应在
    语言判定给不出具体语种（en/latin/mixed）时才用它。
    """
    low = (query or "").lower()
    if not low:
        return ""
    for lang, words in _LANG_HINT_WORDS.items():
        for w in words:
            if w.lower() in low:
                return lang
    return ""


def engine_langs(engine: str, spec: dict[str, Any] | None = None) -> set[str]:
    """引擎语言能力集合。优先级：spec.langs > ENGINE_LANGS > 默认 ["*"]。"""
    if spec and isinstance(spec.get("langs"), list) and spec["langs"]:
        return {str(x) for x in spec["langs"]}
    got = ENGINE_LANGS.get(engine)
    if got:
        return set(got)
    return set(ENGINE_LANGS_DEFAULT)


def lang_allows(engine: str, lang: str, spec: dict[str, Any] | None = None) -> bool:
    """该引擎对 lang 是否有召回价值（"*" = 语言中立）。"""
    langs = engine_langs(engine, spec)
    if "*" in langs or lang in langs:
        return True
    # 查询只判到书写系统时，认该语系下的具体语言源（见 _SCRIPT_FAMILY_LANGS）
    return any(code in langs for code in _SCRIPT_FAMILY_LANGS.get(lang, ()))


def engines_not_for_lang(engines: list[str], lang: str,
                         specs: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """对 lang 无召回价值的引擎名单（供 ja/ko 组合过滤——剔除中文绑定源）。

    规则：langs 不含 lang 且含 "zh"（中文绑定）才剔除；英文/语言中立源
    保留——忠实原 _JA_KO_CN 语义（ja/ko 用户读英文社区仍有效）。
    """
    out: list[str] = []
    for e in engines:
        spec = (specs or {}).get(e)
        langs = engine_langs(e, spec)
        if "*" in langs or lang in langs:
            continue
        if "zh" in langs:
            out.append(e)
    return out


def engines_demote_for_lang(engines: list[str], lang: str,
                            specs: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """语言重排降级名单：对该查询语言无召回价值的源，整体移尾。

    zh 查询：仅降英文社区类（family=social 对中文零召回——垂直源如
    finviz 对「AAPL 美股盘前」这类中文查询仍完全有效，不降）。
    其余语言（含 en/ja/ko 与 ru/ar/es/th/vi 等）：降**语言专用源**——
    声明了具体语言、不含查询语言、**且不含 en** 的那些。

    2026-09-21 泛化说明：原实现对 en/ja/ko 只降「含 zh 的源」，其余语言
    完全不降——这在只有中英日源时等价，但接入 14 个本地语言新闻源与
    cinii（ja）这类国别垂直库后就漏了：中文查询会原样保留 cinii（日文论文），
    英文查询会保留 kor_law（韩文判例）。

    **「不含 en」这条判据不能省**：英文源是国际通用的（韩语/中文用户读英文
    社区仍有价值），中文源、日文源则是单语读者专用的。所以
    `["zhihu(zh)", "hackernews(en)"]` 在 ko 查询下要降 zhihu 而保留
    hackernews——若改成「凡不含查询语言就降」，两者同时移尾、相对顺序不变，
    等于没降（tests/test_review_round3 的 ko/ja 两个用例正是锁这个语义）。

    注意：这条只管**顺序**（移尾，让位给匹配源），不删源——删除是
    route._filter_lang_bound_family 对语言绑定族（world_news）的处置，
    理由见该函数 docstring。
    """
    out: list[str] = []
    for e in engines:
        spec = (specs or {}).get(e)
        langs = engine_langs(e, spec)
        if "*" in langs or lang in langs:
            continue
        if lang == "zh":
            if family_of(e, spec) == "social":
                out.append(e)
        elif "en" not in langs:
            out.append(e)
    return out


def family_candidates(family: str, lang: str = "*",
                      *, enabled: set[str] | None = None,
                      specs: dict[str, dict[str, Any]] | None = None,
                      mode: str = "auto",
                      limit: int | None = None) -> list[str]:
    """能力族 × 语言 × 模式 → 可用源排序（分发层单一取源口）。

    research 语言/学术 boost、recovery 保底、可达性门、DSH 子代理面都应
    从这里派生——新源注册一次（registry + family/langs）全路径可见，
    不再有「源存在但任何分发路径都到不了」的死源。priority 升序=优先。
    """
    if specs is None:
        try:
            from config import load_config, get_engines
            specs = get_engines(load_config(), routable_only=False)
        except Exception:
            specs = {}
    out: list[str] = []
    for name, spec in (specs or {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get("enabled") is False:
            continue
        if enabled is not None and name not in enabled:
            continue
        if family_of(name, spec) != family:
            continue
        if lang and lang != "*" and not lang_allows(name, lang, spec):
            continue
        if mode in ("fast", "budget") and \
                (spec.get("cost_tier") or "free") == "paid":
            continue
        out.append(name)

    def _prio(n: str) -> tuple:
        s = (specs or {}).get(n) or {}
        p = s.get("priority")
        return (p if isinstance(p, (int, float)) else 999, n)

    out.sort(key=_prio)
    if limit:
        out = out[:limit]
    return out


def family_label(family: str) -> str:
    return FAMILY_LABELS.get(family, family)


def group_by_family(engines: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """把引擎字典按能力族分组。"""
    groups: dict[str, list[str]] = {}
    for name, spec in engines.items():
        f = family_of(name, spec)
        groups.setdefault(f, []).append(name)
    return groups


def dedupe_by_family(engine_list: list[str], max_per_family: int = 2,
                     spec_lookup: dict[str, dict[str, Any]] | None = None,
                     limit_families: frozenset[str] | None = None) -> list[str]:
    """按能力族去重：同族至多保留 max_per_family 个，避免同质源堆叠。

    用于 combo 组合后处理：web_general 族有 4 个源时，只留最靠前的 2 个，
    给其他族腾出预算位。保序去重。

    limit_families：仅对这些族应用上限（route 层只收缩 web_general，
    垂直族保留多源交叉验证）；None 时对所有族去重。
    """
    spec_lookup = spec_lookup or {}
    counts: dict[str, int] = {}
    out: list[str] = []
    for e in engine_list:
        f = family_of(e, spec_lookup.get(e))
        if limit_families is not None and f not in limit_families:
            out.append(e)
            continue
        if counts.get(f, 0) >= max_per_family:
            continue
        counts[f] = counts.get(f, 0) + 1
        out.append(e)
    return out


# 互补回填排除的族：产出非「查询相关」内容（热榜列表/策展/归档），
# 回填通用 combo 只会引入噪声，不放行。
_REFILL_EXCLUDED_FAMILIES = frozenset({
    "misc_vertical",
    "hot_trending",
    "world_news",
    "media_book",
    "archive",
    "personal_data",
})


def complement_refill(
    combo: list[str],
    *,
    enabled: set[str] | None = None,
    spec_lookup: dict[str, dict[str, Any]] | None = None,
    domain_primary: str | None = None,
    max_slots: int = 2,
) -> list[str]:
    """能力互补回填：为「全 web_general」的 combo 追加互补能力族的启用引擎。

    语义：family 去重的目的是消灭同质源堆叠，若去重后 combo 里已存在垂直族
    成员，能力多样性已具备，不再追加（尊重域作者配置）。仅当 combo 全部是
    web_general 时才回填，兑现「给其他族腾出预算位」。

    候选规则（保守，宁缺毋滥）：
      - 只取 enabled 且不在 combo 中的引擎
      - 族未在 combo 中出现（能力互补，不重复检索方式）
      - 排除 _REFILL_EXCLUDED_FAMILIES（热榜/策展/归档等噪声族）
      - 与域主引擎 coverage 标签至少重叠 1 个（主题相关性的数据信号）；
        主引擎无 coverage 标签时不回填（无主题信号，不猜测）
      - 按 config priority 升序（数字小=优先，与 family_candidates 同计算方式），
        最多 max_slots 个，保序追加

    返回新列表，绝不重排或删减入参 combo。
    """
    if not combo:
        return combo
    if enabled is None or not spec_lookup:
        return combo
    # 已有垂直族成员 → 能力多样性已具备
    if any(family_of(e, spec_lookup.get(e)) != "web_general" for e in combo):
        return combo
    primary_cov = set((spec_lookup.get(domain_primary or "") or {}).get("coverage") or [])
    if not primary_cov:
        return combo

    represented = {family_of(e, spec_lookup.get(e)) for e in combo}
    candidates: list[tuple[tuple, str]] = []
    for name in enabled:
        if name in combo:
            continue
        spec = spec_lookup.get(name) or {}
        fam = family_of(name, spec)
        if fam in represented or fam in _REFILL_EXCLUDED_FAMILIES:
            continue
        if set(spec.get("coverage") or []) & primary_cov:
            p = spec.get("priority")
            # 缺 priority 视为最差（999），与 family_candidates._prio 一致。
            # 此前是 `or 0` + 降序，且 docstring 写「降序」——三条计算方式互相矛盾，
            # 实际效果是专挑优先级数字最大（最差）的源回填。
            prio = p if isinstance(p, (int, float)) else 999
            candidates.append(((prio, name), name))
    candidates.sort(key=lambda x: x[0])
    return combo + [name for _, name in candidates[:max_slots]]


def describe_families() -> str:
    """调试/文档用：列出所有族及其成员。"""
    lines = []
    for f, label in FAMILY_LABELS.items():
        members = [e for e, ef in _ENGINE_FAMILY_OVERRIDES.items() if ef == f]
        if members:
            lines.append(f"  {f} ({label}): {', '.join(sorted(members))}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    print("能力族全景：")
    print(describe_families())

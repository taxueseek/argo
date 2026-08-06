#!/usr/bin/env python3
"""
engine_families.py — 搜索源能力族分类（第一性原理重构）

问题重定义：
  旧：每个搜索源是独立个体，type/cost/coverage 散落，路由靠 domain 手写 combo，
      同类源（如 byted/bocha/duckduckgo 都是全网搜索）无法互换、无法统一测试。
  新：搜索源按「检索能力」归族（MECE），同族源共享统一调用契约——
      任意组合、标准化输入输出、golden set 按 family 断言、A/B 可替换。

能力族（MECE，互斥且穷尽）：
  web_general      全网网页检索（多语言）    byted / bocha / duckduckgo / tavily / anysearch / octen / exa
  web_chinese      中文全网检索              bocha / byted / wechat_sogou / local_bing 等
  academic         学术文献                  arxiv / openalex / crossref / semantic_scholar / dblp / europepmc
  code             代码/包/文档              github / pypi / npm / mdn / stackoverflow / crates / gitlab / devto
  finance_market   行情/资金                 sina_quote / tencent_quote / em_flow / finviz
  finance_macro    宏观数据                  fred / worldbank / nbs_stats / eurostat / fx_rate
  news_flash       快讯/电报                 cls_telegraph / em_global_news / jin10 / em_miaoxiang
  social           社区 UGC                 zhihu / zhihu_global / v2ex / juejin / reddit / twitter / xiaohongshu / bilibili / weibo
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
  - golden set 断言按 family 而非引擎名（源可替换不破坏测试）
  - engine_status / list-engines 展示按族分组
  - research profile 的 vertical_engines 可按 family 扩展
"""

from __future__ import annotations

from typing import Any

# ── 引擎 → 能力族 映射（config.yaml 引擎声明的 family 字段优先，本表兜底） ──

# 默认族：未声明 family 的引擎归 web_general（全网搜索是通用兜底）
DEFAULT_FAMILY = "web_general"

# 引擎名 → family（显式覆盖表；config.yaml 声明 family 时以其为准）
_ENGINE_FAMILY_OVERRIDES: dict[str, str] = {
    # 全网搜索
    "byted": "web_general",
    "bocha": "web_general",
    "duckduckgo": "web_general",
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
    "local_duckduckgo": "web_general",
    "local_mojeek": "web_general",
    "local_startpage": "web_general",
    "local_google": "web_general",
    "local_yandex": "web_general",
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
    # 垂直结构化模态卡（统一语义识别，跨垂直域）
    "bocha_ai": "structured_card",
    # 本地聚合
    "local_search": "web_general",
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
    "misc_vertical": "其他垂直",
}


def family_of(engine: str, spec: dict[str, Any] | None = None) -> str:
    """返回引擎的能力族。

    优先级：config.yaml 引擎声明的 `family` 字段 > 本表显式覆盖 > 默认 web_general。
    spec 传入时优先读 spec["family"]（声明式，config 是真源）。
    """
    if spec and isinstance(spec, dict):
        f = spec.get("family")
        if isinstance(f, str) and f:
            return f
    return _ENGINE_FAMILY_OVERRIDES.get(engine, DEFAULT_FAMILY)


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
    "media_book",
    "archive",
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
      - 按 config priority 降序，最多 max_slots 个，保序追加

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
    candidates: list[tuple[int, str]] = []
    for name in enabled:
        if name in combo:
            continue
        spec = spec_lookup.get(name) or {}
        fam = family_of(name, spec)
        if fam in represented or fam in _REFILL_EXCLUDED_FAMILIES:
            continue
        if set(spec.get("coverage") or []) & primary_cov:
            candidates.append((spec.get("priority") or 0, name))
    candidates.sort(key=lambda x: -x[0])
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

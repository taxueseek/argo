#!/usr/bin/env python3
"""caliber_isolation.py — 口径隔离：不同口径的引擎结果不直接 merge。

同口径引擎的结果才能 merge 到同一条目，不同口径的结果按优先级并列输出。
口径分类表 + 分组逻辑是纯函数，与 search_rank.py 的调度/融合职责不同。
"""

from __future__ import annotations

from typing import Any

_CALIBER_GROUPS = {
    "financial_data": frozenset({
        "fred", "worldbank", "eurostat", "frankfurter", "fx_rate",
        "eastmoney", "sina_quote", "tencent_quote", "yahoo_finance",
        "sec_companyfacts", "finviz", "un_comtrade",
        "em_flow", "em_miaoxiang", "tencent_kline", "jin10",
    }),
    "encyclopedia": frozenset({
        "wikipedia", "baidu_baike", "wikidata",
        "wikisource", "wikimedia_commons", "dnb",
    }),
    "social": frozenset({
        "twitter", "weibo", "zhihu", "zhihu_global", "zhihu_user",
        "xiaohongshu", "wechat_sogou", "bilibili_hot", "douyin_hot",
        "weibo_hot", "toutiao_hot", "hackernews", "reddit",
    }),
    "code": frozenset({
        "github", "stackoverflow", "so", "local_gitlab", "deps_dev",
        "endoflife", "pypi", "npm", "crates",
    }),
    "news": frozenset({
        "google_news", "bing_rss", "cls_telegraph", "em_global_news",
        "people_daily",
    }),
    "academic": frozenset({
        "arxiv", "local_pubmed", "google_scholar", "semantic_scholar",
        "crossref", "openalex", "doaj", "core", "gdacs",
        "clinicaltrials", "openfda", "uniprot", "rcsb_pdb",
    }),
    "media": frozenset({
        "imdb", "tvmaze", "bangumi", "deezer",
        "itunes", "listenbrainz", "figshare", "met_museum",
    }),
    "reference": frozenset({
        "open_library", "weread", "douban_book", "gutenberg", "rfc_editor", "courtlistener", "hal", "eu_opendata",
    }),
}

# 口径优先级与 IASF 自适应温度 k_c（高精结构化口径采用陡峭梯度，泛网与社交抗噪）
_CALIBER_PRIORITY = {
    "financial_data": 0, "academic": 1, "encyclopedia": 2, "news": 3,
    "social": 4, "code": 5, "media": 6, "reference": 7, "general": 8,
}
_CALIBER_K = {
    "financial_data": 15, "academic": 20, "encyclopedia": 20, "code": 25,
    "reference": 25, "news": 45, "media": 50, "social": 60, "general": 60,
}
_CALIBER_REDUNDANCY_DECAY = {"general": 0.75, "social": 0.80, "news": 0.85}


def caliber_of(engine: str) -> str:
    """返回引擎的口径分类，未分类的引擎归为 general。"""
    for caliber, engines in _CALIBER_GROUPS.items():
        if engine in engines:
            return caliber
    return "general"


def group_by_caliber(ranked_lists: list[list[dict[str, Any]]]) -> dict[str, list[list[dict[str, Any]]]]:
    """按口径分组：同口径的引擎结果才能 merge。

    分组单位是**整条引擎列表**，不是单条结果。此前实现把每条结果包成
    单元素列表，`merge_within_caliber` 里的 `enumerate(group)` 于是恒为
    `i=0`，同一引擎的所有结果拿到相同 RRF 分——名次衰减被抹平，输出退化成
    「按引擎权重整段拼接」（2026-10-17 实测：anysearch 5 条同分 0.017213）。
    一个列表通常来自一个引擎；若上游混入多引擎，按口径拆开但保持原序。
    """
    caliber_groups: dict[str, list[list[dict[str, Any]]]] = {}
    for results in ranked_lists:
        if not results:
            continue
        by_caliber: dict[str, list[dict[str, Any]]] = {}
        for r in results:
            if not isinstance(r, dict):
                continue
            eng = r.get("_engine") or r.get("source", "") or ""
            by_caliber.setdefault(caliber_of(eng), []).append(r)
        for cal, group in by_caliber.items():
            if group:
                caliber_groups.setdefault(cal, []).append(group)
    return caliber_groups


def merge_within_caliber(
    groups: list[list[dict[str, Any]]],
    k: int = 60,
    weighted: bool = True,
    lang: str | None = None,
    _canonical_url=None,
    _engine_weight=None,
    caliber: str = "general",
) -> list[dict[str, Any]]:
    """同口径内做 IASF 信息精算与子模 RRF 融合。"""
    if _canonical_url is None:
        from url_canon import canonical_url as _canonical_url
    if _engine_weight is None:
        from search_rank import _engine_weight as _engine_weight
    k_eff = _CALIBER_K.get(caliber, k) if (weighted and k == 60) else k
    decay = _CALIBER_REDUNDANCY_DECAY.get(caliber, 0.90) if weighted else 1.0
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}

    for group in groups:
        for i, r in enumerate(group):
            key = (
                _canonical_url(r.get("url", ""))
                or (f"__title__:{r.get('title', '')}" if r.get("title") else "")
                or (f"__card__:{r.get('card_type', '')}" if r.get("card_type") else "")
                or f"__idx__:{id(r)}"
            )
            eng = r.get("_engine") or r.get("source", "") or ""
            w = _engine_weight(eng, lang=lang) if weighted else 1.0
            m = len(items[key].get("consensus_engines") or []) if key in items else 0
            disc = (decay ** m) if (m > 0 and eng not in (items[key].get("consensus_engines") or [])) else 1.0
            scores[key] = scores.get(key, 0.0) + disc * (w / (k_eff + i + 1))
            if key not in items:
                item = dict(r)
                item["_rrf_score"] = 0.0
                cons: list[str] = []
                if eng:
                    cons.append(eng)
                item["consensus_engines"] = cons
                items[key] = item
            else:
                cur = items[key]
                new_txt = f"{r.get('title', '')} {r.get('snippet', '')}"
                cur_txt = f"{cur.get('title', '')} {cur.get('snippet', '')}"
                if len(new_txt) > len(cur_txt):
                    cur["title"] = r.get("title", cur.get("title"))
                    cur["snippet"] = r.get("snippet", cur.get("snippet"))
                sources = {cur.get("source", ""), r.get("source", "")}
                cur["source"] = "/".join(s for s in sources if s)
                cons = list(cur.get("consensus_engines") or [])
                if eng and eng not in cons:
                    cons.append(eng)
                cur["consensus_engines"] = cons

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    out = []
    for key, _ in ranked:
        item = items[key]
        item["_rrf_score"] = round(scores[key], 6)
        out.append(item)
    return out


def merge_by_caliber(
    ranked_lists: list[list[dict[str, Any]]],
    k: int = 60,
    weighted: bool = True,
    lang: str | None = None,
    _canonical_url=None,
    _engine_weight=None,
) -> list[dict[str, Any]]:
    """口径隔离的 RRF 融合：同口径内 merge，不同口径按优先级并列输出。

    这是 rrf_merge 的口径隔离版本，调用方（search_rank.rrf_merge）传入
    私有函数引用，本模块不反向依赖 search_rank。

    当 weighted=False 时，不分组，走全局 RRF（经典 RRF 行为）。
    """
    if not weighted:
        # 经典 RRF：不分组，但**保留每个引擎列表的独立名次**——把所有结果
        # 拍平成一个列表会让后一个引擎的 rank 接着前一个数，名次衰减错位。
        return merge_within_caliber(
            ranked_lists,
            k=k, weighted=False, lang=lang,
            _canonical_url=_canonical_url, _engine_weight=_engine_weight,
        )

    caliber_groups = group_by_caliber(ranked_lists)
    merged_by_caliber: dict[str, list[dict[str, Any]]] = {}
    for cal, groups in caliber_groups.items():
        merged_by_caliber[cal] = merge_within_caliber(
            groups, k=k, weighted=weighted, lang=lang,
            _canonical_url=_canonical_url, _engine_weight=_engine_weight, caliber=cal,
        )

    # 不同口径按优先级排序后并列输出；拼接后再按 URL 合并一次，
    # 避免同一文档因跨口径命中被交付两遍（共识证据并入首条）。
    out = []
    for cal in sorted(merged_by_caliber.keys(),
                     key=lambda c: _CALIBER_PRIORITY.get(c, 8)):
        out.extend(merged_by_caliber[cal])
    from caliber_dedupe import merge_cross_caliber_duplicates
    return merge_cross_caliber_duplicates(out, _canonical_url)

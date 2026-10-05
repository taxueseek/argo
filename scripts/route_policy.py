#!/usr/bin/env python3
"""route_policy.py — 预算与保留策略（combo 定稿的最后一道）。

职责只有两件：把 combo 截到本次 mode/depth/context 允许的规模；决定谁在截断
中必须留下（must_keep、垂直域主源、新专源加槽）。本模块不做语言判断、不做族
去重——那些是 route_combo / route_lang 的事。

为什么单独成模块：加槽机制（批次九）与预算截断曾经与「谁该在场」混在同一个
千行函数里，导致「路由命中但零结果」这类缺陷只能在结果侧观察到。
"""

from __future__ import annotations

from typing import Any


_VERTICAL_NEW_SOURCE: dict[str, tuple[str, ...]] = {
    "species_search": ("worms",),               # 海洋分类学权威命名，无重叠
    "medical": ("who_don",),                    # 疫情通报，与 clinicaltrials/openfda 不同能力
    "book_search": ("k10plus",),                # 德语区最大联合目录，补区域空白
    "film_search": ("tvmaze",),                 # 电视剧元数据（imdb/douban 偏电影）
    "sports_search": ("openf1", "openligadb"),  # F1/德甲结构化赛程比分
    "org_entity": ("ror",),                     # 研究机构标识（含域名映射）
    "media_search": ("deezer", "listenbrainz"),  # 国际曲库 + 开源收听记录，能力互不重叠
    # 2026-09-16：五个免密钥国内源接线。共同点是「补结构性空白」而非同质重复，
    # 故声明在既有源之后、由本表加槽，不用新源挤掉既有源的位次。
    "hot_trending": ("weibo_hot", "douyin_hot"),  # 微博/抖音两条主榜单，该域原先一条都没有
    "cn_tech_community": ("csdn",),               # 中文技术社区最大一站，补掘金/少数派之外
    "financial_news": ("wallstreetcn",),          # 快讯流上游与财联社/金十不同源
    "weather_query": ("weather_cn",),             # 国内城市实况，补国际源的城市覆盖缺口
    # 2026-09-17：国际新闻与事实核查两个新域。前者的三个种子按 feed_mode=hot
    # 给「最新流」语义（与话题检索分流，见 engines_builders_feeds 说明）；
    # 后者补声明级核验——argo 此前没有任何「这个说法被判真伪」的一手源。
    "intl_news_flash": ("guardian_rss", "france24", "dw_news"),  # 一手外媒实时流，补英文主流媒体直采空白
    "claim_check": ("factcheck_org", "full_fact"),               # 美/英两法域核查口径
    # 2026-09-19：academic 域两个源。core 是**新增**（开放获取全文 + PDF 直链，
    # 与 openalex/crossref 的元数据、arxiv/biorxiv 的预印本分层不同）；
    # local_pubmed 是**修复**（旧实现静默 400 且解析不出字符串数组，实测 0 条，
    # 详见 engines_builders_batch9._build_pubmed_engine 文档串）。两者声明在
    # 既有源之后，由本表加槽，不挤掉 arxiv/openreview 等既有位次。
    "academic": ("local_pubmed", "core", "cinii"),     # 生物医学全文 + 机构仓储开放获取 + 日本学术 + 数学索引
    # 2026-09-21：academic 补两个国别/学科专门源。位次 5/6 是照本域上方注释
    # 的既有实践选的（deepest=6 → 加槽后预算覆盖到 6，前四位次一律不动）。
    # cinii 声明 langs=ja，靠 engines_demote_for_lang 的语言专用源降级，
    # 只在日语查询里占位；zbmath 语言中立，数学查询与 deep 档可达。
    "soil_agri": ("openfoodfacts",),   # 食品成分库：补 usda（单一国别）之外的多国食品数据
}


_VERTICAL_KEEP: frozenset[str] = frozenset({
    "film_search", "sports_search", "geo_places", "org_entity", "media_search",
    "modal_card",
    "astro_space", "energy_grid", "transport_rt", "vehicle_data",
    "japan_law", "soil_agri", "species_search", "art_museum",
    "anime_encyclopedia", "book_search", "medical", "earth_science",
})


def _new_source_budget_extra(
    domain: dict[str, Any] | None,
    engines_combo: list[str],
    enabled: set[str],
    *,
    mode: str,
    depth: str,
    context: str,
    live_combo: list[str] | None = None,
) -> int:
    """垂直域「新专源」的加槽额度（0 = 不加槽，行为与改造前逐位一致）。

    按新专源在 combo 中的**最深位次**定额度，保证它落在本模式预算之内：
    位次 d 的源需要 budget >= d，故 extra = max(d - base, 0)。

    调用方应传域**声明**的 combo（不是已被前置裁剪动过的当前列表），
    否则新源被摘掉后额度恒为 0——详见
    `_apply_policy_with_new_source_slots` 的说明。

    `live_combo` 是当前（已被上游重排过的）combo，用于第二处位次：新源**还在
    combo 里、但被重排推到预算窗口之外**。只按声明位次定额度会漏掉这一态——
    声明位次恰好等于预算时（financial_news 的 wallstreetcn：位次 3 = 预算 3）
    extra 恒为 0，而 `_apply_policy_with_new_source_slots` 的 must_keep 补位
    只在「源已不在 combo 里」时触发，两处保护同时失效，源被静默截断。
    2026-09-16 实测：「财经」丢掉 wallstreetcn、「财经新闻」保住，同一个域
    两种结果。位次取当前 combo 的较大者后额度随之抬高，新源留在预算内，且
    额度是**扩容**不是腾位——既有源一个不少。

    上限按模式分档 —— fast 强制串行（见 `_apply_intent_parallelism`），加槽直接
    乘在延迟上，故上限 2；auto/balanced 走并行，放宽到 4。deep/research 本就
    不截断（base 为 None），extra 无意义。

    domain 可能为 None（TF-IDF 无命中时的 catch-all 分支），此时无域可谈，返回 0。
    """
    if not domain or not engines_combo:
        return 0

    # 学术域固定加成：该域有 11+ 个引擎（arxiv/openreview/biorxiv/openalex/
    # crossref/europepmc/dblp/semantic_scholar/local_pubmed/core/cinii），
    # auto 模式 budget=3 只够前 3 个，后 8 个永远轮不到。学术搜索需要多源
    # 交叉验证才严谨，固定 +3 让 budget 达到 6，覆盖到 semantic_scholar。
    if domain.get("name") == "academic":
        try:
            from engine_policy import combo_budget
            base = combo_budget(mode=mode, depth=depth, context=context)
            if base is not None:
                return min(3, max(0, 6 - base))
        except Exception:
            pass

    pending = [
        e for e in _VERTICAL_NEW_SOURCE.get(domain.get("name"), ())
        if e in engines_combo and e in enabled
    ]
    if not pending:
        return 0
    deepest = max(engines_combo.index(e) + 1 for e in pending)
    if live_combo:
        # 新源在 live_combo 里的位次（可能比声明位次更靠后，见 docstring）
        deepest = max(
            [deepest] + [live_combo.index(e) + 1 for e in pending if e in live_combo]
        )
    try:
        from engine_policy import combo_budget
        base = combo_budget(mode=mode, depth=depth, context=context)
    except Exception:
        return 0
    if base is None:  # 不截断的模式（deep/research）
        return 0
    cap = 2 if ((mode or "auto") in ("fast", "budget")
                or (depth or "fast") == "fast") else 4
    return min(max(deepest - base, 0), cap)


def _apply_engine_policy(
    engines_combo: list[str],
    *,
    mode: str = "auto",
    depth: str = "fast",
    context: str = "search",
    engines_boost: list[str] | None = None,
    enabled: set[str] | None = None,
    must_keep: list[str] | None = None,
    budget_extra: int = 0,
) -> list[str]:
    """boost 垂直源 + tier/budget 截断（单一策略入口）。

    must_keep：预算截断后仍强制保留的引擎（如 geo 的 local_openstreetmap），
    必要时从尾部腾位，避免特化源被 budget 裁掉。

    budget_extra：垂直域新专源的**加槽**额度。与 must_keep 的区别是「不腾位、
    只扩容」——must_keep 会挤掉既有可用源（实测会把 douban_movie/musicbrainz
    挤出），而新专源与既有源多为互补能力，应当共存而非替换。
    """
    try:
        from engine_policy import boost_into_combo, combo_budget, filter_combo_by_policy
    except ImportError:
        return engines_combo
    out = list(engines_combo or [])
    if engines_boost:
        out = boost_into_combo(out, engines_boost, enabled=enabled)
    out = filter_combo_by_policy(out, mode=mode, depth=depth, context=context,
                                 budget_extra=budget_extra)
    if must_keep:
        budget = combo_budget(mode=mode, depth=depth, context=context,
                              extra=budget_extra)
        keep_set = set(must_keep)
        for e in must_keep:
            if not e or e in out:
                continue
            # must_keep 强制保留，不因 enabled 缺失丢弃
            # （modal_card 缺 key 时仍保留 bocha_ai/bocha，由执行层返回 error item）
            if budget is not None and len(out) >= budget:
                # 替换末位「非保底」成员，腾出槽位；保底成员之间不互踩
                # （modal_card 整 combo 保底：bocha/train 依次补位时不得顶掉彼此）
                replace_idx = next(
                    (i for i in range(len(out) - 1, -1, -1)
                     if out[i] not in keep_set),
                    None,
                )
                if replace_idx is not None:
                    out = out[:replace_idx] + out[replace_idx + 1:] + [e]
                else:
                    # 尾部全是保底成员：直接追加（保底优先于预算）
                    out.append(e)
            else:
                out.append(e)
        # 去重保序
        seen: set[str] = set()
        deduped: list[str] = []
        for e in out:
            if e not in seen:
                seen.add(e)
                deduped.append(e)
        out = deduped
    return out


def _apply_policy_with_new_source_slots(
    domain: dict[str, Any] | None,
    engines_combo: list[str],
    *,
    mode: str,
    depth: str,
    context: str,
    enabled: set[str] | None = None,
    engines_boost: list[str] | None = None,
    must_keep: list[str] | None = None,
) -> list[str]:
    """按域预算截断 combo，并为该域的「新专源」加槽（不腾位）。

    正则域命中分支与 catch-all 分支此前各抄一份
    `_new_source_budget_extra(...)` + `_apply_engine_policy(...)`（参数与注释
    逐字相同）；收紧成单一入口后，加槽计算方式只有一处可改。

    **为什么额度按「声明位次」而非当前 combo 算**：进到这里的 combo 已经被
    两道前置裁剪动过手——`_get_engines_combo` 的 web_general 能力族去重
    （max_per_family=2）与 `_apply_intent_parallelism` 的意图裁剪。新专源
    声明在 combo 后排，正是这两道裁剪的常客，源一旦被摘掉，按当前 combo
    算的额度就恒为 0，加槽机制被静默废掉。实测两例（2026-09-13）：
      - sports_search：openf1/openligadb 未声明 family → 落 web_general，
        被族去重摘掉，加槽恒不生效；
      - org_entity：ror 被意图裁剪摘掉，加槽恒不生效。
    故额度按域**声明**的位次算；被摘掉的新专源在同一额度内补回（额度不够
    时不强塞，交回既有 must_keep 换位逻辑，避免反过来挤掉既有源）。

    额度取「声明位次」与「当前 combo 位次」的**较大者**（`live_combo` 参数）。
    只取声明位次还有第二种失效态：源**没被摘掉，只是被排到窗口之外**。此时
    `e not in engines_combo` 为假，上面那段补位不触发；而声明位次恰好等于预算
    时 extra 也是 0——两处保护同时失效，源被预算静默截断。实测（2026-09-16）
    financial_news 的 wallstreetcn：声明位次 3 = auto/balanced 预算 3，查询
    「财经」时被自适应重排挤到 eastmoney 之后而落选，「财经新闻」时保住。
    取较大者后该源额度为 1，两个源并存（扩容而非腾位）。
    """
    declared = list((domain or {}).get("engines_combo") or [])
    live = [e for e in _VERTICAL_NEW_SOURCE.get((domain or {}).get("name"), ())
            if e in declared and (enabled is None or e in enabled)]
    keep = list(must_keep or [])
    for e in live:
        if e not in engines_combo and e not in keep:
            keep.append(e)
    return _apply_engine_policy(
        engines_combo, mode=mode, depth=depth, context=context,
        engines_boost=engines_boost, enabled=enabled,
        must_keep=keep or None,
        budget_extra=_new_source_budget_extra(
            domain, declared or engines_combo, enabled or set(),
            mode=mode, depth=depth, context=context,
            # 传当前 combo：新源还在里面但被重排推出窗口时，额度要按它在
            # 当前 combo 的位次算（只在声明位次上算会得到 0，见该函数说明）
            live_combo=engines_combo),
    )

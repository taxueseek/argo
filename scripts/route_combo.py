#!/usr/bin/env python3
"""route_combo.py — 引擎组合的装配与裁剪。

一个域命中之后，combo 要经过这些步骤才定稿：展开 local_search 子引擎 →
语言相关追加与排序（在 route_lang）→ geo 补充 → 能力族去重 → 自适应学习器
重排 → 预算截断（在 route_policy）。本模块承载「装配」这一段。

与 route_policy 的分工：本模块决定**谁该在场、什么顺序**，route_policy 决定
**预算允许多少、谁必须留下**。两者分开是因为前者的失败形态是「选错源」，
后者是「源被裁掉」——症状不同，改动的判据也不同。
"""

from __future__ import annotations

from typing import Any

from config import get_engines
from quota import get_quota_manager
from route_lang import (_enabled_local_engines, _filter_lang_bound_family,
                        _get_registry, _lang_must_keep, _specs_snapshot,
                        lang_allows)

# 自适应学习器（可选依赖）：按历史成败微调同族引擎的次序。
# 从 route.py 搬来这里：它是 _get_engines_combo 的私有状态，放在 route.py 会让
# 「import route」白付一次 adaptive 装载（路由热路径，缓存命中也要走）。
#
# 惰性化（2026-09-27）：上一段注释的意图此前并没有兑现——route.py:125 是
# **模块级**导入本模块，`get_learner()` 照样在 import route 时构造。而构造
# 即 `_init_db()`：connect + 3 PRAGMA + CREATE TABLE + 3×PRAGMA table_info +
# CREATE INDEX + 全表 DELETE 剪枝 + commit，随 engine_perf 行数线性增长
# （实测 5k 行 1–3 ms、50k 行 10–30 ms），每次进程启动都付。改成首次
# **使用**时才构造：缓存命中路径（不进 _get_engines_combo）与只读路由路径
# 都不再付这笔钱。
#
# `_UNRESOLVED` 哨兵而非 None：测试用 `patch("route_combo._adaptive_learner",
# None)` 隔离学习器干扰，None 必须是「已解析且没有」的合法值，不能与
# 「还没解析」共用同一个值。
# 垂直保护的分数地板（2026-10-02）：自适应分低于此值的引擎
# 是「已被证明失败」（无数据引擎为中性 0.5），不再受垂直保护、
# primary 豁免与 must_keep 庇护。取值依据：learner 分数 = 成功率×
# 延迟因子×质量，0.15 对应长期近乎全败（实测 thesportsdb 0.084、
# 近期 12/13 次失败）。route.py 的 must_keep 组装也导入此常量。
_VERTICAL_FLOOR = 0.15

_UNRESOLVED: Any = object()
_adaptive_learner: Any = _UNRESOLVED


def _get_learner() -> Any:
    """首次使用时才构造自适应学习器（失败或无依赖返回 None）。"""
    global _adaptive_learner
    if _adaptive_learner is _UNRESOLVED:
        try:
            from adaptive import get_learner
            _adaptive_learner = get_learner()
        except Exception:
            _adaptive_learner = None
    return _adaptive_learner


def _expand_local_search(engine_list: list[str], features: dict | None = None) -> list[str]:
    """将 local_search 扩展为具体的子引擎（基于查询特征）。"""
    if "local_search" not in engine_list:
        return engine_list
    if _get_registry is None:
        return engine_list

    sub_engines = _enabled_local_engines()  # 内部自取注册表，此处不再重复加载
    if not sub_engines:
        return engine_list

    selected = _select_sub_engines(sub_engines, features)
    result = [e for e in engine_list if e != "local_search"]
    # 截断只限本地子引擎数量；远端成员总量由 route_query 末尾
    # engine_policy.filter_combo_by_policy 统一管。曾用 result[:4] 整体截断，
    # combo 声明顺序即生死（第 5 位起被无差别砍掉，octen/子引擎全灭）。
    for eng in selected[:4]:
        if eng not in result:
            result.append(eng)
    return result


def _general_fallback(enabled: set[str]) -> list[str]:
    """本地优先 + 通用免费源保底（清单唯一来源 engine_policy.GENERAL_FREE_FALLBACK）。

    域内引擎全被过滤 / 无匹配域时的回退组合：先 local_search（展开成本地子引擎），
    再通用免费源。清单与 recovery L3 共用同一常量，避免两处清单漂移。
    """
    try:
        from engine_policy import GENERAL_FREE_FALLBACK
    except ImportError:
        GENERAL_FREE_FALLBACK = ("anysearch", "local_bing")
    return ["local_search"] + [e for e in GENERAL_FREE_FALLBACK if e in enabled]


def _breaker_blocked(engine_id: str) -> bool:
    """单引擎熔断判定：disabled / open 且冷却未过 → True（剔除）。

    half_open 保留探测资格（与 _get_engines_combo 内熔断感知过滤
    同一语义）。状态读取失败 ≠ 熔断：fail-open 返回 False，留痕与
    _filter_breaker_blocked 既有口径一致（017b01a 姊妹形状）。
    """
    try:
        from circuit_breaker import get_breaker
        st = get_breaker().status(engine_id)
    except Exception as _exc:
        import logging
        logging.getLogger("unified_search").debug(
            f"breaker.status 读取失败，按健康处理: {_exc!r}")
        return False
    state = st.get("state")
    if state == "disabled":
        return True
    return state == "open" and int(st.get("cooldown_remain") or 0) > 0


def _filter_breaker_blocked(engine_list: list[str]) -> list[str]:
    """剔除确定熔断态引擎（disabled / open 且冷却未过），与 _get_engines_combo
    内的熔断感知过滤同一语义（half_open 保留探测资格）。

    D4：语言引擎追加、通用保底、TF-IDF 注入等路径在 _get_engines_combo 之外，
    追加的引擎可能处于熔断态仍进 combo，白占并行槽位。统一统一处理到最终组装后。
    """
    if not engine_list:
        return engine_list
    return [e for e in engine_list if not _breaker_blocked(e)]


def engine_is_dead(engine_id: str) -> bool:
    """死源判定：自适应分 <_VERTICAL_FLOOR（已被证明失败）。

    学习器未加载或读取失败 → False（fail-open：无数据引擎是
    中性 0.5，不应被误判为死源）。GEC 过滤、route.py 的
    must_keep 与 TF-IDF 注入三处共用同一判据（2026-10-02
    抽取，此前三处各写一遍）。
    """
    learner = _get_learner()
    if learner is None:
        return False
    try:
        return learner.get_score(engine_id) < _VERTICAL_FLOOR
    except Exception:
        return False


def build_refill_pool(engines_combo: list[str], mode: str,
                      depth: str, context: str) -> list[str]:
    """槽位回填池快照：策略（预算截断 + must_keep 腾位）之前的
    候选，已过 GEC 的准入/配额/语言过滤。

    research 语境外的 research_only 源不进池——与
    filter_combo_by_policy 的 tier 过滤同口径，避免回填把
    策略裁掉的垂直研究源悄悄塞回通用 combo。
    """
    pool = list(engines_combo)
    try:
        from engine_policy import _tier_lookup, is_research_context
        if not is_research_context(mode=mode, depth=depth,
                                   context=context):
            _tier_of = _tier_lookup()
            pool = [e for e in pool if _tier_of(e) != "research_only"]
    except ImportError:
        pass
    return pool


def backfill_after_breaker(combo: list[str], enabled: set[str],
                           features: dict | None = None,
                           query: str = "", skip_aux: bool = False,
                           refill_pool: list[str] | None = None
                           ) -> list[str]:
    """breaker_filter + 熔断槽位回填（combo 定稿的最后一道）。

    先走 breaker_filter（熔断摘除 + 语言补充 + 空回退），再把
    熔断摘除让掉的预算内槽位从 refill_pool 按序补回：候选须在
    enabled、非熔断、语言边界合规（与 _filter_lang_bound_family
    同口径——池是策略前快照，语言摘除发生在策略之后，回填不得
    撤销语言摘除）、且不在 combo。补到摘除前长度为止；池耗尽
    则保持短 combo（fail-open）。

    实测根因（2026-10-02）：中文地名查询触发 geo must_keep
    挤掉次引擎（腾位），geo 位引擎随后又因熔断 disabled 被
    摘除——两头损失叠加后 combo 只剩 anysearch 单引擎，丧失
    对冲与早停多样性（anysearch 延迟 1.6-6s 且波动大）。
    """
    target_len = len(combo)
    combo = breaker_filter(combo, enabled, features=features,
                           query=query, skip_aux=skip_aux)
    if refill_pool and len(combo) < target_len:
        for cand in refill_pool:
            if len(combo) >= target_len:
                break
            if cand in combo or cand not in enabled:
                continue
            if _breaker_blocked(cand):
                continue
            if _filter_lang_bound_family(
                    [cand], features, _specs_snapshot(), query) != [cand]:
                continue
            combo.append(cand)
    return combo


# TF-IDF「强证据」线：超过它语义推荐才可领队（catch-all 覆写与 TF-IDF 分支
# 同一条线）。0.12~0.15 是弱证据——推荐引擎只作辅源跟跑，通用保底领队；
# 否则营养库接「餐厅推荐」这类低分错配会占 primary 位（实测 usda 0.145）。
TFIDF_STRONG_SCORE = 0.15


def tfidf_lead_combo(tfidf_best: str | None, tfidf_best_score: float,
                     enabled: set[str]) -> list[str]:
    """TF-IDF 分支的初始 combo：强证据推荐领队 + anysearch 对冲。

    弱证据（≤TFIDF_STRONG_SCORE）时反过来：通用保底领队，推荐引擎殿后
    作辅源——仍在 combo 里并行竞速，能力不减，只是不再占 primary 位
    （含 primary 宽限/充分性判定对它的偏袒）。
    """
    lead = [tfidf_best] if tfidf_best and tfidf_best_score > TFIDF_STRONG_SCORE else []
    if "anysearch" in enabled:
        lead.append("anysearch")
    if tfidf_best and tfidf_best not in lead:
        lead.append(tfidf_best)
    return [e for e in lead if e in enabled]


def select_tfidf_best(query: str,
                      features: dict | None,
                      specs: dict | None,
                      semantic_route,
                      min_score: float = 0.12
                      ) -> tuple[str | None, float, list]:
    """TF-IDF 语义路由选源：返回 (最佳引擎, 分数, 候选表)。

    从 route.py 抽取（2026-10-02）：候选表按分数降序，
    首个通过四道门的候选当选——
      ① 社交引擎须查询带社交信号（否则跳过看下一个）；
      ② 分数 <min_score → 整条分支作废（后续分更低）；
      ③ 语言可达性门：引擎声明的语言能力不含查询语言
         且非语言中立（"*"）时不得顶到首位（实测中文
         法条查询「刑法 判例 司法解释」与 kor_law 共享
         汉字而命中，被注进 legal 域首位）；
      ④ 死源地板（engine_is_dead）：语义最相关也不得
         顶到首位——实测 thesportsdb 0.084 仍是「NBA
         总决赛」类查询的第一候选，注入后撤销 GEC 地板
         过滤并占 wave-1 竞速首位，白等它的单引擎超时。

    semantic_route 由调用方传入：route.py 经模块属性
    访问解析（测试打桩 patch("route.semantic_route")
    的契约不变）。
    """
    social_engines = {
        "twitter", "reddit", "xiaohongshu", "bilibili", "weibo",
        "zhihu", "hackernews", "v2ex",
    }
    tfidf_best: str | None = None
    tfidf_best_score = 0.0
    tfidf_scores: list = semantic_route(query, top_k=3)
    for cand, score, _ in tfidf_scores:
        social_ok = True
        if cand in social_engines:
            ql = query.lower()
            social_signals = (
                "微博", "小红书", "推特", "twitter", "reddit", "舆情",
                "讨论", "网友", "评论", "b站", "bilibili", "抖音",
            )
            social_ok = any(s in ql for s in social_signals)
        if score < min_score:
            # 分数降序：后续候选分更低，整条 TF-IDF 分支作废
            break
        if not social_ok:
            # 社交引擎候选但查询无社交信号：跳过看下一个候选
            continue
        # ja/ko 查询：候选若是中文内容/政策引擎（gov_policy/百科等），
        # 对日/韩用户无关（返回中文站），丢弃让通用 anysearch 主导。
        # 丢弃当前候选后继续看下一个（2026-08 修复：旧逻辑只看 top-1，
        # 丢弃后不检查 top-2/3，可能错失 anysearch 等合格候选）。
        _ql = (features or {}).get("primary_lang") or ""
        if _ql and not lang_allows(cand, _ql, (specs or {}).get(cand)):
            continue
        if engine_is_dead(cand):
            continue
        tfidf_best = cand
        tfidf_best_score = score
        break
    return tfidf_best, tfidf_best_score, tfidf_scores


def _maybe_add_geo_engine(engine_list: list[str], features: dict | None,
                          enabled: set[str]) -> list[str]:
    """P0-001：geo 查询追加 local_openstreetmap（地理编码/POI）。"""
    if not features or not features.get("has_geo"):
        return engine_list
    if "local_openstreetmap" not in enabled:
        return engine_list
    if "local_openstreetmap" in engine_list:
        return engine_list
    return engine_list + ["local_openstreetmap"]


_INTENT_PARALLELISM: dict[str, tuple[int, bool]] = {
    "definition": (1, False),
    "fact": (1, False),
    "news": (2, True),
    "compare": (3, True),
    "social": (3, True),
}


_NARROW_ENGINES = frozenset({
    "mdn", "models_dev", "huggingface", "devto",
    "wikipedia", "baidu_baike", "openalex", "europepmc",
})


def _apply_intent_parallelism(engine_list: list[str], features: dict | None,
                              domain: dict | None, mode: str,
                              default_parallel: bool) -> tuple[list[str], bool]:
    """P0-005：按意图动态裁剪引擎数与并行度。

    definition/fact → 1 引擎串行；news → 2 引擎并行；compare/social → 3 引擎并行。
    深度研究词（has_depth_word）视为 research，保持 3 引擎并行。
    fast 模式强制串行（但仍可裁剪引擎数）。

    Returns:
        (裁剪后的引擎列表, 是否并行)
    """
    if not engine_list:
        return engine_list, default_parallel

    intents = (features or {}).get("intents") or []
    is_research = bool((features or {}).get("has_depth_word"))

    target_n: int | None = None
    want_parallel = default_parallel

    # research/compare 优先（更需要多源）
    if is_research or "compare" in intents:
        target_n, want_parallel = 3, True
    elif "social" in intents:
        target_n, want_parallel = 3, True
    elif "news" in intents:
        target_n, want_parallel = 2, True
    elif "definition" in intents or "fact" in intents:
        target_n, want_parallel = 1, False
        # 窄域引擎单点保护：primary 是窄域引擎时保留 2 引擎并行
        if engine_list and engine_list[0] in _NARROW_ENGINES and len(engine_list) > 1:
            target_n, want_parallel = 2, True

    if target_n is None:
        return engine_list, default_parallel

    trimmed = engine_list[:target_n]
    if mode == "fast":
        want_parallel = False
    if len(trimmed) <= 1:
        want_parallel = False
    return trimmed, want_parallel


def _select_sub_engines(sub_engines: list[str], features: dict | None = None) -> list[str]:
    """根据查询特征选择子引擎。"""
    if not features:
        # 默认保底：快源优先（brave/yahoo 实测 ~1.1s），ddgs 默认后端慢不主动纳入
        return [e for e in ["local_bing", "local_brave", "local_yahoo"]
                if e in sub_engines]

    primary_lang = features.get("primary_lang", "")
    chinese_ratio = features.get("chinese_ratio", 0)

    # 多语种（v2.7）：日/韩查询优先对应语言的本地引擎
    if primary_lang == "ja":
        return [e for e in ["local_bing"] if e in sub_engines]
    if primary_lang == "ko":
        return [e for e in ["local_bing"] if e in sub_engines]
    if chinese_ratio > 0.1:
        # 百度/搜狗结果质量低（SERP 跳转链为主），仅作印证不主动纳入；
        # 中文补充源只用 local_bing
        return [e for e in ["local_bing"] if e in sub_engines]
    elif features.get("has_technical"):
        return [e for e in ["local_github", "local_stackoverflow", "local_bing"] if e in sub_engines]
    elif features.get("has_depth_word"):
        return [e for e in ["local_arxiv", "local_semantic_scholar", "local_bing"] if e in sub_engines]
    else:
        # 2026-09-16 实测校正：原链条 [local_bing, local_duckduckgo, local_mojeek]
        # 里后两个**都已损坏**——local_duckduckgo 连接失败；local_mojeek 被
        # Mojeek 的 captcha 页拦住（HTTP 200 + <title>Captcha</title>，属静默
        # 失败，靠 anti-bot 检测才判成 blocked）。它们占着 combo 槽位，而真正
        # 能出结果的独立索引反而进不来（marginalia/wiby/searchmysite 实测各 10 条）。
        #
        # 换成可用且**更契合长尾**的独立索引：这三个都不是大厂代理，正是冲
        # 「小网站/独立博客/非商业页面」去的，比再塞一个同类大引擎更有价值。
        # local_bing 仍打头（唯一稳定可用的大厂 SERP）。
        #
        # ⚠️ 它们不是 local_* 子引擎，所以不能用 sub_engines 过滤——那会让这一支
        # 恒等于 [local_bing]（实测英文通用查询只剩 1 个源），下面那三个名字
        # 永远等不到。判据改成「本地子引擎 OR 已启用的顶层引擎」。
        try:
            from engines import available_engines
            _available = set(available_engines())
        except Exception:
            _available = set()
        return [e for e in ["local_bing", "marginalia", "wiby", "searchmysite"]
                if e in sub_engines or e in _available]


def _get_engines_combo(domain: dict[str, Any], enabled: set[str], mode: str = "auto",
                       features: dict | None = None) -> list[str]:
    """从域配置获取 engines_combo，过滤不可用/付费（budget 模式）。
    自动将 local_search 扩展为子引擎（消灭黑盒）。

    注意：depth/context 的 combo 预算与 research_only 截断在 route_query 末尾
    统一走 engine_policy.filter_combo_by_policy，本函数只做可用性/成本/健康过滤。
    """
    # 学习器在此处首次解析（惰性，见模块顶部 _get_learner）：走到这里说明
    # 真的要装配 combo，这笔钱躲不掉；不进本函数的路径（缓存命中等）不付。
    _adaptive_learner = _get_learner()
    combo = domain.get("engines_combo", [])
    primary = domain.get("primary", "anysearch")
    fallback = domain.get("fallback")
    if combo:
        filtered = [e for e in combo if e in enabled]
    else:
        engines = [primary]
        if fallback and fallback != primary:
            engines.append(fallback)
        filtered = [e for e in engines if e in enabled]
    # P0-1：fallback 语义修复——combo 非空时也并入 fallback 候选。
    # 旧逻辑只在 combo 为空时读 fallback，而 69 个域全部配置了 engines_combo，
    # 导致 22 个真备用 fallback 全部失效（备用源形同虚设）。
    # 追加到尾部 + 串行执行：正常路径 primary 先跑，early-stop 命中即不触碰
    # fallback（零额外开销）；仅当 primary 无结果/故障时才轮到 fallback 保底。
    if fallback and fallback != primary and fallback in enabled and fallback not in filtered:
        filtered.append(fallback)

    # 🔑 关键改动：将 local_search 扩展为子引擎
    if "local_search" in filtered:
        filtered = _expand_local_search(filtered, features)

    # F7：远端配额耗尽（如 byted 10406 Free quota exhausted）→ 全模式排除，
    # 备用源自然接管，到周期边界惰性自愈（详见 quota.mark_remote_exhausted）。
    # fail-open：全部被排除时保留原 combo，交由执行层把配额错误暴露出来。
    try:
        _qm = get_quota_manager()
        if filtered:
            _alive = [e for e in filtered if not _qm.is_hard_down(e)]
            if _alive:
                filtered = _alive
    except Exception:
        pass

    # fast/budget 模式过滤付费引擎
    if mode in ("fast", "budget"):
        quota_mgr = get_quota_manager()
        filtered = [e for e in filtered if quota_mgr.is_available(e, mode=mode)]

    # fast/budget 模式优先前置零成本子引擎
    if mode in ("fast", "budget"):
        free_locals = [e for e in filtered if e.startswith("local_")]
        others = [e for e in filtered if not e.startswith("local_")]
        filtered = free_locals + others

    # 垂直域主源保护
    # 实测：wikipedia 分数常 <0.3，org_entity 在 wikidata 熔断时会只剩 baidu，英文 HQ 题脏结果。
    # modal_card 的 bocha_ai/bocha 为 cost_tier=low（0.7），fast 的 0.85 阈值会误杀整 combo。
    _VERTICAL_PROTECT = frozenset({
        "film_search", "sports_search", "geo_places", "org_entity", "media_search",
        "modal_card",
        # zhihu_content 的 zhihu_global 曾被 learner 低分过滤饿死（历史用量少
        # →分低→更不被用），37 天仅 53 次；断掉「饿死循环」
        "zhihu_content",
        # soil_agri 的 openfoodfacts（食品成分库）同理：新源无历史分，
        # 实测在 route_query 内被 learner 摘掉（combo 只剩 [usda, anysearch]），
        # 而单独调 _get_engines_combo 时还在——差别就是 learner 是否已加载。
        "soil_agri",
        # world_news 的 14 个本地语言源有同一风险且更严重：它们首次运行时
        # anysearch 先返回并 early-stop，语言源拿不到贡献分 → 分数低于 0.3 →
        # 被自适应过滤摘出 combo → 下次更不可能被选中。实测：接线当天跑过一次
        # 真实搜索后，`오늘 뉴스` 的 combo 就从 [anysearch, yna] 退化成
        # [anysearch, local_bing]（源还在 enabled，只是被 learner 摘掉）。
        # 该族每个源服务一种语言、无同族可替代，被摘掉即该语言通道消失。
        "world_news",
    })
    primary = domain.get("primary")
    domain_name = domain.get("name")
    protect: set[str] = set()
    # primary 保护同样受分数地板约束（2026-10-02，与下方学习器
    # 过滤、route.py must_keep 组装同一 _VERTICAL_FLOOR 口径）：
    # 已被证明失败的 primary（实测 thesportsdb 0.084、近期
    # 12/13 次失败）不进 protect——此前这行无条件豁免让它躲过
    # 学习器过滤，每次垂直查询白等它的单引擎超时（2.5-3.5s）。
    # 保护的本意是防新源无历史分被饿死（无数据引擎为中性
    # 0.5），不是保死源；分数回升后自动回归。它仍是域声明
    # 成员与 fallback，自动进入恢复链 L3 兜底。
    if primary and (
            _adaptive_learner is None
            or _adaptive_learner.get_score(primary) >= _VERTICAL_FLOOR):
        protect.add(primary)
    # 仅 modal_card 整 combo 免 cost 裁剪（结构化路径不可被 anysearch 顶替）
    if domain_name == "modal_card":
        protect.update(filtered)
        protect.update(domain.get("engines_combo") or [])

    # fast 模式：只保留免费引擎；modal_card / primary 保护成员例外
    if mode == "fast":
        from config import get_cost_factor
        filtered = [
            e for e in filtered
            if e in protect or get_cost_factor(e) >= 0.85
        ]

    # 自适应学习过滤（保留主引擎 + 垂直域 combo 成员不被误杀）
    if _adaptive_learner is not None and len(filtered) > 1:
        original = filtered[:]
        # 分数地板：垂直保护与 primary 豁免都不庇护「已被证明失败」
        # 的引擎（自适应分 <_VERTICAL_FLOOR）。无数据引擎是中性
        # 0.5、不会落进这个区间——落到这里的是有失败历史且近乎
        # 全败的源（实测 thesportsdb 0.084、近期 12/13 次失败）：
        # 保护的本意是防新源无历史分被饿死，不是保死源；否则每次
        # 垂直查询都白等它的单引擎超时（2.5-3.5s）。分数回升后
        # 自动回归 combo，且它仍是域 fallback（恢复链 L3 候选）。
        if domain_name in _VERTICAL_PROTECT:
            protect = {e for e in original
                       if _adaptive_learner.get_score(e) >= _VERTICAL_FLOOR
                       } | protect
        filtered = [
            e for e in filtered
            if e in protect
            or (e == primary
                and _adaptive_learner.get_score(e) >= _VERTICAL_FLOOR)
            or _adaptive_learner.get_score(e) >= 0.3
        ]
        if not filtered:
            filtered = original

    # 网络环境感知排序（独立于过滤，主引擎永远第一）。
    # 只重排「非主引擎」，且只在同能力族内排序（避免跨族调整破坏
    # combo 预算——垂直族必须保持在 web_general 之前）。
    # 有显著分数差（≥0.15）时同族内快源前置；不足则顺序不变（缓存键稳定）。
    if _adaptive_learner is not None and len(filtered) > 1:
        primary = domain.get("primary")
        try:
            from engine_families import family_of
            # 引擎声明必须传下去：config.yaml 的 family 字段才是来源。
            # 不传时 family_of 会退回静态覆盖表、再退到默认值 web_general——
            # 实测 16 个引擎（含本次新接的 osv / cisa_kev / federal_register /
            # unpaywall / opencitations，以及 sports 三个、weather 两个）因此
            # 被当成通用源，在下面「同族按分数排序」里跟 anysearch(0.886) 同族，
            # 于是一个个被挤到后面（us_legal 的 federal_register 就是这么掉到
            # 第三位的，而它的域声明顺序本来是第二位）。
            _specs = get_engines()
            if not isinstance(_specs, dict):
                _specs = {}
        except ImportError:
            family_of = None
            _specs = {}

        def _fam(e: str) -> str:
            try:
                return family_of(e, _specs.get(e)) if family_of else "?"
            except Exception:
                return "?"

        if primary and primary in filtered:
            primary_eng, rest = primary, [e for e in filtered if e != primary]
        else:
            primary_eng, rest = None, list(filtered)
        if len(rest) > 1 and family_of is not None:
            # 按原始顺序分组（同族相邻），族内按分数稳定排序
            grouped: list[str] = []
            seen_fam: set[str] = set()
            for e in rest:
                f = _fam(e)
                if f not in seen_fam:
                    seen_fam.add(f)
                    members = [x for x in rest if _fam(x) == f]
                    if len(members) > 1:
                        scored = [(x, _adaptive_learner.get_score(x)) for x in members]
                        top_score = max(s for _, s in scored)
                        laggards = [x for x, s in scored if top_score - s >= 0.15]
                        if laggards and len(laggards) < len(scored):
                            fast = [x for x, s in scored if top_score - s < 0.15]
                            grouped.extend(fast + laggards)
                        else:
                            grouped.extend(members)
                    else:
                        grouped.extend(members)
            rest = grouped
        filtered = ([primary_eng] if primary_eng else []) + rest

    # 健康检查过滤：只对本地子引擎（local_*）做健康判定，非本地引擎
    # 无条件保留。两条路径（scripts health_check / health_probe fallback）
    # 必须保持同一语义，否则被劫持/缺模块时行为会静默漂移（曾导致
    # wikipedia 被 health.db 的旧探测失败记录误过滤）。
    try:
        from health_check import is_available as _hc_available
        healthy = []
        for e in filtered:
            if e.startswith("local_"):
                if _hc_available(e):
                    healthy.append(e)
            else:
                healthy.append(e)
        if healthy:
            filtered = healthy
    except ImportError:
        try:
            from health_probe import get_engine_status
            healthy = []
            for e in filtered:
                if e.startswith("local_"):
                    if get_engine_status(e).get("available", True):
                        healthy.append(e)
                else:
                    healthy.append(e)
            if healthy:
                filtered = healthy
        except ImportError:
            pass

    # ── 配额/熔断感知：确定不可用源剔除 + 候选滚动（无缝切换）──────────
    # 正常路径（全部引擎可用）顺序不变 → 引擎集合不变 → 缓存键不变 → 零速度倒退。
    # P0-3：disabled / open+cooldown 的引擎是「确定不可用」——不再沉底保留
    # （沉底后仍会被执行，白耗一次注定失败的超时），而是直接剔除，让域内
    # 候选（fallback / combo 其他成员，天然同主题）自动顶位；域内无候选时
    # 集合收缩，交由 route_query 尾部通用保底 / recovery 按 family 检查补源。
    # open 但 cooldown 已过 → half-open 探测资格，保留（与 allow() 一致）。
    # 缓存键基于 sorted(engines) 集合：剔除改变集合→键变，但 open+cooldown
    # 时负缓存已生效，键变化无损失；且故障源不再被调用。
    usable, unusable = [], []
    for e in filtered:
        ok = True
        try:
            if not get_quota_manager().is_available(e, mode=mode):
                ok = False
        except Exception:
            pass
        if ok:
            try:
                from circuit_breaker import get_breaker
                # 必须用 allow() 而非 status()：allow 内置状态转移（disabled/open
                # 冷却超时 → half_open 探测资格），status 只读。曾导致 disabled
                # 引擎在 route 层被永久剔除、执行层 allow() 永远不被调用、
                # B4 恢复通道成死代码（bocha 卡死 disabled 一天余的根因）。
                allowed, _reason = get_breaker().allow(e)
                if not allowed:
                    ok = False
            except ImportError:
                pass
            except Exception:
                pass
        (usable if ok else unusable).append(e)
    if unusable and usable:
        filtered = usable
    elif unusable and not usable:
        # 域内全部不可用：返回空集，由 route_query 尾部保底（通用免费源 /
        # modal_card 保留声明引擎供执行层返回 error item）
        filtered = []

    # ── 能力族去重 + 互补回填（标准化调用契约）────────────────────────
    # 全网搜索族同质化最高（byted/bocha/octen 都是通用网页检索），
    # 同族堆叠纯属浪费预算位：web_general 至多保留 2 个，垂直族保留多源。
    # config.yaml 引擎声明的 family 字段是来源（spec_lookup 传入 family_of，
    # 不再只看静态覆盖表）。去重腾出的槽位由 complement_refill 用互补能力族
    # 引擎回填（与域主引擎 coverage 重叠的高优先级源），兑现「给其他族腾出
    # 预算位」；已有垂直成员的域不再追加，尊重域作者配置。
    try:
        from engine_families import dedupe_by_family, complement_refill
        specs = get_engines()
        if isinstance(specs, dict):
            # 只收缩 web_general：垂直族（academic/code/finance 等）保留多源
            # 交叉验证，不去重（股票域 sina/eastmoney 双行情源必须共存）。
            deduped = dedupe_by_family(
                filtered, max_per_family=2, spec_lookup=specs,
                limit_families=frozenset({"web_general"}),
            )
            removed = len(filtered) - len(deduped)
            if removed > 0:
                filtered = complement_refill(
                    deduped, enabled=enabled, spec_lookup=specs,
                    domain_primary=domain.get("primary"),
                    max_slots=min(2, removed),
                )
            else:
                filtered = deduped
    except ImportError:
        pass

    return filtered


# ── combo 定稿的三段共享步骤 ───────────────────────────────────────────────────
#
# route_query 的三条分支（域命中 / TF-IDF / 通用保底）各自组装 combo，但有
# 三段逻辑在每条分支里**逐字重复**。重复的代价不是行数，而是「改一处漏两处」：
# 代码注释里已经记着两次因此产生的缺陷（TF-IDF 分支漏了语言过滤、语言/次域追加
# 的引擎绕过了熔断处理）。这三段收在这里，新增一步只需要改一个地方。
#
# 刻意**不做**「把三条分支合并成一个带开关的函数」：三条流水线的步骤集合本身
# 不同（保底分支不走语言摘除与意图收口，域分支多一步 primary 扶正），合并需要
# 五六个模式开关——那是把显式的分支换成隐式的 flag 矩阵，读者更难判断某条路径
# 到底跑了哪些步骤。共享的是**真正相同的步骤**，不是分支本身。


def intent_squeeze(combo: list[str], features: dict | None, domain: dict | None,
                   mode: str, parallel: bool,
                   must_keep: list[str]) -> tuple[list[str], bool]:
    """definition/fact 意图下收紧 combo（幂等二次施加）。

    policy 层的新源 must_keep 补回会放大 combo：单源即答语义下，扩容槽纯属
    阶梯等待浪费（实测 academic definition 1 → 3 引擎）。只对 definition/fact
    收口——social/news/compare 本身要多源，扩容与意图同向。must_keep 成员豁免
    （它们有硬保留理由：geo 的 local_openstreetmap 被裁会退化成 wikidata 单源）。
    """
    intents = (features or {}).get("intents") or []
    if "definition" not in intents and "fact" not in intents:
        return combo, parallel
    keep = set(must_keep)
    rest = [e for e in combo if e not in keep]
    if len(rest) != len(combo):
        rest, parallel = _apply_intent_parallelism(rest, features, domain, mode,
                                                   parallel)
        return rest + [e for e in combo if e in keep and e not in rest], parallel
    return _apply_intent_parallelism(combo, features, domain, mode, parallel)


def geo_lang_must_keep(features: dict | None, enabled: set[str],
                       combo: list[str], query: str) -> list[str]:
    """must_keep 的公共部分：geo 主源 + 语言保护（三条分支都要）。"""
    out: list[str] = []
    if (features or {}).get("has_geo") and "local_openstreetmap" in enabled:
        out.append("local_openstreetmap")
    out.extend(_lang_must_keep(features, enabled, combo, query))
    return out


def lang_aux_engines(features: dict | None, enabled: set[str],
                     query: str) -> list[str]:
    """语言查询的补充引擎：预算定稿后**追加**到 combo 末尾，不进 must_keep。

    与 must_keep 的分工是硬性的，不是风格选择：must_keep 的语义是「别把
    这个挤掉」，而 policy 层会把 must_keep 的项**提到 combo 前部**。实测把
    hatena_bookmark 塞进 must_keep 后，ja 的 TF-IDF 兜底分支 combo 从
    [anysearch, local_bing] 变成 [local_bing, hatena_bookmark]，
    `engine` 也从 anysearch 变成 local_bing——直接违反
    tests/test_multilingual_routing.py::TestJaKoTfidfSkip 钉住的契约
    （「候选全被语言过滤时退到通用保底 anysearch」）。

    追加到末尾既保住了通用保底的首位，也让补充源真的参与这次搜索。
    """
    if not features or not enabled:
        return []
    from route_lang import _LANG_AUX_ENGINES, _family_lang
    lang = _family_lang(features, query)
    if not lang:
        return []
    return [e for e in _LANG_AUX_ENGINES.get(lang, []) if e in enabled]


def apply_lang_aux(combo: list[str], features: dict | None,
                   enabled: set[str], query: str,
                   skip: bool = False) -> list[str]:
    """把语言补充源追加到 combo 末尾（幂等）。

    `skip` 对应 route.py 的 `_pure_combo`（用户显式指定引擎）：那类 combo 是
    用户意图本身，不该被路由补充。做成参数而不是让两处调用方各自 if，是为了让
    route.py 少一行——它已在 1000 行门禁的临界值上。
    """
    if skip:
        return combo
    for e in lang_aux_engines(features, enabled, query):
        if e not in combo and e in enabled:
            combo.append(e)
    return combo


def breaker_filter(combo: list[str], enabled: set[str],
                   empty: tuple[str, ...] = ("anysearch", "local_bing"),
                   features: dict | None = None, query: str = "",
                   skip_aux: bool = False) -> list[str]:
    """熔断统一处理 + 空回退 + 语言补充源追加（combo 定稿的最后一道）。

    combo 非空是执行层的前提（空 combo 会让 engines[0] IndexError），且语言/
    geo/次域/TF-IDF 追加的引擎都可能处于熔断态——任何拼装路径的末尾都必须过这
    一道，所以它是共享步骤而不是各分支自己写。`empty` 允许分支声明自己的兜底
    次序（通用保底路径只认 anysearch）。

    语言补充源（`_LANG_AUX_ENGINES`）也收在这里，而不是让 route.py 的两条
    combo 定稿路径各调一次 `apply_lang_aux`：那是同一个「按语言调整 combo」的
    动作，放在定稿收口里既保证一定被应用，也省掉 route.py 的行数——它已在
    1000 行模块体积门禁的临界值上。放在熔断之后是有意的：补充源若恰处熔断
    态，会被 `_filter_breaker_blocked` 一并摘掉。
    """
    combo = apply_lang_aux(combo, features, enabled, query, skip=skip_aux)
    combo = _filter_breaker_blocked(combo)
    if not combo:
        combo = [e for e in empty if e in enabled] or ["anysearch"]
    return combo


# 强语义注入的通用引擎黑名单：这些引擎已被域 combo 覆盖，注入会喧宾夺主。
# 放在模块级而不是 route_query 体内：它是**常量数据**，藏在热路径函数里每次
# 调用重建（实测 0.074 µs，性能上无所谓，但读代码的人会以为它随调用变化）。
_GENERAL_ENGINES = frozenset({
    "anysearch", "byted", "bocha", "octen",
    "local_search", "zhihu", "wechat_sogou", "uapi", "tavily",
    "brave", "bocha_ai", "google_scholar", "arxiv", "wikipedia",
})


def inject_strong_semantic(combo: list[str], *, tfidf_best: str | None,
                           score: float, is_catch_all: bool,
                           enabled: set[str]) -> tuple[list[str], bool]:
    """TF-IDF 强匹配时把推荐引擎前置（v2.7.10）。返回 (combo, 是否注入)。

    位置由调用方决定：必须在 primary 扶正**之后**——否则被扶正压到第二位，
    串行 early-stop 下永远不执行。

    判据：分数 ≥0.6（远高于最低阈值 0.12）表示查询与引擎文档强匹配，前置注入
    不锁死（域主源仍在尾部备位，注入引擎失败时自然补位）；已在 combo 里、
    属于通用源（会被域 combo 覆盖）、catch-all 域、或不在 enabled 都不注入。
    """
    if not (tfidf_best and score >= 0.6 and not is_catch_all
            and tfidf_best not in combo
            and tfidf_best not in _GENERAL_ENGINES
            and tfidf_best in enabled):
        return combo, False
    return [tfidf_best] + [e for e in combo if e != tfidf_best], True

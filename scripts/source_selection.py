#!/usr/bin/env python3
"""source_selection.py — 通用层候选源的统一排序。

## 为什么（2026-10-26）

通用 fallback 此前是 `engine_policy.GENERAL_FREE_FALLBACK` 的静态元组，
再由 `filter_combo_by_policy` 按**位置**截断到前 2 位。实测后果：

  · 第一、二位是 octen（余额不足，403）与 anysearch（p50 1.7s），
    3015 条真实查询里 44.5% 最终由 anysearch 参与交付；
  · 实测 0.8–1.3s、词面质量更高的 tavily_free / parallel_free / firecrawl
    排在第 6–8 位，日常预算截断后**永远不可达**；
  · 学习器算了 engine×domain×lang 的分，但通用路径不读它，
    「这个源在我这里已经坏了」进不了选源。

本模块只做**排序**，不做过滤（quota/熔断/语言仍由既有层处理）。过滤放在
这里会让「为什么这个源没出现」多一个隐藏分支；排序只回答「同样可用时
先试谁」。

## 先验的来源

`GENERAL_PRIORS` 是 2026-10-26 单机实测的固定先验（4–6 条真实查询 ×
单引擎单跑；复跑入口 `python3 scripts/bench_general_pool.py`，脚本会用
隔离状态目录交错跑 static/dynamic 两臂）。它是**冷启动种子**，不是长期
真值：源一旦有 ≥3 条自适应样本，自适应分占 6 成、先验占 4 成。重测后用
新数据更新此表，不要按印象调。

## 覆盖地板与语言门（2026-10-27，issue #23）

只按「快 + 词面」排序会选出交付很薄的源，或与查询语言错配的源（英文
查询由中文语料源领跑，5 条结果全是中文长摘要，体积翻倍且相关性不对）。
现在排序先分层、层内再比分数：

  0 合格：覆盖率 ≥ COVERAGE_FLOOR（3 条）且语言适配；
  1 语言错配：声明了 chinese 却拿来跑英文查询（或反之）；
  2 交付太薄：实测平均交付 < 3 条（如 tavily_free 的匿名通道常态 2 条）。

层级是**先验数据**决定的硬约束，不参与自适应混合——自适应分学习的是
「这个源好不好」，回答不了「它能不能交够数」。非中英脚本不参与分层
（语言覆盖源本身就是唯一召回，见 _NON_LATIN_LANG_BOOST）。
"""

from __future__ import annotations

from typing import Callable
from except_sets import IO_BENIGN, SHAPE_BENIGN  # 收窄词汇表（2026-11-13 裸吞收口）

# 先验 = 实测延迟 + 词面相关 + 稳定性的人工压缩（0–1）。只列通用候选；
# 未列出的源给 DEFAULT_PRIOR，不会凭名字占便宜。
GENERAL_PRIORS: dict[str, float] = {
    # 0.6s / 0.76（6 条），中文硬新闻弱于 anysearch
    "yuanbao": 1.00,
    # 1.0s / 0.92（4 条），免 key 匿名通道
    "tavily_free": 0.98,
    # 1.1s / 0.92（4 条）
    "parallel_free": 0.96,
    # 1.3s / 0.88（4 条），1000 credits/月，配额层会限
    "firecrawl": 0.90,
    # 2.7s / 0.78（6 条），中文新闻与长尾覆盖最强，慢
    "anysearch": 0.86,
    # 0.4s，英文技术/国际新闻强，中文通用弱；官方 API 带 authority/date。
    # 付费，预算内才进池，fast/budget 永不进（见 _paid_candidate_allowed）。
    "deepseek_search": 0.84,
    # 2.3s / 0.89（4 条），慢但内容扎实，研究档应提权
    "uapi": 0.72,
    # 2.5s / 0.60（5 条），语义泛化，作最后备选
    "local_bing": 0.62,
    # 实测 4/4 零结果或不是通用搜索源：留在池里只作解释，排序垫底
    "local_baidu": 0.10,
    "wikipedia": 0.10,
    "octen": 0.10,
}
DEFAULT_PRIOR = 0.50

# 允许进入通用池的付费候选。与 paid_policy 的域级对冲是两条不同的路：
# 域级对冲只在 english_tech / intl_news_flash 的第二位；这里是通用池的
# 第 3+ 候选（日常预算截断到 2 时通常轮不到，研究档不截断才参与）。
# fast/budget 永不进；ARGO_PAID_SEARCH=off 或预算耗尽也不进。
PAID_GENERAL_CANDIDATES = frozenset({"deepseek_search"})

# ─── 交付覆盖（2026-10-27 单引擎单跑实测）──────────────────────────────────
#
# 排序只看「快 + 词面」会选出交付很薄的源：2026-10-27 nightly（issue #23）
# 里无 key 环境的领队 tavily_free 对 Alan Turing 只交 2 条（基线 5 条）。
# 覆盖地板让这种源顺延给交付够数的源，而不是靠早停后才发现「怎么只有两条」。
#
# 数据来源：同一条查询 `Alan Turing` 单引擎单跑（--no-cache，agent 档）。
# 未列出的源给 COVERAGE_FLOOR 中性值——缺数据不惩罚，交给自适应分兜底。
GENERAL_COVERAGE: dict[str, float] = {
    "yuanbao": 5.0,        # 5 条，但全是中文长摘要（见 GENERAL_BYTES 与语言门）
    "tavily_free": 2.0,    # 2 条：匿名通道交付薄，不能当领队
    "parallel_free": 5.0,
    "anysearch": 5.0,
    "deepseek_search": 5.0,
    "local_bing": 2.0,     # 单点实测 0 条，历史口径也不高；给保守值
}
COVERAGE_FLOOR = 3.0

# agent 档 5 条结果的平均字节（同日实测）。**不参与排序**，只做监控报表与
# bench 对照：把它折进分数需要凭空定一个「每 KB 折多少毫秒」的系数，而中文
# 查询的长摘要（yuanbao 12.5KB）恰恰是分析场景要的上下文——体积该由 drift
# 门禁守，不该让排序替用户做取舍。英文查询的体积问题由语言门解决。
GENERAL_BYTES: dict[str, int] = {
    "yuanbao": 12508,
    "tavily_free": 3993,
    "parallel_free": 6790,
    "anysearch": 5859,
    "deepseek_search": 7064,
}

# 语言与源声明的适配：只对中文/英文查询生效。zh 查询不让「只声明 english」
# 的源领跑，en 查询不让「只声明 chinese」的源领跑；没声明语言的源（anysearch
# 的 general/tech）保持中性。声明真源是 config 引擎字典（sync_backends 从
# spec 派生），读不到时不判（fail-open）——缺数据不等于不适配。
_LANG_PAIR = {"zh": ("chinese", "english"), "en": ("english", "chinese")}
_COVERAGE_CACHE: dict[str, tuple[str, ...]] | None = None


def _coverage_of(engine: str) -> tuple[str, ...]:
    global _COVERAGE_CACHE
    if _COVERAGE_CACHE is None:
        try:
            from config import load_config
            engines = load_config().get("engines") or {}
            _COVERAGE_CACHE = {
                str(name): tuple((meta or {}).get("coverage") or ())
                for name, meta in engines.items() if isinstance(meta, dict)}
        except Exception:
            _COVERAGE_CACHE = {}
    return _COVERAGE_CACHE.get(engine, ())


def _lang_misfit(engine: str, lang: str) -> bool:
    """查询语言与源声明语言错配（覆盖中文/英文及明确非中文语种对中文专源错配）。"""
    cov = {str(c).lower() for c in _coverage_of(engine)}
    pair = _LANG_PAIR.get(lang)
    if pair:
        own, other = pair
        return other in cov and own not in cov
    if lang and lang not in ("zh", "mixed", "other"):
        # 非中文语种查询避免把仅声明 chinese 的源当首选
        return "chinese" in cov and "english" not in cov and "multilingual" not in cov
    return False


# 非中英脚本（西里尔/阿拉伯/日韩…）不参与覆盖地板与语言门：那些语言下
# anysearch/local_bing 这类语言覆盖源本就是唯一召回，交给既有 boost 处理。
_TIERED_LANGS = frozenset({"zh", "en", "mixed", "other", ""})


def _tier(engine: str, lang: str) -> int:
    """排序层级：0 合格（覆盖够 + 语言适配）/ 1 语言错配 / 2 交付太薄。"""
    if lang not in _TIERED_LANGS:
        return 1 if _lang_misfit(engine, lang) else 0
    if GENERAL_COVERAGE.get(engine, COVERAGE_FLOOR) < COVERAGE_FLOOR:
        return 2
    return 1 if _lang_misfit(engine, lang) else 0

# 非中英查询的语言覆盖加成。先验表是在中英查询上测的；西里尔/阿拉伯/
# 日韩及欧洲小语种查询下，多语言覆盖源（anysearch/local_bing 带动态 setlang）
# 比英文或中文单语快源更有召回价值。
_NON_LATIN_LANG_BOOST: dict[str, float] = {
    "anysearch": 0.35,
    "local_bing": 0.40,
}
_LATIN_OR_CN_LANGS = frozenset({"zh", "en", "mixed", "other", ""})

# 意图微调权重（零基兜底分化：同语言层级下，新闻偏时效多源，对比/事实偏结构摘要）
_INTENT_PRIOR_BOOST: dict[str, dict[str, float]] = {
    "news": {"anysearch": 0.08, "local_bing": 0.04},
    "compare": {"parallel_free": 0.06, "tavily_free": 0.04, "uapi": 0.04},
    "definition": {"parallel_free": 0.06, "tavily_free": 0.04, "uapi": 0.04},
    "fact": {"parallel_free": 0.06, "tavily_free": 0.04},
    "social": {"anysearch": 0.05},
}

# 有 ≥3 条自适应样本时，先验与自适应分各占一半；否则先验做主。
_ADAPTIVE_BLEND_SAMPLES = 3


def _prior_for(engine: str, lang: str,
               intents: list[str] | tuple[str, ...] | None = None) -> float:
    prior = GENERAL_PRIORS.get(engine, DEFAULT_PRIOR)
    if lang not in _LATIN_OR_CN_LANGS:
        prior += _NON_LATIN_LANG_BOOST.get(engine, 0.0)
    if intents:
        bonus = max((_INTENT_PRIOR_BOOST.get(it, {}).get(engine, 0.0) for it in intents), default=0.0)
        prior += bonus
    return prior


def _is_research(mode: str, depth: str, context: str) -> bool:
    try:
        from engine_policy import is_research_context
        return is_research_context(mode=mode, depth=depth, context=context)
    except Exception:
        return mode == "deep" or depth == "deep" or context == "research"


def _usable(candidates: list[str], *, mode: str) -> list[str]:
    """硬性不可用（远端配额耗尽 / fast 档付费源）不参与排序。

    fail-open：quota 模块不可用时保留原列表，由执行层暴露真实失败。
    """
    out = list(candidates)
    try:
        from quota import get_quota_manager
        qm = get_quota_manager()
        try:
            exhausted = qm.remote_exhausted_marks()
            out = [e for e in out if e not in exhausted]
        except IO_BENIGN + SHAPE_BENIGN:
            pass
        if mode in ("fast", "budget"):
            out = [e for e in out if qm.is_available(e, mode=mode)]
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(
            f"quota 可用性过滤跳过（fail-open）: {type(e).__name__}")
    return out


def _paid_search_enabled() -> bool:
    try:
        from engine_env import get_env
        raw = str(get_env("ARGO_PAID_SEARCH", "auto") or "auto")
    except Exception:
        import os
        raw = os.environ.get("ARGO_PAID_SEARCH", "auto")
    return raw.strip().lower() not in ("off", "0", "false", "no")


def _paid_candidate_allowed(engine: str, mode: str) -> bool:
    """付费候选的准入：白名单 + 非 fast/budget + 开关开 + 预算未耗尽。

    与 paid_policy 的 `_paid_hedge_ready` 同一套门槛，只是位置不同：这里
    进通用池，那里插域 combo 第二位。花钱的事一律 fail-closed。
    """
    if engine not in PAID_GENERAL_CANDIDATES or mode in ("fast", "budget"):
        return False
    if not _paid_search_enabled():
        return False
    try:
        from engine_env import is_engine_allowed_by_env
        if not is_engine_allowed_by_env(engine):
            return False
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(
            f"引擎 env 准入检查异常按禁止处理: {type(e).__name__}")
        return False
    try:
        from engine_admission import is_blocked
        if is_blocked(engine):
            return False
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(
            f"引擎准入 is_blocked 检查异常按禁止处理: {type(e).__name__}")
        return False
    try:
        from quota import get_quota_manager
        return not get_quota_manager().is_hard_down(engine)
    except Exception:
        return False


def candidate_allowed(engine: str, enabled: set[str], mode: str) -> bool:
    """通用候选是否允许参与：enabled 成员，或预算内的白名单付费源。

    两条通用路径都要用这一处判据：`rank_general_candidates` 的池过滤，
    以及 `tfidf_lead_combo` 的最终 `out` 过滤。后者的 `if e in enabled`
    曾把 paid 候选单独丢掉——池排序里有它，组合装配时又被剔掉。
    """
    return engine in enabled or _paid_candidate_allowed(engine, mode)


def rank_general_candidates(
    candidates: list[str],
    *,
    enabled: set[str],
    mode: str = "auto",
    depth: str = "fast",
    context: str = "search",
    lang: str = "",
    intents: list[str] | tuple[str, ...] | None = None,
    score_fn: Callable[[str, str, str, str], tuple[float, int]] | None = None,
) -> list[str]:
    """按「先验 × 自适应 × 用途 × (语言/意图分化)」排序通用候选源。

    `score_fn(engine, domain, lang, purpose) -> (score, samples)` 可注入，
    测试用假学习器即可确定性断言，不依赖本机 adaptive.db 的时点数据。
    """
    pool = _usable(candidates, mode=mode)
    pool = [e for e in pool if candidate_allowed(e, enabled, mode)]
    if not pool:
        return []

    purpose = "research" if _is_research(mode, depth, context) else "daily"
    if score_fn is None:
        def score_fn(engine: str, domain: str, q_lang: str,
                     use: str) -> tuple[float, int]:
            from adaptive import get_learner
            return get_learner().get_score_with_samples(
                engine, domain, q_lang, use)

    ranked: list[tuple[int, float, int, str]] = []
    for idx, engine in enumerate(pool):
        prior = _prior_for(engine, lang, intents=intents)
        try:
            adaptive_score, samples = score_fn(
                engine, "general_search", lang, purpose)
        except Exception:
            adaptive_score, samples = 0.5, 0
        if samples >= _ADAPTIVE_BLEND_SAMPLES:
            # 有样本时自适应占 6 成：先验是冷启动种子，不能让「高先验但已
            # 证实失败」的源靠乘法永远压住健康源（实测反例：先验 1.0、
            # 自适应 0 的源乘出来 0.5，仍高于先验 0.5、自适应 0.5 的健康源）。
            weight = 0.4 * prior + 0.6 * min(1.0, float(adaptive_score))
        else:
            weight = prior
        # 层级先于分数：覆盖太薄 / 语言错配的源，即便更快更高分也排到
        # 合格源之后（分数在层内仍然说了算）。idx 取负：同分保持候选池
        # 原顺序（稳定排序，便于 A/B 复现）。
        ranked.append((_tier(engine, lang), -weight, idx, engine))
    ranked.sort()
    return [engine for _t, _w, _i, engine in ranked]

#!/usr/bin/env python3
"""
route.py — Unified Search v2 三层路由决策

路由策略：
  1. 用户指定引擎 → 直接返回
  2. TF-IDF 语义路由（二元组 + boost + cost + quota）
  3. 正则硬规则匹配（config.yaml domains）
  4. 融合决策：正则 + TF-IDF 验证 → 高置信度
  5. budget 模式：过滤付费引擎

每种决策都带 reason 字符串。
"""

from __future__ import annotations

import re
import time
from typing import Any

try:
    from config import load_config, get_engines, get_domains, get_cost_factor
    from tfidf_router import semantic_route, get_router
    from quota import get_quota_manager
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from config import load_config, get_engines, get_domains, get_cost_factor
    from tfidf_router import semantic_route, get_router
    from quota import get_quota_manager

# 世界银行国家表：macro_data 域按国家词分流（非美国国家查询让 worldbank 优先，
# 避免 FRED 美国序列冒充「中国GDP」这类答案）
try:
    from engines_builders_data import is_foreign_macro_query
except Exception:
    def is_foreign_macro_query(query: str) -> bool:
        return False

# 自适应学习（可选依赖）
try:
    from adaptive import get_learner
    _adaptive_learner = get_learner()
except Exception:
    _adaptive_learner = None

# 引擎注册中心（子引擎可见性）
try:
    from argo_engine_registry import get_registry as _get_registry
except Exception:
    _get_registry = None


# ── 特征提取 ──────────────────────────────────────────────────────────────────

_RE_CHINESE = re.compile(r"[一-鿿]")
_RE_COMPARE = re.compile(r"\b(vs|versus)\b|(对比|比较|区别|相比|哪个好)", re.I)
_RE_TECH = re.compile(
    r"\b(api|python|javascript|typescript|code|react|vue|node|rust|go|"
    r"golang|docker|kubernetes|linux|git|sql|error|bug|debug|exception|"
    r"function|class|async|thread|database|algorithm|programming|framework|library)\b|"
    r"(函数|方法|类|库|框架|报错|调试|编程|代码|开发|技术|源码|架构)", re.I)
_RE_QUESTION = re.compile(
    r"\b(how|what|why|when|where|which|who)\b|"
    r"(怎么|什么|为什么|如何|哪里|哪个|谁|多少|几|吗|呢)", re.I)
_RE_DEPTH = re.compile(
    r"\b(deep|comprehensive|review|survey|research|paper|thesis)\b|"
    r"(对比分析|深度|全面|详细|深入|系统|完整|综述|研究|探究|详解|论文)", re.I)

def _build_engine_names() -> dict[str, str]:
    """从 config.yaml 引擎声明的 label 构建显示名映射（唯一真源）。

    新增引擎只需在 config.yaml 声明 label，路由 reason 自动使用，
    不再需要手工同步本表。
    """
    try:
        engines = get_engines()
    except Exception:
        return {}
    return {name: spec.get("label") or name for name, spec in engines.items()}


_ENGINE_NAMES = _build_engine_names()


def extract_features(query: str) -> dict[str, Any]:
    """提取查询特征向量。

    P0-001：并入 has_geo / has_negation / intents，供下游路由/并行度决策使用。
    查询理解不可用时退化为纯正则特征，不影响原有字段。
    """
    total = len(query)
    chinese = len(_RE_CHINESE.findall(query))
    ratio = chinese / max(total, 1)
    features: dict[str, Any] = {
        "chinese_ratio": ratio,
        "english_ratio": 1.0 - ratio,
        "length": total,
        "has_compare": bool(_RE_COMPARE.search(query)),
        "has_technical": bool(_RE_TECH.search(query)),
        "has_question": bool(_RE_QUESTION.search(query)),
        "has_depth_word": bool(_RE_DEPTH.search(query)),
        "has_geo": False,
        "has_negation": False,
        "intents": [],
    }
    try:
        from query_understanding import _understand_cached as understand
        qu = understand(query)
        features["has_geo"] = bool(qu.geo)
        features["has_negation"] = bool(qu.exclude_terms)
        features["intents"] = list(qu.intents)
    except ImportError:
        pass  # query_understanding 不可用，保留默认值
    except Exception as e:
        import logging
        logging.getLogger("unified_search.route").debug(
            f"查询理解特征跳过: {type(e).__name__}")
    return features


def _feature_labels(features: dict[str, Any]) -> str:
    labels = []
    cr = features.get("chinese_ratio", 0)
    if cr > 0.6:
        labels.append("中文")
    elif cr < 0.1:
        labels.append("英文")
    for key, name in (("has_technical", "技术向"), ("has_compare", "对比分析"),
                      ("has_depth_word", "深度研究"), ("has_question", "问答型")):
        if features.get(key):
            labels.append(name)
    return " + ".join(labels) if labels else "通用查询"


# ── 域匹配（预编译 + mtime 缓存，避免每次 route 重新 compile 全部正则） ────────

_compiled_domains: list[dict[str, Any]] | None = None
_compiled_domains_id: int | None = None  # id(domains list) or len+name fingerprint


def _compile_domain_patterns(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compiled = []
    for idx, domain in enumerate(domains):
        patterns = domain.get("patterns", [])
        if isinstance(patterns, str):
            patterns = []
        regexes = []
        for p in patterns:
            try:
                regexes.append(re.compile(p))
            except re.error:
                continue
        compiled.append({**domain, "_idx": idx, "_compiled": regexes})
    return compiled


def _get_compiled_domains(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    global _compiled_domains, _compiled_domains_id
    # domains 列表在 load_config 缓存命中时是同一对象
    dom_id = id(domains)
    if _compiled_domains is not None and _compiled_domains_id == dom_id:
        return _compiled_domains
    _compiled_domains = _compile_domain_patterns(domains)
    _compiled_domains_id = dom_id
    return _compiled_domains


def match_domain(query: str, domains: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """按 config.yaml domains 顺序匹配；无命中返回 catch-all。"""
    if domains is None:
        domains = get_domains()
    compiled = _get_compiled_domains(domains)
    catch_all: dict[str, Any] | None = None
    for domain in compiled:
        if not domain.get("patterns", []):
            catch_all = domain
            continue
        for regex in domain["_compiled"]:
            if regex.search(query):
                return domain
    return catch_all


def _expand_local_search(engine_list: list[str], features: dict | None = None) -> list[str]:
    """将 local_search 扩展为具体的子引擎（基于查询特征）。"""
    if "local_search" not in engine_list:
        return engine_list
    if _get_registry is None:
        return engine_list

    registry = _get_registry()
    sub_engines = registry.list_local_engines(available_only=False)
    if not sub_engines:
        return engine_list

    selected = _select_sub_engines(sub_engines, features)
    result = [e for e in engine_list if e != "local_search"]
    for eng in selected:
        if eng not in result:
            result.append(eng)
    return result[:4]


def _add_language_engines(engine_list: list[str], features: dict | None = None) -> list[str]:
    """为已路由的查询添加语言相关的本地引擎（补充源）。

    当 TF-IDF 已选中主引擎后，根据查询语言特征追加本地子引擎，
    实现多源融合（网页引擎 + 本地零成本引擎）。
    """
    if _get_registry is None:
        return engine_list
    if not features:
        return engine_list

    chinese_ratio = features.get("chinese_ratio", 0)

    # 已包含 local_ 引擎则跳过
    if any(e.startswith("local_") for e in engine_list):
        return engine_list

    registry = _get_registry()
    sub_engines = registry.list_local_engines(available_only=False)
    if not sub_engines:
        return engine_list

    selected: list[str] = []
    # 只要含中文字符就追加中文引擎（阈值 0.1 覆盖中英混合查询）
    # 百度/搜狗质量低，仅作印证；自动追加只用 local_bing
    if chinese_ratio > 0.1:
        selected = [e for e in ["local_bing"] if e in sub_engines]
    elif features.get("has_depth_word"):
        selected = [e for e in ["local_arxiv", "local_semantic_scholar"] if e in sub_engines]

    result = list(engine_list)
    for eng in selected[:2]:  # 最多追加 2 个
        if eng not in result:
            result.append(eng)
    return result


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


# 意图 → (期望引擎数, 是否并行)。P0-005 动态并行度。
_INTENT_PARALLELISM: dict[str, tuple[int, bool]] = {
    "definition": (1, False),
    "fact": (1, False),
    "news": (2, True),
    "compare": (3, True),
    "social": (3, True),
}

# 窄域引擎单点保护：这类引擎「永不返回零结果」或只覆盖单一主题
# （跨域查询产出噪声，如 mdn 的 quantum computing → Cloud computing）。
# definition/fact 意图裁到 1 引擎时，若主引擎是窄域引擎，强制保留 2 引擎，
# 避免单引擎独占时噪声无处可挡。
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
        return [e for e in ["local_bing", "local_duckduckgo", "local_mojeek"] if e in sub_engines]

    chinese_ratio = features.get("chinese_ratio", 0)
    if chinese_ratio > 0.1:
        # 百度/搜狗结果质量低（SERP 跳转链为主），仅作印证不主动纳入；
        # 中文补充源只用 local_bing/local_duckduckgo
        return [e for e in ["local_bing", "local_duckduckgo"] if e in sub_engines]
    elif features.get("has_technical"):
        return [e for e in ["local_github", "local_stackoverflow", "local_bing"] if e in sub_engines]
    elif features.get("has_depth_word"):
        return [e for e in ["local_arxiv", "local_semantic_scholar", "local_bing"] if e in sub_engines]
    else:
        return [e for e in ["local_bing", "local_duckduckgo", "local_mojeek"] if e in sub_engines]


def _get_engines_combo(domain: dict[str, Any], enabled: set[str], mode: str = "auto",
                       features: dict | None = None) -> list[str]:
    """从域配置获取 engines_combo，过滤不可用/付费（budget 模式）。
    自动将 local_search 扩展为子引擎（消灭黑盒）。

    注意：depth/context 的 combo 预算与 research_only 截断在 route_query 末尾
    统一走 engine_policy.filter_combo_by_policy，本函数只做可用性/成本/健康过滤。
    """
    combo = domain.get("engines_combo", [])
    if combo:
        filtered = [e for e in combo if e in enabled]
    else:
        primary = domain.get("primary", "anysearch")
        fallback = domain.get("fallback")
        engines = [primary]
        if fallback and fallback != primary:
            engines.append(fallback)
        filtered = [e for e in engines if e in enabled]

    # 🔑 关键改动：将 local_search 扩展为子引擎
    if "local_search" in filtered:
        filtered = _expand_local_search(filtered, features)

    # fast/budget 模式过滤付费引擎
    if mode in ("fast", "budget"):
        quota_mgr = get_quota_manager()
        filtered = [e for e in filtered if quota_mgr.is_available(e, mode=mode)]

    # fast/budget 模式优先前置零成本子引擎
    if mode in ("fast", "budget"):
        free_locals = [e for e in filtered if e.startswith("local_")]
        others = [e for e in filtered if not e.startswith("local_")]
        filtered = free_locals + others

    # fast 模式：只保留免费引擎（条数预算交给 engine_policy）
    if mode == "fast":
        from config import get_cost_factor
        filtered = [e for e in filtered if get_cost_factor(e) >= 0.85]

    # 自适应学习过滤（保留主引擎不被过滤）
    if _adaptive_learner is not None and len(filtered) > 1:
        original = filtered[:]
        primary = domain.get("primary")
        filtered = [e for e in filtered if e == primary or _adaptive_learner.get_score(e) >= 0.3]
        if not filtered:
            filtered = original

    # 健康检查过滤
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
            healthy = [e for e in filtered if get_engine_status(e).get("available", True)]
            if healthy:
                filtered = healthy
        except ImportError:
            pass

    # ── 配额/熔断感知沉底：主引擎不可用时自动切换相近备选 ──────────────
    # 正常路径（全部引擎可用）顺序不变 → 引擎集合不变 → 缓存键不变 → 零速度倒退。
    # 仅当主引擎配额耗尽或熔断打开时沉底，主引擎位置自动落到第一个可用的
    # 相近备选（同一域 combo 内的其他引擎，天然是同主题的备选源）。
    if len(filtered) > 1:
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
                    st = get_breaker().status(e)
                    if st.get("state") == "open":
                        ok = False
                except ImportError:
                    pass
                except Exception:
                    pass
            (usable if ok else unusable).append(e)
        # 首位不可用才重排（避免无谓的顺序扰动）；重排保持集合不变
        if usable and unusable and filtered[0] in unusable:
            filtered = usable + unusable

    return filtered


def _apply_engine_policy(
    engines_combo: list[str],
    *,
    mode: str = "auto",
    depth: str = "fast",
    context: str = "search",
    engines_boost: list[str] | None = None,
    enabled: set[str] | None = None,
    must_keep: list[str] | None = None,
) -> list[str]:
    """boost 垂直源 + tier/budget 截断（单一策略入口）。

    must_keep：预算截断后仍强制保留的引擎（如 geo 的 local_openstreetmap），
    必要时从尾部腾位，避免特化源被 budget 裁掉。
    """
    try:
        from engine_policy import boost_into_combo, combo_budget, filter_combo_by_policy
    except ImportError:
        return engines_combo
    out = list(engines_combo or [])
    if engines_boost:
        out = boost_into_combo(out, engines_boost, enabled=enabled)
    out = filter_combo_by_policy(out, mode=mode, depth=depth, context=context)
    if must_keep:
        budget = combo_budget(mode=mode, depth=depth, context=context)
        for e in must_keep:
            if not e or e in out:
                continue
            if enabled is not None and e not in enabled:
                continue
            if budget is not None and len(out) >= budget:
                # 保留首位主源，替换末位
                if len(out) <= 1:
                    out.append(e)
                else:
                    out = out[:-1] + [e]
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


# ── 路由主函数 ─────────────────────────────────────────────────────────────────

def route_query(query: str, engine_override: str = "auto",
                mode: str = "auto",
                depth: str = "fast",
                context: str = "search",
                engines_boost: list[str] | None = None) -> dict[str, Any]:
    """路由决策主函数。

    Args:
        query: 查询词
        engine_override: 用户指定引擎
        mode: 预算模式 (fast/auto/deep/budget)
        depth: 搜索深度 (fast/balanced/deep)，参与 combo 预算
        context: search | research；research 放行 research_only 且不截断 combo
        engines_boost: 垂直引擎前置（研究子查询 boost，不锁死单引擎）

    Returns:
        dict: {engine, engines, engines_combo, reason, confidence, domain, ...}
    """
    start = time.perf_counter()

    def _done(**kw: Any) -> dict[str, Any]:
        base = {"elapsed_ms": round((time.perf_counter() - start) * 1000, 3)}
        base.update(kw)
        return base

    if engine_override != "auto":
        return _done(
            engine=engine_override, engines=[engine_override],
            engines_combo=[engine_override],
            reason=f"用户指定: {engine_override}", confidence=1.0,
            features={}, domain=None, parallel=False, mode=mode,
            depth=depth, context=context,
        )

    features = extract_features(query)
    cfg = load_config()
    # 自动路由：仅启用且 env 就绪、未 blocked 的引擎
    try:
        enabled = set(get_engines(cfg, routable_only=True).keys())
    except TypeError:
        enabled = set(get_engines(cfg).keys())
    # 若过滤过狠导致空集，回退到 enabled 全集（避免完全不可用）
    if not enabled:
        enabled = set(get_engines(cfg).keys())

    # 预算模式过滤可用引擎
    quota_mgr = get_quota_manager()
    if mode in ("fast", "budget"):
        enabled = {e for e in enabled if quota_mgr.is_available(e, mode=mode)}

    # 正则硬规则优先（cheap）；fast + 实域命中时跳过 TF-IDF，省掉语义路由开销
    domains_cfg = get_domains(cfg)
    domain = match_domain(query, domains_cfg)
    hard_domain = bool(domain and domain.get("patterns"))

    TFIDF_MIN_SCORE = 0.12
    SOCIAL_ENGINES = {"twitter", "reddit", "xiaohongshu", "bilibili", "weibo"}
    tfidf_best = None
    tfidf_best_score = 0.0
    tfidf_scores: list = []
    skip_tfidf = mode == "fast" and hard_domain

    if not skip_tfidf:
        try:
            tfidf_scores = semantic_route(query, top_k=3)
            if tfidf_scores:
                cand, score, _ = tfidf_scores[0]
                social_ok = True
                if cand in SOCIAL_ENGINES:
                    ql = query.lower()
                    social_signals = (
                        "微博", "小红书", "推特", "twitter", "reddit", "舆情",
                        "讨论", "网友", "评论", "b站", "bilibili", "抖音",
                    )
                    social_ok = any(s in ql for s in social_signals)
                if score >= TFIDF_MIN_SCORE and social_ok:
                    tfidf_best = cand
                    tfidf_best_score = score
                else:
                    tfidf_best = None
                    tfidf_best_score = score
        except ImportError:
            pass
        except Exception as e:
            import logging
            logging.getLogger("unified_search.route").debug(
                f"TF-IDF 路由跳过: {type(e).__name__}"
            )

    if domain:
        engines_combo = _get_engines_combo(domain, enabled, mode, features)
        # 🔑 中文查询 + 学术类域 → 剔除英文论文源（openalex/europepmc 对中文查询噪声大）
        if (domain.get("name") in ("tech_deep", "academic")
                and any("\u4e00" <= ch <= "\u9fff" for ch in query)):
            engines_combo = [e for e in engines_combo
                             if e not in ("openalex", "europepmc")]
            if not engines_combo:
                engines_combo = [e for e in ["arxiv", "anysearch", "local_search"]
                                 if e in enabled]
        # 🔑 macro_data 域 + 非美国国家词 → worldbank 前置（FRED 无该国数据，
        # 且错误结果会触发 early-stop 短路，导致「中国GDP」只回美国数据）
        if (domain.get("name") == "macro_data"
                and is_foreign_macro_query(query)
                and "worldbank" in engines_combo):
            engines_combo = ["worldbank"] + [e for e in engines_combo if e != "worldbank"]
        # 🔑 为中文/学术查询追加本地引擎
        engines_combo = _add_language_engines(engines_combo, features)
        if not engines_combo:
            # 域内引擎全被过滤，回退
            engines_combo = [e for e in ["local_search", "anysearch", "duckduckgo"] if e in enabled]
            if not engines_combo:
                engines_combo = sorted(enabled)[:2] if enabled else ["anysearch"]
            # 扩展 local_search → 子引擎
            engines_combo = _expand_local_search(engines_combo, features)

        # TF-IDF 验证 + catch-all 修复（仅高分才覆写）
        is_catch_all = not domain.get("patterns", [])  # 无模式 = 兜底域

        if tfidf_best and tfidf_best in engines_combo:
            confidence = 0.95
        elif tfidf_best and tfidf_best != engines_combo[0]:
            confidence = 0.8
            # catch-all 域 + TF-IDF 高置信度推荐 → 注入推荐引擎到首位
            if is_catch_all and tfidf_best_score > 0.15 and tfidf_best in enabled:
                engines_combo = [tfidf_best] + [e for e in engines_combo if e != tfidf_best]
                confidence = 0.85
        else:
            confidence = 0.9
            # catch-all 域 + TF-IDF 推荐但不在 combo 中 → 前置
            if is_catch_all and tfidf_best and tfidf_best_score > 0.15 and tfidf_best in enabled:
                engines_combo.insert(0, tfidf_best)
                confidence = 0.8

        # P0-001：geo 查询追加 OpenStreetMap
        engines_combo = _maybe_add_geo_engine(engines_combo, features, enabled)

        parallel = bool(domain.get("parallel", False)) or len(engines_combo) > 2
        # fast 模式强制串行，先 local_search 成功即避免额外 HTTP 开销
        if mode == "fast":
            parallel = False

        # P0-005：意图驱动动态并行度（覆写域默认 parallel）
        engines_combo, parallel = _apply_intent_parallelism(
            engines_combo, features, domain, mode, parallel)

        # P0：boost + tier/budget（depth/context）— 放在意图裁剪之后统一截断
        must_keep = []
        if features.get("has_geo") and "local_openstreetmap" in enabled:
            must_keep.append("local_openstreetmap")
        engines_combo = _apply_engine_policy(
            engines_combo, mode=mode, depth=depth, context=context,
            engines_boost=engines_boost, enabled=enabled, must_keep=must_keep,
        )
        if not engines_combo:
            engines_combo = [e for e in ["anysearch", "duckduckgo"] if e in enabled] or ["anysearch"]
        # budget 截断后对齐 parallel，避免短 combo 仍开多余并行
        if mode == "fast" or len(engines_combo) <= 1:
            parallel = False
        elif len(engines_combo) <= 2 and not domain.get("parallel", False):
            # 双引擎默认串行，利于 early-stop（答案域）
            parallel = parallel and len(engines_combo) > 2

        return _done(
            engine=engines_combo[0],
            engines=engines_combo,
            engines_combo=engines_combo,
            engines_fallback=[e for e in enabled if e not in engines_combo],
            reason=(
                f"{_feature_labels(features)} → 命中域 [{domain.get('name', '?')}]"
                + (f" [TF-IDF→{tfidf_best}]" if tfidf_best else "")
                + (f" [TF-IDF覆写catch-all]" if is_catch_all and tfidf_best and tfidf_best_score > 0.15 and tfidf_best in engines_combo else "")
                + (f" [boost={engines_boost}]" if engines_boost else "")
                + f" → {_ENGINE_NAMES.get(engines_combo[0], engines_combo[0])}"
            ),
            confidence=confidence, features=features,
            domain=domain.get("name"), parallel=parallel,
            no_early_stop=bool(domain.get("no_early_stop", False)),
            early_stop_min_results=domain.get("early_stop_min_results"),
            tfidf_scores=[{"engine": n, "score": s} for n, s, _ in tfidf_scores],
            mode=mode, depth=depth, context=context,
        )

    # 正则未命中，用 TF-IDF 结果（已过滤低分）
    if tfidf_best and tfidf_best in enabled:
        engines_combo = [tfidf_best]
        if "anysearch" in enabled and "anysearch" not in engines_combo:
            engines_combo.append("anysearch")
        engines_combo = [e for e in engines_combo if e in enabled]
        # 🔑 展开 local_search → 子引擎
        engines_combo = _expand_local_search(engines_combo, features)
        # 🔑 为中文/学术查询追加本地引擎
        engines_combo = _add_language_engines(engines_combo, features)
        # P0-001：geo 查询追加 OpenStreetMap
        engines_combo = _maybe_add_geo_engine(engines_combo, features, enabled)
        if mode == "fast":
            parallel = False
        else:
            parallel = len(engines_combo) > 1

        # P0-005：意图驱动动态并行度
        engines_combo, parallel = _apply_intent_parallelism(
            engines_combo, features, None, mode, parallel)

        must_keep = []
        if features.get("has_geo") and "local_openstreetmap" in enabled:
            must_keep.append("local_openstreetmap")
        engines_combo = _apply_engine_policy(
            engines_combo, mode=mode, depth=depth, context=context,
            engines_boost=engines_boost, enabled=enabled, must_keep=must_keep,
        )
        if not engines_combo:
            engines_combo = [e for e in ["anysearch", "duckduckgo"] if e in enabled] or ["anysearch"]
        if mode == "fast":
            parallel = False
        else:
            parallel = len(engines_combo) > 1

        return _done(
            engine=engines_combo[0],
            engines=engines_combo,
            engines_combo=engines_combo,
            reason=(
                f"TF-IDF 语义路由 → {_ENGINE_NAMES.get(engines_combo[0], engines_combo[0])}"
                f" (score={tfidf_best_score:.3f}, 正则未命中)"
                + (f" [boost={engines_boost}]" if engines_boost else "")
            ),
            confidence=0.85, features=features, domain=None,
            parallel=parallel,
            tfidf_scores=[{"engine": n, "score": s} for n, s, _ in tfidf_scores],
            mode=mode, depth=depth, context=context,
        )

    # 兜底：免费通用引擎（零分 TF-IDF 也走这里）
    fallback_combo = [e for e in ["local_search", "anysearch", "duckduckgo", "local_bing"] if e in enabled]
    if not fallback_combo:
        fallback_combo = sorted(enabled)[:2] if enabled else ["anysearch"]
    fallback_combo = _expand_local_search(fallback_combo, features)
    fallback_combo = _add_language_engines(fallback_combo, features)
    # P0-001：geo 查询追加 OpenStreetMap
    fallback_combo = _maybe_add_geo_engine(fallback_combo, features, enabled)
    must_keep_fb = []
    if features.get("has_geo") and "local_openstreetmap" in enabled:
        must_keep_fb.append("local_openstreetmap")
    fallback_combo = _apply_engine_policy(
        fallback_combo, mode=mode, depth=depth, context=context,
        engines_boost=engines_boost, enabled=enabled, must_keep=must_keep_fb,
    )
    if not fallback_combo:
        fallback_combo = ["anysearch"]

    low = tfidf_scores and all(s[1] < TFIDF_MIN_SCORE for s in tfidf_scores)
    reason = (
        f"TF-IDF 低分回退通用引擎 → {_ENGINE_NAMES.get(fallback_combo[0], fallback_combo[0])}"
        if low else
        f"无匹配域，回退 {_ENGINE_NAMES.get(fallback_combo[0], fallback_combo[0])}"
    )

    return _done(
        engine=fallback_combo[0],
        engines=fallback_combo,
        engines_combo=fallback_combo,
        engines_fallback=[],
        reason=reason,
        confidence=0.35 if low else 0.3,
        features=features, domain="general_search",
        parallel=False if mode == "fast" else len(fallback_combo) > 1,
        tfidf_scores=[{"engine": n, "score": s} for n, s, _ in tfidf_scores],
        mode=mode, depth=depth, context=context,
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Unified Search v2 路由器")
    parser.add_argument("query")
    parser.add_argument("--engine", default="auto")
    parser.add_argument("--mode", default="auto", choices=["fast", "auto", "deep", "budget"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    decision = route_query(args.query, engine_override=args.engine, mode=args.mode)
    if args.json:
        print(json.dumps(decision, ensure_ascii=False, indent=2))
    else:
        print(f"引擎: {decision['engine']}")
        print(f"组合: {decision.get('engines_combo', decision['engines'])}")
        print(f"原因: {decision['reason']}")
        print(f"置信度: {decision['confidence']:.2f}")
        print(f"耗时: {decision.get('elapsed_ms', 0):.3f} ms")


if __name__ == "__main__":
    _cli()

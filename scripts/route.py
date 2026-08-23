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

import os
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
    from engines_builders_data_macro import is_foreign_macro_query
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
_RE_KANA = re.compile(r"[\u3040-\u30ff]")
_RE_HANGUL = re.compile(r"[\uac00-\ud7af]")
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

# P2-3：显式语言意图 → 覆盖语言（「用英文搜」「in English」「日本語で」等）。
# 命中后 lang_override 直接决定语言引擎选择与 must_keep，不被习惯/系统 locale 淹没。
_LANG_OVERRIDE_MAP: dict[str, tuple[str, ...]] = {
    "en": ("用英文", "用英语", "in english", "english version", "english only", "英語で"),
    "ja": ("用日文", "用日语", "in japanese", "日本語で", "日本语"),
    "ko": ("用韩文", "用韩语", "in korean", "한국어로"),
    "zh": ("用中文", "用汉语", "in chinese", "中文版"),
    "cyrillic": ("用俄语", "用俄文", "in russian", "по-русски"),
}


def _detect_lang_override(query: str) -> str | None:
    """检测显式语言覆盖意图，返回目标语言（无则 None）。"""
    if not query:
        return None
    ql = query.lower()
    for lang, keys in _LANG_OVERRIDE_MAP.items():
        for key in keys:
            if key in ql:
                return lang
    return None

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

    多语种（v2.7）：primary_lang 判定主语言（zh/en/ja/ko/latin/cyrillic/thai/
    arabic/hebrew/greek/devanagari/mixed/other），script 给出书写系统类别，
    is_latin 标记是否为拉丁语系查询。跨语言回退依据：非拉丁书写系统的主语言
    在通用英文源覆盖可能不足，路由会追加英文通用源（duckduckgo/anysearch）。
    """
    total = len(query)
    chinese = len(_RE_CHINESE.findall(query))
    latin = len(re.findall(r"[A-Za-z]", query))
    ratio = chinese / max(total, 1)

    # 主语言判定统一走 lang_detect（假名/谚文/西里尔/泰/阿/希伯来/希腊强信号优先）
    primary_lang = "mixed"
    script = "other"
    is_latin = False
    try:
        from lang_detect import detect_language, detect_script
        primary_lang = detect_language(query)
        script = detect_script(query)
        is_latin = primary_lang in ("en", "latin")
    except ImportError:
        kana = len(_RE_KANA.findall(query))
        hangul = len(_RE_HANGUL.findall(query))
        if kana / max(total, 1) >= 0.15:
            primary_lang = "ja"
            script = "kana"
        elif hangul / max(total, 1) >= 0.15:
            primary_lang = "ko"
            script = "hangul"
        elif ratio > 0.3:
            primary_lang = "zh"
            script = "cjk"
        elif latin / max(total, 1) > 0.5:
            primary_lang = "en"
            script = "latin"
            is_latin = True
        else:
            primary_lang = "mixed"
            script = "mixed"

    features: dict[str, Any] = {
        "chinese_ratio": ratio,
        "english_ratio": latin / max(total, 1),
        "length": total,
        "primary_lang": primary_lang,
        "script": script,
        "is_latin": is_latin,
        # P2-3：显式语言覆盖（「用英文搜」→ en）；无覆盖意图时为 None
        "lang_override": _detect_lang_override(query),
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


# ── 登录态意图检测（P0-4：五路协同的种子）────────────────────────────────
# 公开引擎拿不到登录态内容（收藏/关注/持仓/私密等）。route 只做标注不阻塞执行，
# 上层（CLI/MCP 调用方）看到 login_hint 后可引导登录态搜索补充。
# 判定分级：强信号词任意域触发；弱信号词仅登录敏感域触发（避免「如何注册账号」
# 这类公开查询误报）。

_LOGIN_STRONG_SIGNALS = (
    "我的关注", "我的收藏", "我的基金", "我的持仓", "我的订阅",
    "我的订单", "我的消息", "私密", "私有", "会员专享", "需要登录",
    "登录后", "关注列表", "收藏夹", "订阅列表",
)
_LOGIN_WEAK_SIGNALS = (
    "账号", "账户", "授权", "登录", "我的", "account", "login",
    "sign in", "members only", "subscription", "following", "favorites",
    "my ", "saved", "bookmarked", "private",
)
_LOGIN_SENSITIVE_DOMAINS = frozenset({
    "zhihu_content", "wechat_search", "social_search", "community",
    "user_profile",
})


def _detect_login_intent(query: str, domain_name: str | None) -> dict[str, Any]:
    """识别「可能需要登录态内容」的查询，返回 {needs_login, reason}。"""
    ql = query.lower()
    if any(s in query for s in _LOGIN_STRONG_SIGNALS):
        return {"needs_login": True,
                "reason": "含登录态强信号词（收藏/关注/持仓/私密等）"}
    if any(w in ql for w in _LOGIN_WEAK_SIGNALS):
        if domain_name in _LOGIN_SENSITIVE_DOMAINS:
            return {"needs_login": True,
                    "reason": f"登录敏感域[{domain_name}] + 弱信号"}
    return {"needs_login": False, "reason": ""}


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


# 语言门控白名单：这些域以中文内容为主，明确的非中文查询（ja/ko/en/latin 等主
# 语言）不应命中。日文/韩文查询常含汉字或谚文字符，易被中文泛内容域的 `[一-\u9fff]`
# 或单音节子串正则误爆（如韩语「비교」命中天气的「비」、日语汉字命中 chinese_general），
# 在 match_domains 层按主语言跳过即可修正。2026-08 修复。
_ZH_LANG_GATED_DOMAINS = frozenset({
    "chinese_general", "local_chinese", "chinese_tech_deep",
    "cn_tech_community", "zhihu_content", "zhihu_hot_list",
    "wechat_search", "cn_encyclopedia", "cn_ai_news",
    "moegirl", "juejin", "bilibili", "weibo",
})


def match_domains(query: str, domains: list[dict[str, Any]] | None = None,
                  max_n: int = 3,
                  primary_lang: str | None = None) -> list[dict[str, Any]]:
    """按 config.yaml domains 顺序返回全部命中域（多意图，主域 1 + 次域 max_n-1）。

    旧 match_domain 单射只取首个命中域，多意图查询（如「北京 AI 公司融资」同时
    命中 geo/finance/tech）只走一个域。本函数返回命中列表供 route 主域执行 +
    次域按预算补充。catch-all（无 patterns）只做垫底：有命中时不掺入。
    max_n 限制命中数，防止正则宽泛的域批量命中稀释主域。

    primary_lang：查询主语言（来自 extract_features）。当为明确的非中文语言
    （ja/ko/en/latin/cyrillic/thai 等）时，跳过中文内容域白名单，避免韩/日查询
    被中文泛内容域误捕获。传 None 时不做门控（兼容旧调用方）。
    """
    if domains is None:
        domains = get_domains()
    compiled = _get_compiled_domains(domains)
    hits: list[dict[str, Any]] = []
    catch_all: dict[str, Any] | None = None
    non_zh = bool(primary_lang and primary_lang not in ("zh", "mixed", "other"))
    for domain in compiled:
        if not domain.get("patterns", []):
            catch_all = domain
            continue
        if non_zh and domain.get("name") in _ZH_LANG_GATED_DOMAINS:
            continue
        for regex in domain["_compiled"]:
            if regex.search(query):
                hits.append(domain)
                break
        if len(hits) >= max_n:
            break
    return hits if hits else ([catch_all] if catch_all else [])


def match_domain(query: str, domains: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """兼容旧接口：返回首个命中域（或 catch-all）。"""
    hits = match_domains(query, domains, max_n=1)
    return hits[0] if hits else None


def _enabled_local_engines() -> list[str]:
    """返回已注册（enabled）的本地子引擎名。

    路由选择的引擎必须能被执行层真正调用。list_local_engines(available_only=False)
    返回全部子引擎（含 config.yaml enabled:false 的，如 local_yandex/local_google），
    若路由选到这些引擎，执行层注册表里不存在 → 「未知引擎」空跑。
    这里以 config.yaml 的 enabled 字段为准过滤，保证路由与执行一致。
    """
    if _get_registry is None:
        return []
    try:
        from config import get_engines, load_config
        cfg = load_config()
        engines = get_engines(cfg) or {}
        return [
            e for e in _get_registry().list_local_engines(available_only=False)
            if isinstance(engines.get(e), dict) and engines[e].get("enabled", True)
        ]
    except Exception:
        return []


def _expand_local_search(engine_list: list[str], features: dict | None = None) -> list[str]:
    """将 local_search 扩展为具体的子引擎（基于查询特征）。"""
    if "local_search" not in engine_list:
        return engine_list
    if _get_registry is None:
        return engine_list

    registry = _get_registry()
    sub_engines = _enabled_local_engines()
    if not sub_engines:
        return engine_list

    selected = _select_sub_engines(sub_engines, features)
    result = [e for e in engine_list if e != "local_search"]
    for eng in selected:
        if eng not in result:
            result.append(eng)
    return result[:4]


def _general_fallback(enabled: set[str]) -> list[str]:
    """本地优先 + 通用免费源兜底（清单单一真源 engine_policy.GENERAL_FREE_FALLBACK）。

    域内引擎全被过滤 / 无匹配域时的回退组合：先 local_search（展开成本地子引擎），
    再通用免费源。清单与 recovery L3 共用同一常量，避免两处清单漂移。
    """
    try:
        from engine_policy import GENERAL_FREE_FALLBACK
    except ImportError:
        GENERAL_FREE_FALLBACK = ("anysearch", "duckduckgo", "local_bing")
    return ["local_search"] + [e for e in GENERAL_FREE_FALLBACK if e in enabled]


def _filter_breaker_blocked(engine_list: list[str]) -> list[str]:
    """剔除确定熔断态引擎（disabled / open 且冷却未过），与 _get_engines_combo
    内的熔断感知过滤同一语义（half_open 保留探测资格）。

    D4：语言引擎追加、通用兜底、TF-IDF 注入等路径在 _get_engines_combo 之外，
    追加的引擎可能处于熔断态仍进 combo，白占并行槽位。统一收口到最终组装后。
    """
    if not engine_list:
        return engine_list
    try:
        from circuit_breaker import get_breaker
        breaker = get_breaker()
    except Exception:
        return engine_list
    out = []
    for e in engine_list:
        try:
            st = breaker.status(e)
            st_state = st.get("state")
            if st_state == "disabled":
                continue
            if st_state == "open" and int(st.get("cooldown_remain") or 0) > 0:
                continue
        except Exception:
            pass
        out.append(e)
    return out


def _select_language_engines(features: dict | None = None) -> list[str]:
    """按查询语言选择应追加的语言本地引擎（P2-1 单一入口，与组合无关）。

    多语种（v2.7）：按 primary_lang 追加对应语言的本地引擎——
      中文 → local_bing；日文 → local_yandex（日文索引更好）或 local_bing；
      韩文 → local_google（韩国站点覆盖好）或 local_bing。
    只做选择（已按 enabled 过滤、最多 2 个），不碰既有 combo；
    route_query 内一次计算、三处合并共用，杜绝各路径逻辑漂移。
    """
    if _get_registry is None or not features:
        return []

    primary_lang = features.get("primary_lang", "")
    chinese_ratio = features.get("chinese_ratio", 0)
    sub_engines = _enabled_local_engines()
    if not sub_engines:
        return []

    # P2-3：显式语言覆盖优先——「用英文搜 苹果」即使含中文也按 en 选引擎
    lang_override = features.get("lang_override")
    if lang_override:
        if lang_override == "ja":
            return [e for e in ["local_yandex", "local_bing", "local_duckduckgo"]
                    if e in sub_engines][:2]
        if lang_override == "ko":
            # local_google 已禁用（反爬强），不再引用死引擎
            return [e for e in ["local_bing", "local_duckduckgo"]
                    if e in sub_engines][:2]
        if lang_override == "zh":
            return [e for e in ["local_bing"] if e in sub_engines]
        # en / cyrillic / 其他：中英基线本地引擎（动态 setlang 兜底多语言索引）
        return [e for e in ["local_bing", "local_duckduckgo"] if e in sub_engines]

    # 日/韩：优先对应语言本地引擎。注：local_yandex 走 ddgs yandex 后端
    # （实测 ~2s 可用）；local_google 默认 enabled:false，不引用死引擎。
    if primary_lang == "ja":
        return [e for e in ["local_yandex", "local_bing", "local_duckduckgo"]
                if e in sub_engines][:2]
    if primary_lang == "ko":
        return [e for e in ["local_bing", "local_duckduckgo"]
                if e in sub_engines][:2]

    if chinese_ratio > 0.1:
        # 只要含中文字符就追加中文引擎（阈值 0.1 覆盖中英混合查询）
        # 百度/搜狗质量低，仅作印证；自动追加只用 local_bing
        return [e for e in ["local_bing"] if e in sub_engines]
    if primary_lang in (
        "cyrillic", "thai", "arabic", "hebrew", "greek", "devanagari",
    ):
        # 其他非拉丁语：local_bing 靠动态 setlang 吃多语言索引
        return [e for e in ["local_bing", "local_duckduckgo"] if e in sub_engines]
    if primary_lang in ("mixed", "other", ""):
        # 弱信号：按 lang_pref（习惯/系统/中英基线）选本地引擎
        prefer: list[str] = []
        try:
            from lang_pref import prefer_langs
            prefer = prefer_langs(query_lang=primary_lang)
        except ImportError:
            prefer = ["zh", "en"]
        top = prefer[0] if prefer else "en"
        if top == "ja":
            return [e for e in ["local_yandex", "local_bing"] if e in sub_engines]
        if top == "ko":
            return [e for e in ["local_google", "local_bing"] if e in sub_engines]
        if top == "zh":
            return [e for e in ["local_bing"] if e in sub_engines]
        return [e for e in ["local_bing", "local_duckduckgo"] if e in sub_engines]
    if features.get("has_depth_word"):
        return [e for e in ["local_arxiv", "local_semantic_scholar"] if e in sub_engines]
    return []


def _merge_language_engines(engine_list: list[str], features: dict | None,
                            lang_engines: list[str]) -> list[str]:
    """把预选语言引擎合并进当前 combo（P2-1，三处共用、幂等）。

    日/韩：中文域引擎（byted/bocha 等）是噪声源需剔除，且不因「combo 已有
      local_ 引擎」而跳过——中文引擎对日韩查询无用；
    其他语种：combo 已含 local_ 引擎则跳过（避免与 _expand_local_search 重复追加）。
    """
    if not features:
        return engine_list
    result = list(engine_list)
    # P2-3：显式语言覆盖 ja/ko 与主语言 ja/ko 同等对待（噪声剔除同一套规则）
    if (features.get("lang_override") or features.get("primary_lang")) in ("ja", "ko"):
        cn_noise = {"bocha", "byted", "wechat_sogou", "zhihu", "zhihu_global", "baidu_baike"}
        result = [e for e in result if e not in cn_noise]
        for eng in lang_engines[:2]:
            if eng not in result:
                result.append(eng)
        return result
    # 已包含 local_ 引擎则跳过
    if any(e.startswith("local_") for e in result):
        return result
    for eng in lang_engines[:2]:
        if eng not in result:
            result.append(eng)
    return result


def _add_language_engines(engine_list: list[str], features: dict | None = None) -> list[str]:
    """为已路由的查询添加语言相关的本地引擎（补充源，兼容入口）。

    route_query 内已改为「先 _select_language_engines 一次、再
    _merge_language_engines 三处共用」；本函数保留独立调用能力（幂等），
    供外部或未来调用方使用。
    """
    return _merge_language_engines(engine_list, features, _select_language_engines(features))


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


# 仅日/韩需要 must_keep：域主引擎常是中文噪声源，语言补充源不能被 budget 裁掉。
# 中文 / 其它语种：_merge_language_engines 软追加即可，must_keep 会与垂直域抢预算
# （实测：zh must_keep local_bing 会把 finance_macro 多源压成单源、挤掉 openstreetmap）。
_LANG_PREFERRED_ENGINES: dict[str, list[str]] = {
    "ja": ["local_yandex", "local_bing"],
    "ko": ["local_google", "local_bing"],
}


def _lang_must_keep(features: dict | None, enabled: set[str]) -> list[str]:
    """返回语言相关的 must_keep 引擎（仅日/韩）。

    专用源（yandex/google）默认 disabled 时落到 local_bing；
    多语言结果质量仍靠 engines_base 动态 setlang，不依赖强制占位。
    """
    if not features or not enabled:
        return []
    # P2-3：显式语言覆盖（用日文搜/用韩语搜）与主语言同等进入 must_keep
    lang = features.get("lang_override", "") or features.get("primary_lang", "")
    preferred = _LANG_PREFERRED_ENGINES.get(lang, [])
    for eng in preferred:
        if eng in enabled:
            return [eng]
    return []


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
        # 默认兜底：快源优先（brave/yahoo 实测 ~1.1s），ddgs 默认后端慢不主动纳入
        return [e for e in ["local_bing", "local_brave", "local_yahoo", "local_duckduckgo"]
                if e in sub_engines]

    primary_lang = features.get("primary_lang", "")
    chinese_ratio = features.get("chinese_ratio", 0)

    # 多语种（v2.7）：日/韩查询优先对应语言的本地引擎
    if primary_lang == "ja":
        return [e for e in ["local_yandex", "local_bing", "local_duckduckgo"] if e in sub_engines]
    if primary_lang == "ko":
        return [e for e in ["local_google", "local_bing", "local_duckduckgo"] if e in sub_engines]
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
    # fallback（零额外开销）；仅当 primary 无结果/故障时才轮到 fallback 兜底。
    if fallback and fallback != primary and fallback in enabled and fallback not in filtered:
        filtered.append(fallback)

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

    # 垂直域主源保护
    # 实测：wikipedia 分数常 <0.3，org_entity 在 wikidata 熔断时会只剩 baidu，英文 HQ 题脏结果。
    # modal_card 的 bocha_ai/bocha 为 cost_tier=low（0.7），fast 的 0.85 阈值会误杀整 combo。
    _VERTICAL_PROTECT = frozenset({
        "film_search", "sports_search", "geo_places", "org_entity", "media_search",
        "modal_card",
    })
    primary = domain.get("primary")
    domain_name = domain.get("name")
    protect: set[str] = set()
    if primary:
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
        if domain_name in _VERTICAL_PROTECT:
            protect = set(original) | protect
        filtered = [
            e for e in filtered
            if e in protect or e == primary or _adaptive_learner.get_score(e) >= 0.3
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
        except ImportError:
            family_of = None

        def _fam(e: str) -> str:
            try:
                return family_of(e) if family_of else "?"
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
    # 集合收缩，交由 route_query 尾部通用兜底 / recovery 按 family 门禁补源。
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
                st = get_breaker().status(e)
                st_state = st.get("state")
                # 自适应禁用：disabled 引擎直接不可用（持久跳过）
                if st_state == "disabled":
                    ok = False
                elif st_state == "open" and int(st.get("cooldown_remain") or 0) > 0:
                    ok = False
            except ImportError:
                pass
            except Exception:
                pass
        (usable if ok else unusable).append(e)
    if unusable and usable:
        filtered = usable
    elif unusable and not usable:
        # 域内全部不可用：返回空集，由 route_query 尾部兜底（通用免费源 /
        # modal_card 保留声明引擎供执行层返回 error item）
        filtered = []

    # ── 能力族去重 + 互补回填（标准化调用契约）────────────────────────
    # 全网搜索族同质化最高（byted/bocha/duckduckgo/octen 都是通用网页检索），
    # 同族堆叠纯属浪费预算位：web_general 至多保留 2 个，垂直族保留多源。
    # config.yaml 引擎声明的 family 字段是真源（spec_lookup 传入 family_of，
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


# ── 路由主函数 ─────────────────────────────────────────────────────────────────

# P2-6：语言路由采样——按采样率记录决策结果（features 齐全的决策点）。
# 默认 1/20，ARGO_ROUTE_SAMPLE_RATE 可调；采样本身失败静默。
_ROUTE_SAMPLE_RATE = max(1, int(os.environ.get("ARGO_ROUTE_SAMPLE_RATE", "20")))
_route_sample_counter = 0


def _sample_route(done: dict[str, Any], kw: dict[str, Any]) -> None:
    """P2-6：按采样率把路由决策落一条遥测记录。"""
    global _route_sample_counter
    if "features" not in kw or not kw.get("features"):
        return  # engine_override 直通等无语义分支不采样
    _route_sample_counter += 1
    if _route_sample_counter % _ROUTE_SAMPLE_RATE != 0:
        return
    f = kw.get("features") or {}
    try:
        from telemetry import emit
        emit("route", {
            "domain": kw.get("domain"),
            "engine": kw.get("engine"),
            "engines": kw.get("engines"),
            "confidence": kw.get("confidence"),
            "mode": kw.get("mode"),
            "lang_override": f.get("lang_override"),
            "primary_lang": f.get("primary_lang"),
            "script": f.get("script"),
            "has_compare": f.get("has_compare"),
            "has_technical": f.get("has_technical"),
            "chinese_ratio": f.get("chinese_ratio"),
            "intents": f.get("intents"),
        })
    except Exception:
        pass


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
        _sample_route(base, kw)
        return base

    if engine_override and engine_override != "auto":
        # 空串等同 auto：防止空引擎名混入 combo（registry 查无 → 空结果
        # → 熔断器空键 ''），MCP/外部调用可能传入空 engine 参数
        return _done(
            engine=engine_override, engines=[engine_override],
            engines_combo=[engine_override],
            reason=f"用户指定: {engine_override}", confidence=1.0,
            features={}, domain=None, parallel=False, mode=mode,
            depth=depth, context=context,
            login_hint=_detect_login_intent(query, None),
        )

    features = extract_features(query)
    # P2-1：语言引擎选择单一入口——route_query 内只计算一次，
    # 主路径与两条回退路径共用同一结果，杜绝各路径逻辑漂移
    lang_engines = _select_language_engines(features)
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
    # P1-1：多意图路由——主域执行 + 次域按预算补充（仅域命中分支消费 secondary）
    _domain_hits = match_domains(query, domains_cfg,
                                 primary_lang=features.get("primary_lang"))
    domain = _domain_hits[0] if _domain_hits else None
    secondary = _domain_hits[1:] if len(_domain_hits) > 1 else []
    hard_domain = bool(domain and domain.get("patterns"))

    TFIDF_MIN_SCORE = 0.12
    SOCIAL_ENGINES = {
        "twitter", "reddit", "xiaohongshu", "bilibili", "weibo",
        "zhihu", "hackernews", "v2ex",
    }
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
        # modal_card 保持纯结构化路径：只走 bocha_ai → bocha，不混 web/geo 补充源
        _pure_combo = domain.get("name") == "modal_card"
        if not _pure_combo:
            engines_combo = _merge_language_engines(engines_combo, features, lang_engines)
        if not engines_combo:
            if _pure_combo:
                # 密钥缺失时 env_ready 会踢 combo；仍保留域声明引擎，
                # 执行层返回 error item，避免静默改走 anysearch 污染结构化语义
                declared = list(domain.get("engines_combo") or [])
                if not declared and domain.get("primary"):
                    declared = [domain["primary"]]
                try:
                    from engine_env import is_engine_allowed_by_env
                    engines_combo = [
                        e for e in declared if is_engine_allowed_by_env(e)
                    ] or declared
                except ImportError:
                    engines_combo = declared
            if not engines_combo:
                # 域内引擎全被过滤，回退（本地优先 + 通用免费源单一真源）
                engines_combo = _general_fallback(enabled)
                if not engines_combo:
                    engines_combo = sorted(enabled)[:2] if enabled else ["anysearch"]
                # 扩展 local_search → 子引擎
                engines_combo = _expand_local_search(engines_combo, features)

        # TF-IDF 验证 + catch-all 修复（仅高分才覆写）
        is_catch_all = not domain.get("patterns", [])  # 无模式 = 兜底域

        # 强语义注入的通用引擎黑名单：这些引擎已被域 combo 覆盖，注入会喧宾夺主
        _GENERAL_ENGINES = {
            "anysearch", "byted", "bocha", "octen", "duckduckgo",
            "local_search", "zhihu", "wechat_sogou", "uapi", "tavily",
            "brave", "bocha_ai", "google_scholar", "arxiv", "wikipedia",
        }

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

        # P0-001：geo 查询追加 OpenStreetMap（模态卡域跳过，避免稀释结构化路径）
        if not _pure_combo:
            engines_combo = _maybe_add_geo_engine(engines_combo, features, enabled)

        # P1-1：多意图补充——次域 primary 在预算内补充（追加尾部，不占主位）。
        # 预算截断由 _apply_engine_policy 完成；web_general 族计数门禁防同质堆叠
        # （与 _get_engines_combo 的能力族去重语义一致）。modal_card 纯结构化路径
        # 不混入次域源。次域引擎不受 must_keep 保护，预算紧张时自然被裁。
        if secondary and not _pure_combo:
            try:
                from engine_families import family_of
                fam_count: dict[str, int] = {}
                for _e in engines_combo:
                    _f = family_of(_e)
                    fam_count[_f] = fam_count.get(_f, 0) + 1
            except Exception:
                fam_count = None
            for _sec in secondary:
                _sp = _sec.get("primary")
                if not _sp or _sp not in enabled or _sp in engines_combo:
                    continue
                if fam_count is not None:
                    _f = family_of(_sp)
                    if _f == "web_general" and fam_count.get(_f, 0) >= 2:
                        continue
                    fam_count[_f] = fam_count.get(_f, 0) + 1
                engines_combo.append(_sp)
                if len(engines_combo) >= 4:
                    break

        parallel = bool(domain.get("parallel", False)) or len(engines_combo) > 2
        # fast 模式强制串行，先 local_search 成功即避免额外 HTTP 开销
        if mode == "fast":
            parallel = False

        # P0-005：意图驱动动态并行度（覆写域默认 parallel）
        engines_combo, parallel = _apply_intent_parallelism(
            engines_combo, features, domain, mode, parallel)

        # P0：boost + tier/budget（depth/context）— 放在意图裁剪之后统一截断
        must_keep = []
        if features.get("has_geo") and "local_openstreetmap" in enabled and not _pure_combo:
            must_keep.append("local_openstreetmap")
        # 垂直域主源保护：film/sports/geo/org/modal_card 的 primary（及 combo）不被 budget 裁掉
        _VERTICAL_KEEP = frozenset({
            "film_search", "sports_search", "geo_places", "org_entity", "media_search",
            "modal_card",
        })
        if domain.get("name") in _VERTICAL_KEEP:
            p = domain.get("primary")
            # modal_card 可在缺 key（不在 enabled）时仍 must_keep，避免 budget 再裁
            if p and p not in must_keep and (p in enabled or _pure_combo):
                must_keep.append(p)
            # modal_card 整 combo 保底（bocha_ai 无配额时 bocha 必须在位）
            if domain.get("name") == "modal_card":
                for e in domain.get("engines_combo") or []:
                    if e not in must_keep and (e in enabled or _pure_combo):
                        must_keep.append(e)
            elif p and p in enabled and p not in must_keep:
                must_keep.append(p)
            if domain.get("name") == "geo_places" and "local_openstreetmap" in enabled:
                if "local_openstreetmap" not in must_keep:
                    must_keep.append("local_openstreetmap")
        if not _pure_combo:
            must_keep.extend(_lang_must_keep(features, enabled))
        engines_combo = _apply_engine_policy(
            engines_combo, mode=mode, depth=depth, context=context,
            engines_boost=engines_boost, enabled=enabled, must_keep=must_keep,
        )
        # 域 primary 扶正：已在 combo 且未熔断时置首（不覆盖冷却中的熔断沉底）
        # open 但 cooldown 已过 → 允许扶正，交给 half-open 探测。
        p = domain.get("primary")
        # macro_data 非美国查询：worldbank 前置是领域语义（FRED 无该国数据，
        # 先跑 fred + early-stop 会拿美国数据冒充），primary 扶正不得覆盖。
        _foreign_macro = (
            domain.get("name") == "macro_data" and is_foreign_macro_query(query)
        )
        if (p and p in engines_combo and engines_combo[0] != p
                and not _foreign_macro):
            try:
                from circuit_breaker import get_breaker
                st = get_breaker().status(p)
                if st.get("state") == "open" and int(st.get("cooldown_remain") or 0) > 0:
                    p = None
            except Exception:
                pass
            if p and p in engines_combo:
                engines_combo = [p] + [e for e in engines_combo if e != p]

        # 强语义注入（v2.7.10）：TF-IDF 高分推荐放宽到所有域，位置在 primary
        # 扶正之后（否则被扶正压到第二位，串行 early-stop 下永远不执行）。
        # marginalia/open_meteo/usda/gov_policy/cnii 等 25 个垂直新引擎有
        # profile 文档，但正则域（chinese_general 等）命中后旧逻辑只对
        # catch-all 域注入，这些引擎永远选不中。分数≥0.6（远高于最低阈值
        # 0.12）表示查询与引擎文档强匹配，前置注入不锁死（域主源仍在尾部
        # 备位，注入引擎失败时自然补位）。
        if tfidf_best and tfidf_best_score >= 0.6 and not is_catch_all \
                and tfidf_best not in engines_combo \
                and tfidf_best not in _GENERAL_ENGINES \
                and tfidf_best in enabled:
            engines_combo = [tfidf_best] + [e for e in engines_combo if e != tfidf_best]
            confidence = 0.9
        # D4：统一熔断收口——语言/geo/次域/TF-IDF 追加的引擎也可能处于熔断态
        engines_combo = _filter_breaker_blocked(engines_combo)
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
            login_hint=_detect_login_intent(query, domain.get("name")),
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
        engines_combo = _merge_language_engines(engines_combo, features, lang_engines)
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
        must_keep.extend(_lang_must_keep(features, enabled))
        engines_combo = _apply_engine_policy(
            engines_combo, mode=mode, depth=depth, context=context,
            engines_boost=engines_boost, enabled=enabled, must_keep=must_keep,
        )
        # D4：统一熔断收口（TF-IDF 注入/语言追加可能绕过 _get_engines_combo）
        engines_combo = _filter_breaker_blocked(engines_combo)
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
            confidence=0.85, features=features, domain="general_search",
            parallel=parallel,
            tfidf_scores=[{"engine": n, "score": s} for n, s, _ in tfidf_scores],
            mode=mode, depth=depth, context=context,
            login_hint=_detect_login_intent(query, None),
        )

    # 兜底：免费通用引擎（零分 TF-IDF 也走这里）——本地优先 + 通用免费源单一真源
    fallback_combo = _general_fallback(enabled)
    if not fallback_combo:
        fallback_combo = sorted(enabled)[:2] if enabled else ["anysearch"]
    # 日/韩主查询：直接用语言专用本地引擎组合，避免默认 local_bing（zh 参数）兜底
    if features.get("primary_lang") in ("ja", "ko") and _get_registry is not None:
        lang_combo = _select_sub_engines(_enabled_local_engines(), features)
        non_local = [e for e in fallback_combo if not e.startswith("local_")]
        fallback_combo = (lang_combo[:2] + non_local) if lang_combo else fallback_combo
    else:
        fallback_combo = _expand_local_search(fallback_combo, features)
    fallback_combo = _merge_language_engines(fallback_combo, features, lang_engines)
    # P0-001：geo 查询追加 OpenStreetMap
    fallback_combo = _maybe_add_geo_engine(fallback_combo, features, enabled)
    must_keep_fb = []
    if features.get("has_geo") and "local_openstreetmap" in enabled:
        must_keep_fb.append("local_openstreetmap")
    must_keep_fb.extend(_lang_must_keep(features, enabled))
    fallback_combo = _apply_engine_policy(
        fallback_combo, mode=mode, depth=depth, context=context,
        engines_boost=engines_boost, enabled=enabled, must_keep=must_keep_fb,
    )
    # D4：统一熔断收口（兜底组合可能含熔断引擎）
    fallback_combo = _filter_breaker_blocked(fallback_combo)
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
        login_hint=_detect_login_intent(query, None),
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

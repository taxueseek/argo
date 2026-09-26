#!/usr/bin/env python3
"""route_lang.py — 语言判定与「按语言选源」策略的唯一实现。

## 为什么单独成模块

2026-09 的两次线上事故都长在这个交界处：韩语查询被中文内容域吃掉、日文查询
落进中文技术源、韩语查询被 TF-IDF 送进韩语法条库。共同点是「语言判定」与
「引擎选源」散在两处，改一边忘一边。现在语言相关的知识只有这一个家：

  - 查询侧：书写系统/主语言判定（`extract_features`）、语言标签、
    「用英文搜」这类显式语言覆盖；
  - 引擎侧：语言绑定源的筛选与排序（`_filter_lang_bound_family`、
    `_lang_aware_combo_order`、`_merge_language_engines`）、多语言主力源补位
    （`_inject_multilingual_backup`）、must_keep 的语言保护
    （`_lang_must_keep`）。

判据的单一真源仍是 `engine_families`（ENGINE_LANGS / lang_allows /
engines_not_for_lang）与 `lang_detect`（LANG_LABELS）；本模块只做「怎么用」。

依赖方向：route_lang 不导入 route（route 导入它）。需要 route 侧状态的地方
（如 engines 模块是否已加载）走 `sys.modules` 查询而不是导入。
"""

from __future__ import annotations

import re
import sys
from typing import Any

from config import get_engines, load_config
from engine_families import (
    engine_langs,
    engines_demote_for_lang,
    family_of,
    lang_allows,
    lang_hint_from_query,
)

try:
    from argo_engine_registry import get_registry as _get_registry
except Exception:  # 注册中心不可用时语言选源退化为「无本地子引擎」
    _get_registry = None


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
    # 语言标签取 features.primary_lang（lang_detect 单一真源），**不用
    # chinese_ratio 二分**：后者对谚文/假名/西里尔/阿拉伯查询一律给出
    # chinese_ratio≈0，于是「한국 반도체 산업」这类韩文查询在 route_reason 里
    # 被标成「英文」（2026-09-21 实测）。reason 是给人看的归因入口，标签错了
    # 会把排障引向错误方向（以为命中了英文源）。名称表复用 lang_detect.LANG_LABELS，
    # 不再在这里维护第二份语种名。
    # 阈值语义保持不变：cr>0.6 记中文、0.1~0.6 的混合查询不记语言标签——
    # 这次只修「cr<0.1 却断言是英文」那一条，不改变中英混合的既有形态。
    cr = features.get("chinese_ratio", 0)
    if cr > 0.6:
        labels.append("中文")
    elif cr < 0.1:
        labels.append(_lang_label(features.get("primary_lang")) or "英文")
    for key, name in (("has_technical", "技术向"), ("has_compare", "对比分析"),
                      ("has_depth_word", "深度研究"), ("has_question", "问答型")):
        if features.get(key):
            labels.append(name)
    return " + ".join(labels) if labels else "通用查询"


def _lang_label(lang: Any) -> str | None:
    """primary_lang → 中文名；en/latin/混合/未知返回 None（由调用方兜底）。

    名称表直接取自 lang_detect（单一真源），**不做进程内缓存**：本函数只在
    拼 reason 时调用一次，而 extract_features 每次路由本来就会 import
    lang_detect，多一次 sys.modules 查表是零成本；为它维护一个模块级可变
    缓存只会多一个需要解释的状态。
    """
    if not lang or lang in ("en", "latin", "mixed", "other"):
        return None
    try:
        from lang_detect import LANG_LABELS
    except ImportError:  # 语言模块不可用：不编造语种名，交给调用方兜底
        return None
    return LANG_LABELS.get(lang)


def _inject_multilingual_backup(engines_combo: list[str], enabled: set[str],
                                features: dict) -> list[str]:
    """ja/ko 查询把多语言主力源 anysearch 送到 combo 前二。

    必须在 _apply_engine_policy（预算截断/must_keep 换位）之后调用：此前的
    注入会被 must_keep 的尾位替换挤出去（2026-09-07 实测 geo 域 anysearch
    被 local_bing 顶掉）。hedged 执行下 #2 位=primary 慢或不及格时的第一
    救援；日韩查询过滤掉中文源后常只剩英文/本地源，缺位=无通用主力。

    注：这条规则只覆盖 ja/ko。其余语言的同类问题（垂直专源占住 combo 预算、
    通用保底源被截断剪掉）在声明来源层修，见 config.yaml 各域 engines_combo
    的顺序约定与 tests/test_combo_budget_coverage.py 检查。
    """
    if features.get("primary_lang") not in ("ja", "ko"):
        return engines_combo
    if "anysearch" not in enabled or not engines_combo:
        return engines_combo
    if "anysearch" in engines_combo:
        if engines_combo.index("anysearch") <= 1:
            return engines_combo
        engines_combo = [e for e in engines_combo if e != "anysearch"]
    return engines_combo[:1] + ["anysearch"] + engines_combo[1:]


def _enabled_local_engines() -> list[str]:
    """返回已注册（enabled）的本地子引擎名。

    路由选择的引擎必须能被执行层真正调用。list_local_engines(available_only=False)
    返回全部子引擎（已按 config.yaml enabled 字段过滤，禁用引擎不入选），
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


def _select_language_engines(features: dict | None = None) -> list[str]:
    """按查询语言选择应追加的语言本地引擎（P2-1 单一入口，与组合无关）。

    多语种（v2.7）：按 primary_lang 追加对应语言的本地引擎——
      中文/日文/韩文 → local_bing（动态 setlang 吃对应语言索引；yandex/
      google 直连 html 引擎已随本机可达性门下线，ddgs 后端路子在子技能侧保留）。
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
            return [e for e in ["local_bing"] if e in sub_engines][:2]
        if lang_override == "ko":
            return [e for e in ["local_bing"] if e in sub_engines][:2]
        if lang_override == "zh":
            return [e for e in ["local_bing"] if e in sub_engines]
        # en / cyrillic / 其他：动态 setlang 保底多语言索引
        return [e for e in ["local_bing"] if e in sub_engines]

    # 日/韩：local_bing 动态 setlang 承接（yandex/google 直连引擎已下线，
    # 可达性判决见 references/engines.md；ddgs 后端路子在子技能侧保留）。
    if primary_lang == "ja":
        return [e for e in ["local_bing"] if e in sub_engines][:2]
    if primary_lang == "ko":
        return [e for e in ["local_bing"] if e in sub_engines][:2]

    if chinese_ratio > 0.1:
        # 只要含中文字符就追加中文引擎（阈值 0.1 覆盖中英混合查询）
        # 百度/搜狗质量低，仅作印证；自动追加只用 local_bing
        return [e for e in ["local_bing"] if e in sub_engines]
    if primary_lang in (
        "cyrillic", "thai", "arabic", "hebrew", "greek", "devanagari",
    ):
        # 其他非拉丁语：local_bing 靠动态 setlang 吃多语言索引
        return [e for e in ["local_bing"] if e in sub_engines]
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
            return [e for e in ["local_bing"] if e in sub_engines]
        if top == "ko":
            return [e for e in ["local_bing"] if e in sub_engines]
        if top == "zh":
            return [e for e in ["local_bing"] if e in sub_engines]
        return [e for e in ["local_bing"] if e in sub_engines]
    if features.get("has_depth_word"):
        return [e for e in ["local_arxiv", "local_semantic_scholar"] if e in sub_engines]
    return []


def _merge_language_engines(engine_list: list[str], features: dict | None,
                            lang_engines: list[str]) -> list[str]:
    """把预选语言引擎合并进当前 combo（P2-1，三处共用、重复执行结果一致）。

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
    _merge_language_engines 三处共用」；本函数保留独立调用能力（重复执行结果一致），
    供外部或未来调用方使用。
    """
    return _merge_language_engines(engine_list, features, _select_language_engines(features))


_SOCIAL_ZH_GENERAL = ("anysearch", "local_bing")


def _specs_snapshot() -> dict:
    """引擎 spec 快照（读 config langs 覆盖）；不可用时回退 ENGINE_LANGS 表。

    **已加载才读，不为它去导入。** `engines` 连带 engines_base → urllib/http.client
    整条 HTTP 栈；而它模块级的 `_engine_specs` 只在 registry 被 `_load_registry()`
    填充，route 从不加载 registry——旧写法（`from engines import _engine_specs`）
    付了整笔导入的钱，拿回的却是一个空表，路由结果与「不导入」逐位相同
    （222 引擎全量比对：0 处差异；config 里声明 langs 的只有 6 个引擎，而
    回退的 ENGINE_LANGS 表有 42 条，空表反而走的是更全的那条路）。
    engines 真被导入过时（dispatch / available_engines 之后）两者取到的是
    同一个 dict 对象，语义不变。
    """
    mod = sys.modules.get("engines")
    if mod is None:
        return {}
    return getattr(mod, "_engine_specs", None) or {}


def _move_to_tail(combo: list[str], excluded) -> list[str]:
    """combo 中命中 excluded 的引擎稳定移尾（其余保持原顺序）。"""
    excl = set(excluded)
    keep = [e for e in combo if e not in excl]
    tail = [e for e in combo if e in excl]
    return keep + tail


def _lang_aware_combo_order(combo: list[str], features: dict | None,
                            domain_name: str | None,
                            enabled: set[str],
                            query: str = "") -> list[str]:
    """语言感知的 combo 排序（返回新列表，不改动传入）。

    双向对称：
      zh 查询：纯英文社区引擎稳定移尾；social 域把通用中文 web 源提到最前。
      en/ja/ko 查询：中文专用引擎稳定移尾（patterns 认关键词不认语言，
      英文查询命中 social 域时 zhihu 排头会垄断 budget + 早停）。
    排序发生在 engine_policy 截断之前，保证 budget 截断保留的是对查询
    语言有用的引擎。
    """
    if not combo or not features:
        return combo
    zh_ratio = features.get("chinese_ratio") or 0
    primary_lang = features.get("primary_lang")
    # 汉字占比保底不得命中 ja/ko：假名/谚文文本里的汉字同属 CJK 区段，
    # 纯 ratio 判定会把日文查询（汉字占比常 >0.15）误入中文分支，
    # 中文专用源占前排——与 query_rewriter 的语言门控计算方式一致。
    is_zh = primary_lang == "zh" or (
        primary_lang not in ("ja", "ko") and zh_ratio > 0.15)
    if is_zh:
        ordered = _move_to_tail(
            combo, engines_demote_for_lang(combo, "zh", _specs_snapshot()))
        if domain_name == "social":
            for g in _SOCIAL_ZH_GENERAL:
                if g in enabled and g in ordered:
                    ordered = [g] + [e for e in ordered if e != g]
                    break
        if domain_name == "zhihu_content" and "zhihu_global" in ordered:
            # 站内主搜 + 站外全网搜是成对语义：learner 同族按分重排会把
            # zhihu_global 挪到 anysearch 之后，叠加 auto 预算=2 即被截掉
            # （37 天仅 53 次的死因）。zh 查询下固定提回 #2。
            rest = [e for e in ordered if e not in ("zhihu", "zhihu_global")]
            if "zhihu" in ordered:
                ordered = ["zhihu", "zhihu_global"] + rest
            else:
                ordered = ["zhihu_global"] + rest
    # 对称分支：非中文查询把中文专用源移尾（含 zh_ratio≤0.15 的混合查询）
    elif primary_lang in ("en", "ja", "ko"):
        ordered = _move_to_tail(
            combo, engines_demote_for_lang(
                combo, primary_lang or "en", _specs_snapshot()))
    else:
        ordered = combo
    # 其余语言（ru/ar/es/pt/th/vi/…）此前完全不做降级：en/ja/ko/zh 在上方
    # 分支处理过，这里补上，否则 cinii（声明 ja）在中文查询里、kor_law
    # （声明 ko）在英文查询里都会占着原位。
    _lang = _family_lang(features, query)
    if _lang and _lang not in ("zh", "en", "ja", "ko", "mixed", "other"):
        ordered = _move_to_tail(
            ordered, engines_demote_for_lang(ordered, _lang, _specs_snapshot()))
    # 语言绑定的族（本地新闻流）按查询语言互斥——见函数 docstring 末段。
    return _filter_lang_bound_family(ordered, features, _specs_snapshot(), query)


def _family_lang(features: dict | None, query: str = "") -> str:
    """选源用的查询语言：lang_override > primary_lang > 查询实词兜底。

    第三档专门补拉丁字母语言的判定缺口（lang_detect 对 noticias/berita/
    tin tức 只给 en/latin），见 engine_families._LANG_HINT_WORDS 的说明。
    """
    lang = (features or {}).get("lang_override") or (features or {}).get("primary_lang") or ""
    if lang in ("", "en", "latin", "mixed", "other"):
        hint = lang_hint_from_query(query)
        if hint:
            return hint
    return lang


def _filter_lang_bound_family(combo: list[str], features: dict | None,
                              specs: dict[str, dict[str, Any]] | None,
                              query: str = "") -> list[str]:
    """语言绑定族的成员按查询语言互斥：匹配语言的留原位，其余整体移尾。

    与 hot_trending 不同，world_news 族（各国本地新闻流）是**语言绑定**的：
    俄语查询用韩联社只会拿到韩语新闻，不是「次优」而是「错」。同族内每个
    源各自服务一种语言，所以选源的第一判据是查询语言，不是 priority。

    **非匹配源一律摘除，不做「移尾保留」**——这是与 engines_demote_for_lang
    有意不同的一处。那条路径处理的是「同一份内容的不同语言版本」（降级），
    这条处理的是「完全不同的国家与语言」：俄语查询跑韩联社拿回的是韩语新闻，
    这不是次优而是错。实测 deep 档（不截断预算）下移尾方案会让韩语查询连跑
    elpais/tass/aljazeera 等 16 个源，白付网络与配额。

    摘除的代价是「语言判定失误时拿不到该语言源」，但 combo 里始终有语言中立
    的兜底源（anysearch），不会零结果；判定不确定（mixed/other/空）时整个族
    不动，按原样保留。

    语言取 lang_override 优先（「用韩语搜 X」的显式意图胜过文本推断），
    与 _select_language_engines / _lang_must_keep 同一口径。

    specs 允许为空：_specs_snapshot() 在 engines 模块未加载时返回 {}（性能
    设计，见该函数 docstring），此时 family_of / lang_allows 会回退
    engine_families 的静态表——新源在那边也声明了一次，所以这里不依赖 spec。
    """
    if not combo or not features:
        return combo
    lang = _family_lang(features, query)
    if not lang or lang in ("mixed", "other"):
        return combo
    keep: list[str] = []
    for eng in combo:
        spec = specs.get(eng)
        # 两类源在非匹配语言时摘除，不是移尾：
        #   ① 语言绑定族成员（world_news）：每个源服务一种语言；
        #   ② 语言独占源（_LANG_EXCLUSIVE_ENGINES，如 cinii=ja）。
        # 与 engines_demote_for_lang 的分工——那条只管顺序，这条管「该不该
        # 在场」：移尾在源本来就排末位时等于没动（实测 cinii 在 academic 域
        # 末位，英文查询照样进预算窗口）。
        lang_bound = family_of(eng, spec) == "world_news"
        lang_exclusive = eng in _LANG_EXCLUSIVE_ENGINES
        if (lang_bound or lang_exclusive) and not lang_allows(eng, lang, spec):
            continue
        keep.append(eng)
    # 族内非匹配成员一律摘除：一个匹配的都没有时退化成「整个族摘掉」（该族
    # 服务不了这个查询——域 patterns 误伤，或语言不在这 14 种里）；有匹配时
    # 摘掉的是「别的语言的一手源」。两种情形都不返回它们。通用源仍在 keep
    # 里兜底，不会零结果。
    return keep


_LANG_EXCLUSIVE_ENGINES: frozenset[str] = frozenset({
    "cinii",     # 日本学术总库（ja）：中文/英文查询拿不到任何可用结果
})


_LANG_PREFERRED_ENGINES: dict[str, list[str]] = {
    "ja": ["local_bing"],
    "ko": ["local_bing"],
}


def _lang_must_keep(features: dict | None, enabled: set[str],
                    combo: list[str] | None = None,
                    query: str = "") -> list[str]:
    """返回语言相关的 must_keep 引擎。

    两档判据，优先级从高到低：

    1. **语言绑定的本地源**（world_news 族中匹配查询语言的成员）。这类源是
       该语言的唯一一手通道，而通用 SERP（local_bing）只是二手转述——韩语
       新闻查询被 budget 裁到 2 位时，该保的是韩联社。没有它，本地语言源
       接进来也永远进不了预算窗口（实测：ko 查询 combo 被裁成
       [anysearch, local_bing]，yna 在窗口外）。
    2. 日/韩的通用本地引擎（yandex/google/bing）。专用源默认 disabled 时
       落到 local_bing；多语言结果质量仍靠 engines_base 动态 setlang。
    """
    if not features or not enabled:
        return []
    # P2-3：显式语言覆盖（用日文搜/用韩语搜）与主语言同等进入 must_keep
    lang = _family_lang(features, query)
    if combo and lang:
        bound = [e for e in combo if e in enabled
                 and family_of(e, None) == "world_news"
                 and lang_allows(e, lang, None)]
        if bound:
            return bound[:1]
    preferred = _LANG_PREFERRED_ENGINES.get(lang, [])
    for eng in preferred:
        if eng in enabled:
            return [eng]
    return []

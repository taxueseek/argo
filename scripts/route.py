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
import sys
import time
from typing import Any
from cli_io import dumps

try:
    from config import (load_config, get_engines, get_domains, get_cost_factor,
                        config_stamp)
    from tfidf_router import semantic_route, get_router
    from quota import get_quota_manager
    from engine_families import engines_demote_for_lang, engines_not_for_lang, lang_allows
except ImportError:
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from config import (load_config, get_engines, get_domains, get_cost_factor,
                        config_stamp)
    from tfidf_router import semantic_route, get_router
    from quota import get_quota_manager
    from engine_families import engines_demote_for_lang, engines_not_for_lang, lang_allows

# 世界银行国家表：macro_data 域按国家词分流（非美国国家查询让 worldbank 优先，
# 避免 FRED 美国序列冒充「中国GDP」这类答案）
#
# 从 macro_countries 直接取，**不从 engines_builders_data_macro 转出**：后者会
# 连带来 engines_base → http_client 整条 HTTP 栈（实测 36 ms，占 import route
# 的绝大部分）。路由是每次调用的必经路径（缓存命中也要走），HTTP 栈只有真正
# 打网才需要——为了一个 8 行的纯文本谓词付这笔钱不值得。
from macro_countries import is_foreign_macro_query

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
    """从 config.yaml 引擎声明的 label 构建显示名映射（唯一来源）。

    新增引擎只需在 config.yaml 声明 label，路由 reason 自动使用，
    不再需要手工同步本表。
    """
    try:
        engines = get_engines()
    except Exception:
        return {}
    return {name: spec.get("label") or name for name, spec in engines.items()}


# 惰性初始化：模块级立即调用 get_engines() 会触发 config 全量加载
# （合并全部外置引擎 spec，实测约 0.1s——见 config.peek_cache_db_path 的勘误：
# 此处曾写「约 1.7s」，该数字无法复现，真实成本是「一次合并 ~0.1s，且 import
# 链上被连调 4 次」），让「import route」为一张显示名表付出冷启动大头。改为首次
# 使用时构建（_engine_display 是内部唯一读取入口）。config 内部有 mtime 缓存，
# 进程内第二次起零成本。
#
# 兼容：历史上 _ENGINE_NAMES 是模块级公开名字，外部（含测试）会
# `from route import _ENGINE_NAMES` 直接引用。此处**故意不在模块级绑定**
# _ENGINE_NAMES，配合 PEP 562 模块级 __getattr__：外部首次访问时惰性构建，
# 语义与旧的全量字典完全一致；若模块级绑定的是 None 占位值，__getattr__ 不会触发，
# 外部拿到的就是 None（埋雷）。
_ENGINE_NAMES_CACHE: dict[str, str] | None = None
_ENGINE_NAMES_STAMP: float | None = None


def _engine_names_map() -> dict[str, str]:
    """引擎 id → 显示名 映射，按 config_stamp 热重建。

    此前建一次就永不失效：长驻进程（MCP server / 交互式调用）里新增或改名的
    引擎要重启才可见，而同一个 diff 里的 get_registry 已经按 config_stamp 热
    重建——同一份配置两个缓存两套失效计算方式，是最容易踩的那种不一致。stamp 本身
    按 TTL 记忆（见 config.config_stamp），所以只在配置真的变了才重建，热路径
    上只是一次字典构造。
    """
    global _ENGINE_NAMES_CACHE, _ENGINE_NAMES_STAMP
    try:
        stamp: float | None = config_stamp()
    except Exception:
        # config 不可用时判断不了新鲜度：退化为旧行为（建一次不失效），
        # 而不是每次调用都重建（那会让一张显示名表拖垮热路径）。
        stamp = None
    if _ENGINE_NAMES_CACHE is None or _ENGINE_NAMES_STAMP != stamp:
        _ENGINE_NAMES_CACHE = _build_engine_names()
        _ENGINE_NAMES_STAMP = stamp
    return _ENGINE_NAMES_CACHE


def _engine_display(name: str) -> str:
    """引擎显示名（label 优先），惰性构建映射表。"""
    return _engine_names_map().get(name, name)


def __getattr__(name: str):
    # PEP 562：仅在常规模块属性查找失败时触发。
    if name == "_ENGINE_NAMES":
        return _engine_names_map()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
_compiled_domains_id: tuple | None = None  # (name, patterns 长度) 内容指纹


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
    # 用内容指纹替代 id(domains)，避免 GC 后 id 复用导致缓存失效/错误。
    # 指纹含 patterns 本体（只记长度会漏「改正则不改条数」的编辑）。
    # patterns 拼 hash 而非直接进 tuple：长正则列表构造开销大，hash 一次 O(n) 可控。
    import hashlib
    dom_fp = tuple(
        (d.get("name", ""),
         hashlib.sha1(
             "\x00".join(
                 p if isinstance(p, str) else str(p)
                 for p in (d.get("patterns") or [])
             ).encode("utf-8", "replace")).hexdigest()[:12])
        for d in domains
    )
    if _compiled_domains is not None and _compiled_domains_id == dom_fp:
        return _compiled_domains
    _compiled_domains = _compile_domain_patterns(domains)
    _compiled_domains_id = dom_fp
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
    # 中文政策/百科/医疗域：日韩查询不应进（如日文「政策金利」命中 gov_policy）
    "gov_policy", "baidu_baike", "medical",
})

# 语言过滤名单改由 engine_families.ENGINE_LANGS 派生（lang_allows/
# engines_not_for_lang）：语言能力随引擎注册声明一次（config `langs` 可
# 覆盖），四张手写冻结表（_ZH_CONTENT/_JA_KO_CN/_EN_ONLY/_ZH_ONLY）
# 2026-09-07 收紧删除。成员忠实自原表推导，多语言契约测试验收。


# ── 结构化平台语法（唯一来源：config.yaml 的 social 域 patterns）─────────────
# 查询含平台搜索语法（from:/subreddit:/lang:/filter: 等）时把 social 域提前为
# 主域，避免查询里的实体词（GPT/Llama/api）把 model/_tech 域排前面。
# 语法判定不再在 Python 侧复制正则（此前与 config.yaml social patterns 第三
# 条字面重复，两处改动必须同步）；social 域 patterns 命中即判定，route 只做
# 顺序调整。repo:/site: 另勘。


def _social_domain_first(_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """命中列表里有 social 域 → 提到首位（无则原样）。

    排位在 social 之前的更具体域保持原序不抢占：「小红书技能排行」的
    redskill_search（小红书技能垂直域）、「我的收藏/我的回答」的
    zhihu_user_data（个人数据意图，收藏/关注是社交平台通用功能词，
    泛 social 域语义更宽）。
    """
    social = [h for h in _hits if h.get("name") == "social"]
    if not social:
        return _hits
    idx_first_social = _hits.index(social[0])
    # 「热搜/热榜」是比泛 social 更具体的意图：查询里带微博/抖音这类平台名时，
    # social 会被提前，把 hot_trending 顶掉——用户问的是榜单，不是社交帖子。
    _SPECIFIC_BEFORE_SOCIAL = ("redskill_search", "zhihu_user_data", "hot_trending")
    idx_specific = next(
        (i for i, h in enumerate(_hits)
         if h.get("name") in _SPECIFIC_BEFORE_SOCIAL), None
    )
    if idx_specific is not None and idx_specific < idx_first_social:
        return _hits
    return social + [h for h in _hits if h.get("name") != "social"]


def _diffuse_intent_guard(hits: list[dict[str, Any]], query: str,
                          features: dict | None = None) -> list[dict[str, Any]]:
    """面查意图守卫：点查域在「主题句式查询」下让位。

    点查域（package_search / ai_model）语义是「查询主体即目标对象」：
    「pnpm add lodash」「GPT-4o」该走结构化源。但当查询是长主题句
    （去重 token ≥5）且不含该域意图词时，实体词（pnpm/DeepSeek）只是
    上下文，域命中属词面误抢——2026-09-06 实测：「pnpm file: directory
    dependency no content hash reinstall」（无意图词）被锁死 pypi，
    返回单字垃圾包且骗过覆盖守卫早停；「DeepSeek Harness DSH 插件开发」
    被锁死 models_dev。让位后主域由次域 / TF-IDF / 通用组合接管。

    判别优先级：面查信号 > 意图豁免 > token 门槛。面查信号命中即让位
    （「npm 包 安装 报错」意图是解决报错，不是找包）；意图词命中则豁免
    （「python 环境安装 requests 库」确实是包查询）。
    """
    if not hits:
        return hits
    try:
        from tfidf_router import tokenize
        n_tokens = len(set(tokenize(query)))
    except Exception:
        return hits  # 分词不可用不设卡（fail-open，同覆盖守卫口径）
    if n_tokens < _POINTED_MIN_TOKENS:
        return hits
    diffuse_hit = re.search(_DIFFUSE_SIGNAL_RE, query) is not None
    kept: list[dict[str, Any]] = []
    for d in hits:
        name = d.get("name") or ""
        intent_re = _POINTED_INTENT_RE.get(name)
        if intent_re is None:
            kept.append(d)
            continue
        if diffuse_hit:
            continue  # 面查信号压过意图豁免：报错/排查/对比类走通用
        if re.search(intent_re, query):
            kept.append(d)
            continue
        continue  # 长主题句 + 无意图词：实体词只是上下文，让位
    return kept


# 点查域 → 意图豁免词（命中即视为真正的结构化点查）。
# 英文备选吃 \b；中文备选必须在 \b 外——CJK 字符全是 \w，「npm安装报错」
# 这类无空格连写永远撞不上词边界（2026-09-06 审查实锤），会让真实包查询
# 被误让位。同 _DIFFUSE_SIGNAL_RE 中文备选的既有计算方式。
_POINTED_INTENT_RE: dict[str, str] = {
    "package_search": r"(?i)\b(install|add|uninstall|download)\b|安装|下载|替代包|包名",
    "ai_model": r"(?i)(价格|pricing|上下文|context window|token limit|vision|多模态|免费|开源|多少钱)",
}
# 面查信号：查询在研究/排障/对比一个主题，而非定位一个对象
_DIFFUSE_SIGNAL_RE = re.compile(
    r"(?i)\b(issue|bug|regression|reinstall|stale|not.?work|broken|crash"
    r"|how (to|does|do)|why (is|does|do)|difference|vs\.?)\b"
    r"|报错|失效|不生效|不更新|出错|排查|区别|对比"
)
# 长主题句门槛：去重 token 少于此值不设卡（短查询大概率是点查）
_POINTED_MIN_TOKENS = 5


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
        GENERAL_FREE_FALLBACK = ("anysearch", "duckduckgo", "local_bing")
    return ["local_search"] + [e for e in GENERAL_FREE_FALLBACK if e in enabled]


def _filter_breaker_blocked(engine_list: list[str]) -> list[str]:
    """剔除确定熔断态引擎（disabled / open 且冷却未过），与 _get_engines_combo
    内的熔断感知过滤同一语义（half_open 保留探测资格）。

    D4：语言引擎追加、通用保底、TF-IDF 注入等路径在 _get_engines_combo 之外，
    追加的引擎可能处于熔断态仍进 combo，白占并行槽位。统一统一处理到最终组装后。
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
        # en / cyrillic / 其他：中英基线本地引擎（动态 setlang 保底多语言索引）
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


# 语言重排的排除名单改为 engine_families.ENGINE_LANGS 派生（engines_not_for_lang），
# 三张手写冻结表（_EN_ONLY/_ZH_ONLY/_JA_KO_CN）2026-09-07 收紧删除——
# 新源声明 langs 一次，全部分发路径自动生效。_SOCIAL_ZH_GENERAL 保留
# （social 域中文查询的通用 web 保底，按优先级）：平台词命中 social 域的查询
# （提到「小红书/微信」≠ 搜小红书/微信），通用源覆盖真实主题，防平台噪声全占。
# anysearch 优先：local_bing 直抓 bing.com 对长中文查询存在降级服务风险
# （2026-08-29 实测：整条查询被 Bing 降级为单字「拍」匹配，返回字典页）。
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
                            enabled: set[str]) -> list[str]:
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
        return ordered
    # 对称分支：非中文查询把中文专用源移尾（含 zh_ratio≤0.15 的混合查询）
    if features.get("primary_lang") in ("en", "ja", "ko"):
        return _move_to_tail(
            combo, engines_demote_for_lang(
                combo, primary_lang or "en", _specs_snapshot()))
    return combo


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
        # 默认保底：快源优先（brave/yahoo 实测 ~1.1s），ddgs 默认后端慢不主动纳入
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
    # 全网搜索族同质化最高（byted/bocha/duckduckgo/octen 都是通用网页检索），
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


# ── 垂直域「新专源」保底表（批次九）────────────────────────────────────────────
# 批次九的 11 个新源声明在 combo 后排（位次 3~6），而 auto/balanced 的 budget=3、
# fast=2 —— 截断后它们在日常路由里永远轮不到，表现为「按 --engine 能单跑、路由
# 命中正确域、该域专属源却不参与」。这与批次九已修的准入粘滞 bug 是同一症状、
# 不同成因（卡在 budget 而非 blocked）。
#
# 为何不直接改 combo 顺序：把新源提前会顶掉既有可用源（实测会把
# open_library / douban_movie / musicbrainz 挤出预算），属横向替换而非净增益。
# 故采用**加槽**（扩容）而非**顶位**，两者共存。
#
# 收录计算方式：只收「能力与既有源不重叠」的源；同能力横向重复
# （如 art_museum 的 artic vs cleveland）不收，避免用同质源挤掉同质源。
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
}

# 垂直域主源保护名单：这些域的专属源被 budget 裁掉后该域等于没源可用。
# 模块级常量（原先定义在 route_query 内，每次调用重建一个 18 元素 frozenset）。
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
        # 逗号多引擎与 --list-engines 路径（search.py --engine split(',')）
        # 同一计算方式；此分支曾整串直通——整串被当成一个引擎名进 combo，
        # registry 查无 → 「未知引擎」空跑，用户显式指定的引擎全部失效。
        engines = [e.strip() for e in engine_override.split(",") if e.strip()]
        if not engines:
            engines = ["anysearch"]
        # 空串等同 auto：防止空引擎名混入 combo（registry 查无 → 空结果
        # → 熔断器空键 ''），MCP/外部调用可能传入空 engine 参数
        return _done(
            engine=engines[0], engines=engines,
            engines_combo=engines,
            reason=f"用户指定: {', '.join(engines)}", confidence=1.0,
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
    # 结构化域优先：social 域 patterns（config.yaml 唯一来源）命中即提前，
    # 避免被 query 里的实体词（GPT/Llama/api）误抢到模型库/技术域。
    _domain_hits = _social_domain_first(_domain_hits)
    # 面查意图守卫：长主题句 + 无意图词时点查域让位（防 pypi/models_dev 词面误抢）
    _domain_hits = _diffuse_intent_guard(_domain_hits, query, features)
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
            for cand, score, _ in tfidf_scores:
                social_ok = True
                if cand in SOCIAL_ENGINES:
                    ql = query.lower()
                    social_signals = (
                        "微博", "小红书", "推特", "twitter", "reddit", "舆情",
                        "讨论", "网友", "评论", "b站", "bilibili", "抖音",
                    )
                    social_ok = any(s in ql for s in social_signals)
                if score < TFIDF_MIN_SCORE or not social_ok:
                    # 分数降序：后续候选分更低，整条 TF-IDF 分支作废
                    break
                # ja/ko 查询：候选若是中文内容/政策引擎（gov_policy/百科等），
                # 对日/韩用户无关（返回中文站），丢弃让通用 anysearch 主导。
                # 丢弃当前候选后继续看下一个（2026-08 修复：旧逻辑只看 top-1，
                # 丢弃后不检查 top-2/3，可能错失 anysearch 等合格候选）。
                # 语言可达性门（对所有查询生效，不再只限 ja/ko）：引擎声明的
                # 语言能力不含查询语言且非语言中立（"*"）时，TF-IDF 前置注入
                # 不得把它顶到首位。实测缺陷：中文法条查询「刑法 判例 司法解释」
                # 与 kor_law（langs=["ko"]）的文档共享汉字而命中，被注进 legal
                # 域首位——一个中文法条查询优先去打了韩国判例库。
                _ql = features.get("primary_lang") or ""
                if _ql and not lang_allows(cand, _ql, _specs_snapshot().get(cand)):
                    continue
                tfidf_best = cand
                tfidf_best_score = score
                break
        except ImportError:
            pass
        except Exception as e:
            import logging
            logging.getLogger("unified_search.route").debug(
                f"TF-IDF 路由跳过: {type(e).__name__}"
            )

    if domain:
        engines_combo = _get_engines_combo(domain, enabled, mode, features)
        # 🔑 ja/ko 查询：域命中路径也剔除中文内容/金融/新闻引擎（补齐 TF-IDF 层过滤缺口，
        # 否则 ja 技术查询落 english_tech 用 octen 返回中文 CSDN）。2026-08 修复。
        # （anysearch 前二注入在策略/预算截断之后统一做，见 _inject_multilingual_backup）
        if features.get("primary_lang") in ("ja", "ko") and engines_combo:
            _drop = set(engines_not_for_lang(
                engines_combo, features.get("primary_lang") or "ja",
                _specs_snapshot()))
            _filtered = [e for e in engines_combo if e not in _drop]
            engines_combo = _filtered or [e for e in ["anysearch"] if e in enabled] or engines_combo
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
        # 🔑 macro_data 域 + 中国宏观词 → nbs_stats（国家统计局）前置：
        # 本国宏观数据权威源，最新年份比 worldbank 全（worldbank 有 1-2 年
        # 数据滞后，「2025 年 GDP」类查询会空手）。与上方 worldbank 前置
        # 配合，中国查询最终位次 [nbs_stats, worldbank, ...]
        if (domain.get("name") == "macro_data"
                and "nbs_stats" in engines_combo
                and ("中国" in query or "china" in query.lower())):
            engines_combo = ["nbs_stats"] + [e for e in engines_combo if e != "nbs_stats"]
        # 🔑 为中文/学术查询追加本地引擎
        # modal_card 保持纯结构化路径：只走 bocha_ai → bocha，不混 web/geo 补充源
        _pure_combo = domain.get("name") == "modal_card"
        if not _pure_combo:
            engines_combo = _merge_language_engines(engines_combo, features, lang_engines)
            engines_combo = _lang_aware_combo_order(
                engines_combo, features, domain.get("name"), enabled)
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
                # 域内引擎全被过滤，回退（本地优先 + 通用免费源唯一来源）
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
        # 预算截断由 _apply_engine_policy 完成；web_general 族计数检查防同质堆叠
        # （与 _get_engines_combo 的能力族去重语义一致）。modal_card 纯结构化路径
        # 不混入次域源。次域引擎不受 must_keep 保护，预算紧张时自然被裁。
        if secondary and not _pure_combo:
            try:
                from engine_families import family_of
                # 同 _get_engines_combo 的排序：必须把引擎声明传下去，否则
                # config.yaml 里声明的族被忽略、一律算成 web_general，这里的
                # 「同族已达 2 个就不再补」会误判，把次域的专业源挡在外面。
                _sec_specs = get_engines()
                if not isinstance(_sec_specs, dict):
                    _sec_specs = {}
                fam_count: dict[str, int] = {}
                for _e in engines_combo:
                    _f = family_of(_e, _sec_specs.get(_e))
                    fam_count[_f] = fam_count.get(_f, 0) + 1
            except Exception:
                fam_count = None
            for _sec in secondary:
                _sp = _sec.get("primary")
                if not _sp or _sp not in enabled or _sp in engines_combo:
                    continue
                if fam_count is not None:
                    _f = family_of(_sp, _sec_specs.get(_sp))
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
        # 垂直域主源保护（名单见模块级 _VERTICAL_KEEP）：这些域的专属源在
        # combo 里不是「通用源」，被 budget 截断后该域等于没源可用（实测
        # medical 的 who_don、japan_law 的 egov_law 均因 budget=2 被裁掉
        # → 路由命中但零结果）。
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
        engines_combo = _apply_policy_with_new_source_slots(
            domain, engines_combo,
            mode=mode, depth=depth, context=context,
            enabled=enabled, engines_boost=engines_boost, must_keep=must_keep,
        )
        # ja/ko：多语言主力源 anysearch 送到前二（策略截断后注入才不会被
        # must_keep 换位挤出；primary 扶正前注入，域主源仍居首）
        engines_combo = _inject_multilingual_backup(engines_combo, enabled,
                                                    features)
        # 域 primary 扶正：已在 combo 且未熔断时置首（不覆盖冷却中的熔断沉底）
        # open 但 cooldown 已过 → 允许扶正，交给 half-open 探测。
        p = domain.get("primary")
        # macro_data 非美国查询：worldbank 前置是领域语义（FRED 无该国数据，
        # 先跑 fred + early-stop 会拿美国数据冒充），primary 扶正不得覆盖。
        _foreign_macro = (
            domain.get("name") == "macro_data" and is_foreign_macro_query(query)
        )
        # research 语境 + 画像 boosts：研究垂直源（arxiv/semantic_scholar 等）
        # 前置是选题语义，primary（如 ai_model 的 models_dev 目录）不得顶回首位
        _research_boost = bool(context == "research" and engines_boost)
        if (p and p in engines_combo and engines_combo[0] != p
                and not _foreign_macro and not _research_boost):
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
        # D4：统一熔断统一处理——语言/geo/次域/TF-IDF 追加的引擎也可能处于熔断态
        engines_combo = _filter_breaker_blocked(engines_combo)
        if not engines_combo:
            engines_combo = [e for e in ["anysearch", "duckduckgo"] if e in enabled] or ["anysearch"]
        # budget 截断后保持一致 parallel，避免短 combo 仍开多余并行
        # research 语境例外：子查询跑满 combo（no_early_stop），串行会拖垮
        # 整条研究管线，强制并行
        if (mode == "fast" and context != "research") or len(engines_combo) <= 1:
            parallel = False
        elif context == "research":
            parallel = True
        elif len(engines_combo) <= 2 and not domain.get("parallel", False):
            # 双引擎默认串行，利于 early-stop（答案域）
            parallel = parallel and len(engines_combo) > 2

        return _done(
            engine=engines_combo[0],
            engines=engines_combo,
            engines_combo=engines_combo,
            # 恢复链 L3 候选：域声明但被预算截掉的成员优先（域最清楚自己
            # 的保底次序，实测 macro_data 六成员被截成两个、恰好截掉国家
            # 统计局），其余 enabled 引擎殿后
            engines_fallback=(
                [e for e in (domain.get("engines_combo") or [])
                 if e not in set(engines_combo)]
                + [e for e in enabled if e not in engines_combo
                   and e not in set(domain.get("engines_combo") or [])]),
            reason=(
                f"{_feature_labels(features)} → 命中域 [{domain.get('name', '?')}]"
                + (f" [TF-IDF→{tfidf_best}]" if tfidf_best else "")
                + (f" [TF-IDF覆写catch-all]" if is_catch_all and tfidf_best and tfidf_best_score > 0.15 and tfidf_best in engines_combo else "")
                + (f" [boost={engines_boost}]" if engines_boost else "")
                + f" → {_engine_display(engines_combo[0])}"
            ),
            confidence=confidence, features=features,
            domain=domain.get("name"), parallel=parallel,
            no_early_stop=bool(domain.get("no_early_stop", False)),
            early_stop_min_results=domain.get("early_stop_min_results"),
            # 域命中时 combo 来自域配置，TF-IDF 候选只有真正进入 combo 才
            # 算参与了决策；tfidf_best 落选仍照搬原始得分会误导消费方
            # （实测「asyncio tutorial」报 qiita 前三、实际执行 octen/exa）。
            # 落选的近失信号由 reason 的 [TF-IDF→x] 标注承载。
            tfidf_scores=([{"engine": n, "score": s} for n, s, _ in tfidf_scores]
                          if tfidf_best and tfidf_best in engines_combo else []),
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
        engines_combo = _apply_policy_with_new_source_slots(
            domain, engines_combo,
            mode=mode, depth=depth, context=context,
            enabled=enabled, engines_boost=engines_boost, must_keep=must_keep,
        )
        # ja/ko catch-all 与主域分支同计算方式：anysearch 前二（TF-IDF 直选路径
        # 也会把多语言主力挤掉）
        engines_combo = _inject_multilingual_backup(engines_combo, enabled,
                                                    features)
        # D4：统一熔断统一处理（TF-IDF 注入/语言追加可能绕过 _get_engines_combo）
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
                f"TF-IDF 语义路由 → {_engine_display(engines_combo[0])}"
                f" (score={tfidf_best_score:.3f}, 正则未命中)"
                + (f" [boost={engines_boost}]" if engines_boost else "")
            ),
            confidence=0.85, features=features, domain="general_search",
            parallel=parallel,
            tfidf_scores=[{"engine": n, "score": s} for n, s, _ in tfidf_scores],
            mode=mode, depth=depth, context=context,
            login_hint=_detect_login_intent(query, None),
        )

    # 保底：免费通用引擎（零分 TF-IDF 也走这里）——本地优先 + 通用免费源唯一来源
    fallback_combo = _general_fallback(enabled)
    if not fallback_combo:
        fallback_combo = sorted(enabled)[:2] if enabled else ["anysearch"]
    # 日/韩主查询：优先 anysearch（多语言源对日/韩结果语言匹配更好），
    # 再并语言专用本地引擎；避免旧逻辑只锁 local_bing（zh 参数）返回中文站。
    # 注：byted 经实测对 ja/ko 也可，但其被 test_multilingual 定义为中文引擎，
    # 强制优先会破坏 ja/ko 路由契约；byted 通过融合权重提权即可。2026-08。
    if features.get("primary_lang") in ("ja", "ko") and _get_registry is not None:
        lang_combo = _select_sub_engines(_enabled_local_engines(), features)
        non_local = [e for e in fallback_combo if not e.startswith("local_")]
        if "anysearch" in non_local:
            non_local = ["anysearch"] + [e for e in non_local if e != "anysearch"]
        if lang_combo:
            fallback_combo = (non_local + lang_combo) if non_local else lang_combo
        else:
            fallback_combo = non_local or fallback_combo
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
    # D4：统一熔断统一处理（保底组合可能含熔断引擎）
    fallback_combo = _filter_breaker_blocked(fallback_combo)
    if not fallback_combo:
        fallback_combo = ["anysearch"]

    low = tfidf_scores and all(s[1] < TFIDF_MIN_SCORE for s in tfidf_scores)
    reason = (
        f"TF-IDF 低分回退通用引擎 → {_engine_display(fallback_combo[0])}"
        if low else
        f"无匹配域，回退 {_engine_display(fallback_combo[0])}"
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
        # 保底路径 tfidf_best 必为空（否则已走 TF-IDF 分支）：低于阈值的
        # 候选分不是路由依据，输出只会误导，一律空表。
        tfidf_scores=[{"engine": n, "score": s} for n, s, _ in tfidf_scores]
        if tfidf_best else [],
        mode=mode, depth=depth, context=context,
        login_hint=_detect_login_intent(query, None),
    )


# ── 路由决策缓存（跨进程） ─────────────────────────────────────────────────────
#
# 为什么需要（2026-09-17 实测）：route_query 的**首次调用**要付约 103 ms 的
# 进程级初始化——238 条域正则编译 33 ms（而跑完全部匹配只要 0.2 ms）、惰性导入、
# 引擎环境/准入与 TF-IDF 装载；同进程内的后续调用只要 3.7 ms。CLI 每次调用都是
# 新进程，于是这笔启动税每次重付。实测：跳过一次 route_query 后，缓存命中的
# 一次完整搜索只要 26 ms（对比 route 首调 137 ms + 执行 3 ms）。
#
# 判据为什么与 config 磁盘缓存不同：那一层的产物被当作**事实**（db_path 等），
# 必须逐字节正确，所以用内容摘要；这一层的产物是**优化结果**，偏差的后果只是
# 一段时间内引擎排序不最优，且有 TTL 与下游失败分型兜底，故用 config_stamp()
# 这个既有的 mtime 综合戳（registry 热加载同款）——键计算从约 5 ms 降到 0.1 ms。

_ROUTE_CACHE_SCHEMA = 1
# TTL 只兜自适应学习器（adaptive.db）的渐进漂移——影响路由的持久状态
# （config 改动 / 额度耗尽 / 熔断禁用）都在指纹里，变了键就换。原值 300 s
# 让隔了几分钟的重复查询白付整笔 route_query 启动税（实测 130–300 ms），
# 而指纹盖不住的那点排序漂移在一小时内不构成路由错误，放宽到 1 h。
_ROUTE_CACHE_TTL_S = 3600.0
_ROUTE_CACHE_MAX_ENTRIES = 200


def _route_cache_enabled() -> bool:
    """ARGO_ROUTE_CACHE=0/false/no/off 关闭；判定链不可用时按「开」处理。

    缓存是纯性能优化，关掉不影响正确性；判不开时维持既有行为（每次都实算）。
    """
    try:
        from engine_env import env_flag
        return env_flag("ARGO_ROUTE_CACHE", default=True)
    except Exception:
        return True


def _route_cache_file():
    import argo_paths
    return argo_paths.state_path("route-cache.json")


def _route_state_fingerprint() -> str:
    """影响路由决策的可变状态摘要；取不到就返回空串（等价于不用缓存）。

    - `config_stamp()`：config.yaml 与外置声明的 mtime，覆盖 enabled / domains /
      engines_combo 的改动。
    - 配额与熔断取**派生集合**（已耗尽额度 / 已自动禁用），不取状态文件的字节或
      mtime。为什么：`quota.json` 每次运行都会被状态机重写（mtime 必变），文件里的
      用量计数也随每次搜索变动——**实测拿 mtime 做摘要会让缓存 100% 失效**
      （第二次调用就换了键）；而真正改变路由结果的只是「哪些源现在不可用」这个
      集合，它只在源真的挂掉或恢复时变化。

    刻意**不含** adaptive.db：自适应学习器每次搜索都写它，同理会让缓存立即失效；
    它只影响引擎排序的软信号、变化渐进，由 TTL 兜住。

    这是**粗粒度**信号：覆盖「源挂了 / 被禁 / 额度耗尽」这类持久状态，瞬时节流
    （rpm 抖动）不在其中，由 TTL 兜住。空串判据与 config 磁盘缓存 digest 取不到
    时的保守选择一致：空摘要永不等于任何已存条目的键，因此不会读到旧结论。
    """
    try:
        from config import config_stamp
        parts = [f"cfg={config_stamp():.0f}"]
    except Exception:
        return ""
    try:
        from quota import get_quota_manager
        marks = get_quota_manager().remote_exhausted_marks()
        parts.append("qe=" + ",".join(sorted(marks)))
    except Exception:
        return ""
    try:
        from circuit_breaker import get_breaker
        parts.append("cb=" + ",".join(sorted(get_breaker().auto_disabled())))
    except Exception:
        return ""
    return "|".join(parts)


def _route_cache_key(query: str, engine_override: str, mode: str, depth: str,
                     context: str, engines_boost: list[str] | None,
                     fingerprint: str) -> str:
    import hashlib
    import json
    raw = json.dumps({
        "v": _ROUTE_CACHE_SCHEMA,
        # 归一化空白：同一问题多打几个空格不该是两次路由
        "q": " ".join(str(query or "").split()),
        "eo": engine_override or "auto",
        "mode": mode, "depth": depth, "context": context,
        "boost": [str(b) for b in (engines_boost or [])],
        "fp": fingerprint,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _route_cache_read() -> dict[str, Any]:
    import json
    try:
        payload = json.loads(_route_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != _ROUTE_CACHE_SCHEMA:
        return {}
    entries = payload.get("entries")
    return entries if isinstance(entries, dict) else {}


def _route_cache_prune(entries: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    fresh = {k: v for k, v in entries.items()
             if isinstance(v, dict)
             and now - float(v.get("ts") or 0) <= _ROUTE_CACHE_TTL_S}
    if len(fresh) > _ROUTE_CACHE_MAX_ENTRIES:
        newest = sorted(fresh.items(),
                        key=lambda kv: float(kv[1].get("ts") or 0), reverse=True)
        fresh = dict(newest[:_ROUTE_CACHE_MAX_ENTRIES])
    return fresh


def _route_cache_write(entries: dict[str, Any]) -> None:
    import argo_paths
    try:
        argo_paths.atomic_write_json(
            _route_cache_file(),
            {"schema": _ROUTE_CACHE_SCHEMA, "entries": entries},
            indent=None,
        )
    except Exception:
        return


def invalidate_route_cache() -> bool:
    """删除磁盘上的路由决策缓存（测试隔离与显式失效用）。"""
    try:
        _route_cache_file().unlink()
        return True
    except OSError:
        return False


def route_query_cached(query: str, engine_override: str = "auto",
                       mode: str = "auto", depth: str = "fast",
                       context: str = "search",
                       engines_boost: list[str] | None = None) -> dict[str, Any]:
    """route_query 的跨进程缓存包装：命中即跳过整笔启动税。

    调用方会就地改 decision（如 research 置 no_early_stop），而命中返回的是
    每次从**磁盘重新读出**的对象，改动不会影响后续调用或存档内容——这条契约由
    tests/test_route_cache.py::test_hit_returns_deep_copy 锁住（新增进程内记忆化
    之类的优化会打破它，届时测试会红）。

    route_cached 的三种取值是有意的：True=命中、False=走了缓存但未命中、
    **缺席**=本次没走缓存（开关关闭或指纹不可用）。缺席与 False 对调用方的
    含义不同——前者是「缓存没参与」，后者是「缓存参与过、这个输入是新的」。

    任何不满足缓存条件的情况都回落到 route_query，缓存永不改变功能，只改变
    「要不要再算一次」。
    """
    import json

    if not _route_cache_enabled():
        return route_query(query, engine_override=engine_override, mode=mode,
                           depth=depth, context=context, engines_boost=engines_boost)
    fingerprint = _route_state_fingerprint()
    if not fingerprint:
        return route_query(query, engine_override=engine_override, mode=mode,
                           depth=depth, context=context, engines_boost=engines_boost)

    t0 = time.time()
    key = _route_cache_key(query, engine_override, mode, depth, context,
                           engines_boost, fingerprint)
    entries = _route_cache_read()
    hit = entries.get(key)
    if isinstance(hit, dict) and isinstance(hit.get("decision"), dict) \
            and time.time() - float(hit.get("ts") or 0) <= _ROUTE_CACHE_TTL_S:
        decision = hit["decision"]
        decision["route_cached"] = True
        # 用真实耗时覆盖存档值：这个字段会经 plan 输出给用户，报旧值就是撒谎
        decision["elapsed_ms"] = round((time.time() - t0) * 1000, 2)
        return decision

    decision = route_query(query, engine_override=engine_override, mode=mode,
                           depth=depth, context=context, engines_boost=engines_boost)
    decision["route_cached"] = False
    # 语义可逆才缓存：JSON 往返会把元组静默变列表，那等于改变了决策的形态
    # （与 config 磁盘缓存「缓存不得改变语义」同一条纪律）。
    try:
        if json.loads(json.dumps(decision, ensure_ascii=False)) == decision:
            entries[key] = {"ts": time.time(), "decision": decision}
            _route_cache_write(_route_cache_prune(entries))
    except (TypeError, ValueError):
        pass
    return decision


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="Unified Search v2 路由器")
    parser.add_argument("query")
    parser.add_argument("--engine", default="auto")
    parser.add_argument("--mode", default="auto", choices=["fast", "auto", "deep", "budget"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    decision = route_query(args.query, engine_override=args.engine, mode=args.mode)
    if args.json:
        print(dumps(decision))
    else:
        print(f"引擎: {decision['engine']}")
        print(f"组合: {decision.get('engines_combo', decision['engines'])}")
        print(f"原因: {decision['reason']}")
        print(f"置信度: {decision['confidence']:.2f}")
        print(f"耗时: {decision.get('elapsed_ms', 0):.3f} ms")


if __name__ == "__main__":
    _cli()

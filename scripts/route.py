#!/usr/bin/env python3
"""
route.py — 路由**编排**（本模块只做决策调度，规则与策略各自成模块）

route_query 的三条分支，按优先级：
  1. 用户指定引擎 → 直接返回（不做任何推导）
  2. 域命中（route_domains 判据）→ route_combo 装配 combo → route_policy 截断
  3. 未命中 → TF-IDF 语义路由；再不行 → 通用保底组合

模块分工（改动前先读对应模块的 docstring，别在这里加特例）：
  route_domains   命中哪些域（声明式规则 + unless + intent_required）
  route_lang      语言判定与按语言选源/排序
  route_combo     引擎组合装配（谁在场、什么顺序）
  route_policy    预算截断与保留（谁必须留下）
  route_cache     决策缓存的存储层
  route_telemetry 决策采样上报（旁路，失败静默）

每种决策都带 reason 字符串（给人看的归因入口，字段口径见 _feature_labels）。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Callable
from cli_io import dumps

try:
    from config import (load_config, get_engines, get_domains, config_stamp)
    from quota import get_quota_manager
    from engine_families import (engines_demote_for_lang, engines_not_for_lang,
                                 engine_langs, family_of, lang_allows,
                                 lang_hint_from_query)
except ImportError:
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from config import (load_config, get_engines, get_domains, config_stamp)
    from quota import get_quota_manager
    from engine_families import (engines_demote_for_lang, engines_not_for_lang,
                                 engine_langs, family_of, lang_allows,
                                 lang_hint_from_query)

# tfidf_router 刻意**不在这里导入**（与下面 macro_countries 同一理由）：
# 它只被 route_query 的 `if not skip_tfidf:` 分支用到，而 fast 模式命中硬域
# 时整段跳过（-X importtime 实测 tfidf_router 588us / route 累计 3596us）。
# `--engine X` 指定引擎、`argo paths` 这类不走向量路由的调用不必替它买单。
# 延迟导入点见下方模块级 __getattr__（保留 route.semantic_route 可 patch）。

# 世界银行国家表：macro_data 域按国家词分流（非美国国家查询让 worldbank 优先，
# 避免 FRED 美国序列冒充「中国GDP」这类答案）
#
# 从 macro_countries 直接取，**不从 engines_builders_data_macro 转出**：后者会
# 连带来 engines_base → http_client 整条 HTTP 栈（实测 36 ms，占 import route
# 的绝大部分）。路由是每次调用的必经路径（缓存命中也要走），HTTP 栈只有真正
# 打网才需要——为了一个 8 行的纯文本谓词付这笔钱不值得。
from macro_countries import is_foreign_macro_query


# 引擎注册中心（子引擎可见性）
try:
    from argo_engine_registry import get_registry as _get_registry
except Exception:
    _get_registry = None

# 域规则（声明式加载 + 匹配 + 精度守卫）整体住在 route_domains：
# 「查询命中哪些域」与「命中之后怎么组合引擎」是两件事，混在一个文件里时
# 域规则只能靠 2400 行文件里的行号定位。这里保留同名转出，调用方与既有
# 测试（route._get_compiled_domains / route.match_domains）不需要改。
from route_domains import (  # noqa: E402
    match_domains,
    match_domain,
    _get_compiled_domains,
    _compile_domain_patterns,
    _social_domain_first,
    _intent_gate,
    _ZH_LANG_GATED_DOMAINS,
    _DIFFUSE_SIGNAL_RE,
    _INTENT_MIN_TOKENS,
)

# 路由决策缓存（存储层）住在 route_cache；route_query_cached 是本模块的编排
# （「什么时候用缓存」），留在下面。同名转出，既有测试与调用方无需改。
from route_cache import (  # noqa: E402
    _ROUTE_CACHE_SCHEMA,
    _ROUTE_CACHE_TTL_S,
    _ROUTE_CACHE_MAX_ENTRIES,
    _route_cache_enabled,
    _route_cache_file,
    _route_state_fingerprint,
    _route_cache_key,
    _route_cache_read,
    _route_cache_prune,
    _route_cache_write,
    invalidate_route_cache,
)

# 决策采样上报（旁路）：同名转出，测试打桩需打在 route_telemetry。
from route_telemetry import sample_route  # noqa: E402

# ── 语言与选源策略（route_lang）、组合装配（route_combo）、预算策略（route_policy）
# 三块按职责拆出，这里同名转出：调用方与既有测试（route.extract_features /
# route._get_engines_combo / route._VERTICAL_NEW_SOURCE …）无需改。
from route_lang import (  # noqa: E402
    extract_features,
    _detect_lang_override,
    _feature_labels,
    _lang_label,
    _enabled_local_engines,
    _specs_snapshot,
    _select_language_engines,
    _merge_language_engines,
    _add_language_engines,
    _inject_multilingual_backup,
    _lang_aware_combo_order,
    _lang_must_keep,
    _family_lang,
    _filter_lang_bound_family,
    _move_to_tail,
    _LANG_EXCLUSIVE_ENGINES,
    _LANG_PREFERRED_ENGINES,
    _SOCIAL_ZH_GENERAL,
    _get_registry,
)
from route_combo import (  # noqa: E402
    _expand_local_search,
    _general_fallback,
    _filter_breaker_blocked,
    _maybe_add_geo_engine,
    _apply_intent_parallelism,
    _select_sub_engines,
    _get_engines_combo,
    _adaptive_learner,
    _NARROW_ENGINES,
    _INTENT_PARALLELISM,
    inject_strong_semantic,
    # combo 定稿的三段共享步骤（三条分支逐字重复过的那三段）
    intent_squeeze,
    geo_lang_must_keep,
    breaker_filter,
)
from route_policy import (  # noqa: E402
    _VERTICAL_NEW_SOURCE,
    _VERTICAL_KEEP,
    _new_source_budget_extra,
    _apply_engine_policy,
    _apply_policy_with_new_source_slots,
)


# ── 特征提取 ──────────────────────────────────────────────────────────────────


# P2-3：显式语言意图 → 覆盖语言（「用英文搜」「in English」「日本語で」等）。
# 命中后 lang_override 直接决定语言引擎选择与 must_keep，不被习惯/系统 locale 淹没。


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
    if name in ("semantic_route", "get_router"):
        # 延迟导入：tfidf_router 只被 route_query 的 `if not skip_tfidf:` 分支
        # 用到（fast 模式命中硬域时整段跳过），却要占启动时间（-X importtime
        # 实测 tfidf_router 588us / route 累计 3596us）。放模块级 __getattr__
        # 而不是函数内 import：既让 `import route` 不拉起它，又保留
        # `route.semantic_route` 这个可 patch 的模块属性——
        # tests/test_multilingual_routing.py 用 patch("route.semantic_route")
        # 打桩，把名字删干净会让 3 条用例直接 AttributeError。
        # import 成功后写回 globals()，后续查找走正常属性路径不再进这里。
        from tfidf_router import get_router, semantic_route
        globals()["semantic_route"] = semantic_route
        globals()["get_router"] = get_router
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


# 语言重排的排除名单改为 engine_families.ENGINE_LANGS 派生（engines_not_for_lang），
# 三张手写冻结表（_EN_ONLY/_ZH_ONLY/_JA_KO_CN）2026-09-07 收紧删除——
# 新源声明 langs 一次，全部分发路径自动生效。_SOCIAL_ZH_GENERAL 保留
# （social 域中文查询的通用 web 保底，按优先级）：平台词命中 social 域的查询
# （提到「小红书/微信」≠ 搜小红书/微信），通用源覆盖真实主题，防平台噪声全占。
# anysearch 优先：local_bing 直抓 bing.com 对长中文查询存在降级服务风险
# （2026-08-29 实测：整条查询被 Bing 降级为单字「拍」匹配，返回字典页）。


# 仅日/韩需要 must_keep：域主引擎常是中文噪声源，语言补充源不能被 budget 裁掉。
# 中文 / 其它语种：_merge_language_engines 软追加即可，must_keep 会与垂直域抢预算
# （实测：zh must_keep local_bing 会把 finance_macro 多源压成单源、挤掉 openstreetmap）。
# 语言独占源：只服务一种语言、对别的语言**零召回**的源，非匹配语言时从
# combo 里摘除（不只是移尾）。
#
# 为什么与 engines_demote_for_lang 的「移尾」分开：移尾在源本来就排末位时
# 等于没动——实测 cinii 在 academic 域末位，英文查询照样进预算窗口（加槽
# 后窗口扩到 6，它正好在第 6 位）。对「服务不了这个查询」的源，移尾是不够的。
#
# 为什么用白名单而不是「凡声明了具体语言的源都摘」：既有源（zhihu/kor_law/
# bailian 等）的移尾语义是 2026-09-07 review 定下的契约，被
# tests/test_review_round3 与 test_zh_search_quality 两处锁着；新源从接入起
# 就按摘除处理，不回溯改既有行为。后续接入语言独占源时加进本表即可。


# 意图 → (期望引擎数, 是否并行)。P0-005 动态并行度。

# 窄域引擎单点保护：这类引擎「永不返回零结果」或只覆盖单一主题
# （跨域查询产出噪声，如 mdn 的 quantum computing → Cloud computing）。
# definition/fact 意图裁到 1 引擎时，若主引擎是窄域引擎，强制保留 2 引擎，
# 避免单引擎独占时噪声无处可挡。


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

# 垂直域主源保护名单：这些域的专属源被 budget 裁掉后该域等于没源可用。
# 模块级常量（原先定义在 route_query 内，每次调用重建一个 18 元素 frozenset）。


# ── 分支上下文 ─────────────────────────────────────────────────────────────────
# 三条分支共享的输入。逐个传参要 11~14 个形参——那是把「谁在调用」变成「参数
# 摆法」的噪声；用只读上下文传递，分支函数体才能逐字搬运（可验证）。

# TF-IDF 语义路由的最低采纳分（原先藏在 route_query 体内，三条分支都要读它）。
TFIDF_MIN_SCORE = 0.12


@dataclass(frozen=True)
class _RouteCtx:
    """route_query 三条分支的共享输入。done 是收尾闭包（负责 elapsed_ms 与采样）。"""

    query: str
    features: dict[str, Any]
    enabled: set[str]
    mode: str
    depth: str
    context: str
    engines_boost: list[str] | None
    lang_engines: list[str]
    tfidf_best: str | None
    tfidf_best_score: float
    tfidf_scores: list
    done: Callable[..., dict[str, Any]]


# ── 路由主函数 ─────────────────────────────────────────────────────────────────

def _route_by_domain(ctx: _RouteCtx, domain: dict[str, Any], secondary: list[dict[str, Any]]) -> dict[str, Any]:
    """域命中分支：combo 来自域声明，走完整的装配 + 策略 + 扶正流水线。

    is_catch_all（无 patterns 的兜底域）在本分支内判定——它同时决定
    「TF-IDF 推荐可否前置」与 reason 里是否标注覆写。
    """
    query = ctx.query
    features = ctx.features
    enabled = ctx.enabled
    mode, depth, context = ctx.mode, ctx.depth, ctx.context
    engines_boost, lang_engines = ctx.engines_boost, ctx.lang_engines
    tfidf_best, tfidf_best_score = ctx.tfidf_best, ctx.tfidf_best_score
    tfidf_scores = ctx.tfidf_scores
    _done = ctx.done
    is_catch_all = not domain.get("patterns", [])
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
            engines_combo, features, domain.get("name"), enabled, query)
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
    # 注意：本分支的 must_keep 组装**不能**并成一次调用——geo 项与 lang 项
    # 之间夹着垂直域主源保护，而 policy 是按 must_keep 的**顺序**补位的
    # （见 _apply_engine_policy 的 `for e in must_keep`），合并会改变 combo 次序。
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
        must_keep.extend(_lang_must_keep(features, enabled, engines_combo, query))
    engines_combo = _apply_policy_with_new_source_slots(
        domain, engines_combo,
        mode=mode, depth=depth, context=context,
        enabled=enabled, engines_boost=engines_boost, must_keep=must_keep,
    )
    # 语言摘除必须在 policy 之后再走一遍：加槽补位会把「不在 combo 里」
    # 的新专源当成「被预算裁掉」补回来（实测 cinii 在英文查询里被补回
    # academic 域）。语言不匹配的源不是被裁掉，是不该在场——补回来等于
    # 把摘除撤销。判据与顺序说明见 _filter_lang_bound_family docstring。
    engines_combo = _filter_lang_bound_family(
        engines_combo, features, _specs_snapshot(), query)
    # 意图裁剪收口（幂等二次施加）：policy 层的新源 must_keep 补回会放大
    # combo。只对 definition/fact 收口——单源即答语义下，扩容槽（新源
    # 加槽补回的引擎）纯属阶梯等待浪费（实测 academic definition 1 → 3
    # 引擎，test_p0_v25 锁的正是这条契约）。social/news/compare 本身要
    # 多源，扩容与意图同向，且新源可达性由探针测试锁定（
    # test_new_source_reachability），不收口。
    # must_keep 成员豁免：它们有硬保留理由（geo 的 local_openstreetmap
    # 被裁会退化成 wikidata 单源，实测 R_en_geo 矩阵 FAIL）。
    engines_combo, parallel = intent_squeeze(
        engines_combo, features, domain, mode, parallel, must_keep)
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
        except Exception:  # 侧信道：熔断状态只影响 primary 扶正提示（规则见 except_sets）
            pass
        if p and p in engines_combo:
            engines_combo = [p] + [e for e in engines_combo if e != p]

    # 强语义注入（v2.7.10）：判据与「为什么放在 primary 扶正之后」见
    # route_combo.inject_strong_semantic 的 docstring。
    engines_combo, _strong = inject_strong_semantic(
        engines_combo, tfidf_best=tfidf_best, score=tfidf_best_score,
        is_catch_all=is_catch_all, enabled=enabled)
    if _strong:
        confidence = 0.9
    # D4：统一熔断统一处理——语言/geo/次域/TF-IDF 追加的引擎也可能处于熔断态
    engines_combo = breaker_filter(engines_combo, enabled)
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
            + (" [TF-IDF覆写catch-all]" if is_catch_all and tfidf_best and tfidf_best_score > 0.15 and tfidf_best in engines_combo else "")
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


def _route_by_tfidf(ctx: _RouteCtx) -> dict[str, Any]:
    """TF-IDF 语义路由分支：正则未命中，直接用语义推荐引擎 + 通用保底。

    domain 恒为 None——有兜底域时 match_domains 会返回它，走的是域分支。
    """
    query = ctx.query
    features = ctx.features
    enabled = ctx.enabled
    mode, depth, context = ctx.mode, ctx.depth, ctx.context
    engines_boost, lang_engines = ctx.engines_boost, ctx.lang_engines
    tfidf_best, tfidf_best_score = ctx.tfidf_best, ctx.tfidf_best_score
    tfidf_scores = ctx.tfidf_scores
    _done = ctx.done
    domain = None  # 能走到这里说明没有域命中（含兜底域）
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

    must_keep = geo_lang_must_keep(features, enabled, engines_combo, query)
    engines_combo = _apply_policy_with_new_source_slots(
        domain, engines_combo,
        mode=mode, depth=depth, context=context,
        enabled=enabled, engines_boost=engines_boost, must_keep=must_keep,
    )
    # 语言摘除必须在 policy 之后再走一遍：加槽补位会把「不在 combo 里」
    # 的新专源当成「被预算裁掉」补回来（实测 cinii 在英文查询里被补回
    # academic 域）。语言不匹配的源不是被裁掉，是不该在场——补回来等于
    # 把摘除撤销。判据与顺序说明见 _filter_lang_bound_family docstring。
    engines_combo = _filter_lang_bound_family(
        engines_combo, features, _specs_snapshot(), query)
    # 意图裁剪收口（与主域分支同一问题：policy 的 must_keep 补回会放大
    # combo，抵消上面的意图裁剪）。同样只对 definition/fact 收口，
    # must_keep 成员豁免（理由见主域分支注释）。
    engines_combo, parallel = intent_squeeze(
        engines_combo, features, None, mode, parallel, must_keep)
    # ja/ko catch-all 与主域分支同计算方式：anysearch 前二（TF-IDF 直选路径
    # 也会把多语言主力挤掉）
    engines_combo = _inject_multilingual_backup(engines_combo, enabled,
                                                features)
    # D4：统一熔断统一处理（TF-IDF 注入/语言追加可能绕过 _get_engines_combo）
    engines_combo = breaker_filter(engines_combo, enabled)
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


def _route_by_fallback(ctx: _RouteCtx) -> dict[str, Any]:
    """通用保底分支：零分 TF-IDF 与无语义候选都走这里（本地优先 + 免费通用源）。
    """
    query = ctx.query
    features = ctx.features
    enabled = ctx.enabled
    mode, depth, context = ctx.mode, ctx.depth, ctx.context
    engines_boost, lang_engines = ctx.engines_boost, ctx.lang_engines
    tfidf_best, tfidf_best_score = ctx.tfidf_best, ctx.tfidf_best_score
    tfidf_scores = ctx.tfidf_scores
    _done = ctx.done
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
    must_keep_fb = geo_lang_must_keep(features, enabled, fallback_combo, query)
    fallback_combo = _apply_engine_policy(
        fallback_combo, mode=mode, depth=depth, context=context,
        engines_boost=engines_boost, enabled=enabled, must_keep=must_keep_fb,
    )
    # D4：统一熔断统一处理（保底组合可能含熔断引擎）。
    # 兜底次序与本分支的语义一致：通用保底路径只认 anysearch。
    fallback_combo = breaker_filter(fallback_combo, enabled, empty=("anysearch",))

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
        base = {
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
            # 请求侧身份：用户点名了引擎就是那个名字，否则 auto。
            #
            # 结果缓存的键必须用它，**不能用 engines_combo**——combo 是决策
            # 结果，会被 adaptive 学习器按上一次搜索的成败逐次改写。拿结果当
            # 键就是「用缓存让缓存失效」：实测同一查询连跑两次，进键的引擎串
            # 从 `anysearch+octen` 变成 `exa+octen`，30% 的重复查询因此白跑
            # 一遍网络。route 决策缓存早已识别过同一模式（见
            # `_route_state_fingerprint` 刻意排除 adaptive.db 的说明），
            # 结果缓存这条只是绕了一层。
            "engine_request": (engine_override or "auto"),
        }
        base.update(kw)
        sample_route(base, kw)
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
    # 结构化域提前与面查意图门都在 match_domains 内部按契约顺序完成
    # （见 route_domains 模块 docstring）：域精度的全部规则只有那一个实现，
    # 这里不再各调一次，避免「某个守卫忘了在另一条路径上施加」。
    _domain_hits = match_domains(query, domains_cfg,
                                 primary_lang=features.get("primary_lang"))
    domain = _domain_hits[0] if _domain_hits else None
    secondary = _domain_hits[1:] if len(_domain_hits) > 1 else []
    hard_domain = bool(domain and domain.get("patterns"))

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
            # 经模块属性访问：未导入时由 __getattr__ 延迟导入，测试打桩
            # patch("route.semantic_route") 也走这里。直接写全局名会
            # NameError 被 try 吞掉，语义路由静默失效（ruff F821 抓的即此）
            _semantic_route = sys.modules[__name__].semantic_route
            tfidf_scores = _semantic_route(query, top_k=3)
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

    # 三条分支的共享输入在这里定稿（TF-IDF 候选已算完），分支函数才无需逐个传参
    ctx = _RouteCtx(
        query=query, features=features, enabled=enabled, mode=mode, depth=depth,
        context=context, engines_boost=engines_boost, lang_engines=lang_engines,
        tfidf_best=tfidf_best, tfidf_best_score=tfidf_best_score,
        tfidf_scores=tfidf_scores, done=_done,
    )
    if domain:
        return _route_by_domain(ctx, domain, secondary)

    # 正则未命中，用 TF-IDF 结果（已过滤低分）
    if tfidf_best and tfidf_best in enabled:
        return _route_by_tfidf(ctx)

    return _route_by_fallback(ctx)




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

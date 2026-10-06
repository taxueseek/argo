#!/usr/bin/env python3
"""route_domains.py — 域规则的唯一实现：声明、匹配、精度守卫。

## 为什么单独成模块

「查询 → 命中哪些域」是本仓最容易被写坏的一步：域命中会锁死整条引擎组合，
而多数垂直域的 engines_combo **不含通用保底源**，误命中时结果与查询无关且
无从纠正（历史上出现过「monetary policy」→ 艺术馆、「川菜 做法」→ OpenStreetMap）。
这类缺陷的共同形态是「一条正则悄悄吃掉了一整类查询」，因此：

  - 规则**全部是声明式数据**（config.yaml 的 domains 段），本模块只做求值；
  - 精度机制**只有一套**（见下），不允许在别处再长出第二套；
  - tests/test_domain_rule_schema.py 把「写坏」变成可见失败。

## 域规则的字段契约（唯一真源：config.yaml）

| 字段 | 语义 |
|------|------|
| `patterns` | 触发词列表，每项是字符串或 `{match, unless}` |
| `patterns[].match` | 正向触发词：命中即候选 |
| `patterns[].unless` | **该触发词**的否决词：命中则这一条不算（**不要**手写 `^(?!.*...)`） |
| `intent_required` | 点查域意图豁免词：长主题句下必须命中其一，否则让位 |
| `engines_combo` / `primary` / `parallel` / `no_early_stop` | 命中之后怎么执行（route_combo 消费） |

`unless` 与 `intent_required` 是同一件事的两面，都在回答「什么情况下这条命中
是假的」：前者是「查询里出现了别的语义」，后者是「查询里缺了本域的语义」。
历史上它们一个写成内联负向前瞻（藏在 patterns 里）、一个写成 Python 字典表
（`_POINTED_INTENT_RE`），两处都不在 config 里，改域规则要看三个地方。现在
统一为声明式字段。

**`unless` 的作用域必须与被否决的触发词一样窄**：挂在整个域上会把「行政区划
代码」（真地理查询，但含「代码」）一起毙掉。2026-09-21 用 3709 条查询的路由
快照实测到这条漂移，因此否决是逐触发词的，不是逐域的。

## 求值顺序（三阶段，顺序本身是契约）

1. `_match_raw`：按 config 顺序扫描，正向命中 ∧ ¬`exclude_if`，最多 max_n 个；
2. `_social_domain_first`：结构化平台语法命中时把 social 域提到首位；
3. `_intent_gate`：点查域在「长主题句 + 无意图词」时让位（面查信号优先）。

阶段 1 的 max_n 截断发生在阶段 3 之前是**既有语义**（被让位的域仍占名额），
改动它属于行为变更，需单独评估——本模块刻意保持逐位一致，由
`tests/golden` + 路由快照把关。
"""

from __future__ import annotations

import re
from typing import Any

from config import get_domains


# ── 中文内容域语言门 ──────────────────────────────────────────────────────────
# 这些域以中文内容为主，明确的非中文查询（ja/ko/en/latin 等主语言）不应命中。
# 日文/韩文查询常含汉字或谚文字符，易被中文泛内容域的 `[一-\u9fff]` 或单音节
# 子串正则误爆（如韩语「비교」命中天气的「비」、日语汉字命中 chinese_general），
# 在匹配层按主语言跳过即可修正。2026-08 修复。
#
# 留在代码里而不是 config 字段：它对所有成员语义相同、没有逐域差异，加字段只是
# 把同一句话抄 16 遍。判断它该不该动的标准是「成员是否各自有理由」——有则上
# config，无则留表。
_ZH_LANG_GATED_DOMAINS = frozenset({
    "chinese_general", "local_chinese", "chinese_tech_deep",
    "cn_tech_community", "zhihu_content", "zhihu_hot_list",
    "wechat_search", "cn_encyclopedia", "cn_ai_news",
    "moegirl", "juejin", "bilibili", "weibo",
    # 中文政策/百科/医疗域：日韩查询不应进（如日文「政策金利」命中 gov_policy）
    "gov_policy", "baidu_baike", "medical",
})


# ── 域预编译（内容指纹缓存） ──────────────────────────────────────────────────

_compiled_domains: list[dict[str, Any]] | None = None
_compiled_domains_id: tuple | None = None  # (name, patterns/exclude_if 内容指纹)


def _compile_domain_patterns(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把域声明编译成可执行形态（`_rules` = (触发词, 该词的否决词|None) 列表）。

    写坏的正则**静默丢弃**（既有语义：一个域的某条正则写错不该让整条路由崩），
    代价是该域从此永不命中——由 tests/test_domain_rule_schema.py 把这种静默
    失效变成可见失败。
    """
    compiled = []
    for idx, domain in enumerate(domains):
        rules = []
        for p in _pattern_entries(domain):
            match_src, unless_src = _split_pattern_entry(p)
            match_re = _try_compile(match_src)
            if match_re is None:
                continue  # 触发词写坏 → 丢这一条（与旧行为一致）
            rules.append((match_re, _try_compile(unless_src)))
        intent_re = None
        intent_src = domain.get("intent_required")
        if isinstance(intent_src, str) and intent_src:
            try:
                intent_re = re.compile(intent_src)
            except re.error:
                intent_re = None
        compiled.append({**domain, "_idx": idx, "_rules": rules,
                         "_intent": intent_re})
    return compiled


def _try_compile(src: Any) -> re.Pattern | None:
    if not isinstance(src, str) or not src:
        return None
    try:
        return re.compile(src)
    except re.error:
        return None


def _pattern_entries(domain: dict[str, Any]) -> list[Any]:
    patterns = domain.get("patterns", [])
    if isinstance(patterns, str):
        return []
    return list(patterns) if isinstance(patterns, list) else []


def _split_pattern_entry(entry: Any) -> tuple[Any, Any]:
    """触发词条目的两种形态：字符串，或 `{match, unless}`。"""
    if isinstance(entry, dict):
        return entry.get("match"), entry.get("unless")
    return entry, None


def _domain_fires(domain: dict[str, Any], query: str) -> bool:
    """一个域是否成立：任一触发词命中，且它自己的否决词没命中。"""
    for match_re, unless_re in domain["_rules"]:
        if unless_re is not None and unless_re.search(query):
            continue
        if match_re.search(query):
            return True
    return False


def _get_compiled_domains(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    global _compiled_domains, _compiled_domains_id
    # 用内容指纹替代 id(domains)，避免 GC 后 id 复用导致缓存失效/错误。
    # 指纹含规则本体（只记长度会漏「改正则不改条数」的编辑），且必须覆盖
    # exclude_if / intent_required——它们和 patterns 一样会改变命中结果。
    # 拼接后 hash 而非直接进 tuple：长正则列表构造开销大，hash 一次 O(n) 可控。
    import hashlib
    dom_fp = tuple(
        (d.get("name", ""),
         hashlib.sha1(
             "\x00".join(
                 p if isinstance(p, str) else str(p)
                 for p in _rule_sources(d)
             ).encode("utf-8", "replace")).hexdigest()[:12])
        for d in domains
    )
    if _compiled_domains is not None and _compiled_domains_id == dom_fp:
        return _compiled_domains
    _compiled_domains = _compile_domain_patterns(domains)
    _compiled_domains_id = dom_fp
    return _compiled_domains


def warm_compiled_domains() -> None:
    """全量预编译域正则（长驻进程的后台预热入口，如 MCP initialize 后）。

    match_domains 懒编译让「扫到哪个域才编译哪个域」；长驻进程在这里一次
    付清全部域的编译税，首包 tools/call 不再带路由冷启动。失败静默——
    懒编译与全量回退都会自愈，预热是纯优化。
    """
    try:
        _get_compiled_domains(get_domains())
    except Exception:
        pass


def _rule_sources(domain: dict[str, Any]) -> list[str]:
    """参与指纹的全部规则源（新增字段必须加进来，否则编辑它不会让缓存失效）。"""
    out: list[str] = []
    for entry in _pattern_entries(domain):
        match_src, unless_src = _split_pattern_entry(entry)
        out.append(str(match_src))
        if unless_src:
            out.append(str(unless_src))
    intent = domain.get("intent_required")
    if isinstance(intent, str):
        out.append(intent)
    return out


# ── 阶段 2：结构化平台语法 ────────────────────────────────────────────────────

def _social_domain_first(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """命中列表里有 social 域 → 提到首位（无则原样）。

    排位在 social 之前的更具体域保持原序不抢占：「小红书技能排行」的
    redskill_search（小红书技能垂直域）、「我的收藏/我的回答」的
    zhihu_user_data（个人数据意图，收藏/关注是社交平台通用功能词，
    泛 social 域语义更宽）。
    """
    social = [h for h in hits if h.get("name") == "social"]
    if not social:
        return hits
    idx_first_social = hits.index(social[0])
    # 「热搜/热榜」是比泛 social 更具体的意图：查询里带微博/抖音这类平台名时，
    # social 会被提前，把 hot_trending 顶掉——用户问的是榜单，不是社交帖子。
    _SPECIFIC_BEFORE_SOCIAL = ("redskill_search", "zhihu_user_data", "hot_trending")
    idx_specific = next(
        (i for i, h in enumerate(hits)
         if h.get("name") in _SPECIFIC_BEFORE_SOCIAL), None
    )
    if idx_specific is not None and idx_specific < idx_first_social:
        return hits
    return social + [h for h in hits if h.get("name") != "social"]


# ── 阶段 3：点查域意图门 ──────────────────────────────────────────────────────

# 面查信号：查询在研究/排障/对比/学用一个主题，而非定位一个对象
_DIFFUSE_SIGNAL_RE = re.compile(
    r"(?i)\b(issue|bug|regression|reinstall|stale|not.?work|broken|crash"
    r"|how (to|does|do)|why (is|does|do)|difference|vs\.?)\b"
    r"|报错|失效|不生效|不更新|出错|排查|区别|对比"
    # 学用类信号（2026-10-06 补）：实体名只是教程/文档的上下文，不是查询目标。
    # 「claude code mcp tutorial」（4 token）被 ai_model 锁死返回模型规格——
    # 阈值从 5 降到 4 后这类查询进意图门，靠这组词判定让位。
    r"|\b(tutorial|guide|documentation|docs|getting started|how to use)\b"
    r"|教程|文档|指南|怎么用|如何用|入门|上手|使用说明|用法"
)
# 长主题句门槛：去重 token 少于此值不设卡（短查询大概率是点查）。
# 5 → 4（2026-10-06）：4 token 的「claude code mcp tutorial」不过门，
# 实体名（Claude）+ 主题词（tutorial）被 ai_model 锁死，返回模型规格
# 还进了 L2 缓存。4 token 已足够构成主题句式；真点查（"GPT-4o pricing"）
# 普遍 ≤3 token，且 4 token 点查有意图词豁免保护。
_INTENT_MIN_TOKENS = 4


def _intent_gate(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """点查域在「主题句式查询」下让位。

    点查域（声明了 `intent_required` 的域）语义是「查询主体即目标对象」：
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
    if not any(d.get("_intent") is not None for d in hits):
        return hits  # 没有点查域参与：连分词都不必算（本函数最常见的路径）
    try:
        from tfidf_router import tokenize
        n_tokens = len(set(tokenize(query)))
    except Exception:
        return hits  # 分词不可用不设卡（fail-open，同覆盖守卫口径）
    if n_tokens < _INTENT_MIN_TOKENS:
        return hits
    diffuse_hit = re.search(_DIFFUSE_SIGNAL_RE, query) is not None
    kept: list[dict[str, Any]] = []
    for d in hits:
        intent_re = d.get("_intent")
        if intent_re is None:
            kept.append(d)
            continue
        if diffuse_hit:
            continue  # 面查信号压过意图豁免：报错/排查/对比类走通用
        if intent_re.search(query):
            kept.append(d)
            continue
        continue  # 长主题句 + 无意图词：实体词只是上下文，让位
    return kept


# ── 对外入口 ──────────────────────────────────────────────────────────────────

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

    三阶段顺序（截断 → 结构化域提前 → 意图门）是契约，见模块 docstring。
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
        if _domain_fires(domain, query):
            hits.append(domain)
        if len(hits) >= max_n:
            break
    hits = _social_domain_first(hits)
    hits = _intent_gate(hits, query)
    return hits if hits else ([catch_all] if catch_all else [])


def match_domain(query: str, domains: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """兼容旧接口：返回首个命中域（或 catch-all）。"""
    hits = match_domains(query, domains, max_n=1)
    return hits[0] if hits else None


def intent_gated_domains(domains: list[dict[str, Any]] | None = None) -> set[str]:
    """声明了 `intent_required` 的域名集合（回归门用：这份名单必须保持最小）。"""
    if domains is None:
        domains = get_domains()
    return {d.get("name", "") for d in domains if d.get("intent_required")}

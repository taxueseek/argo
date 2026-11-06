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


# ── 域预编译（内容指纹缓存与锚点快筛） ──────────────────────────────────────────

_compiled_domains: list[dict[str, Any]] | None = None
_compiled_domains_id: tuple | None = None  # (name, patterns/exclude_if 内容指纹)
_compiled_domains_ref: int | None = None
_anchor_matcher: Any = None
_anchor_to_domains: dict[str, list[int]] = {}
_anchor_first_char_lens: dict[str, list[int]] = {}
_unconstrained_domains: set[int] = set()

_ANCHOR_LOOKAROUND_RE = re.compile(r"\(\?[=!<][^()]*\)")
_ANCHOR_OPT_GROUP_RE = re.compile(r"\((?:\?:)?[^()]+\)(?:\?|\*|\{0,[^}]*\})")
_ANCHOR_CHARSET_RE = re.compile(r"\[(?:\\.|[^\]\\])*\]\??\*?\+?(?:\{\d+,?\d*\})?")
_ANCHOR_ESCAPE_RE = re.compile(r"\\[wWdDsSbB]|\\.")
_ANCHOR_OPT_CHAR_RE = re.compile(r"[^()|\\]\?|\.\*|\.\+|\.\?")
_ANCHOR_QUANT_RE = re.compile(r"\{\d+,?\d*\}")
_ANCHOR_INNER_GROUP_RE = re.compile(r"\((?:\?:)?([^()]+)\)")
_ANCHOR_RUN_RE = re.compile(
    r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]{1,}"
    r"|[a-zA-Z0-9_\-\.\u00c0-\u024f\u0400-\u04ff\u0600-\u06ff\u0590-\u05ff\u0e00-\u0e7f]{2,}"
)


def _split_top_level_pipe(s: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    in_br = False
    escape = False
    cur: list[str] = []
    for ch in s:
        if escape:
            cur.append(ch)
            escape = False
            continue
        if ch == "\\":
            cur.append(ch)
            escape = True
            continue
        if ch == "[" and not in_br:
            in_br = True
        elif ch == "]" and in_br:
            in_br = False
        elif not in_br:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "|" and depth == 0:
                parts.append("".join(cur))
                cur = []
                continue
        cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _extract_anchors(src: Any, _depth: int = 0) -> set[str] | None:
    """从正则源码中提取一组必须包含其一的字面锚点词集合（全小写）。

    对每个顶层 | 分支提取必现字面量（剥离环视断言、字符集、转义类与可选量词），
    若某分支无顶层字面量但含必现括号组，则递归提取该组的锚点并求并集。
    若任一分支无法提取安全锚点，返回 None（该域归入 _unconstrained_domains）。
    """
    if not isinstance(src, str) or not src or _depth > 3:
        return None
    s = re.sub(r"^\(\?[a-zA-Z]+\)", "", src.strip())
    while s.startswith("(") and s.endswith(")"):
        inner = s[1:-1]
        if len(_split_top_level_pipe(inner)) >= 1 and inner.count("(") == inner.count(")"):
            d = 0
            ok = True
            for ch in inner:
                if ch == "(":
                    d += 1
                elif ch == ")":
                    d -= 1
                    if d < 0:
                        ok = False
                        break
            if ok and d == 0:
                s = inner
                continue
        break
    if s.startswith("?:"):
        s = s[2:]

    parts = _split_top_level_pipe(s)
    if not parts:
        return None
    anchors: set[str] = set()
    for part in parts:
        p_no_la = _ANCHOR_LOOKAROUND_RE.sub(" ", part)
        p_no_opt_grp = _ANCHOR_OPT_GROUP_RE.sub(" ", p_no_la)
        p_outside = re.sub(r"\([^()]*\)", " ", p_no_opt_grp)
        while "(" in p_outside and ")" in p_outside:
            p_outside_next = re.sub(r"\([^()]*\)", " ", p_outside)
            if p_outside_next == p_outside:
                break
            p_outside = p_outside_next
        p_clean = _ANCHOR_CHARSET_RE.sub(" ", p_outside)
        p_clean = _ANCHOR_ESCAPE_RE.sub(" ", p_clean)
        p_clean = _ANCHOR_OPT_CHAR_RE.sub(" ", p_clean)
        p_clean = _ANCHOR_QUANT_RE.sub(" ", p_clean)
        runs = _ANCHOR_RUN_RE.findall(p_clean)
        valid = [r.strip(".-_").lower() for r in runs if len(r.strip(".-_")) >= 2 or any("\u4e00" <= c <= "\u9fff" for c in r)]
        valid = [v for v in valid if v]
        if valid:
            anchors.add(max(valid, key=len))
            continue
        found_grp = False
        for m_grp in _ANCHOR_INNER_GROUP_RE.finditer(p_no_opt_grp):
            grp_anc = _extract_anchors(m_grp.group(1), _depth + 1)
            if grp_anc:
                anchors.update(grp_anc)
                found_grp = True
                break
        if not found_grp:
            return None
    return anchors if anchors else None


def _init_anchor_index(compiled: list[dict[str, Any]]) -> None:
    """初始化全局锚点字典索引（子串闭包 + 首字符长度表贪心扫描）。"""
    global _anchor_matcher, _anchor_to_domains, _anchor_first_char_lens, _unconstrained_domains
    _anchor_to_domains = {}
    _anchor_first_char_lens = {}
    _unconstrained_domains = set()
    all_anchors: set[str] = set()

    for idx, domain in enumerate(compiled):
        entries = _pattern_entries(domain)
        if not entries:
            _unconstrained_domains.add(idx)
            continue
        dom_anchors: set[str] = set()
        unconstrained = False
        for p in entries:
            match_src, _ = _split_pattern_entry(p)
            anc = _extract_anchors(match_src)
            if anc is None:
                unconstrained = True
                break
            dom_anchors.update(anc)
        if unconstrained or not dom_anchors:
            _unconstrained_domains.add(idx)
        else:
            for term in dom_anchors:
                _anchor_to_domains.setdefault(term, []).append(idx)
                all_anchors.add(term)

    if all_anchors:
        by_fc: dict[str, set[int]] = {}
        for t in all_anchors:
            by_fc.setdefault(t[0], set()).add(len(t))
        _anchor_first_char_lens = {
            k: sorted(v, reverse=True) for k, v in by_fc.items()
        }
        _anchor_matcher = True
    else:
        _anchor_matcher = None


def _get_candidate_domain_indices(query: str) -> set[int] | None:
    """通过锚点字典快速扫描返回候选域索引集合；无索引时返回 None。

    为与旧 `_anchor_matcher.finditer` 的非重叠贪心最长匹配语义逐位一致，
    命中长为 ln 的锚点词后游标推进 ln（不回看其内部被长词覆盖的短子串）。
    """
    if not _anchor_matcher:
        return None
    candidates = set(_unconstrained_domains)
    q_low = query.lower()
    qn = len(q_low)
    fc_map = _anchor_first_char_lens
    a2d = _anchor_to_domains
    i = 0
    while i < qn:
        lens = fc_map.get(q_low[i])
        if not lens:
            i += 1
            continue
        rem = qn - i
        matched_ln = 0
        for ln in lens:
            if ln <= rem:
                doms = a2d.get(q_low[i:i + ln])
                if doms:
                    candidates.update(doms)
                    matched_ln = ln
                    break
        i += matched_ln if matched_ln > 0 else 1
    return candidates


def _ensure_domain_compiled(domain: dict[str, Any]) -> list[tuple[Any, Any]]:
    """按需编译单个域的触发词与意图正则（扫到哪个域才编译哪个域）。

    返回的 matcher 是鸭子类型：`re.Pattern` 或 `_LiteralMatcher`，两者都提供
    `.search(query)`（前者返回 `re.Match`，后者返回 `_LiteralMatch`，均带 `.span()`）。
    """
    rules = domain.get("_rules")
    if rules is not None:
        return rules
    rules = []
    for p in _pattern_entries(domain):
        match_src, unless_src = _split_pattern_entry(p)
        match_re = _try_compile(match_src)
        if match_re is None:
            continue  # 触发词写坏 → 丢这一条（与旧行为一致）
        rules.append((match_re, _try_compile(unless_src)))
    intent_src = domain.get("intent_required")
    domain["_intent"] = (
        _try_compile(intent_src) if isinstance(intent_src, str) and intent_src else None
    )
    domain["_rules"] = rules
    return rules


def _compile_domain_patterns(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把域声明编译成可执行形态（`_rules` = (触发词, 该词的否决词|None) 列表）。

    写坏的正则**静默丢弃**（既有语义：一个域的某条正则写错不该让整条路由崩），
    代价是该域从此永不命中——由 tests/test_domain_rule_schema.py 把这种静默
    失效变成可见失败。
    """
    compiled = []
    for idx, domain in enumerate(domains):
        entry = {**domain, "_idx": idx, "_rules": None, "_intent": None}
        _ensure_domain_compiled(entry)
        compiled.append(entry)
    return compiled


# 纯字面 alternation 判定：只认「剥掉开头 (?i) 与最外层括号后，由 | 分隔、
# 每段都是纯字面字符」的 pattern。字符类里出现正则元字符（. * + ? ^ $ \ [ ] ( ) |）
# 或内联结构一律不认——判定必须保守，判不准就交回 re.compile，宁可少优化。
_LITERAL_CHAR = re.compile(r"^[\w\u4e00-\u9fff \-/#&:,.'’]+\Z", re.UNICODE)


def _literal_alternatives(src: str) -> tuple[list[str], bool] | None:
    """判定 pattern 是否为纯字面 alternation；是则返回 (词表, 是否忽略大小写)。

    等价性：`(?i)(a|b)` 的 `re.search` 与「query 含 a 或 b（忽略大小写）」等价；
    无 `(?i)` 时与「query 含 a 或 b（区分大小写）」等价。含非 ASCII 拉丁字母
    （é/ß 等）时 `(?i)` 与 `str.lower()` 的 Unicode 规则不完全一致，故退回。
    """
    if not src:
        return None
    s = src
    ignore_case = False
    if s.startswith("(?i)"):
        s = s[4:]
        ignore_case = True
        # `(?i)` 只作用于其后的表达式。若顶层还有 `|` 且不是「整体被一对括号
        # 包裹」，flag 作用域只覆盖第一个分支（如 `(?i)foo|bar` 的 bar 区分
        # 大小写），此时按整体忽略大小写处理会误判 → 退回 re.compile。
        wrapped = (s.startswith("(") and s.endswith(")")
                   and s.count("(") == 1 and s.count(")") == 1)
        if not wrapped:
            depth = 0
            for ch in s:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "|" and depth == 0:
                    return None
    # 只剥「整体被一对括号包裹」的情形（顶层无其他内容）
    if s.startswith("(") and s.endswith(")") and s.count("(") == 1 and s.count(")") == 1:
        s = s[1:-1]
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    if not parts:
        return None
    words: list[str] = []
    for p in parts:
        if not p or not _LITERAL_CHAR.match(p):
            return None
        if ignore_case:
            if any(ord(c) > 127 and not ("\u4e00" <= c <= "\u9fff") for c in p):
                return None
            p = p.lower()
        words.append(p)
    return words, ignore_case


class _LiteralMatch:
    """`_LiteralMatcher.search` 的返回值：只提供调用方用到的 `.span()`。"""

    __slots__ = ("_span",)

    def __init__(self, span: tuple[int, int]):
        self._span = span

    def span(self) -> tuple[int, int]:
        return self._span


class _LiteralMatcher:
    """纯字面 alternation 的匹配器：子串查找替代 `re.compile` + `re.search`。

    为什么值得（2026-11-04 实测）：域规则 326 条 pattern 里 134 条是纯字面
    alternation（如 `(?i)(金十|jin10)`、`(?i)(技术社区|程序员社区)`），它们每次
    冷进程都要付一次 `re.compile`（326 条全量实测 23.8 ms），而子串查找零编译、
    更快。判定由 `_literal_alternatives` 保守把关，判不准的一律走原正则路径。
    """

    __slots__ = ("_words", "_ignore_case")

    def __init__(self, words: list[str], ignore_case: bool):
        self._words = words
        self._ignore_case = ignore_case

    def search(self, query: str) -> _LiteralMatch | None:
        hay = query.lower() if self._ignore_case else query
        best: tuple[int, int] | None = None
        for w in self._words:
            pos = hay.find(w)
            if pos < 0:
                continue
            # 与 re 的 leftmost 语义一致：取最早出现；同位置取分支顺序在前者
            if best is None or pos < best[0]:
                best = (pos, pos + len(w))
        return _LiteralMatch(best) if best is not None else None


def _try_compile(src: Any) -> Any:
    """编译域规则：纯字面 alternation 走子串匹配，其余走 `re.compile`。

    返回值是鸭子类型（均提供 `.search(query)`，返回值带 `.span()`）：
    `_LiteralMatcher` 或 `re.Pattern`。写坏的正则仍返回 None 被丢弃，
    「编译失败静默丢弃」的既有语义不变（纯字面判定本身保证每段都是字面
    字符，不可能写坏）。
    """
    if not isinstance(src, str) or not src:
        return None
    literal = _literal_alternatives(src)
    if literal is not None:
        return _LiteralMatcher(*literal)
    try:
        return re.compile(src)
    except re.error:
        return None


def _pattern_entries(domain: dict[str, Any]) -> list[Any]:
    patterns = domain.get("patterns", [])
    if isinstance(patterns, str):
        return []
    return list(patterns) if isinstance(patterns, list) else []


# 声明式否决宏（2026-10-29，issue #34.1）：蛋白/化合物/通用医疗词汇在「市场规模/
# 销售额/集采/医保」等商业研报语境下不是查生信/化学/临床试验库。把公共商业否决集
# 抽成命名宏 `@commercial_market`，在 config.yaml 的 unless 字段按需引用，消除多域
# 重复粘贴并保持 `_split_pattern_entry` 单点展开。
_UNLESS_MACROS: dict[str, str] = {
    "@commercial_market": (
        r"(?i)(市场规模|市场份额|销售额|营收|市值|融资|财报|竞争格局|集采|医保|报销|"
        r"价格|多少钱|商业化|市场预测|行情|股价|龙头股|概念股|"
        r"market\s+size|market\s+share|sales|revenue|valuation|market\s+forecast|pricing)"
    ),
    # 比较决策/获奖新闻否决集（2026-10-31 评测基线）：「…是什么」尾部触发词在
    # cn_encyclopedia 与 fact_check 两处各自存在，把比较题（「A 和 B 的区别/
    # 优劣/怎么选」）与得主类新闻题整类锁进点查域。共用一份否决词，新增守卫域
    # 时在 config.yaml 引用 @compare_or_award，不要复制粘贴展开后的正则。
    "@compare_or_award": (
        r"(?i)(区别|比较|对比|怎么选|哪个好|优劣|利弊|差异|权衡|\bvs\b|得主|获奖)"
    ),
    # 本地/免 key 镜像通道否决集（2026-11-04）：`local_*` 六个域是用户**显式
    # 选择**本地化通道的意图（`references/usage.md`：「local_ 前缀是本地化/免 key
    # 镜像通道」），但它们在 config 里排在末尾（idx 92-97），而域匹配先到先得——
    # 于是「本地搜索」「arxiv api」「wikipedia api」「bing news」「百度」全部被
    # 靠前的 catch-all / academic / code_search / news_realtime 截走，那六个域
    # 永远拿不到主位（实测 6/6 失配）。
    #
    # 修法：加 `_channel_intent_gate`（见文件下方），查询含本宏意图词时把
    # local_* 域提到首位。词表与 local_* 六域的 patterns 对齐。
    #
    # **词表只收「带通道语义的写法」，不收裸平台名**：`github repo`、
    # `stackoverflow`、`npm package` 既是通道意图也是普通技术词，收进来会误伤
    # 「github repo 教程 推荐」（实测被误判 local_code）。裸平台名交给各域
    # 自己的 patterns 处理，通道门只认明确的通道修饰语——即「平台名 + 本地/
    # 镜像/免 key」的组合，或「本地 + 平台名」的组合。
    "@local_channel": (
        r"(?i)(本地搜索|local search|零成本|free search|本地新闻|本地中文|"
        r"本地代码|本地学术|本地参考|本地通用|"
        r"arxiv api|semantic scholar|crossref|pubmed|site:arxiv\.org|"
        r"wikipedia api|wiktionary|wikiquote|nominatim|openstreetmap|"
        r"site:baidu|site:sogou|"
        r"(?:github|gitlab|stackoverflow|npm|pip|cargo)\s+(?:repo|package)(?:\s+(?:本地|镜像|免\s?key|local|mirror))|"
        r"(?:本地|镜像)\s+(?:github|gitlab|stackoverflow|npm|pip|cargo))"
    ),
}


def _split_pattern_entry(entry: Any) -> tuple[Any, Any]:
    """触发词条目的两种形态：字符串，或 `{match, unless}`（unless 支持 @宏名 展开）。

    `unless` 可以是：

    - 字符串正则（原样编译）；
    - `@宏名`（整体替换成宏的正则）；
    - **列表**：每项是字符串正则或 `@宏名`，展开后用 `|` 拼接成一个正则。

    列表形态是 2026-11-04 为「一个触发词要挂多个否决集」加的：academic 的
    论文条目原有写作技巧否决，又要加本地通道否决，两个都要生效——拼接成
    一个字符串会让 `(?i)` 的作用域只覆盖前半段（实测宏不展开），而整体
    替换成宏名又会丢掉原有否决。列表让两类否决各占一项、语义清晰。
    """
    if isinstance(entry, dict):
        unless = entry.get("unless")
        if isinstance(unless, list):
            parts: list[str] = []
            for item in unless:
                if isinstance(item, str) and item in _UNLESS_MACROS:
                    parts.append(_UNLESS_MACROS[item])
                elif isinstance(item, str) and item:
                    parts.append(item)
            # 拼接前剥掉后续项开头的 `(?i)`：Python 3.11+ 不允许内联 flag
            # 出现在非开头位置（`(?i)a|(?i)b` 编译失败），而宏与正则都自带
            # `(?i)`，剥掉后语义不变（整条仍由第一个 `(?i)` 控制）。
            if parts:
                merged = parts[0]
                for p in parts[1:]:
                    if p.startswith("(?i)"):
                        p = p[4:]
                    merged += "|" + p
                unless = merged
            else:
                unless = None
        elif isinstance(unless, str) and unless in _UNLESS_MACROS:
            unless = _UNLESS_MACROS[unless]
        return entry.get("match"), unless
    return entry, None


def _domain_fires_with_span(domain: dict[str, Any], query: str) -> tuple[bool, int, int]:
    """一个域是否成立，并返回命中的 (是否成立, match_span_start, match_span_len)。"""
    for match_re, unless_re in _ensure_domain_compiled(domain):
        if unless_re is not None and unless_re.search(query):
            continue
        m = match_re.search(query)
        if m is not None:
            start, end = m.span()
            return True, start, end - start
    return False, 0, 0


def _domain_fires(domain: dict[str, Any], query: str) -> bool:
    """一个域是否成立：任一触发词命中，且它自己的否决词没命中。"""
    fires, _, _ = _domain_fires_with_span(domain, query)
    return fires


def _get_compiled_domains(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    global _compiled_domains, _compiled_domains_id, _compiled_domains_ref
    # 快速指针比对：当传入同一 domains 引用且已编译时 O(1) 返回，免除 97 域的 SHA1 开销
    dom_ref = id(domains)
    if _compiled_domains is not None and _compiled_domains_ref == dom_ref:
        return _compiled_domains
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
    # 槽位懒初始化：实际正则在 _domain_fires 扫到该域时才按需编译，
    # 冷启动路由省去未扫到域（及被语言门跳过的域）的正则编译税。
    _compiled_domains = [
        {**domain, "_idx": idx, "_rules": None, "_intent": None}
        for idx, domain in enumerate(domains)
    ]
    _compiled_domains_id = dom_fp
    _compiled_domains_ref = dom_ref
    _init_anchor_index(_compiled_domains)
    return _compiled_domains


def warm_compiled_domains() -> None:
    """全量预编译域正则（长驻进程的后台预热入口，如 MCP initialize 后）。

    match_domains 懒编译让「扫到哪个域才编译哪个域」；长驻进程在这里一次
    付清全部域的编译税，首包 tools/call 不再带路由冷启动。失败静默——
    懒编译与全量回退都会自愈，预热是纯优化。
    """
    try:
        for d in _get_compiled_domains(get_domains()):
            _ensure_domain_compiled(d)
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
    # video 同理：「抖音视频 红烧肉」要的是视频，不是社交帖子；2026-10-27
    # 加 video 域时一并登记，否则 social 提权会把它压回第二位。
    _SPECIFIC_BEFORE_SOCIAL = ("redskill_search", "zhihu_user_data",
                               "hot_trending", "video")
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


# ── 阶段 4：显式通道意图门（2026-11-04）──────────────────────────────────────
#
# 与 `_intent_gate` 对称但方向相反：那个是「点查域在主题句下让位」，这个是
# 「泛化域在显式通道意图下让位」。
#
# 为什么需要：`local_*` 六个域是用户**显式选择**本地化/免 key 通道的意图
# （`references/usage.md`：「local_ 前缀是本地化/免 key 镜像通道」），但它们在
# config 里排在末尾（idx 92-97），而域匹配先到先得——「本地搜索」「arxiv api」
# 「wikipedia api」「github repo」「bing news」「百度」全部被靠前的泛化域截走，
# 那六个域永远拿不到主位（实测 6/6 失配）。
#
# 为什么不在每个泛化域上加 unless：那是打地鼠——code_search 有四条 pattern
# 命中同一个查询，逐个加否决总会漏（实测加到第三条仍失配）。而且新增泛化域
# 时又会漏。门放在匹配层，一次覆盖所有泛化域。
#
# 判据：查询含 `@local_channel` 宏的意图词，且命中列表里有 `local_*` 域时，
# 把非 local_ 的命中域降级为次域（保留在列表里，不删除——它们仍可能补位）。

_CHANNEL_INTENT_RE: re.Pattern | None = None


def _channel_intent_re() -> re.Pattern | None:
    """惰性编译通道意图正则（复用 @local_channel 宏，避免词表两处维护）。"""
    global _CHANNEL_INTENT_RE
    if _CHANNEL_INTENT_RE is None:
        src = _UNLESS_MACROS.get("@local_channel")
        if not src:
            return None
        try:
            _CHANNEL_INTENT_RE = re.compile(src, re.I)
        except re.error:
            return None
    return _CHANNEL_INTENT_RE


def _channel_intent_gate(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """显式通道意图存在时，把 local_* 域提到首位（泛化域降为次域）。

    不删除泛化域——它们仍可能在 local 通道不可用时补位（fallback 语义）。
    只调整顺序：主域是 local_*，泛化域退到次域位置。
    """
    if not hits:
        return hits
    re_ = _channel_intent_re()
    if re_ is None or re_.search(query) is None:
        return hits
    local_hits = [h for h in hits if str(h.get("name", "")).startswith("local_")]
    if not local_hits:
        return hits  # 没有 local 域命中：不调整（说明 local 域规则没覆盖）
    others = [h for h in hits if not str(h.get("name", "")).startswith("local_")]
    return local_hits + others


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
    secondary_hits: list[tuple[dict[str, Any], float]] = []
    catch_all: dict[str, Any] | None = None
    non_zh = bool(primary_lang and primary_lang not in ("zh", "mixed", "other"))
    candidates = _get_candidate_domain_indices(query)
    q_len = max(len(query), 1)

    for idx, domain in enumerate(compiled):
        if not domain.get("patterns", []):
            catch_all = domain
            continue
        if non_zh and domain.get("name") in _ZH_LANG_GATED_DOMAINS:
            continue
        if candidates is not None and idx not in candidates:
            continue
        fires, span_start, span_len = _domain_fires_with_span(domain, query)
        if fires:
            if not hits:
                hits.append(domain)
            else:
                pos_ratio = span_start / q_len
                # 方案二：跨度高（特异性强）优先提拔；同跨度下严格保持前序特化域顺位稳定
                score = (span_len * 2.0) - (idx * 0.1) + (pos_ratio * 0.01)
                secondary_hits.append((domain, score))
            if len(hits) + len(secondary_hits) >= max_n:
                break

    if secondary_hits:
        secondary_hits.sort(key=lambda x: x[1], reverse=True)
        hits.extend(h[0] for h in secondary_hits)

    hits = _social_domain_first(hits)
    hits = _intent_gate(hits, query)
    # 显式通道意图门放在最后：它只重排已命中列表（local_* 提前），不改变
    # 「哪些域命中」这个事实，因此不影响前面的意图门与社交提前逻辑。
    hits = _channel_intent_gate(hits, query)
    hits = hits[:max_n]
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

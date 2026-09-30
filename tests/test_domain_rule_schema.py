#!/usr/bin/env python3
"""test_domain_rule_schema.py — 域规则的 schema 与精度机制门禁（纯本地）。

## 这道门禁在防什么

域规则是「一条正则悄悄吃掉一整类查询」的重灾区：命中的域会锁死整条引擎组合，
而多数垂直域的 combo 不含通用保底源，误命中时结果与查询无关且无从纠正。
历史上同一类事故在三种文字系统上各复发一次：

  - ASCII 子串：monet ⊂ monetary、hn ⊂ technique、VIN ⊂ Kevin（2026-09-19）
  - 整词多义：指南 ⊂ 编程指南、指数 ⊂ 指数函数（2026-09-19）
  - CJK 单字：산 ⊂ 산업、강 ⊂ 강아지、川 ⊂ 川菜、塔 ⊂ 塔罗牌（2026-09-21）

每次修完都「加了几条排除词」，但排除词的**写法和位置**没有约束，于是下一个人
又会把否决写成内联 `^(?!.*...)`、或者干脆不加。这道门禁把「怎么写」变成可检查
的契约：

| 检查 | 拦住什么 |
|------|----------|
| 字段白名单 | 往域上塞未声明的字段（route 会静默忽略） |
| 触发词条目形态 | `patterns` 里塞非字符串非 `{match, unless}` 的东西 |
| 禁止查询级内联否决 | 再手写 `^(?!.*...)`——它必须写成 `unless` |
| 全部正则可编译 | 写坏的正则被 route_domains 静默丢弃 → 该域永久失能 |
| `unless` 有反例 | 加了否决却没有任何查询验证它真的生效 |
| `intent_required` 名单最小 | 意图门被扩充 → 静默的能力回退（见 precision 测试） |
"""

from __future__ import annotations

import os
import re
import sys

import pytest

SCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from config import load_config  # noqa: E402
from route_domains import (  # noqa: E402
    _compile_domain_patterns,
    _pattern_entries,
    _split_pattern_entry,
)

DOMAINS = [d for d in (load_config().get("domains") or []) if isinstance(d, dict)]

# 域声明允许出现的字段。**新增字段必须同时改这里与 route_domains 的模块 docstring**，
# 否则字段会被静默忽略（读 config 的人以为它生效了）。
ALLOWED_FIELDS = frozenset({
    "name", "desc", "patterns", "intent_required",
    "engines_combo", "primary", "fallback", "parallel",
    "no_early_stop", "early_stop_min_results", "quote", "research_only",
    # search.py 的 `_domain_score_floors()` 消费：按域/源设相关性地板
    "score_floors",
})

# 「查询级内联否决」的形状：行首锚定 + 负向前瞻扫全文。这类写法必须写成 unless。
_INLINE_QUERY_VETO = re.compile(r"\^\(\?!")


def _entries_of(domain: dict) -> list:
    return _pattern_entries(domain)


def test_domain_fields_are_declared():
    unknown = {}
    for d in DOMAINS:
        extra = sorted(set(d) - ALLOWED_FIELDS)
        if extra:
            unknown[d.get("name", "?")] = extra
    assert not unknown, (
        "域上出现未声明字段（route_domains 不认识 → 静默无效）。"
        f"要么删掉，要么加进 ALLOWED_FIELDS 与模块 docstring：{unknown}")


def test_pattern_entries_have_known_shape():
    bad = []
    for d in DOMAINS:
        for p in _entries_of(d):
            if isinstance(p, str):
                continue
            if isinstance(p, dict) and set(p) <= {"match", "unless"} and p.get("match"):
                continue
            bad.append((d.get("name"), p))
    assert not bad, (
        "触发词条目只允许字符串或 {match, unless}（match 必填）："
        f"{bad}")


def test_no_inline_query_level_veto_in_patterns():
    """查询级否决必须写成 `unless`，不许手写 `^(?!.*...)`。

    内联写法把「否决」和「触发」压进同一条正则，读者无法一眼看出哪些词是配套的；
    更糟的是它只在**这一条**正则里生效，下次想给同域另一条触发词加同类约束时，
    很容易误以为是域级规则（2026-09-21 实测：把否决误挂到域级，3709 条查询里
    3 条漂移，其中「行政区划代码」是真地理查询被毙）。
    """
    offenders = []
    for d in DOMAINS:
        for p in _entries_of(d):
            src, _unless = _split_pattern_entry(p)
            if isinstance(src, str) and _INLINE_QUERY_VETO.search(src):
                offenders.append((d.get("name"), src[:80]))
    assert not offenders, (
        "patterns 里出现内联查询级否决 `^(?!.*...)`，请改写成 unless："
        f"{offenders}")


def test_every_rule_compiles_and_survives_compilation():
    """写坏的正则会被静默丢弃 → 该域永久失能。这里把静默变可见。

    同时对账条数：声明了几条触发词、几条 unless，就必须编译出几条。少一条说明
    有正则写坏或条目形态不对。
    """
    broken, mismatched = [], []
    for src, comp in zip(DOMAINS, _compile_domain_patterns(DOMAINS)):
        want = len(_entries_of(src))
        want_unless = sum(
            1 for p in _entries_of(src) if _split_pattern_entry(p)[1])
        rules = comp.get("_rules") or []
        got_unless = sum(1 for _m, u in rules if u is not None)
        if want != len(rules) or want_unless != got_unless:
            mismatched.append(
                f"{src.get('name')}: 声明 {want} 条触发词/{want_unless} 条 unless，"
                f"编译成功 {len(rules)}/{got_unless}")
        for p in _entries_of(src):
            match_src, unless_src = _split_pattern_entry(p)
            for label, s in (("match", match_src), ("unless", unless_src)):
                if not isinstance(s, str) or not s:
                    continue
                try:
                    re.compile(s)
                except re.error as e:
                    broken.append(f"{src.get('name')} {label}: {s[:60]} → {e}")
    assert not broken, f"域正则编译失败（该域已静默失能）：{broken}"
    assert not mismatched, f"有规则被静默丢弃：{mismatched}"


@pytest.mark.parametrize(
    "domain_name,query",
    [
        # 每条 unless 至少一条反例：否决必须真的能拦住它要拦的东西
        ("weather_query", "气候变化 极端天气 2026"),
        ("geo_places", "在哪里 查看 报错 日志"),
        ("geo_places", "图片 位置 居中 CSS"),
        # academic unless（2026-09-30）：裸 GPT/BERT 子串不再把产品语境抢进论文域
        ("academic", "chatgpt 使用技巧"),
        ("academic", "如何用 GPT 写周报"),
    ],
)
def test_unless_has_a_negative_case(domain_name, query):
    """`unless` 不是装饰：声明的每条否决都要有查询证明它生效。

    这里只覆盖「否决生效」的方向；「真查询不被误伤」的方向由
    tests/test_domain_pattern_precision.py 的 LEGITIMATE_ROUTES 双向锁。
    """
    from route_domains import match_domains
    hits = {d.get("name") for d in match_domains(query, max_n=3)}
    assert domain_name not in hits, (
        f"{query!r} 仍命中 {domain_name}——该域的 unless 没生效或已被删掉")


def test_unless_declared_domains_are_covered_by_negative_cases():
    """声明了 unless 的域，必须在上面的反例表里出现（新增域时别忘配反例）。"""
    covered = {"weather_query", "geo_places", "academic"}
    declared = {d.get("name") for d in DOMAINS
                if any(_split_pattern_entry(p)[1] for p in _entries_of(d))}
    missing = sorted(declared - covered)
    assert not missing, (
        f"这些域声明了 unless 但没有反例用例（加一条 query 到本文件的参数表）：{missing}")

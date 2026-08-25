#!/usr/bin/env python3
"""query_enhance.py — 首轮查询增强（无 LLM）：归一化 + 拆词变体 + 复杂度门控

目标：让「简单问题首轮就命中、复杂问题才走多轮」，避免简单问题被拖成多轮
浪费 token。纯规则，零外部依赖，中英双语。

设计（对齐 Adaptive-RAG 的复杂度分流 + Algolia 的 query expansion/relaxation）：
  1. normalize_query      词形规范化：全角→半角、拆斜杠、压多余空格。
                           治「型号/日期/分隔符检索命中难」，如 LongCat-2.0/1.6 万亿。
  2. retrieval_variants   生成有序检索变体（归一化主词 + 拆连字符型号词 + 概念/同义词）。
                           供「首轮多路召回」用（低复杂度时用少数变体并行补齐召回）。
  3. complexity_gate      复杂度门控：把查询分为 low / medium / high。
                           low  → 单轮多路召回即可，不放行多轮 recovery；
                           high → 允许多轮/全变体（复杂、多跳、对比）。

复杂度信号来源：query_understanding 的 intents/confidence/multi_intent_splits + 长度 +
多跳词（对比/因果/区别/为什么/vs 等）。与 query_understanding 正交，仅做启发式分级。
"""

from __future__ import annotations

import re

# ── 词形规范化 ──────────────────────────────────────────────────────────────

# 全角标点/空白 → 半角：中文输入常见，易导致精确源（版本号/路径/符号）漏检
_FULL_TO_HALF = str.maketrans(
    "，。！？：；（）【】《》“”‘’％＋－",
    ",.!?:;()[]<>\"\"''%+-",
)
# 全角字母数字/符号 → 半角：ＡＢＣ１２３ 常见于粘贴文本，与标点同批处理
_FULL_ALNUM = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ￥～",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz¥~",
)
_FULL_SPACE = str.maketrans("\u3000", " ")


def normalize_query(query: str) -> str:
    """词形规范化：全角→半角（标点+字母数字）、拆斜杠、压缩多余空格。
    保留连字符（避免 GPT-5 拆散）。"""
    if not query or not isinstance(query, str):
        return query
    q = query.translate(_FULL_TO_HALF).translate(_FULL_ALNUM).translate(_FULL_SPACE)
    q = re.sub(r"/", " ", q)          # LongCat-2.0/1.6 万亿 → LongCat-2.0 1.6 万亿
    q = re.sub(r"\s+", " ", q).strip()
    return q


# ── 检索变体（首轮多路召回用）────────────────────────────────────────────────

def retrieval_variants(query: str, max_n: int = 3) -> list[str]:
    """返回有序检索变体：归一化主词 + 拆连字符型号词 + 概念/同义词。

    max_n 默认 3（低复杂度只取少量，控制成本）。去重、保持稳定顺序。
    """
    if not query or not isinstance(query, str):
        return []
    base = normalize_query(query)
    out = [base]

    # 拆连字符型号：LongCat-2.0 → LongCat 2.0（仅字母后接数字，避免拆分普通词）
    split_hyphen = re.sub(r"(?<=[A-Za-z])\-(?=[0-9])", " ", base)
    split_hyphen = re.sub(r"\s+", " ", split_hyphen).strip()
    if split_hyphen and split_hyphen != base:
        out.append(split_hyphen)

    # 概念/同义变体（复用 query_variants 的概念/缩写表，只取首个替代）
    concept = _concept_variant(base)
    if concept and concept not in out:
        out.append(concept)

    seen = set()
    uniq = []
    for v in out:
        k = v.strip().lower()
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    return uniq[:max_n]


def _concept_variant(base: str) -> str | None:
    """把命中的概念/缩写替换为首个等价表述；无命中返回 None。"""
    try:
        from query_variants import CONCEPT_MAP, ACRONYM_MAP
    except ImportError:
        return None
    ql = base.lower()
    for word, alts in CONCEPT_MAP.items():
        if word in ql and alts:
            target = alts[0]
            if target.lower() == ql:
                continue
            return re.sub(r"\b" + re.escape(word) + r"\b", target, base, count=1,
                          flags=re.IGNORECASE)
    for word, full in ACRONYM_MAP.items():
        if word in ql:
            return re.sub(r"\b" + re.escape(word) + r"\b", full, base, count=1,
                          flags=re.IGNORECASE)
    return None


# ── 复杂度门控 ──────────────────────────────────────────────────────────────

# 多跳 / 对比 / 归因信号：这类问题往往需要多轮或全变体位
_MULTI_HOP = re.compile(
    r"对比|比较|区别|差异|为什么|导致|原因|vs|versus|between|compared|"
    r"impact\s+of|effect\s+of|influence|relationship|影响|关系|关联|如何影响|"
    r"列举|有哪些|总结|回顾|evolution|timeline|历程|归因|走势|趋势",
    re.I,
)


def complexity_gate(query: str, qu=None) -> str:
    """返回查询复杂度：low / medium / high。

    low  → 短、单意图、无多跳词、置信度较高 → 单轮多路召回即可，不放行多轮 recovery。
    high → 多跳/对比词 或 多意图 或 较长 → 允许多轮/全变体。
    medium → 其余。
    """
    if not query or not isinstance(query, str):
        return "low"
    ql = query.strip()
    length = len(ql)

    multi_hop = bool(_MULTI_HOP.search(ql))

    intent_cnt = 0
    conf = 0.0
    multi_splits = 0
    if qu is not None:
        intent_cnt = len(getattr(qu, "intents", []) or [])
        conf = float(getattr(qu, "confidence", 0.0) or 0.0)
        multi_splits = len(getattr(qu, "multi_intent_splits", []) or [])

    if multi_hop or intent_cnt >= 2 or multi_splits >= 2 or length > 60:
        return "high"
    if length <= 24 and intent_cnt <= 1 and not multi_hop:
        return "low"
    if conf >= 0.6 and length <= 30 and not multi_hop:
        return "low"
    return "medium"


def low_complexity(query: str, qu=None) -> bool:
    """是否为「首轮即可解决」的低复杂度查询。"""
    return complexity_gate(query, qu) == "low"

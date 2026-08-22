#!/usr/bin/env python3
"""research_expand.py — 检索扩词（不是问题树）。

把原 decompose_query 的正则启发式收成扩词器。
Agent 的 MECE 问题树走 research_work_packages，不要把扩词当研究拆解。
"""

from __future__ import annotations

import re
def expand_query(query: str, num_sub: int = 4) -> list[dict[str, str]]:
    """按关键词特征扩检索词。产出的是搜索变体，不是可验证子问题。"""
    sub_queries: list[dict[str, str]] = []

    has_chinese = any("\u4e00" <= c <= "\u9fff" for c in query)
    has_english = any(c.isascii() and c.isalpha() for c in query)

    if has_chinese and has_english:
        eng_words = " ".join(w for w in query.split() if w.isascii() and len(w) > 2)
        if eng_words:
            sub_queries.append({
                "query": eng_words,
                "intent": "英文核心概念搜索",
                "strategy": "english_focused",
            })

    year_match = re.search(r"20\d{2}", query)
    if year_match:
        year = year_match.group()
        sub_queries.append({
            "query": f"{query} {year} latest update",
            "intent": f"{year}年最新进展",
            "strategy": "temporal",
        })

    compare_match = re.search(r"(?:vs| versus |对比|比较|和|与|及)", query, re.I)
    if compare_match:
        parts = re.split(r"(?:vs| versus |对比|比较|和|与|及)", query, flags=re.I)
        for part in parts[:2]:
            part = part.strip()
            if part and len(part) > 2:
                sub_queries.append({
                    "query": part,
                    "intent": f"独立搜索：{part[:20]}",
                    "strategy": "split_compare",
                })

    how_match = re.search(r"(?:如何|怎么|how|why|为什么|最佳实践|best practice)", query, re.I)
    if how_match:
        sub_queries.append({
            "query": f"{query} tutorial guide best practices",
            "intent": "教程/最佳实践",
            "strategy": "tutorial",
        })

    bug_match = re.search(
        r"(?:bug|error|问题|报错|故障|issue|panic|crash|exception)", query, re.I
    )
    if bug_match:
        sub_queries.append({
            "query": f"{query} solution fix workaround community",
            "intent": "社区解决方案",
            "strategy": "community_fix",
        })

    academic_match = re.search(
        r"(?:论文|paper|arxiv|学术|综述|review|survey|研究)", query, re.I
    )
    if academic_match:
        sub_queries.append({
            "query": f"{query} arxiv semantic scholar 2024 2025",
            "intent": "学术文献补充",
            "strategy": "academic",
        })

    security_match = re.search(
        r"(?:CVE|漏洞|vulnerability|security|exploit|PoC)", query, re.I
    )
    if security_match:
        sub_queries.append({
            "query": f"{query} NVD exploit PoC advisory",
            "intent": "安全数据源补充",
            "strategy": "security",
        })

    finance_match = re.search(
        r"(?:股价|财报|基金|股票|行情|金融|financial|earnings|stock)", query, re.I
    )
    if finance_match:
        sub_queries.append({
            "query": f"{query} 东方财富 雪球 研报",
            "intent": "金融数据补充",
            "strategy": "finance",
        })

    if not sub_queries:
        sub_queries.append({
            "query": query,
            "intent": "原始查询",
            "strategy": "direct",
        })

    if len(sub_queries) < num_sub:
        sub_queries.append({
            "query": query,
            "intent": "综合搜索",
            "strategy": "general",
        })

    return _deduplicate_sub_queries(sub_queries[:num_sub])


def _deduplicate_sub_queries(sub_queries: list[dict[str, str]]) -> list[dict[str, str]]:
    """基于 Jaccard 相似度去重子查询。"""
    def _tokens(q: str) -> set[str]:
        return set(re.findall(r"[a-zA-Z]+|[\u4e00-\u9fff]", q.lower()))

    unique: list[dict[str, str]] = []
    seen_tokens: list[set[str]] = []
    for sq in sub_queries:
        tokens = _tokens(sq["query"])
        is_dup = False
        for prev in seen_tokens:
            jaccard = len(tokens & prev) / max(len(tokens | prev), 1)
            if jaccard > 0.6:
                is_dup = True
                break
        if not is_dup:
            unique.append(sq)
            seen_tokens.append(tokens)
    return unique


# 旧名保留给测试与外部 import
decompose_query = expand_query

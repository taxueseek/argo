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

    # 「{query} {year} latest update」分支已删除：year_match 来自查询本身，
    # 查询必然已含该年份，拼出的变体对引擎就是原查询 + 噪声词（实测相对
    # 原查询新信息率仅 0.18，纯冗余且抢占子查询槽位）。年份限定语义已由
    # 原查询表达，时间窗过滤走 search 的 --since/--until。

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
        r"(?:股价|财报|年报|中报|季报|业绩|营收|利润|基金|股票|行情|金融"
        r"|financial|earnings|stock|revenue)", query, re.I
    )
    if finance_match:
        sub_queries.append({
            "query": f"{query} 东方财富 雪球 研报",
            "intent": "金融数据补充",
            "strategy": "finance",
        })

    # anchor：原查询本身永远占一席（唯一保证与用户意图对齐的子查询，
    # 也是 dossier 的 baseline）。模板产出不含原查询文本时补上。
    if len(sub_queries) < num_sub and all(
            sq["query"] != query for sq in sub_queries):
        sub_queries.append({
            "query": query,
            "intent": "综合搜索",
            "strategy": "general",
        })

    return _deduplicate_sub_queries(sub_queries[:num_sub])


def _deduplicate_sub_queries(sub_queries: list[dict[str, str]]) -> list[dict[str, str]]:
    """按「新信息率」去重子查询。

    旧判据（与已有集合 Jaccard>0.6 判重）与扩词目的相反：Jaccard 衡量
    重合度，扩词的价值恰恰是增量。实测（2026-09-20，char 粒度）：
      「{query} 2024 latest update」0.83 —— 杀掉了原查询本身（anchor）；
      「{query} arxiv semantic scholar ...」0.77 —— 杀掉了带 3 个英文
        新词的学术变体；
      「what is {query} and how does it work」套中文实体 0.59 —— 冗余
        壳反而存活。
    判据改为：子查询的新 token（未出现在已有集合）占比 <0.25 判冗余；
    direct/general（=原查询）是 anchor，永不剔除。

    token 粒度：英文按词、中文按双字组。旧实现的中文单字粒度下任意扩展
    都摊薄重合度（0.5-0.59 全在阈值下），判据对中文形同虚设。
    """
    def _tokens(q: str) -> set[str]:
        chars = re.findall(r"[\u4e00-\u9fff]", q.lower())
        words = re.findall(r"[a-zA-Z]+", q.lower())
        return set(words) | {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}

    def _novelty(tokens: set[str], covered: set[str]) -> float:
        if not tokens:
            return 1.0
        return len(tokens - covered) / len(tokens)

    anchor = {"direct", "general"}
    unique: list[dict[str, str]] = []
    covered: set[str] = set()
    for sq in sub_queries:
        toks = _tokens(sq["query"])
        if unique and sq.get("strategy") not in anchor \
                and _novelty(toks, covered) < 0.25:
            continue
        unique.append(sq)
        covered |= toks
    return unique


# 旧名保留给测试与外部 import
decompose_query = expand_query

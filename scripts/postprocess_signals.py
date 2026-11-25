#!/usr/bin/env python3
"""postprocess_signals.py — 加工段两个可选信号：TF-IDF 语义抛光 + 数字交叉验证。

2026-10-16 从 search_pipeline.postprocess 提取（方案二梳理的「真源收敛」一步）。
留在主管线里的代价是实测过的：+37 行把 search_pipeline 顶破祖父上限 1033，
且每个特性自带 try/except + 环境开关，主管线的异常面被无谓放大。提取后：

  - 主管线每个特性只留 3~4 行调用点（与 search_attribution / phrase_filter
    同一形态，本文件也只留调用点的那条纪律在这里是反过来的应用）；
  - 开关与它守护的功能同文件：ARGO_TFIDF_RERANK / ARGO_NUMBER_CROSSCHECK
    不再散在主管线里，读代码的人在同一处看到「功能+门控+降级」；
  - 失败各自降级：本模块的函数不向主管线抛异常，异常只留一行日志。

两个入口都是「纯加工」签名：吃主管线的中间态，吐改写后的结果。
"""
from __future__ import annotations

from typing import Any, Callable


def apply_tfidf_polish(
    query: str,
    merged: list[dict[str, Any]],
    max_results: int,
    mode: str,
    reranker_status: str,
    rank_method: str,
    log: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], str, bool]:
    """精排降级时的零依赖 TF-IDF 语义抛光（防 SERP 壳链靠 RRF 高分回顶）。

    返回 (merged, rank_method, ran)：
      - ran=True：确实重排过，rank_method 已带 "+tfidf"/"tfidf" 标记；
      - ran=False：未触发（开关关 / fast 档 / 结果不足 / 非降级态）或执行
        失败。两者对调用方含义相同——本轮排序没有 TF-IDF 贡献——由调用方
        决定 none 兜底；异常只留一行日志，不外抛。

    mode 透传给 rerank_tfidf：fast/budget 档其内部直接跳过（零延迟）。
    """
    from search_rank import _RERANK_DEGRADED_STATUSES

    # fast/budget 档先拦：rerank_tfidf 内部虽也会跳过，但那样 ran 会被误置
    # True、rank_method 被误标 "+tfidf"——本轮实际没有任何重排贡献
    # （2026-10-16 本模块测试锁住该语义）。
    if mode in ("fast", "budget"):
        return merged, rank_method, False
    if not merged or len(merged) <= 1:
        return merged, rank_method, False
    if reranker_status not in _RERANK_DEGRADED_STATUSES:
        return merged, rank_method, False
    try:
        from engine_env import env_flag
        if not env_flag("ARGO_TFIDF_RERANK", True):
            return merged, rank_method, False
        from rerank_tfidf import rerank as _tfidf_rerank
        merged = _tfidf_rerank(query, merged, top_k=max_results, mode=mode)
        rank_method = (rank_method + "+tfidf") if rank_method != "none" else "tfidf"
        return merged, rank_method, True
    except Exception as e:
        if log:
            log(f"TF-IDF rerank 跳过: {type(e).__name__}")
        return merged, rank_method, False


def detect_number_conflict(
    query: str,
    merged: list[dict[str, Any]],
    mode: str,
    depth: str,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """多源同指标数值分歧信号（与 fact_alignment 并列的轻量核验提示）。

    仅 deep/auto 且结果 ≥3 时启用；只对比前 10 条（O(n) 正则扫描，<1ms）。
    有分歧返回 {has_conflict, indicator, values, sources, suggestion}，
    无分歧 / 未启用 / 执行失败返回 None（异常只留一行日志不外抛）。
    values[i] 与 sources[i] 同序配对。
    """
    if not merged or len(merged) < 3 or mode in ("fast", "budget") or depth == "fast":
        return None
    try:
        from engine_env import env_flag
        if not env_flag("ARGO_NUMBER_CROSSCHECK", True):
            return None
        from fact_crosscheck import crosscheck_numbers
        nc = crosscheck_numbers(query, merged[:10])
        if nc.get("has_conflict"):
            return {
                "has_conflict": True,
                "indicator": nc.get("indicator", ""),
                "values": nc.get("values", []),
                "sources": nc.get("sources", []),
                "suggestion": nc.get("suggestion", ""),
            }
    except Exception as e:
        if log:
            log(f"数字交叉验证跳过: {type(e).__name__}")
    return None


# ── 查询词回声（2026-10-17 实锤）──────────────────────────────────────────────
# 上游把整句当单词去查时，返回的是查询中某一个词的词典释义/同主题页。
# 判据与理由在 query_signals.is_query_echo；这里只做「后处理侧」的接线：
# 主管线（search_pipeline）只留调用点，逻辑不落在那里。


_LIST_DOMAINS: frozenset[str] = frozenset({
    "hot_trending",
    "zhihu_hot_list",
    "ths_hot_search",
})


def echo_recovery_needed(
    results: list[dict[str, Any]],
    query: str,
    *,
    domain: str | None = None,
) -> bool:
    """恢复链是否该因「查询词回声」触发——有结果、但整批是查询某一个词的
    词典释义（判据见 query_signals.is_query_echo）。

    零结果走 `if not merged` 的老路；这里补的是「有结果却是垃圾」：anysearch
    超时后仅剩的 local_bing 回声会被原样交付（2026-10-17 实锤）。
    榜单型域（hot_trending 等）按设计忽略查询词，标题偶发命中查询单字不是回声。
    """
    if domain in _LIST_DOMAINS:
        return False
    try:
        from query_signals import is_query_echo
    except ImportError:
        return False  # 判据不可用不拦（fail-open）
    return bool(results) and is_query_echo(results, query)


def interleave_by_source(
    results: list[dict[str, Any]],
    max_results: int,
) -> list[dict[str, Any]]:
    """按平台轮询交错取样，保证全网热榜等横向聚合域各平台头部均衡露出。"""
    if not results or max_results <= 0:
        return []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        src = str(r.get("source") or r.get("_engine") or "unknown")
        buckets.setdefault(src, []).append(r)
    if len(buckets) <= 1:
        return results[:max_results]
    ordered = list(buckets.values())
    out: list[dict[str, Any]] = []
    idx = 0
    while len(out) < max_results:
        progressed = False
        for lst in ordered:
            if idx < len(lst):
                progressed = True
                out.append(lst[idx])
                if len(out) >= max_results:
                    break
        if not progressed:
            break
        idx += 1
    return out


def drop_echo_items(results: list[dict[str, Any]],
                   query: str) -> list[dict[str, Any]]:
    """过滤查询词回声条目，让恢复链继续往「换引擎」走。

    恢复链的 L1 放宽查询后，原引擎仍会返回同一批词典释义；若把它们算作
    「恢复成功」，L3 换引擎永不执行（2026-10-17 实测 level_used=L1 而交付的
    仍是回声）。

    两段判据，缺一不可：
      1. 整批先过 is_query_echo——它要求「≥3 条」正是为了不误伤一两条短标题
         的正确答案（实测 "Paris capital" 对 "what is the capital of France"
         只命中 capital 一个词，但那就是答案）；
      2. 整批判定为回声后，再逐条剔除「只命中 1 个查询词」的条目——一批里
         混着的好结果不该被连坐。
    """
    if not results:
        return results
    try:
        from query_signals import is_query_echo
        from tfidf_router import tokenize
    except ImportError:
        return results  # 判据不可用不过滤（fail-open）
    if not is_query_echo(results, query):
        return results
    q_set = set(tokenize(query))
    return [
        r for r in results
        if not (isinstance(r, dict)
                and 0 < len(q_set & set(tokenize(r.get("title") or ""))) <= 1)
    ]

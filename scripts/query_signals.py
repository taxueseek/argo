#!/usr/bin/env python3
"""query_signals.py — 查询改写与「结果够不够」的判定。

从 search.py 中外提。这里放的全是**本地启发式**判断，不发起网络请求、
不依赖搜索编排状态，因此可以被搜索之外的层（评测、路由、批量任务）复用。

两类信号：
  - 查询改写（apply_query_rewrite）：把口语化查询换成更适合检索的形式
  - 早停判定（results_sufficient / cumulative_sufficient / query_coverage_ok）：
    结果是否已经够用、可以不再等慢源
"""

from __future__ import annotations

from typing import Any


def apply_query_rewrite(query: str) -> tuple[str, dict | None]:
    """统一查询改写逻辑，返回 (改写后的查询, 改写结果字典)。

    改写失败时静默返回原查询，不影响搜索流程。
    """
    try:
        from query_rewriter import rewrite_query as do_rewrite
        result = do_rewrite(query)
        if result["rewritten"] and result["confidence"] >= 0.7:
            return result["rewritten"], result
    except ImportError:
        pass  # query_rewriter 模块不可用，使用原查询
    except Exception as e:
        import logging
        logging.getLogger("unified_search").debug(f"查询改写跳过: {type(e).__name__}")
    return query, None


def results_sufficient(
    results: list[dict[str, Any]],
    mode: str = "auto",
    min_results: int | None = None,
    query: str = "",
) -> bool:
    return _sufficient_internal(results, mode, min_results, query)


def cumulative_sufficient(raw_results: dict[str, list[dict[str, Any]]],
                          mode: str = "auto",
                          min_results: int | None = None,
                          query: str = "") -> bool:
    """跨引擎累计结果是否已够（wave-2 提前终止判定）。

    与 results_sufficient 同阈值，但把 raw_results 里所有已完成的
    引擎结果合并成一条列表再判，避免单一引擎不足时重复等待慢源。
    """
    merged: list[dict[str, Any]] = []
    for res in raw_results.values():
        if not res:
            continue
        merged.extend(
            r for r in res if isinstance(r, dict) and "error" not in r
        )
    return _sufficient_internal(merged, mode, min_results, query)


def query_coverage_ok(results: list[dict[str, Any]], query: str) -> bool:
    """查询-结果词面覆盖守卫（fast/auto 早停质量门槛）。

    2026-09-02 实测教训：fast 早停原只看计数+snippet，首引擎上游波动时
    返回 5 条高计数但不相关结果（查询 Crawl4AI 却返回无关 MDN 页），
    早停吞掉 wave-2，单引擎垃圾即成最终答案。本守卫只在极端场景（多数
    结果与查询零词面交集）拒绝早停，让既有 wave-2/串行次引擎补跑——
    只影响「是否停」，不丢弃任何结果，最坏代价是多跑一个引擎。

    CJK 说明：中文查询分词为单字，覆盖判定在字符级——结果与查询零字符
    交集同样会被拒早停（弱信号但非空）；同义词改写场景（如「电脑」vs
    「计算机」）可能多跑一个引擎，属可接受代价：宁可多一次调用，不放走
    单引擎垃圾。
    """
    if not query or not query.strip():
        return True
    try:
        from tfidf_router import tokenize
    except ImportError:
        return True  # 分词不可用不设卡（fail-open）
    q_set = set(tokenize(query))
    if not q_set:
        return True
    # 同构垃圾检测：结构性包索引被域外查询误抢时，会把查询切词后逐词
    # 返回单 token 包名（title≈1 词、URL=/project/<word>），词面覆盖因此
    # 虚高（垃圾包名恰好是查询关键词）→ 覆盖守卫被骗过、垃圾即成最终
    # 答案。多数结果标题 ≤1 token 且查询有 ≥3 token 时，视为索引噪声，
    # 拒绝早停、放行串行次引擎补跑。
    single_token_titles = sum(
        1 for r in results if len(set(tokenize(r.get("title") or ""))) <= 1
    )
    if len(q_set) >= 3 and single_token_titles * 2 > len(results):
        return False
    covered = 0
    for r in results:
        text = f"{r.get('title') or ''} {r.get('snippet') or ''}"
        if q_set & set(tokenize(text)):
            covered += 1
    return covered >= max(1, (len(results) + 1) // 2)


def result_relevance(query: str, results: list[dict[str, Any]],
                     top_k: int = 3) -> float | None:
    """单个引擎这次结果的相关度代理 ∈ [0,1]；**拿不到信号时返回 None**。

    用途：给 `adaptive` 学习器回写质量维度（见 adaptive.quality_factor），
    让引擎排序不再只学「快慢/成败」而能学「好坏」。零网络成本——只读结果
    文本与查询词元。

    与 `query_coverage_ok` 的区别是本末颠倒：那个问「够不够好到可以停」
    （二元判定，判据保守到宁可多跑一个引擎），这个问「有多好」
    （连续量，用于跨引擎、跨时间比较）。两者共用词面覆盖的直觉，但口径必须
    分开：早停判据要求高敏感度（漏放=放出垃圾），质量分要求可比较
    （量纲稳定，否则引擎之间的分数不可比）。

    取「前 top_k 条里每条对查询的覆盖率，再取均值」，而不是全量平均：
    一条半相关文档不该替整个结果集背书，反之亦然。

    返回 None 而非 0.0 是关键：0.0 在自适应评分里是「测过了、很差」，
    None 才是「本次没测」。两者混同会让「无法判定」把引擎打落
    （`adaptive.record` 负责把 None 落成不参与判定的 0.0）。
    """
    if not query or not query.strip():
        return None
    try:
        from tfidf_router import tokenize
    except ImportError:
        return None  # 分词不可用 → 无信号（fail-open，不影响主路径）
    q_set = set(tokenize(query))
    if not q_set:
        return None  # 查询无内容词元（纯符号）→ 无法判定
    scored: list[float] = []
    for r in results:
        if not isinstance(r, dict) or "error" in r:
            continue
        text = f"{r.get('title') or ''} {r.get('snippet') or ''}"
        d_set = set(tokenize(text))
        if not d_set:
            scored.append(0.0)  # 有结果却零词元：结构化源常见，计为不相关
            continue
        scored.append(len(q_set & d_set) / len(q_set))
        if len(scored) >= top_k:
            break
    if not scored:
        return None  # 无有效结果（全部 error / 空列表）→ 无信号
    return sum(scored) / len(scored)


def score_clarity_ok(results: list[dict[str, Any]]) -> bool:
    """无标注 QPP 信号（2026-09-16）：结果分平坦且数量仅达下限时拒绝早停。

    方法论：无 relevance judgments 的查询性能预测（QPP）——NQC/Clarity 一族
    的共同直觉是「分数分布越尖锐（有明确赢家），查询表现越可预期；分布
    平坦说明检索器对查询拿不准」。这里取最保守的判据：

      - 只在**计数恰好等于下限**时参与判定（_sufficient_internal 传入
        barely）：结果富余本身就是信心，不必再问分数分布；
      - 至少 2 个数值 score 才参与（单条无从谈分布），否则 fail-open
        （放行早停）——下限取 2 而非 3 是刻意的：fast 档计数下限就是 2，
        取 3 会让 fast 档永远够不着信号；两条全等的占位分与三条同样可疑；
      - 全体 score 完全相等（std=0，结构化源常见的占位分）视为最平坦。

    「拒绝早停」的代价实打实是多跑一个引擎（成本语义见 engine_dispatch
    wave-1 注释），所以阈值取最极端的平坦而非「偏平坦」；宁可多一次调用，
    不放走「字段齐全、计数达标、分数无区分度」的单引擎垃圾（2026-09-02
    实测教训的分数维度补丁，与词面覆盖守卫互补——那个管文本，这个管排序）。
    """
    scores: list[float] = []
    for r in results:
        try:
            v = r.get("score")
            if v is None:
                continue
            v = float(v)
        except (TypeError, ValueError):
            continue
        scores.append(v)
    if len(scores) < 2:
        return True  # 信号不可用：fail-open
    mean = sum(scores) / len(scores)
    if mean <= 0:
        return True
    var = sum((s - mean) ** 2 for s in scores) / len(scores)
    std = var ** 0.5
    return (std / mean) > 0.02  # 变异系数 ≤2% 视为平坦（实测占位分全等=0）


def _sufficient_internal(
    results: list[dict[str, Any]],
    mode: str,
    min_results: int | None,
    query: str = "",
) -> bool:
    """渐进检索 early-stop：结果是否已够用。

    轻量启发式（不依赖网络/LLM）：
      - 默认 auto：至少 3 条非错误 + 2 条有 snippet
      - 默认 fast：至少 2 条 + 1 个 snippet
      - min_results：域配置覆盖（答案型源 1 条快照即够用，计数语义不动）
      - 通用路径叠加词面覆盖守卫（query_coverage_ok）：结果与查询几乎
        无交集时不许早停
      - 两条路径都叠加平坦分守卫（score_clarity_ok）：计数恰好压线且
        分数无区分度时不许早停
    """
    goods = [r for r in results if isinstance(r, dict) and "error" not in r]
    if not goods:
        return False
    with_snippet = sum(
        1 for r in goods
        if (r.get("snippet") or r.get("title") or "").strip()
    )
    if min_results is not None:
        try:
            need = max(1, int(min_results))
        except (TypeError, ValueError):
            need = 1
        # 答案型 1 条要求有可展示正文；≥3 条时至少 2 条有 snippet
        need_snip = 1 if need <= 2 else max(2, need - 1)
        ok = len(goods) >= need and with_snippet >= min(need_snip, len(goods))
        # 词面覆盖守卫不适用于答案型（1 条快照无「多数覆盖」可言），
        # 平坦分守卫适用：占位分快照同样可能是垃圾
        return ok and (len(goods) > need or score_clarity_ok(goods))
    threshold = 2 if mode == "fast" else 3
    ok = len(goods) >= threshold and with_snippet >= (1 if mode == "fast" else 2)
    return (ok
            and query_coverage_ok(goods, query)
            and (len(goods) > threshold or score_clarity_ok(goods)))

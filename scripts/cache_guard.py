#!/usr/bin/env python3
"""cache_guard.py — 公共缓存的写入守卫（登录态 + 上游退化）。

从 cache.py 拆出（2026-09-27，方案 C1）。拆的理由是纪律性的：cache.py 已在
tests/test_module_size_gate.py 的 GRANDFATHERED 里登记（1362 行上限），
任何净增长都会被门禁拦下——而「写缓存前该不该写」这条判断不属于缓存的存储
实现，属于「缓存的准入策略」，两者混在一起只会让 cache.py 继续变大。

## 两条守卫

**登录态守卫**（原在 cache.py，本模块只转出）：登录态检索的结果不得进入
公共缓存，避免一个账号的私有结果被所有人共享。

**退化守卫**（本次新增）：上游抖动返回的残次品不得写入。
实锤事故见 is_degraded_results 的 docstring——一次上游把整句当单词查、
返回 8 条词典释义，相关性全为 0.1429，却照常缓存并从此固化。

## 为什么退化守卫在「写入」这一层而不是「返回」那一层

退化结果**当场返回**是有价值的：它至少让调用方看到「这次没搜到」，
且恢复路径是重打一次（无缓存命中，自然重新请求上游）。
但它**不该被缓存**：一旦缓存，退化就从「一次抖动」变成「永久结果」——
后续每次命中缓存都复现，且没有任何信号提示它曾经退化过。
写入层拦截是唯一能切断这个固化的位置。
"""
from __future__ import annotations

from typing import Any

# 退化判据的阈值。与 rank_signals 的相关性算子配套：正常结果的 rel 普遍
# ≥0.35，查询词回声（上游把整句当单词查、返回词典释义）稳定在 0.05-0.20。
# 0.25 落在两者之间，且远高于噪声、又低于任何真实命中。
DEGRADED_RELEVANCE_FLOOR = 0.25

# 「多数条目低于地板」的比例门槛。不用 1.0：上游降级时通常仍混有 1-2 条
# 正常结果（降级不是全有全无）。0.7 是「压倒性低分」与「正常长尾」的分界。
DEGRADED_BATCH_RATIO = 0.7

# 可判定条目数下限。低于此不判：样本太少时比例不可信，且宁可漏拦不误伤。
DEGRADED_MIN_SCORED = 3


class DegradedCacheRejected(ValueError):
    """退化结果（低相关/查询词回声）禁止写入公共 SearchCache。"""


def _entry_relevance(item: Any) -> float | None:
    """取单条结果的相关性；取不到返回 None。

    相关性通常不在顶层，而在 `rerank_dims` 里（本地五维 rerank 写入）。
    顶层也看一眼是为了兼容未跑 rerank 的引擎直写路径。
    踩过的坑：只读顶层会得到「全部取不到 → 一律不判」，守卫静默失效。
    """
    if not isinstance(item, dict):
        return None
    rel = item.get("relevance")
    if not isinstance(rel, (int, float)) or isinstance(rel, bool):
        dims = item.get("rerank_dims")
        rel = dims.get("relevance") if isinstance(dims, dict) else None
    if isinstance(rel, (int, float)) and not isinstance(rel, bool):
        return float(rel)
    return None


def is_degraded_results(results: object) -> bool:
    """这批结果是否是「上游退化」——不完整命中查询词的回声。

    2026-09-27 实锤（`detecting LLM generated content farm SEO spam`）：
    路由正确（anysearch + local_bing），但上游某一刻把整句当成单词去查，
    返回 8 条「detecting 这个词的词典释义」。它们的 rerank_dims.relevance
    全部是 **0.1429**（只命中查询里的 detecting 一词），却照常返回、照常
    进入排序、照常写入缓存——于是这次上游抖动被**固化**下来，之后每次命中
    缓存都复现同样的垃圾结果。funnel 看着完全正常（returned 10 → kept 8），
    所以极难察觉。

    这与「结果少」是两件事：`funnel` 只数数量，不看每条像不像答案。

    判据刻意保守——**只拦整批退化**，不拦单条低分：
      - 单条低相关完全正常（长尾查询本就有弱命中），拦它会误伤真结果；
      - 整批都低于地板才是信号：要么上游确实降级了，要么这个查询真的没有
        答案（此时缓存一份垃圾不如重打一次）。
    """
    if not isinstance(results, list) or not results:
        return False
    scored = [v for v in (_entry_relevance(it) for it in results) if v is not None]
    if len(scored) < DEGRADED_MIN_SCORED:
        # 拿不到足够的相关性数据就不判——不猜，也不用缺字段去拒绝正常写入。
        return False
    low = sum(1 for v in scored if v < DEGRADED_RELEVANCE_FLOOR)
    return (low / len(scored)) >= DEGRADED_BATCH_RATIO


def assert_not_degraded(results: object, *, context: str) -> None:
    """退化结果硬拒绝：宁可下次重打，也不要把上游抖动固化进缓存。"""
    if is_degraded_results(results):
        raise DegradedCacheRejected(
            f"{context}: 多数条目 relevance < {DEGRADED_RELEVANCE_FLOOR}，"
            f"判定为上游退化，不写入公共 SearchCache"
        )

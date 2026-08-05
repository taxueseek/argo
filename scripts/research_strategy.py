#!/usr/bin/env python3
"""
research_strategy.py — 深度研究路由策略解析（从 research.py 拆分）

把「决策树路由」的推导从 collect_sources 的 _search_one 闭包里提取为
纯函数，减少闭包内部分支复杂度（code-review：special-case branching）。

策略语义：
  - local_first：先试零成本本地聚合；结果不足阈值再升级通用/垂直源
  - cost_aware（默认）：mode=fast 自动走 local_first；其余按 mode
  - full：直接全量，不走两段式
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=8)
def resolve_route_strategy(route_strategy: str | None, mode: str) -> str:
    """把显式策略 + mode 解析为实际生效策略（local_first / cost_aware / full）。

    显式策略优先；未指定时按 mode 推导（fast → local_first）。
    """
    if route_strategy in ("local_first", "full"):
        return route_strategy
    if route_strategy == "cost_aware":
        return "cost_aware"
    # 未指定 → 默认 cost_aware；fast 模式自动本地优先
    if mode == "fast":
        return "local_first"
    return "cost_aware"


def should_use_local_first(strategy: str) -> bool:
    """该策略是否走「先本地、不足升级」的两段式。"""
    return strategy == "local_first"

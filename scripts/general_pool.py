#!/usr/bin/env python3
"""general_pool.py — 通用候选池的顺序策略（单一入口）。

## 为什么独立成模块

通用源顺序此前有两个真源：`route_combo._general_fallback` 读静态元组，
`route_combo.tfidf_lead_combo` 写死 anysearch/octen。同一个「无匹配域」的
查询走哪条分支，就换了源——2026-10-26 实测：走 TF-IDF 分支的查询永远
用不到 tavily_free/yuanbao，即使它们更快更准。

顺序策略（池子从哪来、逃生门、按什么排序）统一在这里；route_combo 只留
`tfidf_lead_combo` 的组合逻辑，避免它继续长过 1000 行硬上限。

## 两道逃生门（逐位复现旧行为）

  · ARGO_GENERAL_FALLBACK_STATIC=1：完全回到 engine_policy 静态元组；
  · ARGO_GENERAL_FALLBACK_LEAD=<engine> / LEGACY=1：旧语义，且钉住顺序。

钉住时不再排序——A/B 需要固定臂，线上回滚也需要可预期。
"""

from __future__ import annotations

import os
from typing import Callable

# 静态兜底常量不可用时的最后一行（与 engine_policy 的历史默认同值）。
_STATIC_FALLBACK = ("anysearch", "octen", "local_bing")


def configured_general_pool() -> list[str] | None:
    """通用候选池（config.yaml 的 general_fallback.engines）。

    只声明「谁有资格进池」，不声明优先级——优先级由 source_selection 按
    实测先验 × 自适应分排序。配置缺失时回落到 engine_policy 常量，保证
    旧部署行为可预期。
    """
    try:
        from config import load_config
        pool = (load_config().get("general_fallback") or {}).get("engines")
        if isinstance(pool, list):
            clean = [str(e).strip() for e in pool if str(e).strip()]
            if clean:
                return clean
    except Exception:
        return None
    return None


def _static_fallback() -> tuple[str, ...]:
    try:
        from engine_policy import GENERAL_FREE_FALLBACK
        return tuple(GENERAL_FREE_FALLBACK)
    except ImportError:
        return _STATIC_FALLBACK


def general_kwargs(features: dict | None, mode: str, depth: str,
                   context: str) -> dict[str, str]:
    """通用选源需要的上下文参数（路由三条分支共用一个样板）。

    选源按用途（日常/研究）与语言排序；把四个参数散在各调用点复制，既容易
    漏传，也把 route.py 顶过行数门禁。放在池模块，因为它只服务选源。
    """
    feats = features or {}
    lang = str(
        feats.get("lang_override")
        or feats.get("effective_lang")
        or feats.get("primary_lang")
        or ""
    )
    return {
        "mode": mode, "depth": depth, "context": context,
        "lang": lang,
    }


def _env_flag(name: str, default: bool = False) -> bool:
    try:
        from engine_env import env_flag
        return bool(env_flag(name, default=default))
    except Exception:
        return default


def _get_env(name: str, default: str = "") -> str:
    """与 route_cache 指纹同源地读旋钮（含 ~/.config/argo/env 文件）。

    只读 os.environ 会让写在 env 文件里的 LEAD/STATIC 被指纹看见、却不被
    选源层看见——开关显示已设置，行为没变，A/B 两臂还会串味。
    """
    try:
        from engine_env import get_env
        return str(get_env(name, default) or "")
    except Exception:
        return os.environ.get(name, default)


def general_order() -> tuple[list[str], bool]:
    """原始候选顺序 + 是否被逃生门钉住（排序在 ranked_general）。"""
    static_fallback = _static_fallback()
    static = _env_flag("ARGO_GENERAL_FALLBACK_STATIC")
    order = (list(static_fallback) if static
             else (configured_general_pool() or list(static_fallback)))

    # 领队覆盖（2026-10-21）：A/B 与线上回退都走这里，避免为了复测改常量。
    # 只有**生效的**覆盖才钉住顺序；拼错引擎名不得让动态排序静默失效。
    lead_override = _get_env("ARGO_GENERAL_FALLBACK_LEAD").strip()
    pinned = bool(lead_override and lead_override in order)
    if pinned:
        order = [lead_override] + [e for e in order if e != lead_override]
    # 逃生开关：A/B 与线上回退都走这条，避免为了复测改常量。
    if _env_flag("ARGO_GENERAL_FALLBACK_LEGACY"):
        order = [e for e in order if e != "octen"]
        pinned = True
    return order, (static or pinned)


def ranked_general(enabled: set[str], *, mode: str = "auto",
                   depth: str = "fast", context: str = "search",
                   lang: str = "",
                   score_fn: Callable[[str, str, str, str],
                                      tuple[float, int]] | None = None
                   ) -> list[str]:
    """通用候选池的当前排序（`_general_fallback` 与 `tfidf_lead_combo` 共用）。

    `score_fn` 仅供测试注入假学习器；生产路径留空，走真实 adaptive.db。
    """
    order, pinned = general_order()
    if pinned:
        return [e for e in order if e in enabled]
    try:
        from source_selection import rank_general_candidates
        ranked = rank_general_candidates(
            order, enabled=enabled, mode=mode, depth=depth,
            context=context, lang=lang, score_fn=score_fn)
        if ranked:
            return ranked
    except Exception as exc:
        # 排序是加速器不是依赖：失败退回池顺序，但必须留痕——静默回退会让
        # 「新排序好像没生效」成为线上唯一症状（与 engine_dispatch 的反爬
        # 收紧同一条纪律）。
        import logging
        logging.getLogger("unified_search").debug(
            "通用池排序失败，按池顺序回退: %r", exc)
    # 配置池与本轮 enabled 无交集（手工 enabled 集合 / 极端降级）或排序失败：
    # 退回**池**顺序，不再用先验表当成员白名单——那会把 config 明确排除的
    # local_baidu/wikipedia 重新放回来，也会漏掉未进先验表的新源。
    return [e for e in order if e in enabled]


def general_fallback(enabled: set[str], *, mode: str = "auto",
                     depth: str = "fast", context: str = "search",
                     lang: str = "") -> list[str]:
    """通用候选池排序 + local_search 殿后。

    local_search 殿后（2026-10-15 实测定案）：日常档 combo_budget=2，它一旦
    占住前排，展开出的本地子引擎必被截断、只剩聚合器独跑（1059 条查询里
    独跑 109 次、中位 2.9s、占墙钟 369s，82% 是纯英文技术查询）。
    """
    order = ranked_general(
        enabled, mode=mode, depth=depth, context=context, lang=lang)
    if not order:
        # 最后一道 fail-open：池子与本轮 enabled 完全无交集时，任取两个
        # 可用源也比空 combo 好（执行层至少能暴露真实错误）。
        order = sorted(enabled)[:2] if enabled else ["anysearch"]
    if "local_search" in enabled and "local_search" not in order:
        order.append("local_search")
    return order

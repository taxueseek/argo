#!/usr/bin/env python3
"""engine_policy.py — 引擎分层与 combo 预算（日常 vs 深度研究）

单一策略层，避免 route/search/research 各自 if 分叉。

tier（引擎 config 可覆写 engines.<id>.tier）：
  daily_core      默认；意图命中即可跑
  daily_support   同 daily，但优先被 budget 截断掉（靠 combo 后排）
  research_only   仅 context=research / depth=deep / mode=deep

combo 预算：
  fast / budget / depth=fast → 最多 2
  auto / balanced             → 最多 3
  deep / research            → 不截断（全 combo + research_only）
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

# 默认 research_only：长尾/慢/脆，日常窄域主源不要标这里
DEFAULT_RESEARCH_ONLY: frozenset[str] = frozenset({
    "wayback_cdx",
    "archive_org",
    "gutenberg",
    "seeking_alpha",
    "courtlistener",
    "nasa_cmr",
    "usgs",  # earth_science 双源时 daily 只留 usgs 需 domain 有别的 core；见 filter 保底
    "rcsb_pdb",  # protein 以 uniprot 为主
})

# 明确 daily 答案源，永不标 research_only（防误配）
FORCE_DAILY_CORE: frozenset[str] = frozenset({
    "sina_quote", "tencent_quote", "em_flow", "eastmoney",
    "fred", "worldbank", "fx_rate", "nbs_stats", "eurostat",
    "pubchem", "rfc_editor", "gbif", "clinicaltrials",
    "finviz", "anysearch", "duckduckgo", "byted", "octen",
    "arxiv", "github", "wikipedia",
})


def is_research_context(
    *,
    mode: str = "auto",
    depth: str = "fast",
    context: str = "search",
) -> bool:
    """是否允许 research_only 引擎与全量 combo。"""
    if (context or "search") == "research":
        return True
    if (depth or "fast") == "deep":
        return True
    if (mode or "auto") == "deep":
        return True
    return False


def combo_budget(
    *,
    mode: str = "auto",
    depth: str = "fast",
    context: str = "search",
) -> int | None:
    """combo 最大引擎数；None = 不截断。"""
    if is_research_context(mode=mode, depth=depth, context=context):
        return None
    if (mode or "auto") in ("fast", "budget") or (depth or "fast") == "fast":
        return 2
    # auto + balanced：主源 + 最多 2 备选
    return 3


def get_engine_tier(engine_id: str, spec: dict[str, Any] | None = None) -> str:
    """解析引擎 tier。"""
    if engine_id in FORCE_DAILY_CORE:
        return "daily_core"
    if spec:
        t = (spec.get("tier") or "").strip().lower()
        if t in ("daily_core", "daily_support", "research_only"):
            return t
    if engine_id in DEFAULT_RESEARCH_ONLY:
        return "research_only"
    return "daily_core"


def _tier_lookup() -> Callable[[str], str]:
    try:
        from config import get_engines, load_config
        engines = get_engines(load_config())
    except Exception:
        engines = {}

    def _t(eid: str) -> str:
        return get_engine_tier(eid, engines.get(eid) if isinstance(engines, dict) else None)

    return _t


def filter_combo_by_policy(
    combo: list[str],
    *,
    mode: str = "auto",
    depth: str = "fast",
    context: str = "search",
    tier_of: Callable[[str], str] | None = None,
) -> list[str]:
    """按 tier + budget 过滤 combo。

    若 research_only 过滤后为空（域内全是研究源），保留原 combo 再截断预算，
    避免「问蛋白质却一个引擎都没有」。
    """
    if not combo:
        return []
    tier_of = tier_of or _tier_lookup()
    allow_ro = is_research_context(mode=mode, depth=depth, context=context)

    if allow_ro:
        filtered = list(combo)
    else:
        kept = [e for e in combo if tier_of(e) != "research_only"]
        filtered = kept if kept else list(combo)

    budget = combo_budget(mode=mode, depth=depth, context=context)
    if budget is not None and len(filtered) > budget:
        filtered = filtered[:budget]
    return filtered


def boost_into_combo(
    combo: list[str],
    boosts: Iterable[str] | None,
    *,
    enabled: set[str] | None = None,
    max_total: int | None = None,
) -> list[str]:
    """将 boost 引擎插入 combo 前部（去重，可选 enabled 过滤）。

    用于研究子查询：不锁死单引擎，而是抬高垂直源优先级后仍走 auto 融合。
    """
    base = list(combo or [])
    if not boosts:
        out = base
    else:
        head: list[str] = []
        for e in boosts:
            if not e or e == "auto":
                continue
            if enabled is not None and e not in enabled:
                continue
            if e not in head:
                head.append(e)
        tail = [e for e in base if e not in head]
        out = head + tail
    if max_total is not None and max_total > 0:
        out = out[:max_total]
    return out


def research_engine_hints(
    profile: dict[str, Any] | None,
    sub_index: int,
) -> list[str]:
    """从选题 profile 生成子查询引擎 boost 列表。

    子查询 0：vertical + priority 前 2（广度）
    子查询 k>0：轮转 priority/vertical
    """
    if not profile:
        return []
    vertical = list(profile.get("vertical_engines") or [])
    priority = list(profile.get("engines_priority") or [])
    # 合并去重，vertical 优先
    merged: list[str] = []
    for e in vertical + priority:
        if e and e not in merged:
            merged.append(e)
    if not merged:
        return []
    if sub_index <= 0:
        return merged[:3]
    # 轮转：每次取 2 个从 offset 起
    n = len(merged)
    start = (sub_index - 1) % n
    picked = [merged[(start + i) % n] for i in range(min(2, n))]
    return picked

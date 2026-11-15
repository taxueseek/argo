#!/usr/bin/env python3
"""rrf_classic.py — 经典 RRF 融合（weighted=False 路径）。

所有引擎列表在同一个池中竞争，每个列表独立贡献排名。
为什么单独成模块：search_rank.py 已压着祖父上限，经典 RRF 路径
作为独立模块拆出后，search_rank.py 只留调用点。
"""

from __future__ import annotations

from typing import Any

from caliber_isolation import _dedupe_key, _merge_evidence


def rrf_classic(
    ranked_lists: list[list[dict[str, Any]]],
    k: int = 60,
    lang: str | None = None,
    _canonical_url=None,
) -> list[dict[str, Any]]:
    """经典 RRF：所有结果在同一个池中竞争，不分组。

    _canonical_url 由调用方（search_rank）传入，避免本模块反向依赖。
    """
    if _canonical_url is None:
        from url_canon import canonical_url as _canonical_url

    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}
    for _li, results in enumerate(ranked_lists):
        for i, r in enumerate(results):
            key = _dedupe_key(r, _canonical_url, f"__idx__:{_li}:{i}")
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + i + 1)
            eng = r.get("_engine") or r.get("source", "") or ""
            if key not in items:
                item = dict(r)
                item["_rrf_score"] = 0.0
                item["consensus_engines"] = [eng] if eng else []
                items[key] = item
            else:
                _merge_evidence(items[key], r, extra_eng=eng)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    out = []
    for key, _ in ranked:
        item = items[key]
        item["_rrf_score"] = round(scores[key], 6)
        out.append(item)
    return out

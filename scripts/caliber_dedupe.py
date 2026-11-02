#!/usr/bin/env python3
"""caliber_dedupe.py — 跨口径同 URL 合并（口径拼接之后的最后一道去重）。

## 为什么必须在口径拼接之后再做一次

`caliber_isolation.merge_within_caliber` 只在**同一口径内**按 URL 折叠。
同一 URL 被不同口径的引擎同时命中时（维基词条既被百科源也被通用源返回），
拼接路径会让它出现两次：

  - 用户看到重复条目；
  - `ranking_eval` 的 nDCG 因为「同一相关项被计两次」算出大于 1 的值
    （实测金标 `consensus_vs_longtext` nDCG=1.4525，而定义上界是 1）。

## 去重不删证据

合并 `consensus_engines`、取最大 `_rrf_score`、保留更长的标题/摘要、
拼接 `source`。跨口径共识信号因此保留，而不是被当成重复丢掉。
"""

from __future__ import annotations

from typing import Any


def merge_cross_caliber_duplicates(
    rows: list[dict[str, Any]],
    canonical_url=None,
) -> list[dict[str, Any]]:
    """按归一 URL 合并跨口径重复条；无重复时原样返回。"""
    if not rows or len(rows) <= 1:
        return rows
    if canonical_url is None:
        from url_canon import canonical_url
    index: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = (
            canonical_url(r.get("url", ""))
            or (f"__title__:{r.get('title', '')}" if r.get("title") else "")
            or (f"__card__:{r.get('card_type', '')}" if r.get("card_type") else "")
            or f"__idx__:{id(r)}"
        )
        cur = index.get(key)
        if cur is None:
            index[key] = r
            out.append(r)
            continue
        cons = list(cur.get("consensus_engines") or [])
        for eng in r.get("consensus_engines") or []:
            if eng and eng not in cons:
                cons.append(eng)
        cur["consensus_engines"] = cons
        cur["_rrf_score"] = round(max(
            float(cur.get("_rrf_score") or 0.0),
            float(r.get("_rrf_score") or 0.0),
        ), 6)
        sources = {cur.get("source", ""), r.get("source", "")}
        cur["source"] = "/".join(s for s in sources if s)
        new_txt = f"{r.get('title', '')} {r.get('snippet', '')}"
        cur_txt = f"{cur.get('title', '')} {cur.get('snippet', '')}"
        if len(new_txt) > len(cur_txt):
            cur["title"] = r.get("title", cur.get("title"))
            cur["snippet"] = r.get("snippet", cur.get("snippet"))
    return out

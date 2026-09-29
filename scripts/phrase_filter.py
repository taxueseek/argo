#!/usr/bin/env python3
"""phrase_filter.py — 短语精确匹配后置过滤（Step 3，argo 普适性改进）。

用户用引号声明「必须精确匹配某短语」（型号 / 术语 / 引文）时，在 title+snippet
里做大小写不敏感的短语包含过滤，剔除不含该短语的结果（查询侧识别引号短语、
结果侧做包含过滤，是精准检索的通用能力）。

保守语义（三条，防误伤）：
  - 仅当 query 含引号短语时触发；无引号查询完全不受影响（no-op）；
  - 多个短语取「任一命中」（OR），不是全部命中；
  - 过滤会清空结果时**回退到过滤前**——精确过滤绝不删掉唯一正确结果。

为何独立成模块：search_pipeline.py 受 1000 行门禁约束（见 test_module_size_gate），
本模块只做纯函数式提取 + 过滤，零网络、零依赖。
"""
from __future__ import annotations

import re
from typing import Any

# 引号内 ≥2 字符的短语（单字符无区分度，不纳入）
_QUOTED = re.compile(r'"([^"]{2,})"')


def extract_phrases(query: str) -> list[str]:
    """提取 query 里的引号短语（去首尾空白、去重、保序）。无则空列表。"""
    if not query or not isinstance(query, str):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _QUOTED.finditer(query):
        p = m.group(1).strip()
        k = p.lower()
        if p and k not in seen:
            seen.add(k)
            out.append(p)
    return out


def apply_phrase_filter(results: list[dict[str, Any]],
                        phrases: list[str]) -> tuple[list[dict[str, Any]], int]:
    """按短语（任一命中，大小写不敏感）过滤 title+snippet；返回 (结果, 剔除数)。

    保守：过滤会清空时原样返回（剔除数 0）——精确过滤不该删掉唯一正确结果。
    """
    if not phrases or not results:
        return results, 0
    pl = [p.lower() for p in phrases]
    kept = []
    for r in results:
        text = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
        if any(p in text for p in pl):
            kept.append(r)
    if not kept:
        return results, 0  # 回退：不过滤，保留原结果
    return kept, len(results) - len(kept)

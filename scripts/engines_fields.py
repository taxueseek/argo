#!/usr/bin/env python3
"""engines_fields.py — 声明式 output_map 的字段取值与模板渲染（无依赖小模块）。

从 engines_parsers.py 抽出（2026-11-11）：结构漂移诊断拆到
engines_schema_drift.py 后，drift 需要 `_dig` / `extract_items` /
`_format_url_template`，而 parsers 又要 re-export drift，形成
parsers → drift → parsers 的循环导入（直接 `import engines_schema_drift`
会 ImportError）。把这三项纯函数下沉到本模块，依赖方向变为：

    engines_parsers ──→ engines_schema_drift ──→ engines_fields
    engines_parsers ──→ engines_fields

本模块只依赖标准库，不反向导入任何 engines_* 模块。
"""

from __future__ import annotations

import re
from typing import Any


def _dig(obj: Any, path: str) -> Any:
    """按点分路径取嵌套值；路径段为数字时按数组索引。"""
    if not path:
        return obj
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return cur


def _coerce_field(val: Any, *, max_len: int | None = None) -> str:
    """把 API 字段压成字符串：list 取首项、dict 丢弃、其余 str。"""
    if val is None:
        return ""
    if isinstance(val, list):
        if not val:
            return ""
        val = val[0]
    if isinstance(val, dict):
        return ""
    s = str(val).strip()
    if max_len is not None and len(s) > max_len:
        return s[:max_len]
    return s


def _format_url_template(template: str, item: dict) -> str:
    """支持 {field} 与 {a.b.c} 点分路径的 URL 模板；任一占位为空则返回空串。"""
    vals = {m.group(1): _coerce_field(_dig(item, m.group(1)))
            for m in re.finditer(r"\{([^}]+)\}", template)}
    if any(not v for v in vals.values()):
        return ""
    try:
        return re.sub(r"\{([^}]+)\}", lambda m: vals[m.group(1)], template)
    except Exception:
        return ""


def extract_items(data: Any, path: str) -> list:
    """从 JSON 按路径提取列表。path 为 . / $ / [] / 空 且 data 为 list 时直接返回。"""
    if path in ("", ".", "$", "[]"):
        return data if isinstance(data, list) else []
    if not isinstance(data, dict):
        return []
    obj = _dig(data, path)
    if isinstance(obj, list):
        return obj
    # DBLP 等 API 在 h=1 时把 hit 收成对象而非数组
    if isinstance(obj, dict):
        return [obj]
    return []

#!/usr/bin/env python3
"""wiki_extract.py — MediaWiki generator=search + prop=extracts 的专用解析器。

为什么存在（2026-10-31 评测 fe01/fz15）：list=search 的 snippet 是「匹配碎片」
——剥掉高亮标签后仍只有查询词附近几行，事实性内容（碳十四半衰期 5730 年、
太和殿脊兽数量）大多不在碎片窗口里；prop=extracts（TextExtracts）给的是正文
导语明文，定义与关键数字通常就在首段。generator=search 把两段拼成一个请求：
搜索定页 + 每页导语，en/zh 两个 wikipedia 引擎共用。

响应形态：query.pages 是 **pageid 为键的 dict**——通用 output_map 的点路径
解析器吃不了 dict 值迭代，也会丢失搜索排序，因此走 _CUSTOM_JSON_PARSERS 专用
解析，按页内 index 字段（generator=search 自带）排序还原相关度序。

长度：exchars=380 由请求侧控制；engines_base 统一 300 字截断只挂在 output_map
路径，本解析器路径由调用方（引擎 spec 侧 exchars）自己收口。
"""
from __future__ import annotations

import re
from typing import Any

_TAG_RE = re.compile(r"</?[a-zA-Z][^>]{0,160}?>")


def parse_extracts(data: dict[str, Any]) -> list[dict[str, Any]]:
    """MediaWiki generator=search 响应 → 相关度序的 title/url/snippet 行。"""
    pages = (data.get("query") or {}).get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, dict):
        return []
    rows = []
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        extract = str(page.get("extract") or "")
        if _TAG_RE.search(extract):
            extract = _TAG_RE.sub("", extract)
        rows.append({
            "title": str(page.get("title") or ""),
            "url": str(page.get("fullurl") or ""),
            "snippet": extract,
            "_index": page.get("index"),
        })
    rows.sort(key=lambda r: r["_index"] if isinstance(r["_index"], int) else 99)
    for r in rows:
        r.pop("_index", None)
    return rows

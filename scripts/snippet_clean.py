#!/usr/bin/env python3
"""snippet_clean.py — 交付前的摘要清洗（输出质量层，纯函数）。

## 为什么需要

搜索源返回的 snippet 经常是「页面开头的一段原文」，而不是编辑过的摘要：
anysearch 的条目以 `- #` 这类 markdown 列表/标题标记开头，站点导航
（首页/登录/注册/关于我们）直接粘在正文前面，站名还会重复一遍
（`技术栈技术栈Python asyncio…`）。这些字符对 Agent 没有信息增量，
却挤占上下文、干扰标题与摘要的对齐。

实测（2026-11-02，8 条日常查询 × 5 条结果）：38 条 snippet 里 8 条以
markdown 标记开头（21%），4 条含站点导航词（11%）。

## 边界

只做**可逆的装饰性清洗**：剥开头标记/导航链/重复站名前缀、压空白。
不改写正文、不截断、不做语义判断。清洗失败（空串/非字符串）原样返回，
绝不因输出层装饰让一次搜索失败。
"""

from __future__ import annotations

import re

__all__ = ["clean_snippet", "NAV_WORDS"]

# 只有「站点框架」词进表；搜索/分享/评论这类既可能是导航也可能是正文的
# 词刻意不收——它们被误剥的代价高于收益。
NAV_WORDS: tuple[str, ...] = (
    "首页", "登录", "注册", "菜单", "导航", "关于我们", "联系我们",
    "版权所有", "下载APP", "APP下载", "扫码下载", "返回顶部", "网站地图",
)

_MD_LEAD = re.compile(r"^\s*(?:[-*+>#]+\s*)+")
_DUP_PREFIX = re.compile(r"^([\u4e00-\u9fff]{2,8})\1")
_NAV_LEAD = re.compile(
    r"^(?:(?:" + "|".join(map(re.escape, NAV_WORDS)) + r")[\s·|/、,，。:：\-]*)+",
    re.IGNORECASE,
)
_NAV_TRAIL = re.compile(
    r"[\s·|/、,，。:：\-]*(?:(?:" + "|".join(map(re.escape, NAV_WORDS))
    + r")[\s·|/、,，。:：\-]*)+$",
    re.IGNORECASE,
)


def clean_snippet(text: str) -> str:
    """清洗一条摘要；非字符串或空串原样返回。"""
    if not isinstance(text, str):
        return text
    s = text.strip()
    if not s:
        return s
    s = _MD_LEAD.sub("", s)
    s = _DUP_PREFIX.sub(r"\1", s)
    s = _NAV_LEAD.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _NAV_TRAIL.sub("", s).strip()
    return s

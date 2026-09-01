#!/usr/bin/env python3
"""fetch_quality.py — 抓取结果的质量信号计算（fetch_v3 第三级）。

从 fetch_v3 拆出：本模块只做纯信号计算（来源分类 / 页面类型 / 质量分 /
内容安全），不参与抓取编排，也不持有外部状态。独立成模块可让 fetch_v3
聚焦降级链编排，避免文件持续膨胀。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

# 判定为 article 的最小正文字数阈值单一真源在 content_signals.MIN_ARTICLE_CHARS
# （detect_page_type 的 markdown-only 回退使用），此处不再复制常量防漂移。



def assess(result: dict) -> dict:
    """公开入口：为抓取结果补充质量信号并返回同一 dict。"""
    return _assess_quality(result)


def _assess_quality(result: dict) -> dict:
    """计算内容质量信号（内联 content_signals 的核心逻辑）。"""
    url = result.get("url", "")
    content = result.get("content", "")
    html = result.get("html", "")

    # source_type + is_official
    source_type, is_official = _classify_domain(url)

    # page_type
    page_type = _detect_page_type(html, content, url)

    # quality_score
    quality_score = _compute_quality(content, html)

    # content_ok
    content_ok = quality_score > 0.25 and len(content) > 80

    # is_stale (简化：无日期信息时保守判定)
    is_stale = False
    content_age_days = -1

    # 内容安全：注入检测 + 清洗（任何抓取内容先过安全引擎）
    security = {}
    if content:
        try:
            from content_security import scrub_to_dict
            security = scrub_to_dict(content)
        except Exception:
            security = {}

    result.update({
        "content_ok": content_ok,
        "page_type": page_type,
        "source_type": source_type,
        "is_official": is_official,
        "is_stale": is_stale,
        "content_age_days": content_age_days,
        "quality_score": quality_score,
        "content_security": security,
    })
    return result


def _classify_domain(url: str) -> tuple[str, bool]:
    """快速域名分类。"""
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return "unknown", False

    if host.endswith(".gov") or ".gov." in host:
        return "gov", True
    if host.endswith(".edu") or host.endswith(".ac.uk"):
        return "edu", True
    if "github.com" in host or host.endswith(".github.io"):
        return "github", True
    if host.startswith("docs.") or host.startswith("developer."):
        return "docs-site", True
    if "stackoverflow" in host or "stackexchange" in host:
        return "qa", False
    if any(m in host for m in ("forum", "community", "discourse")):
        return "forum", False
    if host in ("reddit.com", "www.reddit.com", "old.reddit.com"):
        return "forum", False
    if any(host == d or host.endswith("." + d) for d in (
        "nytimes.com", "bbc.com", "reuters.com", "theguardian.com",
        "bloomberg.com", "techcrunch.com", "theverge.com",
    )):
        return "news", False
    return "unknown", False


def _detect_page_type(html: str, content: str, url: str = "") -> str:
    """检测页面结构类型。

    此前本模块内联了一套独立实现（朴素正则、不认 URL），与
    content_signals.detect_page_type 并存且行为不同——两套判定是真实的
    维护风险。现统一委托给 content_signals（标记更全、支持 URL 感知的
    list 判定、带 confidence），本模块只做字符串适配。

    注意 page_type 目前只是报告字段，不参与 TTL / 质量分计算：
      - TTL 由 evidence_loop.ttl_for_fetch_result 取 source_type，而
        _classify_domain 对任意 URL 都返回非空值（兜底 "unknown"），
        `source_type or page_type` 恒短路，page_type 永不参与 TTL 判定；
      - _compute_quality(content, html) 不读 page_type。
    """
    try:
        from content_signals import detect_page_type
        return detect_page_type(html, url, content).get("page_type", "unknown")
    except Exception:
        # content_signals 不可用时不做静默误判：宁可 unknown 也不猜类型
        return "unknown"


def _compute_quality(content: str, html: str) -> float:
    """计算质量评分（0-1）。"""
    if not content:
        return 0.0
    word_count = len(content.split())
    text_density = len(content.replace(" ", "").replace("\n", "")) / max(len(content), 1)
    has_structure = bool(re.search(r'[.!?。！？].{10,}[.!?。！？]', content))

    score = min(1.0, (
        0.4 * min(word_count / 500, 1.0) +
        0.3 * text_density +
        0.2 * (1.0 if has_structure else 0.0) +
        0.1 * (1.0 if len(content) > 1000 else 0.0)
    ))
    return round(score, 2)



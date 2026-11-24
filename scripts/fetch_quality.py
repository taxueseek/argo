#!/usr/bin/env python3
"""fetch_quality.py — 抓取结果的质量信号计算（fetch_v3 第三级）。

从 fetch_v3 拆出：本模块只做纯信号计算（来源分类 / 页面类型 / 质量分 /
内容安全），不参与抓取调度，也不持有外部状态。独立成模块可让 fetch_v3
聚焦降级链调度，避免文件持续膨胀。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

# 判定为 article 的最小正文字数阈值唯一来源在 content_signals.MIN_ARTICLE_CHARS
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

    # 风险门禁（v2.9.8）：被隔离的内容用清洗后正文替换原始正文，使注入
    # 污染的正文不进 Agent 上下文、不进证据分。正常内容零改动。
    quarantined = bool(security.get("quarantined"))
    if quarantined:
        cleaned = security.get("cleaned_content")
        if cleaned is not None:
            content = cleaned
            result["content"] = cleaned
            result["length"] = len(cleaned)
    # 隔离内容一律判否：来源试图注入，其正文不可信
    content_ok = content_ok and not quarantined

    result.update({
        "quality_breakdown": quality_breakdown(content, html),
        "content_ok": content_ok,
        "page_type": page_type,
        "source_type": source_type,
        "is_official": is_official,
        "is_stale": is_stale,
        "content_age_days": content_age_days,
        "quality_score": quality_score,
        "content_security": security,
        "content_quarantined": quarantined,
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
        _classify_domain 对任意 URL 都返回非空值（保底 "unknown"），
        `source_type or page_type` 恒短路，page_type 永不参与 TTL 判定；
      - _compute_quality(content, html) 不读 page_type。
    """
    try:
        from content_signals import detect_page_type
        return detect_page_type(html, url, content).get("page_type", "unknown")
    except Exception:
        # content_signals 不可用时不做静默误判：宁可 unknown 也不猜类型
        return "unknown"


# 评分口径版本：公式一改就要动它。
#
# 质量分随结果写进缓存（TTL 最长 86400s），公式变更后旧条目里的分数会**静默
# 沿用**——调用方看到的是已经不再成立的口径，而且无从分辨。这与正文产出口径
# 变化的情形同构（见 cache.FETCH_PIPELINE_VERSION）；区别是评分不需要重新联网，
# 用条目里已有的 content 就地重算即可，所以版本记在条目里而不是并进缓存键。
QUALITY_FORMULA_VERSION = 3


# CJK 统一表意文字 + 日文假名 + 谚文：这些文字之间不靠空格分词，
# `content.split()` 会把整段中文算成个位数个「词」。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
                     r"\u3040-\u30ff\uac00-\ud7af]")
# 折算比：中文约 1.6 字/词，取 0.5（即 2 字折 1 词）偏保守——宁可让中文
# 晚一点饱和，也不系统性高估。
_CJK_WORDS_PER_CHAR = 0.5


def effective_words(content: str) -> int:
    """有效词数：拉丁文按空白切词，CJK 按字折算。

    为什么不能直接用 `content.split()`：它按空白切分，而中文没有词间空格——
    实测同一体量的页面，英文得 1,220「词」，中文只有 85，导致四项加权里的
    word_count 项（权重 0.4）对中文几乎是零分（0.07 vs 0.40），中文内容总分
    被系统性压低约 0.3。后果不是显示难看，而是**中文信息源在筛选时被更激进地
    判为低质**——而这项判断本该与语种无关。
    """
    if not content:
        return 0
    cjk = len(_CJK_RE.findall(content))
    latin = len(_CJK_RE.sub(" ", content).split())
    return latin + int(cjk * _CJK_WORDS_PER_CHAR)


def quality_breakdown(content: str, html: str = "") -> dict:
    """质量分的**分项依据**：每个分量得了多少、为什么。

    只给一个总分时，调用方无法分辨「分低是因为太短」还是「因为全是导航链接」，
    而这两者的处置完全不同。四项加权分与结构修正项各自落出来，并指出拖累
    总分的那一项，结论才可用。

    已知残留偏差：density 项量的是「非空白字符占比」，而中文词间本就不写空格，
    因此中文在这一项上天然接近满分，英文约 0.85，折算到总分实测 +0.04~0.06 的系统差（等有效词数下）。
    量级比修复前的词数偏差（−0.29）小约六倍，且方向不再惩罚任一语种，
    故不动它——真正的密度指标需要语种感知的分母，那是另一件事。
    """
    if not content:
        return {"total": 0.0, "version": QUALITY_FORMULA_VERSION,
                "terms": {}, "bonus": 0.0,
                "inputs": {"chars": 0, "word_count": 0, "cjk_chars": 0, "text_density": 0.0,
                           "has_structure": False, "html": bool(html)},
                "drag": "empty"}
    cjk_chars = len(_CJK_RE.findall(content))
    word_count = effective_words(content)
    text_density = len(content.replace(" ", "").replace("\n", "")) / max(len(content), 1)
    has_structure = bool(re.search(r'[.!?。！？].{10,}[.!?。！？]', content))

    terms = {
        "word_count": 0.4 * min(word_count / 500, 1.0),
        "density": 0.3 * text_density,
        "structure": 0.2 * (1.0 if has_structure else 0.0),
        "length": 0.1 * (1.0 if len(content) > 1000 else 0.0),
    }
    score = min(1.0, sum(terms.values()))

    bonus = 0.0
    if html:
        low = html.lower()
        if re.search(r'<(article|main)\b', low):
            bonus += 0.08                      # 语义主内容标签：最强信号
        p_count = low.count("<p")
        if p_count >= 5:
            bonus += 0.05                      # 真实文章由 <p> 组成
        elif p_count >= 2:
            bonus += 0.02
        a_count = low.count("<a ")
        if a_count > word_count / 10:
            bonus -= 0.05                      # 链接密度过高 → 更像列表页
        bonus = max(-0.1, min(0.15, bonus))

    # 拖累项：离自己的满分最远的那个分量（bonus 为负时归因到 structure，因为
    # 结构修正是唯一会因链接密度扣分的地方）
    weights = {"word_count": 0.4, "density": 0.3, "structure": 0.2, "length": 0.1}
    drag = max(weights, key=lambda k: weights[k] - terms[k])
    if bonus < 0:
        drag = "structure"

    return {
        "total": round(min(1.0, max(0.0, score + bonus)), 2),
        "version": QUALITY_FORMULA_VERSION,
        "terms": {k: round(v, 3) for k, v in terms.items()},
        "bonus": round(bonus, 3),
        "inputs": {"chars": len(content), "word_count": word_count,
                   "cjk_chars": cjk_chars,
                   "text_density": round(text_density, 3),
                   "has_structure": has_structure, "html": bool(html)},
        "drag": drag if score + bonus < 0.85 else None,
    }


def _compute_quality(content: str, html: str) -> float:
    """质量评分（0-1）。分项依据见 quality_breakdown——公式只此一处，避免分叉。"""
    return quality_breakdown(content, html)["total"]


def guard_empty_unknown_result(result: dict[str, Any]) -> dict[str, Any]:
    """修正「成功但零正文 + page_type 未识别」的假成功（如二进制页被当 HTML 抓回）。"""
    if (result.get("success") and not (result.get("content") or "").strip()
            and result.get("page_type") in (None, "", "unknown")):
        result["success"] = False
        result["error"] = result.get("error") or (
            "抓取成功但正文为空（URL 可能指向二进制文件或需专用解析器）"
        )
    return result



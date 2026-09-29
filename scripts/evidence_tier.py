#!/usr/bin/env python3
"""evidence_tier.py — 证据分层系统

四级证据分层（A/B/C/D），用于量化内容来源的可信度。
A 级：官方当前公开来源或法律权威文件
B 级：有明确日期的知名第三方报告或公开媒体
C 级：品牌自述但缺乏运营细节
D 级：未验证或市场特定边界项

设计原则：
  - 纯规则、零依赖、微秒级
  - 与 content_signals.classify_source 互补：后者判域名类型，本模块判证据等级
  - 输出可直接用于内容质量评分和来源账本
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


# ── 证据等级 ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EvidenceTier:
    """证据等级常量"""
    A = "official"      # 官方来源
    B = "third_party"   # 知名第三方
    C = "self_claim"    # 品牌自述
    D = "unverified"    # 未验证


# 等级权重（用于加权评分）
TIER_WEIGHTS = {
    EvidenceTier.A: 1.0,
    EvidenceTier.B: 0.7,
    EvidenceTier.C: 0.3,
    EvidenceTier.D: 0.0,
}

# 等级排序（用于比较）
TIER_RANK = {
    EvidenceTier.A: 4,
    EvidenceTier.B: 3,
    EvidenceTier.C: 2,
    EvidenceTier.D: 1,
}


# ── 域名 → 证据等级映射 ────────────────────────────────────────────

# A 级：官方来源
_A_DOMAINS = frozenset([
    # 政府
    "gov.cn", "gov.uk", "gov", "gob.mx", "go.jp", "go.kr",
    # 教育
    "edu", "edu.cn", "ac.uk", "ac.jp", "ac.kr",
    # 官方文档
    "docs.microsoft.com", "developer.apple.com", "developers.google.com",
    "docs.python.org", "readthedocs.io",
    # 官方标准
    "w3.org", "ietf.org", "iso.org", "ieee.org",
])

# B 级：知名第三方
_B_DOMAINS = frozenset([
    # 新闻媒体
    "nytimes.com", "bbc.com", "bbc.co.uk", "reuters.com", "theguardian.com",
    "washingtonpost.com", "bloomberg.com", "apnews.com", "aljazeera.com",
    "cnbc.com", "ft.com", "economist.com", "techcrunch.com", "theverge.com",
    "arstechnica.com", "wired.com", "nature.com", "science.org",
    # 中文媒体
    "xinhuanet.com", "people.com.cn", "chinanews.com.cn", "cctv.com",
    "caixin.com", "163.com", "sohu.com", "sina.com.cn", "qq.com",
    "36kr.com", "huxiu.com", "tmtpost.com", "leiphone.com",
    # 研究机构
    "mckinsey.com", "bcg.com", "bain.com", "deloitte.com", "pwc.com",
    "ey.com", "kpmg.com", "accenture.com", "gartner.com", "forrester.com",
    "idc.com", "statista.com", "pewresearch.org", "brookings.edu",
    # 开源社区
    "github.com", "gitlab.com", "stackoverflow.com", "stackexchange.com",
    "medium.com", "substack.com", "wordpress.com",
    # 百科
    "wikipedia.org", "baike.baidu.com", "zhihu.com",
])

# C 级：品牌自述（需要进一步判断是否有运营细节）
_C_PATTERNS = [
    r"^blog\.",           # 企业博客
    r"^news\.",           # 企业新闻
    r"^press\.",          # 企业公关
    r"^about\.",          # 企业介绍
    r"^company\.",        # 企业信息
]


@dataclass
class EvidenceAssessment:
    """证据评估结果"""
    tier: str                    # 证据等级 A/B/C/D
    tier_weight: float           # 等级权重
    source_type: str             # 来源类型
    is_official: bool            # 是否官方
    has_date: bool = False       # 是否有明确日期
    has_author: bool = False     # 是否有明确作者
    has_citation: bool = False   # 是否有引用/参考
    notes: list[str] = field(default_factory=list)  # 评估说明


def assess_evidence_tier(
    url: str,
    source_type: str = "",
    is_official: bool = False,
    has_date: bool = False,
    has_author: bool = False,
    has_citation: bool = False,
    content: str = "",
) -> EvidenceAssessment:
    """评估内容的证据等级。

    参数:
        url: 内容 URL
        source_type: 来源类型（来自 classify_source）
        is_official: 是否官方来源
        has_date: 是否有明确发布日期
        has_author: 是否有明确作者
        has_citation: 是否有引用/参考来源
        content: 内容文本（用于进一步判断）

    返回:
        EvidenceAssessment 对象
    """
    host = _extract_host(url)
    notes = []

    # 1. 域名匹配
    if _is_a_tier_domain(host):
        tier = EvidenceTier.A
        notes.append(f"官方域名: {host}")
    elif _is_b_tier_domain(host):
        tier = EvidenceTier.B
        notes.append(f"知名第三方域名: {host}")
    elif _is_c_tier_pattern(host):
        tier = EvidenceTier.C
        notes.append(f"企业自述域名: {host}")
    else:
        tier = EvidenceTier.D
        notes.append(f"未知域名: {host}")

    # 2. 内容信号修正
    # 有明确日期 + 有作者 + 有引用 → 升级
    if tier == EvidenceTier.C and has_date and has_author and has_citation:
        tier = EvidenceTier.B
        notes.append("企业自述但有完整证据链（日期+作者+引用），升级为 B 级")

    # 无日期 + 无作者 → 降级
    if tier == EvidenceTier.B and not has_date and not has_author:
        tier = EvidenceTier.C
        notes.append("第三方来源但缺乏日期和作者，降级为 C 级")

    # 3. 内容质量信号
    if content:
        content_signals = _assess_content_signals(content)
        if content_signals["has_data"] and content_signals["has_citation"]:
            if tier == EvidenceTier.C:
                tier = EvidenceTier.B
                notes.append("内容含数据和引用，升级为 B 级")
        elif content_signals["is_thin"]:
            if tier == EvidenceTier.B:
                tier = EvidenceTier.C
                notes.append("内容单薄，降级为 C 级")

    return EvidenceAssessment(
        tier=tier,
        tier_weight=TIER_WEIGHTS[tier],
        source_type=source_type,
        is_official=is_official,
        has_date=has_date,
        has_author=has_author,
        has_citation=has_citation,
        notes=notes,
    )


def _extract_host(url: str) -> str:
    """提取主机名"""
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        if ":" in host:
            host = host.split(":", 1)[0]
        return host
    except Exception:
        return ""


def _is_a_tier_domain(host: str) -> bool:
    """判断是否 A 级域名"""
    if not host:
        return False
    # 精确匹配
    if host in _A_DOMAINS:
        return True
    # 后缀匹配
    for domain in _A_DOMAINS:
        if host.endswith("." + domain):
            return True
    # 特殊后缀
    if host.endswith(".gov") or host.endswith(".edu") or host.endswith(".ac.uk"):
        return True
    return False


def _is_b_tier_domain(host: str) -> bool:
    """判断是否 B 级域名"""
    if not host:
        return False
    if host in _B_DOMAINS:
        return True
    # 支持子域名匹配（如 www.reuters.com 匹配 reuters.com）
    for domain in _B_DOMAINS:
        if host.endswith("." + domain):
            return True
    return False


def _is_c_tier_pattern(host: str) -> bool:
    """判断是否 C 级模式（企业自述）"""
    if not host:
        return False
    for pattern in _C_PATTERNS:
        if re.search(pattern, host):
            return True
    return False


def _assess_content_signals(content: str) -> dict:
    """评估内容信号"""
    if not content:
        return {"has_data": False, "has_citation": False, "is_thin": True}

    # 是否有数据（数字、百分比、日期）
    has_data = bool(re.search(r"\d+[\d,\.]*[%％]?", content))
    # 是否有引用（URL、参考文献标记）
    has_citation = bool(re.search(r"(https?://|参考文献|references|资料来源)", content, re.IGNORECASE))
    # 是否单薄（字数少、句子少）
    word_count = len(content)
    # 计算句子数（按标点符号分割）
    sentence_count = len(re.findall(r'[.!?。！？]', content))
    # 单薄判定：字数少于 200 或句子数少于 3
    is_thin = word_count < 200 or sentence_count < 3

    return {
        "has_data": has_data,
        "has_citation": has_citation,
        "is_thin": is_thin,
    }


def compute_weighted_score(assessments: list[EvidenceAssessment]) -> dict:
    """计算加权证据评分

    参数:
        assessments: 证据评估结果列表

    返回:
        包含加权平均分、等级分布、总体评级的字典
    """
    if not assessments:
        return {
            "weighted_score": 0.0,
            "tier_distribution": {},
            "overall_tier": EvidenceTier.D,
            "assessment_count": 0,
        }

    total_weight = sum(a.tier_weight for a in assessments)
    weighted_score = total_weight / len(assessments)

    # 等级分布
    tier_dist = {}
    for a in assessments:
        tier_dist[a.tier] = tier_dist.get(a.tier, 0) + 1

    # 总体评级：取最低等级（短板效应）
    min_tier = min(assessments, key=lambda a: TIER_RANK[a.tier]).tier

    return {
        "weighted_score": round(weighted_score, 3),
        "tier_distribution": tier_dist,
        "overall_tier": min_tier,
        "assessment_count": len(assessments),
    }

"""tests/test_evidence_tier.py — 证据分层系统单元测试"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from evidence_tier import (
    EvidenceTier,
    assess_evidence_tier,
    compute_weighted_score,
    TIER_WEIGHTS,
    TIER_RANK,
)


class TestEvidenceTier:
    """证据分层基础测试"""

    def test_a_tier_official_domain(self):
        """A 级：官方域名"""
        result = assess_evidence_tier(
            url="https://www.gov.cn/zhengce/zhengceku/202401/content_6925837.htm",
            source_type="gov",
            is_official=True,
            has_date=True,
            has_citation=True,
        )
        assert result.tier == EvidenceTier.A
        assert result.tier_weight == 1.0

    def test_a_tier_docs_domain(self):
        """A 级：官方文档域名"""
        result = assess_evidence_tier(
            url="https://docs.python.org/3/library/os.html",
            source_type="docs-site",
            is_official=True,
        )
        assert result.tier == EvidenceTier.A

    def test_b_tier_news_domain(self):
        """B 级：知名新闻媒体"""
        result = assess_evidence_tier(
            url="https://www.reuters.com/technology/ai-2024-01-15/",
            source_type="news",
            is_official=False,
            has_date=True,
            has_author=True,
            has_citation=True,
        )
        assert result.tier == EvidenceTier.B
        assert result.tier_weight == 0.7

    def test_b_tier_chinese_media(self):
        """B 级：中文知名媒体"""
        result = assess_evidence_tier(
            url="https://36kr.com/p/1234567",
            source_type="news",
            is_official=False,
            has_date=True,
            has_author=True,
        )
        assert result.tier == EvidenceTier.B

    def test_c_tier_corporate_blog(self):
        """C 级：企业博客"""
        result = assess_evidence_tier(
            url="https://blog.example.com/product-update",
            source_type="blog",
            is_official=False,
            has_date=True,
            has_author=True,
        )
        assert result.tier == EvidenceTier.C
        assert result.tier_weight == 0.3

    def test_d_tier_unknown_domain(self):
        """D 级：未知域名"""
        result = assess_evidence_tier(
            url="https://random-site-12345.com/article",
            source_type="unknown",
            is_official=False,
        )
        assert result.tier == EvidenceTier.D
        assert result.tier_weight == 0.0

    def test_c_to_b_upgrade_with_evidence(self):
        """C 级升级为 B 级：有完整证据链"""
        result = assess_evidence_tier(
            url="https://blog.example.com/product-update",
            source_type="blog",
            is_official=False,
            has_date=True,
            has_author=True,
            has_citation=True,
            content="This is a detailed article with data and references. " * 50,
        )
        assert result.tier == EvidenceTier.B

    def test_b_to_c_downgrade_thin_content(self):
        """B 级降级为 C 级：内容单薄"""
        result = assess_evidence_tier(
            url="https://www.reuters.com/technology/ai-2024-01-15/",
            source_type="news",
            is_official=False,
            has_date=True,
            has_author=True,
            has_citation=True,
            content="Short.",
        )
        assert result.tier == EvidenceTier.C


class TestWeightedScore:
    """加权评分测试"""

    def test_empty_list(self):
        """空列表返回默认值"""
        result = compute_weighted_score([])
        assert result["weighted_score"] == 0.0
        assert result["overall_tier"] == EvidenceTier.D

    def test_single_a_tier(self):
        """单条 A 级来源"""
        assessments = [
            assess_evidence_tier(
                url="https://www.gov.cn/zhengce/zhengceku/202401/content_6925837.htm",
                source_type="gov",
                is_official=True,
            )
        ]
        result = compute_weighted_score(assessments)
        assert result["weighted_score"] == 1.0
        assert result["overall_tier"] == EvidenceTier.A

    def test_mixed_tiers(self):
        """混合等级"""
        assessments = [
            assess_evidence_tier(
                url="https://www.gov.cn/zhengce/zhengceku/202401/content_6925837.htm",
                source_type="gov",
                is_official=True,
            ),
            assess_evidence_tier(
                url="https://random-site-12345.com/article",
                source_type="unknown",
                is_official=False,
            ),
        ]
        result = compute_weighted_score(assessments)
        assert result["weighted_score"] == 0.5  # (1.0 + 0.0) / 2
        assert result["overall_tier"] == EvidenceTier.D  # 短板效应

    def test_tier_distribution(self):
        """等级分布统计"""
        assessments = [
            assess_evidence_tier(
                url="https://www.gov.cn/zhengce/zhengceku/202401/content_6925837.htm",
                source_type="gov",
                is_official=True,
            ),
            assess_evidence_tier(
                url="https://docs.python.org/3/library/os.html",
                source_type="docs-site",
                is_official=True,
            ),
            assess_evidence_tier(
                url="https://random-site-12345.com/article",
                source_type="unknown",
                is_official=False,
            ),
        ]
        result = compute_weighted_score(assessments)
        assert result["tier_distribution"] == {"official": 2, "unverified": 1}

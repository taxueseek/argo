"""tests/test_source_ledger.py — 来源账本单元测试"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from datetime import datetime, timezone, timedelta
from source_ledger import (
    SourceLedger,
    SourceRecord,
    create_source_record,
    infer_freshness_cadence,
    infer_access_status,
    FRESHNESS_CADENCE_DAYS,
)


class TestSourceRecord:
    """来源记录基础测试"""

    def test_create_basic_record(self):
        """创建基本来源记录"""
        record = SourceRecord(
            url="https://example.com/article",
            publisher="Example",
            verified_at=datetime.now(timezone.utc),
            access_status="accessible",
            freshness_cadence="daily",
            confidence_tier="B",
        )
        assert record.url == "https://example.com/article"
        assert record.access_status == "accessible"
        assert record.confidence_tier == "B"

    def test_to_dict(self):
        """转换为字典"""
        record = SourceRecord(
            url="https://example.com/article",
            publisher="Example",
            verified_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
            access_status="accessible",
            freshness_cadence="daily",
            confidence_tier="B",
        )
        d = record.to_dict()
        assert d["url"] == "https://example.com/article"
        assert d["publisher"] == "Example"
        assert d["access_status"] == "accessible"


class TestSourceLedger:
    """来源账本测试"""

    def test_empty_ledger(self):
        """空账本"""
        ledger = SourceLedger()
        assert ledger.get_summary()["total_sources"] == 0

    def test_add_and_get_source(self):
        """添加和获取来源"""
        ledger = SourceLedger()
        record = SourceRecord(
            url="https://example.com/article",
            verified_at=datetime.now(timezone.utc),
            access_status="accessible",
            freshness_cadence="daily",
            confidence_tier="B",
        )
        ledger.add_source(record)
        assert ledger.get_source("https://example.com/article") is not None
        assert ledger.get_source("https://not-exist.com") is None

    def test_freshness_risk_accessible(self):
        """可访问来源的新鲜度风险"""
        ledger = SourceLedger()
        record = SourceRecord(
            url="https://example.com/article",
            verified_at=datetime.now(timezone.utc),
            access_status="accessible",
            freshness_cadence="daily",
            confidence_tier="B",
        )
        ledger.add_source(record)
        risk = ledger.get_freshness_risk("https://example.com/article")
        assert risk == 0.0  # 刚验证，无风险

    def test_freshness_risk_stale(self):
        """过期来源的新鲜度风险"""
        ledger = SourceLedger()
        record = SourceRecord(
            url="https://example.com/article",
            verified_at=datetime.now(timezone.utc) - timedelta(days=30),
            access_status="accessible",
            freshness_cadence="daily",
            confidence_tier="B",
        )
        ledger.add_source(record)
        risk = ledger.get_freshness_risk("https://example.com/article")
        assert risk == 1.0  # 30 天前的每日内容，风险最大

    def test_freshness_risk_no_date(self):
        """无验证日期的来源"""
        ledger = SourceLedger()
        record = SourceRecord(
            url="https://example.com/article",
            verified_at=None,
            access_status="accessible",
            freshness_cadence="daily",
            confidence_tier="B",
        )
        ledger.add_source(record)
        risk = ledger.get_freshness_risk("https://example.com/article")
        assert risk == 0.8  # 无日期，高风险

    def test_access_risk_unknown(self):
        """未知访问状态"""
        ledger = SourceLedger()
        record = SourceRecord(
            url="https://example.com/article",
            access_status="unknown",
        )
        ledger.add_source(record)
        risk = ledger.get_access_risk("https://example.com/article")
        assert risk == 0.7

    def test_overall_risk_calculation(self):
        """综合风险计算"""
        ledger = SourceLedger()
        record = SourceRecord(
            url="https://example.com/article",
            verified_at=datetime.now(timezone.utc),
            access_status="accessible",
            freshness_cadence="static",
            confidence_tier="A",
        )
        ledger.add_source(record)
        risk = ledger.get_overall_risk("https://example.com/article")
        assert risk < 0.1  # 低风险

    def test_stale_sources_detection(self):
        """过期来源检测"""
        ledger = SourceLedger()
        # 高风险来源
        ledger.add_source(SourceRecord(
            url="https://farm-12345.com/article",
            verified_at=None,
            access_status="unknown",
            freshness_cadence="unknown",
            confidence_tier="D",
        ))
        # 低风险来源
        ledger.add_source(SourceRecord(
            url="https://www.gov.cn/policy",
            verified_at=datetime.now(timezone.utc),
            access_status="accessible",
            freshness_cadence="static",
            confidence_tier="A",
        ))
        stale = ledger.get_stale_sources(threshold=0.5)
        assert len(stale) == 1
        assert "farm-12345" in stale[0]


class TestAutoInference:
    """自动推断测试"""

    def test_infer_freshness_realtime(self):
        """推断实时内容"""
        cadence = infer_freshness_cadence(
            "https://finance.sina.com.cn/",
            "今日股市行情",
            ""
        )
        assert cadence == "daily"

    def test_infer_freshness_static(self):
        """推断静态内容"""
        cadence = infer_freshness_cadence(
            "https://docs.python.org/3/library/os.html",
            "os — Miscellaneous operating system interfaces",
            ""
        )
        assert cadence == "static"

    def test_infer_access_status_ok(self):
        """推断访问状态：正常"""
        status = infer_access_status("https://example.com", fetch_success=True, http_status=200)
        assert status == "accessible"

    def test_infer_access_status_restricted(self):
        """推断访问状态：受限"""
        status = infer_access_status("https://example.com", fetch_success=True, http_status=403)
        assert status == "restricted"

    def test_infer_access_status_unavailable(self):
        """推断访问状态：不可用"""
        status = infer_access_status("https://example.com", fetch_success=False, http_status=404)
        assert status == "unavailable"


class TestCreateSourceRecord:
    """创建来源记录测试"""

    def test_create_with_all_fields(self):
        """创建完整来源记录"""
        record = create_source_record(
            url="https://www.gov.cn/zhengce/zhengceku/202401/content_6925837.htm",
            publisher="中国政府网",
            fetch_success=True,
            http_status=200,
            title="政策文件",
            content="这是一份政府政策文件，包含具体的数据和实施细节。" * 30,
            verified_at=datetime.now(timezone.utc),
            confidence_tier="A",
        )
        assert record.url == "https://www.gov.cn/zhengce/zhengceku/202401/content_6925837.htm"
        assert record.publisher == "中国政府网"
        assert record.access_status == "accessible"
        assert record.confidence_tier == "A"

    def test_create_without_verified_at(self):
        """创建无验证日期的来源记录"""
        record = create_source_record(
            url="https://example.com/article",
            fetch_success=True,
            http_status=200,
        )
        assert record.verified_at is None
        # 无验证日期应导致高风险
        ledger = SourceLedger()
        ledger.add_source(record)
        risk = ledger.get_freshness_risk(record.url)
        assert risk == 0.8

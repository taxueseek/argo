#!/usr/bin/env python3
"""source_ledger.py — 来源账本

每个来源必须记录访问状态、发布者、URL、验证日期、提取说明、新鲜度节奏。
用于自动识别过期/不可验证来源，对抗内容农场。

设计原则：
  - 纯 stdlib，零依赖
  - 与 evidence_tier 互补：后者判证据等级，本模块判来源健康度
  - 输出可直接用于内容质量评分和 GEO 监测
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from urllib.parse import urlparse


# ── 数据结构 ────────────────────────────────────────────────────────

@dataclass
class SourceRecord:
    """来源记录"""
    url: str
    publisher: str = ""
    verified_at: Optional[datetime] = None
    access_status: str = "unknown"  # accessible / restricted / unavailable / unknown
    freshness_cadence: str = "unknown"  # realtime / daily / weekly / monthly / static / unknown
    confidence_tier: str = "D"  # A / B / C / D
    extraction_note: str = ""
    title: str = ""
    content_hash: str = ""  # 内容指纹，用于检测变更

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "publisher": self.publisher,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "access_status": self.access_status,
            "freshness_cadence": self.freshness_cadence,
            "confidence_tier": self.confidence_tier,
            "extraction_note": self.extraction_note,
            "title": self.title,
            "content_hash": self.content_hash,
        }


# ── 新鲜度节奏 ──────────────────────────────────────────────────────

FRESHNESS_CADENCE_DAYS = {
    "realtime": 0,      # 实时（分钟级）
    "daily": 1,         # 每日
    "weekly": 7,        # 每周
    "monthly": 30,      # 每月
    "quarterly": 90,    # 每季度
    "yearly": 365,      # 每年
    "static": 3650,     # 静态（10 年）
    "unknown": 30,      # 未知，默认按月
}


class SourceLedger:
    """来源账本"""

    def __init__(self):
        self.records: dict[str, SourceRecord] = {}

    def add_source(self, record: SourceRecord) -> None:
        """添加来源记录"""
        self.records[record.url] = record

    def get_source(self, url: str) -> Optional[SourceRecord]:
        """获取来源记录"""
        return self.records.get(url)

    def get_freshness_risk(self, url: str) -> float:
        """计算来源新鲜度风险（0-1，越高越危险）

        风险 = 实际年龄 / 期望新鲜度周期
        """
        record = self.records.get(url)
        if not record:
            return 1.0  # 未知来源，最高风险

        if not record.verified_at:
            return 0.8  # 无验证日期，高风险

        age = datetime.now(timezone.utc) - record.verified_at
        cadence_days = FRESHNESS_CADENCE_DAYS.get(record.freshness_cadence, 30)

        if cadence_days == 0:
            return 0.0  # 实时内容，无风险

        risk = age.days / cadence_days
        return min(risk, 1.0)

    def get_access_risk(self, url: str) -> float:
        """计算访问风险（0-1，越高越危险）"""
        record = self.records.get(url)
        if not record:
            return 1.0

        risk_map = {
            "accessible": 0.0,
            "restricted": 0.5,
            "unavailable": 1.0,
            "unknown": 0.7,
        }
        return risk_map.get(record.access_status, 0.7)

    def get_overall_risk(self, url: str) -> float:
        """计算综合风险（0-1，越高越危险）

        综合风险 = 0.4 * 新鲜度风险 + 0.3 * 访问风险 + 0.3 * 证据等级风险
        """
        freshness_risk = self.get_freshness_risk(url)
        access_risk = self.get_access_risk(url)

        record = self.records.get(url)
        if record:
            tier_risk = 1.0 - {"A": 1.0, "B": 0.7, "C": 0.3, "D": 0.0}.get(record.confidence_tier, 0.0)
        else:
            tier_risk = 1.0

        return round(0.4 * freshness_risk + 0.3 * access_risk + 0.3 * tier_risk, 3)

    def get_stale_sources(self, threshold: float = 0.5) -> list[str]:
        """获取过期来源列表（风险超过阈值）"""
        return [url for url in self.records if self.get_overall_risk(url) > threshold]

    def get_summary(self) -> dict:
        """获取账本摘要"""
        if not self.records:
            return {
                "total_sources": 0,
                "accessible_count": 0,
                "restricted_count": 0,
                "unavailable_count": 0,
                "high_risk_count": 0,
                "tier_distribution": {},
            }

        accessible = sum(1 for r in self.records.values() if r.access_status == "accessible")
        restricted = sum(1 for r in self.records.values() if r.access_status == "restricted")
        unavailable = sum(1 for r in self.records.values() if r.access_status == "unavailable")
        high_risk = sum(1 for r in self.records.values() if self.get_overall_risk(r.url) > 0.5)

        tier_dist = {}
        for r in self.records.values():
            tier_dist[r.confidence_tier] = tier_dist.get(r.confidence_tier, 0) + 1

        return {
            "total_sources": len(self.records),
            "accessible_count": accessible,
            "restricted_count": restricted,
            "unavailable_count": unavailable,
            "high_risk_count": high_risk,
            "tier_distribution": tier_dist,
        }


# ── 自动推断 ────────────────────────────────────────────────────────

def infer_freshness_cadence(url: str, title: str = "", content: str = "") -> str:
    """推断内容的新鲜度节奏

    基于 URL 模式、标题关键词和内容特征推断。
    """
    host = _extract_host(url)
    combined = f"{url} {title} {content[:500]}".lower()

    # 实时信号
    if any(kw in combined for kw in ["实时", "快讯", "直播", "breaking", "live", "realtime"]):
        return "realtime"

    # 每日信号
    if any(kw in combined for kw in ["日报", "每日", "daily", "today", "今日"]):
        return "daily"

    # 每周信号
    if any(kw in combined for kw in ["周报", "每周", "weekly", "week"]):
        return "weekly"

    # 每月信号
    if any(kw in combined for kw in ["月报", "每月", "monthly", "month"]):
        return "monthly"

    # 静态内容
    if any(kw in combined for kw in ["文档", "指南", "教程", "docs", "guide", "tutorial", "reference"]):
        return "static"

    # 默认
    return "unknown"


def infer_access_status(url: str, fetch_success: bool = True, http_status: int = 200) -> str:
    """推断访问状态"""
    if not fetch_success:
        return "unavailable"
    if http_status == 200:
        return "accessible"
    if http_status in (401, 403, 429):
        return "restricted"
    if http_status >= 400:
        return "unavailable"
    return "unknown"


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


def create_source_record(
    url: str,
    publisher: str = "",
    fetch_success: bool = True,
    http_status: int = 200,
    title: str = "",
    content: str = "",
    verified_at: Optional[datetime] = None,
    confidence_tier: str = "D",
) -> SourceRecord:
    """创建来源记录（自动推断缺失字段）

    注意：verified_at 为 None 时，来源会被标记为高风险（无验证日期）。
    只有明确知道验证时间时才传入。
    """
    return SourceRecord(
        url=url,
        publisher=publisher,
        verified_at=verified_at,  # None 表示未知，会被标记为高风险
        access_status=infer_access_status(url, fetch_success, http_status),
        freshness_cadence=infer_freshness_cadence(url, title, content),
        confidence_tier=confidence_tier,
        title=title,
    )

#!/usr/bin/env python3
"""
fact_align.py — 关键事实交叉标记（P0-004）

第一性原理：
  多源检索的价值不止「更多结果」，而是「可交叉验证的事实」。
  从 title+snippet 抽取结构化事实（版本/百分比/金额/日期/法规号），
  按 (type, value) 聚合：
    - 同 type 不同 value → 冲突（fact_conflicts）：提示 Agent 谨慎
    - 同 value ≥2 域名   → 印证（fact_corroborated）：提升可信度

MECE 事实类型（正交、可正则化）：
  version    版本号     v?\\d+\\.\\d+(\\.\\d+)?
  percent    百分比     \\d+(\\.\\d+)?%
  money      金额       ￥/$/€ + 数字（含亿/万/billion/million 单位）
  date       日期       YYYY-MM-DD / YYYY年MM月 / MM/DD/YYYY
  law_code   法规号     《XX法》第N条 / 法释〔YYYY〕N号 等

启用条件：仅 deep/auto 且结果 ≥3；fast 跳过（控制延迟）。
输出：顶层 fact_alignment 字段。

纯本地，仅 stdlib。
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse


# ── 事实抽取正则（每类捕获归一化 value）──────────────────────────────────────

_FACT_PATTERNS: dict[str, re.Pattern] = {
    "version": re.compile(r"(?<![\d.])v?(\d+\.\d+(?:\.\d+)?)(?![\d.])", re.I),
    "percent": re.compile(r"(\d+(?:\.\d+)?)\s*%|(\d+(?:\.\d+)?)\s*percent", re.I),
    "money": re.compile(
        r"(?:[￥$€]|人民币|美元)\s*(\d+(?:\.\d+)?(?:\s*(?:亿|万|千|百万))?)"
        r"|(\d+(?:\.\d+)?)\s*(?:亿元|万元|亿美元|billion|million)", re.I),
    "date": re.compile(
        r"(\d{4}-\d{1,2}-\d{1,2})"
        r"|(\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)"
        r"|(\d{1,2}/\d{1,2}/\d{4})"),
    "law_code": re.compile(
        r"(《[^》]{2,20}(?:法|条例|规定|办法|解释)》(?:第[\d一二三四五六七八九十百]+条)?)"
        r"|(法释〔?\d{4}〕?\d+号)"),
}


def _normalize_value(fact_type: str, raw: str) -> str:
    """归一化 value，便于跨源比较。"""
    v = raw.strip()
    if fact_type == "percent":
        return v.replace(" ", "").rstrip("%") + "%"
    if fact_type == "version":
        return v.lower().lstrip("v")
    if fact_type == "date":
        # 统一分隔符与全角
        return re.sub(r"\s+", "", v).replace("年", "-").replace("月", "-").replace("日", "").strip("-")
    if fact_type == "money":
        return re.sub(r"\s+", "", v)
    return re.sub(r"\s+", "", v)


def extract_facts(text: str) -> list[tuple[str, str]]:
    """从文本抽取 (type, normalized_value) 列表。"""
    facts: list[tuple[str, str]] = []
    if not text:
        return facts
    for ftype, pat in _FACT_PATTERNS.items():
        for m in pat.finditer(text):
            # 取第一个非空捕获组
            raw = next((g for g in m.groups() if g), None)
            if not raw:
                continue
            val = _normalize_value(ftype, raw)
            if val and (ftype, val) not in facts:
                facts.append((ftype, val))
    return facts


def _domain_of(url: str) -> str:
    """提取 URL 的注册域名（用于跨源判定）。"""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def align_facts(results: list[dict[str, Any]], min_results: int = 3,
                mode: str = "auto", depth: str = "balanced") -> dict[str, Any] | None:
    """对多源结果做事实交叉标记。

    Args:
        results: 搜索结果列表（需含 title/snippet/url）。
        min_results: 启用阈值（默认 3）。
        mode: 预算模式；fast 直接跳过。
        depth: 深度；fast 跳过。

    Returns:
        fact_alignment 字典，或 None（未启用/无事实）。
        {
          "enabled": True,
          "fact_conflicts": [{"type","values":[{"value","domains"}]}],
          "fact_corroborated": [{"type","value","domains"}],
          "stats": {"facts_extracted","conflicts","corroborated"}
        }
    """
    if mode == "fast" or depth == "fast":
        return None
    if not results or len(results) < min_results:
        return None

    # (type, value) → set(domains)
    tv_domains: dict[tuple[str, str], set[str]] = defaultdict(set)
    # type → set(values)
    type_values: dict[str, set[str]] = defaultdict(set)

    for r in results:
        if not isinstance(r, dict):
            continue
        text = f"{r.get('title', '')} {r.get('snippet', '')}"
        domain = _domain_of(r.get("url", "")) or (r.get("source", "") or "unknown")
        for ftype, val in extract_facts(text):
            tv_domains[(ftype, val)].add(domain)
            type_values[ftype].add(val)

    if not tv_domains:
        return None

    # 印证：同 (type,value) 出现在 ≥2 个域名
    corroborated = []
    for (ftype, val), domains in tv_domains.items():
        if len(domains) >= 2:
            corroborated.append({
                "type": ftype, "value": val, "domains": sorted(domains),
            })

    # 冲突：同 type 出现 ≥2 个不同 value
    conflicts = []
    for ftype, values in type_values.items():
        if len(values) >= 2:
            value_entries = []
            for val in sorted(values):
                value_entries.append({
                    "value": val,
                    "domains": sorted(tv_domains[(ftype, val)]),
                })
            conflicts.append({"type": ftype, "values": value_entries})

    return {
        "enabled": True,
        "fact_conflicts": conflicts,
        "fact_corroborated": corroborated,
        "stats": {
            "facts_extracted": len(tv_domains),
            "conflicts": len(conflicts),
            "corroborated": len(corroborated),
        },
    }


# ── CLI 测试 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    sample = [
        {"title": "Python 3.12 发布", "snippet": "性能提升 5%，2023-10-02 正式发布",
         "url": "https://python.org/news"},
        {"title": "Python 3.12 新特性", "snippet": "官方称提速约 5% ，发布于 2023年10月",
         "url": "https://realpython.com/py312"},
        {"title": "Python 版本对比", "snippet": "3.11 相比 3.10 提速 25%",
         "url": "https://blog.example.com/x"},
        {"title": "营收报告", "snippet": "该公司营收 120亿元，同比增长 15%",
         "url": "https://finance.a.com"},
        {"title": "财报快讯", "snippet": "营收达 130亿元",
         "url": "https://finance.b.com"},
    ]
    out = align_facts(sample, mode="auto", depth="deep")
    print(json.dumps(out, ensure_ascii=False, indent=2))

#!/usr/bin/env python3
"""cross_validate 的中文相关度口径（issue #18 的 0/N 信号）。

旧实现 `query.lower().split()` 对无空格中文查询只有一个整句 token，结果里
永远不可能出现整句 → 真实相关也报 0/N；反过来，结果只带「2026」年份就被
算作相关。改用 CJK 二元组覆盖率后，两个方向都要判对。
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from evidence import cross_validate  # noqa: E402


def _row(title: str, snippet: str, url: str) -> dict:
    return {"title": title, "snippet": snippet, "url": url}


def test_chinese_relevant_results_are_counted():
    report = cross_validate([
        _row("中汽协:预计2026年汽车总销量为3475万辆",
             "其中新能源汽车销量为1900万辆", "https://a.example/1"),
        _row("2025年我国汽车产销量均突破3400万辆",
             "新能源汽车销量数据继续增长", "https://b.example/2"),
    ], "2026年新能源汽车销量数据")
    assert report["agreement_count"] == 2
    assert report["detail"].startswith("2/2 与查询相关")


def test_year_only_pages_are_not_counted():
    report = cross_validate([
        _row("2026 - Wikipedia", "2026 is a common year", "https://c.example/3"),
        _row("2026中国节假日安排", "元旦、春节、清明", "https://d.example/4"),
    ], "2026年新能源汽车销量数据")
    assert report["agreement_count"] == 0
    assert report["detail"].startswith("0/2 与查询相关")


def test_english_query_counts_only_the_relevant_row():
    report = cross_validate([
        _row("GitHub - dsh-market/dsh-market: The plugin market",
             "dsh plugin market latest version", "https://github.com/x/y"),
        _row("百年银幕之旅", "上海电影", "https://people.example/a"),
    ], "dsh plugin market dshmarket latest version")
    assert report["agreement_count"] == 1

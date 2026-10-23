#!/usr/bin/env python3
"""issue #18：`latest` 单独出现不得把软件版本查询判进 news_realtime。

旧规则 `\\b(news|latest)\\b` 只要看到 latest 就命中实时新闻域，该域 combo
只有新闻源；「dsh plugin market ... latest version」「latest version of
Python」「latest release notes」全部被送进新闻链，人民网模糊匹配不到就回
默认 feed。版本语义的 latest 必须让位给技术/通用域；真正的新闻查询
（latest news / breaking news）仍要走 news_realtime。
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from route import route_query  # noqa: E402


def _domain(query: str) -> str:
    return route_query(query).get("domain") or ""


def test_dsh_market_latest_version_not_news():
    assert _domain("dsh plugin market dshmarket latest version") != "news_realtime"


def test_latest_version_of_python_not_news():
    assert _domain("latest version of Python") != "news_realtime"


def test_latest_release_notes_not_news():
    assert _domain("latest release notes") != "news_realtime"


def test_latest_news_still_routes_to_news():
    assert _domain("latest news on AI") == "news_realtime"


def test_breaking_news_still_routes_to_news():
    assert _domain("breaking news") == "news_realtime"


def test_chinese_news_still_routes_to_news():
    assert _domain("今天有什么最新新闻") == "news_realtime"

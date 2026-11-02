#!/usr/bin/env python3
"""test_snippet_clean — 摘要交付前的装饰性清洗（离线，纯函数）。"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from snippet_clean import clean_snippet  # noqa: E402


def test_strips_markdown_lead_and_repeated_site_prefix():
    raw = "- # 技术栈技术栈Python asyncio 教程\n\n正文开始"
    assert clean_snippet(raw) == "技术栈Python asyncio 教程 正文开始"


def test_strips_leading_site_navigation_chain():
    raw = "首页 登录 注册 关于我们 小红书露营季 怎么玩"
    assert clean_snippet(raw) == "小红书露营季 怎么玩"


def test_strips_trailing_site_chrome():
    raw = "露营装备清单与避坑建议 版权所有 关于我们 联系我们"
    assert clean_snippet(raw) == "露营装备清单与避坑建议"


def test_keeps_normal_content_untouched():
    raw = "露营装备清单：帐篷、睡袋、照明与炊具，附预算分配。"
    assert clean_snippet(raw) == raw


def test_empty_and_non_string_are_safe():
    assert clean_snippet("") == ""
    assert clean_snippet(None) is None  # type: ignore[arg-type]

#!/usr/bin/env python3
"""test_bing_rss_engine.py — Bing RSS 网页搜索引擎回归测试。

锁定四条契约：
  1. RSS item 解析：title/link/description/pubDate；CDATA 与中文内容完整；
     description 里的 HTML 标签剥掉、XML 实体解码
  2. pubDate（RFC 822）→ ISO；解析不了的原样透传，不丢时间信息
  3. URL 构造：format=rss、count 上限 20
  4. 坏 XML / 网络失败 / 缺 title 或 link 的 item → 诚实处理（空列表/跳过）

打桩点打在读取处（ebt._http_get_raw），fixture 离线，无真实网络。
"""

import json
import os
import sys
from email.utils import parsedate_to_datetime

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_builders_tech as ebt  # noqa: E402

# fixture 覆盖三类形态：CDATA+中文+HTML 片段、纯文本+实体、坏 pubDate
_FIXTURE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:bing="https://www.bing.com/search">
<channel>
<title>python 教程 - 搜索结果</title>
<item>
<title><![CDATA[Python 官方中文教程]]></title>
<link>https://docs.python.org/zh-cn/3/tutorial/</link>
<description><![CDATA[Python 是一门<b>易于学习</b>、功能强大的编程语言]]></description>
<pubDate>Wed, 24 Sep 2026 08:00:00 GMT</pubDate>
</item>
<item>
<title>PEP 8 风格指南要点</title>
<link>https://example.org/pep8-notes</link>
<description>缩进用四个空格 &amp; 命名用蛇形</description>
<pubDate>not-a-date</pubDate>
</item>
<item>
<title>没有链接的条目不该出现</title>
<description>残缺 item</description>
</item>
</channel>
</rss>"""


@pytest.fixture()
def engine(monkeypatch):
    monkeypatch.setattr(ebt, "_http_get_raw",
                        lambda u, h, t, engine=None: _FIXTURE_RSS)
    return ebt._build_bing_rss_engine({})


class TestRssParsing:
    def test_cdata_chinese_item_parsed(self, engine):
        rs = engine("python 教程", n=5)
        assert len(rs) == 2  # 残缺 item（无 link）被跳过
        r = rs[0]
        assert r["title"] == "Python 官方中文教程"
        assert r["url"] == "https://docs.python.org/zh-cn/3/tutorial/"
        assert r["source"] == "bing_rss"

    def test_html_tags_stripped_from_description(self, engine):
        s = engine("python 教程", n=5)[0]["snippet"]
        assert "<b>" not in s and "易于学习" in s

    def test_xml_entity_decoded(self, engine):
        s = engine("python 教程", n=5)[1]["snippet"]
        assert "&amp;" not in s and "&" in s

    def test_rfc822_pubdate_to_iso(self, engine):
        r = engine("python 教程", n=5)[0]
        expect = parsedate_to_datetime(
            "Wed, 24 Sep 2026 08:00:00 GMT"
        ).astimezone().isoformat(timespec="seconds")
        assert r["published_at"] == expect

    def test_unparseable_pubdate_passthrough(self, engine):
        r = engine("python 教程", n=5)[1]
        assert r["published_at"] == "not-a-date"

    def test_score_monotonic_decay(self, engine):
        rs = engine("python 教程", n=5)
        assert rs[0]["score"] > rs[1]["score"]


class TestUrlConstruction:
    def test_format_rss_in_url(self, monkeypatch):
        seen = {}

        def fake(url, headers, timeout, engine=None):
            seen["url"] = url
            return _FIXTURE_RSS

        monkeypatch.setattr(ebt, "_http_get_raw", fake)
        ebt._build_bing_rss_engine({})("python 教程", n=5)
        assert "format=rss" in seen["url"]
        assert "count=5" in seen["url"]

    def test_count_capped_at_20(self, monkeypatch):
        seen = {}

        def fake(url, headers, timeout, engine=None):
            seen["url"] = url
            return _FIXTURE_RSS

        monkeypatch.setattr(ebt, "_http_get_raw", fake)
        ebt._build_bing_rss_engine({})("python", n=50)
        assert "count=20" in seen["url"]

    def test_query_url_encoded(self, monkeypatch):
        seen = {}

        def fake(url, headers, timeout, engine=None):
            seen["url"] = url
            return _FIXTURE_RSS

        monkeypatch.setattr(ebt, "_http_get_raw", fake)
        ebt._build_bing_rss_engine({})("机器 学习", n=5)
        assert "q=%E6%9C%BA%E5%99%A8%20%E5%AD%A6%E4%B9%A0" in seen["url"]


class TestHonestEmpty:
    def test_empty_query(self):
        assert ebt._build_bing_rss_engine({})("   ", n=5) == []

    def test_bad_xml(self, monkeypatch):
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: "<html>503</html>")
        assert ebt._build_bing_rss_engine({})("python", n=5) == []

    def test_http_failure(self, monkeypatch):
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: None)
        assert ebt._build_bing_rss_engine({})("python", n=5) == []

    def test_rss_without_items(self, monkeypatch):
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None:
                            '<rss version="2.0"><channel><title>x</title>'
                            "</channel></rss>")
        assert ebt._build_bing_rss_engine({})("python", n=5) == []

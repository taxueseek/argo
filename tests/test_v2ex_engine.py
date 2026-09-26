#!/usr/bin/env python3
"""test_v2ex_engine.py — V2EX 引擎（官方 API 版）回归测试。

背景：旧实现在 `/search?q=` 上抓 `item_title` 正则。但 V2EX 站内搜索需登录，
未登录访问 `/search?q=` 会 302 到 `/go/search`——那是「搜索引擎技术研究」
**节点页**，不是搜索结果。于是旧实现产出的是节点热帖，且：

  - 10 条结果的 url 全部等于查询自身的搜索页地址（不是帖子）
  - snippet 恒为硬编码常量 "V2EX 社区讨论"
  - 标题与查询无关（查「V2EX 社区」返回「装机 配置 预算」）
  - coverage 仍报 status=ok / returned=10 → 失败伪装成成功

现方案走官方开放 API（hot/latest/show/replies），API 无搜索端点，
故「拉候选池 → 本地相关性过滤」。本文件锁定四条契约：
真实可核验链接、相关性过滤有效、无关查询诚实返回空、字段完备。
"""

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_builders_tech as ebt  # noqa: E402


def _topic(tid, title, content="", node_title="", replies=0, username="u1"):
    return {
        "id": tid,
        "title": title,
        "content": content,
        "url": f"https://www.v2ex.com/t/{tid}",
        "created": 1700000000 + tid,
        "replies": replies,
        "node": {"title": node_title, "name": (node_title or "n").lower()},
        "member": {"username": username},
    }


@pytest.fixture()
def engine(monkeypatch):
    """构造引擎，并把候选池替换为固定 fixture（不打真实网络）。"""
    pool = [
        _topic(1, "iPhone Duo 并没有多惊艳", "手机拿出来的时候也就 UI 有点意思", "Apple", 132),
        _topic(2, "MacBook Pro 散热问题", "长时间编译会降频", "Apple", 20),
        _topic(3, "分享一个 Python 脚本", "用来批量重命名文件", "分享发现", 5),
        _topic(4, "今天午饭吃什么", "公司附近都吃腻了", "生活", 8, username="u2"),
    ]

    def fake_get_raw(url, headers, timeout, engine=None):
        import json
        return json.dumps(pool)

    monkeypatch.setattr(ebt, "_http_get_raw", fake_get_raw)
    return ebt._build_v2ex_engine({})


class TestRealVerifiableLinks:
    """每条结果的 URL 必须是真实帖子地址。"""

    def test_url_is_topic_permalink_not_search_page(self, engine):
        rs = engine("iPhone", n=5)
        assert rs, "应命中 iPhone 相关主题"
        for r in rs:
            assert r["url"].startswith("https://www.v2ex.com/t/"), r["url"]
            assert "/search?" not in r["url"], "URL 不得是搜索页地址（旧实现的缺陷）"

    def test_snippet_is_real_content_not_constant(self, engine):
        rs = engine("iPhone", n=5)
        assert rs
        # 旧实现恒为 "V2EX 社区讨论"
        assert rs[0]["snippet"] != "V2EX 社区讨论"
        assert "UI" in rs[0]["snippet"] or "手机" in rs[0]["snippet"]

    def test_social_meta_present(self, engine):
        r = engine("iPhone", n=1)[0]
        m = r["social_meta"]
        assert m["platform"] == "v2ex"
        assert m["content_type"] == "topic"
        assert m["node"] == "Apple"
        assert m["author"] == "u1"
        assert m["replies"] == 132
        assert m["url_verifiable"] is True


class TestRelevanceFiltering:
    """本地相关性过滤必须真的生效。"""

    def test_unrelated_query_returns_empty(self, engine):
        """无关查询诚实返回空，而不是回落到伪造结果。"""
        assert engine("完全不相干的查询xyzabc", n=5) == []

    def test_single_common_word_does_not_match(self, engine):
        """单个通用词不足以拉进结果（旧实现靠节点名蹭命中）。"""
        # 「公司」只出现在 id=4 的正文里，但查询是单字词 → 不达相关阈值
        assert engine("公司附近有什么好吃的呢哦", n=5) == []

    def test_full_substring_ranks_highest(self, engine):
        rs = engine("MacBook Pro", n=5)
        assert rs
        assert rs[0]["title"] == "MacBook Pro 散热问题"

    def test_no_match_in_node_name_only(self, engine):
        """节点名命中不算相关：查「Apple」不应把 Apple 节点下无关主题全拉进来。"""
        rs = engine("zzz不存在的词zzz", n=5)
        assert rs == []

    def test_replies_sort_within_same_relevance(self, engine):
        """同相关度下按回复数降序（社区热度）。"""
        rs = engine("iPhone Duo 并没有多惊艳", n=5)
        assert rs and rs[0]["title"].startswith("iPhone Duo")


class TestTermMatchingBoundary:
    """词命中计算方式：拉丁词边界 + CJK 子串 + 单字符门槛。

    实测教训（本测试来源）：
      - 纯子串匹配下 "a" 命中 Apple/astar，"ab" 命中 Avalonia/Wabou/Workbuddy
      - 一刀切把门槛提到 3 会误伤 "ai"（有真实语义的双字符词）→ 返回 0 条
    正解是「拉丁词加词边界」，门槛只到 2（挡单字符）。
    """

    POOL = [
        _topic(1, "AI 编程助手对比", "Claude 和 GPT 都不错", "程序员", 5),
        _topic(2, "Avalonia 桌面开发", "Wabou 框架体验", "程序员", 3),
        _topic(3, "Apple 新品讨论", "astar 项目进展", "Apple", 9),
        _topic(4, "如何充值 ChatGPT", "美区 apple id 充值失败", "问与答", 2),
    ]

    @pytest.fixture()
    def eng(self, monkeypatch):
        import json
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: json.dumps(self.POOL))
        return ebt._build_v2ex_engine({})

    @pytest.mark.parametrize("q", ["a", "x", "z"])
    def test_single_latin_char_returns_nothing(self, eng, q):
        """单字符拉丁查询不得命中（否则 Apple/astar 全进来）。"""
        assert eng(q, n=5) == []

    @pytest.mark.parametrize("q", ["ab", "av", "wa"])
    def test_short_latin_substring_does_not_match(self, eng, q):
        """"ab" 不得命中 Avalonia/Wabou——词边界必须生效。"""
        assert eng(q, n=5) == []

    def test_two_char_latin_word_still_works(self, eng):
        """"ai" 是有真实语义的双字符词，必须能命中独立词 AI。"""
        rs = eng("ai", n=5)
        assert [r["title"] for r in rs] == ["AI 编程助手对比"]

    def test_uppercase_query_matches(self, eng):
        assert [r["title"] for r in eng("AI", n=5)] == ["AI 编程助手对比"]

    def test_latin_word_exact_match(self, eng):
        assert [r["title"] for r in eng("Avalonia", n=5)] == ["Avalonia 桌面开发"]

    def test_cjk_substring_match(self, eng):
        """CJK 无词边界概念，子串匹配是正确的。"""
        assert [r["title"] for r in eng("编程助手", n=5)] == ["AI 编程助手对比"]

    def test_cjk_word_not_in_text_does_not_match(self, eng):
        assert eng("程序员", n=5) == []  # 只是节点名，不参与匹配


class TestNoResultsIsHonest:
    """无结果即无结果，不得回到「失败伪装成成功」。"""

    def test_empty_pool_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ebt, "_http_get_raw", lambda u, h, t, engine=None: "[]")
        eng = ebt._build_v2ex_engine({})
        assert eng("iPhone", n=5) == []

    def test_api_failure_returns_empty_not_fabricated(self, monkeypatch):
        """API 全失败 → 空列表（旧实现会返回 10 条伪造结果）。"""
        monkeypatch.setattr(ebt, "_http_get_raw", lambda u, h, t, engine=None: None)
        eng = ebt._build_v2ex_engine({})
        assert eng("iPhone", n=5) == []

    def test_malformed_json_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ebt, "_http_get_raw", lambda u, h, t, engine=None: "not-json")
        eng = ebt._build_v2ex_engine({})
        assert eng("iPhone", n=5) == []


# ── sov2ex 全文搜索优先（两级来源）────────────────────────────────────────────

def _sov2ex_body(hits):
    return json.dumps({"took": 3, "total": len(hits), "hits": hits})


class TestSov2exFirstTier:
    """sov2ex 社区全文搜索是第一级来源，官方 API 池路径是降级备胎。

    锁定三条契约：
      - sov2ex 命中 → 直接返回全文结果，不得再消耗官方 API 配额；
      - sov2ex 空 / 失败 / 响应不可解析 → 落回池路径，池路径语义一行不改；
      - 两级都无相关条目 → 诚实空列表（任何一级都不许伪造结果）。
    """

    HIT = [{
        "_id": "10453081",
        "title": "有人用 Claude Code 吗",
        "content": "最近在用 Claude Code 写 V2EX 相关的小工具",
        "created": 1758860000,
        "_score": 3.42,
        "highlight": {"content": ["最近在用 <em>Claude</em> <em>Code</em> 写东西"]},
    }]

    def test_sov2ex_hit_returns_fulltext(self, monkeypatch):
        calls = []

        def fake(url, headers, timeout, engine=None):
            calls.append(url)
            if "sov2ex.com" in url:
                return _sov2ex_body(self.HIT)
            raise AssertionError(f"sov2ex 命中后不得再请求 {url}")

        monkeypatch.setattr(ebt, "_http_get_raw", fake)
        eng = ebt._build_v2ex_engine({})
        rs = eng("Claude Code", n=5)
        assert rs, "sov2ex 命中必须产出结果"
        r = rs[0]
        assert r["url"] == "https://www.v2ex.com/t/10453081"
        assert r["title"] == "有人用 Claude Code 吗"
        assert r["source"] == "v2ex"
        assert "<em>" not in r["snippet"] and "</em>" not in r["snippet"]
        assert "Claude" in r["snippet"]
        assert r["social_meta"]["retrieval_mode"] == "sov2ex_fulltext"
        assert "sov2ex.com" in calls[0]
        assert len(calls) == 1

    def test_sov2ex_hit_published_unix_to_iso(self, monkeypatch):
        from datetime import datetime
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: _sov2ex_body(self.HIT))
        eng = ebt._build_v2ex_engine({})
        r = eng("Claude Code", n=5)[0]
        expect = datetime.fromtimestamp(
            1758860000).astimezone().isoformat(timespec="seconds")
        assert r["published_at"] == expect

    def test_sov2ex_highlight_missing_falls_back_to_content(self, monkeypatch):
        hit = dict(self.HIT[0])
        del hit["highlight"]
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: _sov2ex_body([hit]))
        eng = ebt._build_v2ex_engine({})
        r = eng("Claude Code", n=5)[0]
        assert "V2EX 相关的小工具" in r["snippet"]

    def test_sov2ex_em_marks_stripped_from_plain_content(self, monkeypatch):
        hit = dict(self.HIT[0], content="正文里有 <em>Claude</em> 标记", highlight=None)
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: _sov2ex_body([hit]))
        eng = ebt._build_v2ex_engine({})
        r = eng("Claude Code", n=5)[0]
        assert "<em>" not in r["snippet"] and "Claude" in r["snippet"]

    def test_sov2ex_empty_falls_back_to_pool(self, monkeypatch):
        pool = [_topic(7, "Claude Code 体验", "写 V2EX 插件", "程序员", 3)]

        def fake(url, headers, timeout, engine=None):
            if "sov2ex.com" in url:
                return _sov2ex_body([])
            return json.dumps(pool)

        monkeypatch.setattr(ebt, "_http_get_raw", fake)
        eng = ebt._build_v2ex_engine({})
        rs = eng("Claude Code", n=5)
        assert rs and rs[0]["url"] == "https://www.v2ex.com/t/7"
        assert rs[0]["social_meta"]["retrieval_mode"] != "sov2ex_fulltext"

    def test_sov2ex_failure_falls_back_to_pool(self, monkeypatch):
        pool = [_topic(7, "Claude Code 体验", "写 V2EX 插件", "程序员", 3)]

        def fake(url, headers, timeout, engine=None):
            if "sov2ex.com" in url:
                return None  # 超时/网络错误在统一 GET 出口已折算为 None
            return json.dumps(pool)

        monkeypatch.setattr(ebt, "_http_get_raw", fake)
        eng = ebt._build_v2ex_engine({})
        assert eng("Claude Code", n=5)[0]["url"] == "https://www.v2ex.com/t/7"

    def test_sov2ex_garbage_falls_back_to_pool(self, monkeypatch):
        pool = [_topic(7, "Claude Code 体验", "写 V2EX 插件", "程序员", 3)]

        def fake(url, headers, timeout, engine=None):
            if "sov2ex.com" in url:
                return "<html>blocked by waf</html>"
            return json.dumps(pool)

        monkeypatch.setattr(ebt, "_http_get_raw", fake)
        eng = ebt._build_v2ex_engine({})
        assert eng("Claude Code", n=5)[0]["url"] == "https://www.v2ex.com/t/7"

    def test_both_tiers_empty_is_honest_empty(self, monkeypatch):
        def fake(url, headers, timeout, engine=None):
            if "sov2ex.com" in url:
                return _sov2ex_body([])
            return "[]"

        monkeypatch.setattr(ebt, "_http_get_raw", fake)
        eng = ebt._build_v2ex_engine({})
        assert eng("Claude Code", n=5) == []

    def test_both_tiers_failing_is_honest_empty(self, monkeypatch):
        monkeypatch.setattr(ebt, "_http_get_raw",
                            lambda u, h, t, engine=None: None)
        eng = ebt._build_v2ex_engine({})
        assert eng("Claude Code", n=5) == []

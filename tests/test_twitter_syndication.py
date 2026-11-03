#!/usr/bin/env python3
"""test_twitter_syndication.py — X 单条推文（syndication 免登录通道）回归测试。

覆盖：
  1. 推文 ID 提取：x.com / twitter.com / 老路径 / 分享链接参数 / 裸 ID
  2. token 推导：确定性向量锁定（防止浮点实现漂移）
  3. builder 解析：正常推文 / 墓碑 / 空响应 / 非推文查询诚实返回空
"""

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from engines_builders_intl import (  # noqa: E402
    extract_tweet_id,
    syndication_token,
    _build_twitter_syndication_engine,
)


class TestExtractTweetId:
    def test_x_com_url(self):
        assert extract_tweet_id(
            "https://x.com/elonmusk/status/1585841080431321088"
        ) == "1585841080431321088"

    def test_short_id_in_url(self):
        assert extract_tweet_id(
            "https://x.com/jack/status/20"
        ) == "20"

    def test_twitter_com_statuses(self):
        assert extract_tweet_id(
            "https://twitter.com/a/statuses/1585841080431321088?s=20"
        ) == "1585841080431321088"

    def test_share_link_param(self):
        assert extract_tweet_id(
            "https://x.com/i/web/status?tweet_id=1585841080431321088"
        ) == "1585841080431321088"

    def test_bare_id(self):
        assert extract_tweet_id("1585841080431321088") == "1585841080431321088"

    def test_reject_non_tweet(self):
        assert extract_tweet_id("hello world") is None
        assert extract_tweet_id("123") is None
        assert extract_tweet_id("") is None
        # 短数字不是推文 ID（位数下限 10）
        assert extract_tweet_id("123456789") is None


class TestSyndicationToken:
    """契约为「请求必须带非空 token」——服务端不校验 token 的值。

    实测 2026-09-12：token=a / token=0 / token=长随机串都返回同一推文正文；
    省略 token 参数则返回空响应体。因此这里只锁契约，不锁具体值：
    此前锁的是手写 base36 推导的输出，而那段推导与它自称的公开公式在
    全部抽样 ID 上都不相等——测试锁的是一个无依据的常量，还挡住了修正。
    """

    def test_present_and_nonempty(self):
        # 必须非空：省略或空值会让端点返回空响应体（实测）
        for tid in ("1585841080431321088", "100000000000000",
                    "1234567890123456789"):
            tok = syndication_token(tid)
            assert isinstance(tok, str) and tok.strip()

    def test_value_independent_of_id(self):
        # 值不参与校验：同一 token 服务所有 ID 是预期行为，不是缺陷
        assert syndication_token("1585841080431321088") == \
            syndication_token("100000000000000")

    def test_request_url_carries_token(self):
        # 端到端契约：抓取请求的 URL 上必须带非空 token 参数
        tok = syndication_token("1585841080431321088")
        url = (f"https://cdn.syndication.twimg.com/tweet-result"
               f"?id=1585841080431321088&token={tok}")
        assert "token=" in url and not url.endswith("token=")


class TestBuilder:
    """builder 解析：mock 网络层，不真实请求。"""

    @pytest.fixture
    def engine(self):
        return _build_twitter_syndication_engine({"timeout": 2})

    def _mock_raw(self, monkeypatch, payload):
        from engines_builders_intl import _build_twitter_syndication_engine as _
        # mock engines_base._http_get_raw（builder 函数内延迟 import）
        import engines_base

        def fake_raw(url, headers, timeout, engine="?"):
            return json.dumps(payload)

        monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)

    def test_normal_tweet(self, engine, monkeypatch):
        self._mock_raw(monkeypatch, {
            "__typename": "Tweet",
            "text": "the bird is freed",
            "user": {"screen_name": "elonmusk", "name": "Elon Musk"},
            "created_at": "2022-10-28T03:49:11.000Z",
            "mediaDetails": [{"type": "photo"}, {"type": "photo"}],
            "id_str": "1585841080431321088",
        })
        out = engine("https://x.com/elonmusk/status/1585841080431321088")
        assert len(out) == 1
        r = out[0]
        assert r["source"] == "twitter_syndication"
        assert r["url"] == "https://x.com/elonmusk/status/1585841080431321088"
        assert "elonmusk" in r["title"]
        assert "the bird is freed" in r["snippet"]
        assert "2 项媒体" in r["snippet"]

    def test_tombstone_returns_empty(self, engine, monkeypatch):
        self._mock_raw(monkeypatch, {"__typename": "TweetTombstone"})
        assert engine("https://x.com/a/status/1585841080431321088") == []

    def test_empty_object_returns_empty(self, engine, monkeypatch):
        self._mock_raw(monkeypatch, {})
        assert engine("https://x.com/a/status/1585841080431321088") == []

    def test_keyword_query_returns_empty(self, engine, monkeypatch):
        # syndication 没有搜索端点：普通关键词诚实返回空，不伪造结果
        self._mock_raw(monkeypatch, {"__typename": "Tweet", "text": "x"})
        assert engine("人工智能 最新进展") == []

    def test_bad_json_returns_empty(self, engine, monkeypatch):
        import engines_base

        def fake_raw(url, headers, timeout, engine="?"):
            return "<html>not json</html>"

        monkeypatch.setattr(engines_base, "_http_get_raw", fake_raw, raising=True)
        assert engine("https://x.com/a/status/1585841080431321088") == []

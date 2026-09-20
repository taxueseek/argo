#!/usr/bin/env python3
"""test_itunes_media.py — iTunes 引擎的媒体形态分流（播客为 2026-09-15 新增）。

## 守的是什么

`config.yaml` 与 `engine_registry.yaml` 一直声明 itunes 覆盖 `podcast`，但实现里
`media` 写死成 `music`——播客这条路从来拿不到结果（实测 `--engine itunes`
搜「Lex Fridman Podcast」返回的是他的音乐曲目）。声明了的能力必须真的能用。

本文件锁四件事：
  1. 意图分流：播客词命中 → media=podcast，且 entity 按「节目/单集」分开；
  2. 字段落点：集数/时长/发布日期分别落到 episode_count / duration_minutes /
     published_at，且不新增冻结字段集之外的键；
  3. **陷阱守卫**：节目对象的 trackTimeMillis 不是时长（实测 12 档只有 6 档等于
     最新单集秒数，另 6 档偏小），时长必须来自单集，误用上游字段即被本测试抓住；
  4. 失败形态：二跳取样失败不得连累节目本体（fail-open），无时长的单集不崩。

全部用 mock，不发真实请求。
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import engines_builders_data as bd  # noqa: E402


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, search_payload=None, lookup_payload=None, calls=None,
           lookup_raises=False):
    """按 URL 分流 mock http_open：/lookup 与 /search 走不同载荷。"""
    def handler(req, timeout=None, engine=""):
        url = getattr(req, "full_url", req)
        if calls is not None:
            calls.append(url)
        if "/lookup" in url:
            if lookup_raises:
                raise RuntimeError("lookup 失败")
            payload = lookup_payload if lookup_payload is not None else {"results": []}
        else:
            payload = search_payload if search_payload is not None else {"results": []}
        return _FakeResp(json.dumps(payload).encode())
    monkeypatch.setattr(bd, "http_open", handler, raising=True)


def _engine():
    return bd._build_itunes_engine({"_name": "itunes", "timeout": 2})


def _qs(url: str) -> dict:
    from urllib.parse import parse_qs, urlparse
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# 节目对象：注意 trackTimeMillis=9258 是陷阱值（上游这里不是毫秒时长）
_SHOW = {"results": [{
    "wrapperType": "collection", "kind": "podcast",
    "collectionName": "张小珺Jùn｜商业访谈录",
    "collectionId": 1634356920,
    "collectionViewUrl": "https://podcasts.apple.com/cn/podcast/id1634356920",
    "feedUrl": "https://feed.xyzfm.space/dk4yh3pkpjp3",
    "trackCount": 156, "primaryGenreName": "科技",
    "releaseDate": "2026-09-03T00:00:00Z",
    "trackTimeMillis": 9258,
}]}

# lookup 会先回节目对象再回单集；中位应为 124 分
_LOOKUP_EPS = {"results": [
    {"wrapperType": "collection", "collectionName": "张小珺Jùn｜商业访谈录"},
    {"wrapperType": "podcastEpisode", "trackTimeMillis": 7440000},   # 124 分
    {"wrapperType": "podcastEpisode", "trackTimeMillis": 6000000},   # 100 分
    {"wrapperType": "podcastEpisode", "trackTimeMillis": 9000000},   # 150 分
]}

_EP_SEARCH = {"results": [{
    "wrapperType": "podcastEpisode",
    "trackName": "153. 和曾鸣聊产业史观",
    "collectionName": "张小珺Jùn｜商业访谈录",
    "trackViewUrl": "https://podcasts.apple.com/cn/podcast/153/id1634356920?i=1",
    "episodeUrl": "https://example.com/a.m4a",
    "trackTimeMillis": 9258000,          # 154 分
    "releaseDate": "2026-09-03T00:00:00Z",
}]}

_ALBUM = {"results": [{
    "wrapperType": "collection", "collectionName": "叶惠美",
    "artistName": "周杰伦", "collectionViewUrl": "https://music.apple.com/cn/album/1",
    "releaseDate": "2003-07-31T07:00:00Z",
}]}

_TRACK = {"results": [{
    "wrapperType": "track", "trackName": "稻香", "artistName": "周杰伦",
    "trackViewUrl": "https://music.apple.com/cn/song/1",
    "releaseDate": "2008-10-15T07:00:00Z",
}]}


class TestIntentRouting:
    def test_show_query_goes_to_podcast_entity(self, monkeypatch):
        calls = []
        _patch(monkeypatch, search_payload=_SHOW, lookup_payload=_LOOKUP_EPS, calls=calls)
        out = _engine()("播客 张小珺 商业访谈录", n=3)
        assert out, "播客名查询应有结果"
        q = _qs(calls[0])
        assert q["media"] == "podcast"
        assert q["entity"] == "podcast"
        # 意图词必须从 term 里剥掉，否则「播客」会污染检索词
        assert "播客" not in q["term"]
        # 中文走 cn storefront（pods 的中文库只在 cn 有）
        assert q["country"] == "cn"

    def test_episode_query_goes_to_episode_entity(self, monkeypatch):
        calls = []
        _patch(monkeypatch, search_payload=_EP_SEARCH, calls=calls)
        _engine()("播客 张小珺 曾鸣 单集", n=3)
        q = _qs(calls[0])
        assert q["media"] == "podcast"
        assert q["entity"] == "podcastEpisode"

    def test_music_query_stays_music(self, monkeypatch):
        calls = []
        _patch(monkeypatch, search_payload=_TRACK, calls=calls)
        _engine()("周杰伦 稻香", n=3)
        q = _qs(calls[0])
        assert q["media"] == "music"
        assert "entity" not in q

    def test_album_query_uses_album_entity(self, monkeypatch):
        calls = []
        _patch(monkeypatch, search_payload=_ALBUM, calls=calls)
        _engine()("Taylor Swift album", n=3)
        q = _qs(calls[0])
        assert q["media"] == "music"
        assert q["entity"] == "album"

    def test_podcast_word_wins_over_album_word(self, monkeypatch):
        """「播客 专辑」这类混合词：播客优先，不能落到音乐专辑。"""
        calls = []
        _patch(monkeypatch, search_payload=_SHOW, lookup_payload=_LOOKUP_EPS, calls=calls)
        _engine()("播客 商业访谈录 专辑", n=3)
        q = _qs(calls[0])
        assert q["media"] == "podcast"

    def test_non_chinese_query_has_no_country(self, monkeypatch):
        calls = []
        _patch(monkeypatch, search_payload={"results": [{
            "wrapperType": "collection", "collectionName": "Lex Fridman Podcast",
            "collectionId": 1, "collectionViewUrl": "https://podcasts.apple.com/us/podcast/id1",
            "trackCount": 502, "primaryGenreName": "Technology",
            "releaseDate": "2026-08-26T00:00:00Z",
        }]}, calls=calls)
        _engine()("Lex Fridman Podcast", n=3)
        q = _qs(calls[0])
        assert q["media"] == "podcast"
        assert "country" not in q
        assert "podcast" not in q["term"].lower()


class TestShowFields:
    def test_show_carries_count_date_and_duration(self, monkeypatch):
        _patch(monkeypatch, search_payload=_SHOW, lookup_payload=_LOOKUP_EPS)
        r = _engine()("播客 张小珺 商业访谈录", n=3)[0]
        assert r["episode_count"] == 156          # 集数
        assert r["published_at"] == "2026-09-03T00:00:00Z"   # 发布日期
        assert r["duration_minutes"] == 124       # 时长（来自单集中位）
        assert "156 集" in r["snippet"]
        assert r["source"] == "itunes"

    def test_duration_comes_from_episodes_not_show_field(self, monkeypatch):
        """陷阱守卫：节目对象的 trackTimeMillis=9258 不是时长。

        若实现误读上游该字段，换算结果是 0 分（9258ms）而非 124 分——这条检查即失败。
        """
        _patch(monkeypatch, search_payload=_SHOW, lookup_payload=_LOOKUP_EPS)
        r = _engine()("播客 张小珺 商业访谈录", n=3)[0]
        assert r["duration_minutes"] == 124
        assert r["duration_minutes"] != 0

    def test_show_has_no_new_fields_beyond_contract(self, monkeypatch):
        """只允许既有字段 + 本次显式登记的键（冻结字段集不得被动扩张）。

        transcript_url 为 2026-09-19 登记的官方文字稿直链（Podcasting 2.0）。
        """
        _patch(monkeypatch, search_payload=_SHOW, lookup_payload=_LOOKUP_EPS)
        r = _engine()("播客 张小珺 商业访谈录", n=3)[0]
        allowed = {"title", "url", "snippet", "source", "score",
                   "published_at", "episode_count", "duration_minutes",
                   "transcript_url"}
        assert set(r) <= allowed, f"出现未登记字段: {sorted(set(r) - allowed)}"

    def test_enrichment_failure_is_fail_open(self, monkeypatch):
        """二跳取样失败：节目本体照常返回，只是没有时长。"""
        _patch(monkeypatch, search_payload=_SHOW, lookup_raises=True)
        out = _engine()("播客 张小珺 商业访谈录", n=3)
        assert len(out) == 1
        assert out[0]["episode_count"] == 156
        assert "duration_minutes" not in out[0]

    def test_enrichment_runs_only_once(self, monkeypatch):
        """取样有上限：节目结果再多也只补首条，请求数不随结果数膨胀。

        collectionId 从 1 起（真实形态）：0 会被 _recent_episode_duration
        当空值早退，让「只补首条」退化成「一条都没补」。
        """
        many = {"results": [
            dict(_SHOW["results"][0], collectionName=f"访谈节目{i}", collectionId=i + 1)
            for i in range(4)
        ]}
        calls = []
        _patch(monkeypatch, search_payload=many, lookup_payload=_LOOKUP_EPS, calls=calls)
        out = _engine()("播客 访谈", n=5)
        lookups = [u for u in calls if "/lookup" in u]
        assert len(lookups) == bd._ITUNES_SHOW_ENRICH_MAX
        assert sum(1 for r in out if "duration_minutes" in r) == 1


# ── 官方文字稿增强（Podcasting 2.0 <podcast:transcript>，2026-09-19 新增）────

_FEED_XML_VTT = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel>
<item><title>第 1 期</title>
<podcast:transcript url="https://feed.example/ep1.vtt" type="text/vtt"/>
</item>
<item><title>第 2 期</title>
<podcast:transcript url="https://feed.example/ep2.pdf" type="application/pdf"/>
</item>
</channel></rss>"""

_SAMPLE_VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v 主播>欢迎收听本期<c.highlight>节目</c>

00:00:04.000 --> 00:00:08.000
今天聊播客行业的文字稿标准
"""


class TestShowTranscript:
    """mock 节目的 feedUrl 指向 feed.example（_SHOW 的 xyzfm 真实域名不进测试）。"""

    _FEED_URL = "https://feed.example/show.xml"

    def _show_payload(self, **overrides):
        base = dict(_SHOW["results"][0])
        base["feedUrl"] = overrides.pop("feedUrl", self._FEED_URL)  # 覆盖真实域名
        base.update(overrides)
        return {"results": [base]}

    def _patch_with_feed(self, monkeypatch, search_payload=None,
                         feed_body: bytes | None = None,
                         transcript_body: bytes | None = None,
                         feed_raises: bool = False, calls=None):
        feed = feed_body if feed_body is not None else _FEED_XML_VTT.encode()
        vtt = transcript_body if transcript_body is not None else _SAMPLE_VTT.encode()
        shows = search_payload if search_payload is not None else self._show_payload()

        def handler(req, timeout=None, engine=""):
            url = getattr(req, "full_url", req)
            if calls is not None:
                calls.append(url)
            if "/lookup" in url:
                return _FakeResp(json.dumps(_LOOKUP_EPS).encode())
            if url.endswith(".vtt"):
                return _FakeResp(vtt)
            if url.startswith("https://feed.example"):
                if feed_raises:
                    raise RuntimeError("feed 拉取失败")
                return _FakeResp(feed)
            return _FakeResp(json.dumps(shows).encode())
        monkeypatch.setattr(bd, "http_open", handler, raising=True)

    def test_transcript_enriches_first_show(self, monkeypatch):
        """feed 带 transcript 标签：首条节目拿到 transcript_url + 全文预览。"""
        self._patch_with_feed(monkeypatch)
        r = _engine()("播客 张小珺 商业访谈录", n=3)[0]
        assert r["transcript_url"] == "https://feed.example/ep1.vtt"
        assert "欢迎收听本期节目" in r["snippet"]
        assert "今天聊播客行业的文字稿标准" in r["snippet"]
        assert "-->" not in r["snippet"]

    def test_transcript_fail_open(self, monkeypatch):
        """feed 拉取失败：节目本体照常，只是没有 transcript_url。"""
        self._patch_with_feed(monkeypatch, feed_raises=True)
        out = _engine()("播客 张小珺 商业访谈录", n=3)
        assert len(out) == 1
        assert out[0]["episode_count"] == 156
        assert "transcript_url" not in out[0]

    def test_no_transcript_tag_is_noop(self, monkeypatch):
        """feed 没挂 transcript 标签：条目照常返回，不产生额外字段。"""
        self._patch_with_feed(
            monkeypatch, feed_body=b"<rss><channel><item><title>x</title></item></channel></rss>")
        r = _engine()("播客 张小珺 商业访谈录", n=3)[0]
        assert "transcript_url" not in r

    def test_unsupported_type_falls_to_next_candidate(self, monkeypatch):
        """最新一期只有 PDF 时跳过，取下一期可解析格式（VTT）。"""
        xml = _FEED_XML_VTT.replace(
            '<podcast:transcript url="https://feed.example/ep1.vtt" type="text/vtt"/>',
            '<podcast:transcript url="https://feed.example/ep1.pdf" type="application/pdf"/>',
        ).replace(
            '<podcast:transcript url="https://feed.example/ep2.pdf" type="application/pdf"/>',
            '<podcast:transcript url="https://feed.example/ep2.vtt" type="text/vtt"/>',
        )
        self._patch_with_feed(monkeypatch, feed_body=xml.encode())
        r = _engine()("播客 张小珺 商业访谈录", n=3)[0]
        assert r["transcript_url"] == "https://feed.example/ep2.vtt"

    def test_transcript_url_not_duplicated_by_enrich_quota(self, monkeypatch):
        """配额只给首条：第二条节目不产生 transcript_url，请求数不膨胀。

        collectionId 从 1 起（真实形态），0 会被 _recent_episode_duration
        当空值早退，污染「配额=1」的断言。
        """
        many = {"results": [
            dict(_SHOW["results"][0], collectionName=f"访谈节目{i}",
                 collectionId=i + 1, feedUrl=self._FEED_URL)
            for i in range(3)
        ]}
        calls = []
        self._patch_with_feed(monkeypatch, search_payload=many, calls=calls)
        out = _engine()("播客 访谈", n=5)
        assert sum(1 for r in out if "transcript_url" in r) == 1
        # 恰一次 feed 拉取 + 一次文字稿下载，请求数不随结果数膨胀
        assert sum(1 for u in calls if u.endswith("show.xml")) == 1
        assert sum(1 for u in calls if u.endswith(".vtt")) == 1


class TestEpisodeFields:
    def test_episode_carries_duration_and_date(self, monkeypatch):
        _patch(monkeypatch, search_payload=_EP_SEARCH)
        r = _engine()("播客 张小珺 曾鸣 单集", n=3)[0]
        assert r["duration_minutes"] == 154
        assert r["published_at"] == "2026-09-03T00:00:00Z"
        assert "时长 154 分" in r["snippet"]
        assert "《张小珺Jùn｜商业访谈录》" in r["snippet"]
        # 单集不给出节目级的集数字段
        assert "episode_count" not in r

    def test_episode_without_duration_does_not_crash(self, monkeypatch):
        payload = {"results": [dict(_EP_SEARCH["results"][0], trackTimeMillis=None)]}
        _patch(monkeypatch, search_payload=payload)
        r = _engine()("播客 张小珺 曾鸣 单集", n=3)[0]
        assert "duration_minutes" not in r
        assert "时长" not in r["snippet"]
        assert r["published_at"] == "2026-09-03T00:00:00Z"

    def test_episode_url_prefers_apple_page(self, monkeypatch):
        _patch(monkeypatch, search_payload=_EP_SEARCH)
        r = _engine()("播客 张小珺 曾鸣 单集", n=3)[0]
        assert r["url"].startswith("https://podcasts.apple.com/")


class TestMusicRegression:
    def test_track_has_no_podcast_fields(self, monkeypatch):
        _patch(monkeypatch, search_payload=_TRACK)
        r = _engine()("周杰伦 稻香", n=3)[0]
        assert r["title"] == "稻香"
        assert r["snippet"] == "周杰伦"
        for k in ("episode_count", "duration_minutes", "published_at"):
            assert k not in r, f"音乐结果不该带 {k}"

    def test_album_title_prefers_collection_name(self, monkeypatch):
        _patch(monkeypatch, search_payload=_ALBUM)
        r = _engine()("周杰伦 专辑", n=3)[0]
        assert r["title"] == "叶惠美"
        assert r["snippet"] == "周杰伦"


class TestGeneralContract:
    def test_scores_descend(self, monkeypatch):
        payload = {"results": [
            {"wrapperType": "collection", "collectionName": f"测试播客{i}",
             "collectionId": i,
             "collectionViewUrl": f"https://podcasts.apple.com/cn/podcast/id{i}",
             "trackCount": 10, "releaseDate": "2026-01-01T00:00:00Z"}
            for i in range(4)
        ]}
        _patch(monkeypatch, search_payload=payload)
        out = _engine()("播客 测试", n=4)
        scores = [r["score"] for r in out]
        assert len(scores) >= 3
        assert all(scores[i] > scores[i + 1] for i in range(len(scores) - 1)), scores

    def test_empty_query_returns_empty(self, monkeypatch):
        _patch(monkeypatch, search_payload=_SHOW)
        assert _engine()("   ", n=3) == []

    def test_http_failure_returns_empty(self, monkeypatch):
        def boom(req, timeout=None, engine=""):
            raise RuntimeError("网络挂了")
        monkeypatch.setattr(bd, "http_open", boom, raising=True)
        assert _engine()("播客 测试", n=3) == []


class TestTranscriptHardening:
    """对抗审查补充：安全与边界（2026-09-19）。"""

    def test_mime_with_charset_param(self, monkeypatch):
        """type="text/vtt; charset=utf-8" 带参数不被拒。"""
        xml = _FEED_XML_VTT.replace('type="text/vtt"', 'type="text/vtt; charset=utf-8"')
        TestShowTranscript()._patch_with_feed(monkeypatch, feed_body=xml.encode())
        r = _engine()("播客 张小珺 商业访谈录", n=3)[0]
        assert r["transcript_url"] == "https://feed.example/ep1.vtt"

    def test_single_quoted_attrs(self, monkeypatch):
        """单引号属性的 transcript 标签能取到 URL。"""
        xml = _FEED_XML_VTT.replace(
            '<podcast:transcript url="https://feed.example/ep1.vtt" type="text/vtt"/>',
            "<podcast:transcript url='https://feed.example/ep1.vtt' type='text/vtt'/>")
        TestShowTranscript()._patch_with_feed(monkeypatch, feed_body=xml.encode())
        r = _engine()("播客 张小珺 商业访谈录", n=3)[0]
        assert r["transcript_url"] == "https://feed.example/ep1.vtt"

    def test_malformed_url_does_not_crash(self, monkeypatch):
        """畸形 URL（urljoin 会抛 ValueError）跳过该候选，不崩引擎。"""
        xml = _FEED_XML_VTT.replace(
            '<podcast:transcript url="https://feed.example/ep1.vtt" type="text/vtt"/>',
            '<podcast:transcript url="http://[" type="text/vtt"/>')
        TestShowTranscript()._patch_with_feed(monkeypatch, feed_body=xml.encode())
        out = _engine()("播客 张小珺 商业访谈录", n=3)
        assert len(out) == 1
        assert "transcript_url" not in out[0]

    def test_file_scheme_rejected(self, monkeypatch):
        """file:// 与内网 http 的文字稿 URL 被拒（SSRF 防护）。"""
        xml = _FEED_XML_VTT.replace(
            '<podcast:transcript url="https://feed.example/ep1.vtt" type="text/vtt"/>',
            '<podcast:transcript url="file:///Users/x/.ssh/id_rsa" type="text/plain"/>')
        TestShowTranscript()._patch_with_feed(monkeypatch, feed_body=xml.encode())
        r = _engine()("播客 张小珺 商业访谈录", n=3)[0]
        assert "transcript_url" not in r

    def test_first_candidate_download_fails_falls_to_next(self, monkeypatch):
        """首份候选下载失败不放弃整条节目，转下一份可解析候选。"""
        xml = _FEED_XML_VTT.replace("ep2.pdf", "ep2.vtt").replace(
            '<podcast:transcript url="https://feed.example/ep2.vtt" type="application/pdf"/>',
            '<podcast:transcript url="https://feed.example/ep2.vtt" type="text/vtt"/>')
        calls = []
        base = TestShowTranscript()._patch_with_feed

        def handler(req, timeout=None, engine=""):
            url = getattr(req, "full_url", req)
            calls.append(url)
            if "/lookup" in url:
                return _FakeResp(json.dumps(_LOOKUP_EPS).encode())
            if url.endswith("ep1.vtt"):
                raise RuntimeError("首份下载失败")
            if url.endswith(".vtt"):
                return _FakeResp(_SAMPLE_VTT.encode())
            if url.startswith("https://feed.example"):
                return _FakeResp(xml.encode())
            return _FakeResp(json.dumps(
                TestShowTranscript()._show_payload()).encode())
        monkeypatch.setattr(bd, "http_open", handler, raising=True)
        r = _engine()("播客 张小珺 商业访谈录", n=3)[0]
        assert r["transcript_url"] == "https://feed.example/ep2.vtt"

    def test_transcript_to_text_srt_and_html(self):
        """SRT 序号行/时间轴清除；HTML 去 tag 与实体。"""
        srt = "1\n00:00:01,000 --> 00:00:04,000\n第一句台词\n\n2\n00:00:05,000 --> 00:00:08,000\n第二句台词\n"
        out = bd._transcript_to_text(srt, "text/srt")
        assert "第一句台词" in out and "-->" not in out and out.index("第一句") < out.index("第二句")
        html = "<html><body>R&amp;D 测试<br/>正文&nbsp;行</body></html>"
        out = bd._transcript_to_text(html, "text/html")
        assert "&amp;" not in out and "nbsp" not in out and "正文" in out



if __name__ == "__main__":
    pytest.main([__file__, "-v"])

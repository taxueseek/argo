#!/usr/bin/env python3
"""tests/test_local_search_dedup.py — 结果级去重与 url 模板兜底（2026-10-30）。

守的行为（2026-10-30 实测 local_hackernews 暴露的两个缺陷）：
  1. url 字段配置存在但取值为空时，回落 url_template——此前 if/elif 互斥，
     模板兜底是死代码，HN comment/Ask HN hit 的链接全部丢失
  2. 截断前去重：URL 相同合并；同引擎 title+snippet 近同归并（同一 story
     被上游各收一份，objectID 不同 URL 必不同，按 URL 合并不了）。跨引擎
     同标题是共识信号，不在此归并
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "sub-skills" / "local-search", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import parsers  # noqa: E402
import search_v3  # noqa: E402

_HN_MAPS = {"json": {"hnfake": {
    "items": "hits", "title": "title", "url": "url",
    "snippet": "story_text",
    "url_template": "https://news.ycombinator.com/item?id={objectID}",
}}}


def _parse_hn(hits: list[dict]) -> list[dict]:
    return parsers._parse_json("hnfake", json.dumps({"hits": hits}), _HN_MAPS)


def test_url_null_falls_back_to_template():
    """url=null 的 hit（Ask HN/comment）必须回落模板拼出 HN 链接。"""
    out = _parse_hn([
        {"objectID": "111", "title": "t1", "url": None, "story_text": "s1"},
        {"objectID": "222", "title": "t2", "url": "https://example.com/real",
         "story_text": "s2"},
    ])
    assert out[0]["url"] == "https://news.ycombinator.com/item?id=111"
    assert out[1]["url"] == "https://example.com/real"  # 有值不被模板覆盖


def test_url_missing_key_falls_back_to_template():
    """url 键整体缺失（非 null）同样回落模板。"""
    out = _parse_hn([{"objectID": "333", "title": "t3", "story_text": "s3"}])
    assert out[0]["url"] == "https://news.ycombinator.com/item?id=333"


def test_dedup_same_url_keeps_higher_score():
    """URL 相同（含尾斜杠差异）合并，保留分高者。"""
    rs = [
        {"title": "a", "url": "https://x/1", "snippet": "s", "score": 0.5,
         "_engine": "e1"},
        {"title": "a", "url": "https://x/1/", "snippet": "s", "score": 0.9,
         "_engine": "e1"},
    ]
    out = search_v3._dedup_near_duplicates(rs)
    assert len(out) == 1 and out[0]["score"] == 0.9


def test_dedup_same_engine_same_text_keeps_linked():
    """同引擎同文异链（Algolia 双收录）：保留有链接的那条。"""
    rs = [
        {"title": "Ask HN: BM25", "url": "", "snippet": "same text",
         "score": 0.9, "_engine": "local_hackernews"},
        {"title": "Ask HN: BM25",
         "url": "https://news.ycombinator.com/item?id=1",
         "snippet": "same text", "score": 0.8, "_engine": "local_hackernews"},
    ]
    out = search_v3._dedup_near_duplicates(rs)
    assert len(out) == 1
    assert out[0]["url"] == "https://news.ycombinator.com/item?id=1"


def test_dedup_keeps_same_title_different_snippet():
    """同标题不同正文是两篇独立内容（Ask HN 系列帖），不归并。"""
    rs = [
        {"title": "Ask HN: x", "url": "https://n/1", "snippet": "text one",
         "score": 0.9, "_engine": "local_hackernews"},
        {"title": "Ask HN: x", "url": "https://n/2", "snippet": "text two",
         "score": 0.8, "_engine": "local_hackernews"},
    ]
    assert len(search_v3._dedup_near_duplicates(rs)) == 2


def test_dedup_keeps_cross_engine_same_text():
    """跨引擎同文是共识信号，不在此归并（留给 rrf_merge consensus 标注）。"""
    rs = [
        {"title": "t", "url": "https://a/1", "snippet": "s", "score": 0.9,
         "_engine": "e1"},
        {"title": "t", "url": "https://b/1", "snippet": "s", "score": 0.8,
         "_engine": "e2"},
    ]
    assert len(search_v3._dedup_near_duplicates(rs)) == 2


def test_end_to_end_dedup_before_truncation(monkeypatch):
    """端到端：去重先于截断，重复条目不得挤占结果配额。"""
    dup = {"title": "dup", "url": "https://n/item?id=1", "snippet": "body",
           "score": 0.95}

    def fake(name, query, n=5, timeout=None, since=None, until=None):
        return [dict(dup), dict(dup),
                {"title": "fresh1", "url": "https://n/1", "snippet": "u1",
                 "score": 0.5},
                {"title": "fresh2", "url": "https://n/2", "snippet": "u2",
                 "score": 0.5}], None

    monkeypatch.setattr(search_v3, "_search_one", fake)
    out = search_v3.search_engines(
        "测试", engines=["hnfake"], n=2, skip_cache=True, mode="auto")
    urls = [r["url"] for r in out["results"]]
    assert urls.count("https://n/item?id=1") == 1
    assert any(r["title"].startswith("fresh") for r in out["results"])

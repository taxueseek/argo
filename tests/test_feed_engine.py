#!/usr/bin/env python3
"""test_feed_engine.py — 多份 RSS 种子注册表的离线契约。

锁六件事，全部打桩 http_open，不碰网络：
  1. 多份种子按查询合并，标题只给声明了 label 的那份加「出处 · 」
  2. 一份下载失败，其余照常返回
  3. 纯中文查询不下载英文种子，也不返回英文条目
  4. 纯拉丁查询不下载只声明 zh 的种子
  5. 单份热榜引擎忽略查询、不加出处前缀、超时不切片
  6. 带 BOM 的 XML 能解析；缓存里的标题不被出处前缀改写
"""

import os
import sys
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import engines_builders_feeds as ebf  # noqa: E402


def _rss(title: str, link: str, description: str = "") -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<rss version=\"2.0\"><channel><item>"
        f"<title>{title}</title><link>{link}</link>"
        f"<description>{description}</description>"
        "</item></channel></rss>"
    )
    return xml.encode("utf-8")


_FED = "https://example.test/fed"
_SOL = "https://example.test/solidot"
_DOWN = "https://example.test/down"
_HOT = "https://example.test/guardian"
_BARE = "https://example.test/bare"

_BODIES = {
    _FED: _rss("Federal Reserve holds rates", f"{_FED}/1", "FOMC statement"),
    _SOL: _rss("量子计算新进展", f"{_SOL}/1", "奇客"),
    _HOT: _rss("World desk overnight", f"{_HOT}/1", "a dispatch"),
    _BARE: _rss("Federal Reserve calendar", f"{_BARE}/1", "press"),
}


class _Body:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _install(monkeypatch, bodies=None, fail=()):
    """替换 http_open。返回 (请求过的 url, 每次的 timeout)。"""
    ebf._feed_cache.clear()
    seen_urls: list[str] = []
    seen_timeouts: list[float] = []
    table = dict(_BODIES)
    if bodies:
        table.update(bodies)
    failed = set(fail)

    def fake(req, timeout=10.0, engine=""):
        url = req.full_url
        seen_urls.append(url)
        seen_timeouts.append(timeout)
        if url in failed:
            raise urllib.error.URLError("down")
        if url not in table:
            raise urllib.error.URLError(f"unmapped {url}")
        return _Body(table[url])

    monkeypatch.setattr(ebf, "http_open", fake)
    return seen_urls, seen_timeouts


def _multi_spec():
    return {
        "_name": "feed",
        "engine_id": "feed",
        "feed_mode": "filter",
        "timeout": 12,
        "langs": ["en", "zh"],
        "feeds": [
            {"url": _FED, "label": "美联储", "langs": ["en"]},
            {"url": _SOL, "label": "Solidot", "langs": ["zh"]},
            {"url": _DOWN, "label": "挂了", "langs": ["en"]},
            {"url": _BARE, "label": "", "langs": ["en"]},
        ],
    }


def test_filter_merges_labeled_and_unlabeled(monkeypatch):
    seen, timeouts = _install(monkeypatch, fail=(_DOWN,))
    engine = ebf._build_rss_feed_engine(_multi_spec())
    rows = engine("Federal Reserve", n=10)
    titles = [r["title"] for r in rows]
    assert titles == [
        "美联储 · Federal Reserve holds rates",
        "Federal Reserve calendar",
    ]
    assert all(r["source"] == "feed" for r in rows)
    assert _SOL not in seen
    assert _DOWN in seen
    assert all(t <= 8.0 for t in timeouts)


def test_one_feed_down_keeps_the_rest(monkeypatch):
    _install(monkeypatch, fail=(_DOWN,))
    engine = ebf._build_rss_feed_engine(_multi_spec())
    rows = engine("Federal", n=10)
    assert len(rows) == 2
    assert rows[0]["url"] == f"{_FED}/1"


def test_pure_cjk_skips_english_feeds(monkeypatch):
    seen, _ = _install(monkeypatch, fail=(_DOWN,))
    engine = ebf._build_rss_feed_engine(_multi_spec())
    rows = engine("量子计算", n=10)
    assert [r["title"] for r in rows] == ["Solidot · 量子计算新进展"]
    assert seen == [_SOL]


def test_pure_latin_skips_zh_only_feed(monkeypatch):
    seen, _ = _install(monkeypatch, fail=(_DOWN,))
    engine = ebf._build_rss_feed_engine(_multi_spec())
    engine("Federal Reserve", n=10)
    assert _SOL not in seen
    assert _FED in seen


def test_hot_single_feed_ignores_query_and_keeps_timeout(monkeypatch):
    seen, timeouts = _install(monkeypatch)
    engine = ebf._build_rss_feed_engine({
        "_name": "guardian_rss",
        "feed_mode": "hot",
        "timeout": 12,
        "feed_url": _HOT,
        "langs": ["en"],
    })
    rows = engine("量子计算完全无关", n=5)
    assert len(rows) == 1
    assert rows[0]["title"] == "World desk overnight"
    assert rows[0]["source"] == "guardian_rss"
    assert seen == [_HOT]
    assert timeouts == [12.0]


def test_single_feed_filter_has_no_label_and_skips_cjk_fetch(monkeypatch):
    seen, _ = _install(monkeypatch)
    engine = ebf._build_rss_feed_engine({
        "_name": "factcheck_org",
        "feed_mode": "filter",
        "feed_url": _FED,
        "langs": ["en"],
    })
    hit = engine("Federal", n=5)
    assert hit[0]["title"] == "Federal Reserve holds rates"
    miss = engine("量子计算", n=5)
    assert miss == []
    assert seen == [_FED]


def test_bom_parses_and_cache_is_not_rewritten(monkeypatch):
    bom = b"\xef\xbb\xbf" + _rss(
        "Federal Reserve calendar", f"{_FED}/bom", "press")
    seen, _ = _install(monkeypatch, bodies={_FED: bom})
    spec = {
        "_name": "feed",
        "feed_mode": "filter",
        "langs": ["en", "zh"],
        "feeds": [{"url": _FED, "label": "美联储", "langs": ["en"]}],
    }
    engine = ebf._build_rss_feed_engine(spec)
    first = engine("Federal", n=5)
    second = engine("Federal", n=5)
    assert first[0]["title"] == "美联储 · Federal Reserve calendar"
    assert second[0]["title"] == "美联储 · Federal Reserve calendar"
    assert seen == [_FED]
    cached = ebf._feed_cache[_FED][1][0]["title"]
    assert cached == "Federal Reserve calendar"


def test_multi_feed_shares_call_budget(monkeypatch):
    """调用方传入的超时是整次预算，多份种子要分着用。"""
    _, timeouts = _install(monkeypatch, fail=(_DOWN,))
    engine = ebf._build_rss_feed_engine(_multi_spec())
    engine("Federal Reserve", n=5, _timeout=6)
    # 三份英文种子分 6 秒：第一份拿到 6/3。后面的份额随剩余时间重分，不超过总预算。
    assert timeouts
    assert abs(timeouts[0] - 2.0) < 0.05
    assert all(t <= 6.01 for t in timeouts)


def test_stopwords_only_returns_empty(monkeypatch):
    _install(monkeypatch)
    engine = ebf._build_rss_feed_engine({
        "_name": "factcheck_org",
        "feed_mode": "filter",
        "feed_url": _FED,
        "langs": ["en"],
    })
    assert engine("the latest news", n=5) == []

#!/usr/bin/env python3
"""P0 回归门：国际新闻 RSS 不得归入 hot_trending 族。

## 守的是什么缺陷

2026-09-19 报告第 5 节点名：dw_news / france24 / guardian_rss 是
`type: rss_feed` 的国际新闻实时流，却归 `family: hot_trending`（热榜）。
「热榜」回答「现在什么话题在涨」，新闻 RSS 回答「世界上发生了什么」，
是两类问题。后果在 `dedupe_by_family(max_per_family=2)`：查国际新闻时
这三家与百度热搜、微博热搜、抖音热榜争同一个族的名额，热榜会把新闻挤掉。

2026-11-11 复核：三份 spec 的 family 仍是 hot_trending，未修。
Argo 已有 `world_news` 族承载其余 14 家国际新闻 RSS，语义与落点都对齐。

## 判据

1. 所有 `type: rss_feed` 且 `feed_mode: hot` 的国际新闻流必须归
   `world_news`（或 news_flash），不得归 hot_trending；
2. hot_trending 族里不得再出现 rss_feed 类型的引擎；
3. 三家改完后 world_news 族必须仍包含它们——防止「为了过测试直接删族声明」。

只锁 rss_feed 类型：douyin_hot / weibo_hot / x_trending 是真正的热榜源，
归 hot_trending 正确，不在本门管辖范围。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

NEWS_FEEDS = ("dw_news", "france24", "guardian_rss")


def _specs() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted((ROOT / "engines" / "specs").glob("*.yaml")):
        try:
            spec = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if isinstance(spec, dict) and spec.get("engine_id"):
            out[spec["engine_id"]] = spec
    return out


@pytest.fixture(scope="module")
def specs() -> dict[str, dict]:
    return _specs()


@pytest.mark.parametrize("engine", NEWS_FEEDS)
def test_news_feed_not_in_hot_trending(specs, engine):
    """三家国际新闻 RSS 不得与热榜争族名额。"""
    spec = specs.get(engine)
    assert spec is not None, f"{engine} 的 spec 不存在"
    assert spec["type"] == "rss_feed", f"{engine} 不是 rss_feed，测试前提变了"
    assert spec.get("family") != "hot_trending", (
        f"{engine} 是国际新闻实时流，归 hot_trending 会与百度/微博/抖音热榜"
        f"争 dedupe_by_family(max_per_family=2) 的名额"
    )


@pytest.mark.parametrize("engine", NEWS_FEEDS)
def test_news_feed_in_world_news(specs, engine):
    """必须落到 world_news——不能靠删掉 family 声明蒙混过关。"""
    spec = specs.get(engine)
    assert spec is not None
    assert spec.get("family") == "world_news", (
        f"{engine} 应归 world_news（与其余 14 家国际新闻 RSS 同族），"
        f"实际是 {spec.get('family')!r}"
    )


def test_no_rss_feed_left_in_hot_trending(specs):
    """hot_trending 族里不应有任何 rss_feed 引擎（真热榜源不受影响）。"""
    offenders = [
        name for name, spec in specs.items()
        if spec.get("type") == "rss_feed" and spec.get("family") == "hot_trending"
    ]
    assert not offenders, f"hot_trending 族里仍有 RSS 新闻流：{sorted(offenders)}"


def test_real_hot_sources_keep_their_family(specs):
    """反向自校验：真热榜源仍归 hot_trending，防止把整个族搬空。"""
    for name in ("douyin_hot", "weibo_hot"):
        spec = specs.get(name)
        if spec is None:
            continue
        assert spec.get("family") == "hot_trending", (
            f"{name} 是真实热榜源，修族时不应被一并改动"
        )

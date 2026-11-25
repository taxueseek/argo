#!/usr/bin/env python3
"""tests/test_zhihu_hot.py — 知乎热榜（原 skill hot_list 迁入）"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_zhihu_hot_in_config():
    from config import load_config, get_engines

    engines = get_engines(load_config())
    assert "zhihu_hot" in engines
    spec = engines["zhihu_hot"]
    assert spec.get("enabled") is True
    assert "hot_list" in (spec.get("url") or "")
    assert spec.get("query_param") == ""
    assert str(spec.get("extra_params", {}).get("Limit", "")).find("n") >= 0
    assert int(spec.get("limit") or 0) == 100
    assert (spec.get("period") or "") == "day"


def test_route_zhihu_hot_list():
    from route import route_query
    from unittest.mock import patch

    # 路由契约测试不依赖真实密钥：注入 fake 使其通过 env_ready 检查
    with patch.dict(
        os.environ, {"ARGO_ZHIHU_ACCESS_SECRET": "test-key-for-routing"},
        clear=False,
    ):
        r = route_query("知乎热榜", depth="fast", mode="fast")
    assert r.get("domain") == "zhihu_hot_list", r
    combo = r.get("engines_combo") or []
    assert "zhihu_hot" in combo
    assert combo[0] == "zhihu_hot"


def test_route_generic_hot_does_not_force_zhihu_first():
    """泛热搜仍以免费榜为主，避免日 100 次配额被刷光。"""
    from route import route_query

    r = route_query("今天热搜", depth="fast", mode="fast")
    assert r.get("domain") == "hot_trending", r
    combo = r.get("engines_combo") or []
    # fast 预算 2，zhihu_hot 排在末位，通常不进 combo
    assert combo[0] != "zhihu_hot"


def test_env_alias_shared_with_zhihu():
    from engine_env import required_env_for, KNOWN_ENV_ALIASES

    assert "zhihu_hot" in KNOWN_ENV_ALIASES
    req = required_env_for("zhihu_hot")
    assert any("ZHIHU" in x for x in req)


def test_live_zhihu_hot_if_key():
    """有密钥时打一次 hot_list，确认字段可解析。"""
    if not (os.environ.get("ZHIHU_ACCESS_SECRET") or os.environ.get("ARGO_ZHIHU_ACCESS_SECRET")):
        return
    from search import super_search

    out = super_search("知乎热榜", engine="zhihu_hot", n=5, skip_cache=True, mode="auto")
    results = out.get("results") or []
    assert len(results) >= 1, out
    assert results[0].get("title")
    assert "zhihu.com" in (results[0].get("url") or "") or results[0].get("url")


def test_hot_trending_disables_early_stop_and_covers_major_platforms():
    """全网热榜域必须禁早停，且默认 balanced 预算覆盖主流多平台热榜。"""
    from route import route_query

    r = route_query("今日热榜", depth="balanced", mode="auto")
    assert r.get("domain") == "hot_trending", r
    assert r.get("no_early_stop") is True, f"hot_trending 必须 no_early_stop=True，实得: {r}"
    combo = set(r.get("engines_combo") or r.get("engines") or [])
    major = {"toutiao_hot", "weibo_hot", "zhihu_hot_app", "douyin_hot", "bilibili_hot"}
    assert len(combo & major) >= 4, f"balanced 组合应覆盖至少 4 个主流热榜平台，实得: {combo}"


def test_baidu_hot_url_not_dropped_as_serp_shell():
    """baidu_hot 输出的链接不得被 serp_guard 误判为 SERP 壳链清空，且保留摘要。"""
    import io
    from unittest.mock import patch
    from engines_builders_cn import _build_baidu_hot_engine
    from serp_guard import drop_shell_urls

    fake_html = (
        '{"appUrl":"https://www.baidu.com/s?wd=%E6%B5%8B%E8%AF%95",'
        '"desc":"这是一条百度热搜的真实事件摘要说明",'
        '"hotScore":"7809316","word":"国内月底会暖到离谱"},'
        '{"desc":"第二条热搜详情","hotScore":"6500000","word":"神舟二十四号发射成功"}'
    ).encode("utf-8")

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    engine = _build_baidu_hot_engine({"timeout": 5, "_name": "baidu_hot"})
    with patch("engines_builders_cn.http_open", return_value=_FakeResp(fake_html)):
        items = engine("今日热榜", 5)

    assert len(items) == 2, items
    kept = drop_shell_urls(items)
    assert len(kept) == 2, f"baidu_hot 条目被 serp_guard 误杀: {items}"
    assert "真实事件摘要" in items[0]["snippet"], items[0]


def test_hot_trending_skips_echo_recovery():
    """榜单型结果按设计忽略查询词，偶发命中单字不得触发 echo recovery。"""
    from postprocess_signals import echo_recovery_needed

    # 3 条真实热搜，其中一条恰好包含「今日热榜」中的单字「日」
    hot_items = [
        {"title": "神舟二十四号发射成功", "source": "bilibili_hot", "url": "https://search.bilibili.com/all?keyword=a"},
        {"title": "日本央行宣布维持利率不变", "source": "bilibili_hot", "url": "https://search.bilibili.com/all?keyword=b"},
        {"title": "T1官宣Zeus回归", "source": "douyin_hot", "url": "https://www.douyin.com/search/c"},
    ]
    assert echo_recovery_needed(hot_items, "今日热榜", domain="hot_trending") is False


def test_hot_trending_interleaves_all_platforms():
    """多平台热榜聚合须按平台轮询交错，避免单平台霸屏挤掉其他平台。"""
    from postprocess_signals import interleave_by_source

    items = (
        [{"title": f"抖音热搜{i}", "source": "douyin_hot", "url": f"https://douyin.com/{i}"} for i in range(10)]
        + [{"title": f"微博热搜{i}", "source": "weibo_hot", "url": f"https://weibo.com/{i}"} for i in range(10)]
        + [{"title": f"头条热搜{i}", "source": "toutiao_hot", "url": f"https://toutiao.com/{i}"} for i in range(10)]
        + [{"title": f"B站热搜{i}", "source": "bilibili_hot", "url": f"https://bilibili.com/{i}"} for i in range(10)]
        + [{"title": f"百度热搜{i}", "source": "baidu_hot", "url": f"https://top.baidu.com/{i}"} for i in range(10)]
    )
    out = interleave_by_source(items, max_results=14)
    assert len(out) == 14
    sources = {r["source"] for r in out}
    assert sources == {"douyin_hot", "weibo_hot", "toutiao_hot", "bilibili_hot", "baidu_hot"}



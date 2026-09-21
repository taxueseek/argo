#!/usr/bin/env python3
"""tests/test_route_cache.py — 跨进程路由决策缓存（2026-09-17）

背景：`route_query` 的**首次调用**要付约 103 ms 的进程级初始化（238 条域正则
编译 33 ms、惰性导入、引擎环境/准入与 TF-IDF 装载），而同进程内后续调用只要
3.7 ms。CLI 每次调用都是新进程，于是这笔启动税每次重付。实测跳过一次
route_query 后，缓存命中的一次完整搜索只要 26 ms。

本文件是**缓存自身行为**的门禁。存量测试默认关闭该缓存（见 conftest 的说明：
会话状态目录整轮共享，开着会让用例互相串味），所以这里每个用例都显式打开开关；
关掉时走的是与引入缓存前逐位一致的旧路径，那条路径由存量检查覆盖。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import route  # noqa: E402


def _stub_decision(**overrides):
    d = {
        "engine": "octen",
        "engines": ["octen", "anysearch"],
        "engines_combo": ["octen", "anysearch"],
        "reason": "stub route",
        "confidence": 0.9,
        "domain": "english_tech",
        "parallel": True,
        "tfidf_scores": [],
        "engines_fallback": ["octen", "anysearch", "exa"],
        "mode": "auto",
        "depth": "fast",
    }
    d.update(overrides)
    return d


@pytest.fixture
def cache_on(monkeypatch, tmp_path):
    """打开缓存开关 + 把状态目录换到 tmp，并清掉可能存在的旧条目。"""
    monkeypatch.setenv("ARGO_ROUTE_CACHE", "1")
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
    route.invalidate_route_cache()
    yield tmp_path
    route.invalidate_route_cache()


@pytest.fixture
def counted_route(monkeypatch):
    """用桩替换 route_query，并数它被真正调用了几次。"""
    calls: list[str] = []

    def fake(query, engine_override="auto", mode="auto", depth="fast",
             context="search", engines_boost=None):
        calls.append(str(query))
        return _stub_decision()

    monkeypatch.setattr(route, "route_query", fake)
    return calls


# ── 命中与未命中 ──────────────────────────────────────────────────────────────

def test_first_call_misses_second_hits(cache_on, counted_route):
    """同一输入连问两次：只应真的路由一次。"""
    first = route.route_query_cached("缓存命中测试", mode="auto", depth="fast")
    second = route.route_query_cached("缓存命中测试", mode="auto", depth="fast")
    assert first.get("route_cached") is False
    assert second.get("route_cached") is True
    assert len(counted_route) == 1, "第二次不该再走 route_query"


def test_whitespace_normalized_key(cache_on, counted_route):
    """多打几个空格不该算两次路由。"""
    route.route_query_cached("空白  归一化", mode="auto", depth="fast")
    again = route.route_query_cached("空白 归一化", mode="auto", depth="fast")
    assert again.get("route_cached") is True
    assert len(counted_route) == 1


def test_different_inputs_do_not_share_entry(cache_on, counted_route):
    """mode/depth 不同是两次不同的路由决策。"""
    route.route_query_cached("同一问题", mode="auto", depth="fast")
    route.route_query_cached("同一问题", mode="deep", depth="deep")
    assert len(counted_route) == 2


def test_hit_decision_equals_fresh_route(cache_on, counted_route):
    """命中返回的内容必须与实算一致（时间戳类字段除外）。

    engines_fallback 的**顺序**排除在相等性之外：它由集合迭代序决定，实测
    跨进程本就随机（前两位稳定、其余每进程不同），缓存把某个合法顺序固定到
    TTL 内属于预期行为——它是备用链，任何顺序都成立。
    """
    fresh = route.route_query_cached("等价性测试", mode="auto", depth="fast")
    hit = route.route_query_cached("等价性测试", mode="auto", depth="fast")
    assert hit.get("route_cached") is True
    a = {k: v for k, v in fresh.items()
         if k not in ("elapsed_ms", "route_cached", "engines_fallback")}
    b = {k: v for k, v in hit.items()
         if k not in ("elapsed_ms", "route_cached", "engines_fallback")}
    assert a == b
    assert sorted(fresh["engines_fallback"]) == sorted(hit["engines_fallback"])


def test_hit_reports_its_own_elapsed(cache_on, counted_route):
    """elapsed_ms 必须报本次命中的耗时，不能把存档里的旧值端出来。

    这个字段会经 plan 输出给用户，报旧值就是撒谎——所以把存档值改成 9999，
    命中后必须看不到它。
    """
    route.route_query_cached("耗时字段", mode="auto", depth="fast")
    path = route._route_cache_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload["entries"].values():
        entry["decision"]["elapsed_ms"] = 9999.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    hit = route.route_query_cached("耗时字段", mode="auto", depth="fast")
    assert hit.get("route_cached") is True
    assert hit["elapsed_ms"] < 50, "命中是读文件 + 深拷贝，不该是路由量级"


# ── 隔离性 ────────────────────────────────────────────────────────────────────

def test_hit_returns_deep_copy(cache_on, counted_route):
    """调用方会就地改 decision（research 置 no_early_stop），不能污染缓存。"""
    route.route_query_cached("深拷贝", mode="auto", depth="fast")
    hit = route.route_query_cached("深拷贝", mode="auto", depth="fast")
    hit["engines_combo"].append("被注入的引擎")
    hit["no_early_stop"] = True
    again = route.route_query_cached("深拷贝", mode="auto", depth="fast")
    assert "被注入的引擎" not in again["engines_combo"]
    assert "no_early_stop" not in again


def test_disabled_flag_falls_back(cache_on, counted_route, monkeypatch):
    """开关关掉时既不读也不写，且两次调用都要真路由。"""
    monkeypatch.setenv("ARGO_ROUTE_CACHE", "0")
    d1 = route.route_query_cached("关掉开关", mode="auto", depth="fast")
    d2 = route.route_query_cached("关掉开关", mode="auto", depth="fast")
    assert len(counted_route) == 2
    assert "route_cached" not in d1 and "route_cached" not in d2
    assert not route._route_cache_file().exists()


# ── 失效与容错 ────────────────────────────────────────────────────────────────

def test_stale_entry_expires(cache_on, counted_route):
    """超过 TTL 的条目不得再用。"""
    route.route_query_cached("过期条目", mode="auto", depth="fast")
    path = route._route_cache_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload["entries"].values():
        entry["ts"] = time.time() - route._ROUTE_CACHE_TTL_S - 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    again = route.route_query_cached("过期条目", mode="auto", depth="fast")
    assert again.get("route_cached") is False
    assert len(counted_route) == 2


def test_config_change_busts_cache(cache_on, counted_route, monkeypatch):
    """配置变了（enabled / domains / combo）旧决策必须作废。"""
    import config
    monkeypatch.setattr(config, "config_stamp", lambda: 1000.0)
    route.route_query_cached("配置变更", mode="auto", depth="fast")
    monkeypatch.setattr(config, "config_stamp", lambda: 2000.0)
    again = route.route_query_cached("配置变更", mode="auto", depth="fast")
    assert again.get("route_cached") is False
    assert len(counted_route) == 2


def test_quota_exhausted_set_busts_cache(cache_on, counted_route, monkeypatch):
    """源被标记额度耗尽后，旧决策不得继续把它当可用源。"""
    import quota

    class _Qm:
        def __init__(self, marks):
            self._marks = marks

        def remote_exhausted_marks(self):
            return self._marks

    monkeypatch.setattr(quota, "get_quota_manager", lambda: _Qm({}))
    route.route_query_cached("额度耗尽", mode="auto", depth="fast")
    monkeypatch.setattr(quota, "get_quota_manager",
                        lambda: _Qm({"byted": {"reason": "quota"}}))
    again = route.route_query_cached("额度耗尽", mode="auto", depth="fast")
    assert again.get("route_cached") is False


def test_corrupt_cache_file_is_tolerated(cache_on, counted_route):
    """缓存文件损坏只该导致未命中，不该让搜索失败。"""
    route._route_cache_file().parent.mkdir(parents=True, exist_ok=True)
    route._route_cache_file().write_text("{ 这不是 JSON", encoding="utf-8")
    d = route.route_query_cached("损坏缓存", mode="auto", depth="fast")
    assert d.get("route_cached") is False
    assert d.get("domain") == "english_tech"


def test_unreadable_fingerprint_disables_cache(cache_on, counted_route,
                                               monkeypatch):
    """指纹取不到时退回实算，且不写条目（空摘要永不匹配任何已存键）。

    契约：`route_cached` **缺席**表示「本次没走缓存」（开关关闭或指纹不可用），
    区别于 False（走了缓存但未命中）。两者对调用方的含义不同——
    缺席是「缓存没参与」，False 是「缓存参与过但这个输入是新的」。
    """
    import config
    monkeypatch.setattr(config, "config_stamp",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    d = route.route_query_cached("指纹不可用", mode="auto", depth="fast")
    assert d.get("route_cached") is None
    assert "route_cached" not in d
    assert not route._route_cache_file().exists()


def test_non_json_roundtrip_decision_is_not_cached(cache_on, monkeypatch):
    """决策若过不了 JSON 往返（如含元组）就不缓存——缓存不得改变语义。"""
    def bad(query, **kwargs):
        return _stub_decision(engines_combo=("octen", "anysearch"))

    monkeypatch.setattr(route, "route_query", bad)
    d = route.route_query_cached("语义不可逆", mode="auto", depth="fast")
    assert d["engines_combo"] == ("octen", "anysearch")
    assert not route._route_cache_file().exists(), "不可逆的决策不该被写进缓存"


def test_entries_capped(cache_on, counted_route, monkeypatch):
    """条目数封顶，避免缓存文件无界增长。"""
    # 常量住在 route_cache（存储层），route 只是同名转出；打桩必须打在**读取处**，
    # 否则改的是转出副本，实现仍读原值（拆模块时最容易踩的坑）。
    import route_cache
    monkeypatch.setattr(route_cache, "_ROUTE_CACHE_MAX_ENTRIES", 5)
    for i in range(12):
        route.route_query_cached(f"封顶测试 {i}", mode="auto", depth="fast")
    payload = json.loads(route._route_cache_file().read_text(encoding="utf-8"))
    assert len(payload["entries"]) <= 5

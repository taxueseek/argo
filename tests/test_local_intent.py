#!/usr/bin/env python3
"""tests/test_local_intent.py — 公域查询跳过本机检索的判据与边界。

守四条：
  1. 判据与 local_seek.has_local_code_intent 同口径（防两处漂移）；
  2. 公域 + 无本地信号 → 跳过；公域 + 有本地信号 → 不跳；
  3. 非公域（general / 本地相关域）→ 不跳（宁可多搜也不误杀）；
  4. 显式 include_local=True 恒尊重，不被判据推翻。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import local_intent  # noqa: E402
import local_seek  # noqa: E402


# ── 1. 判据同口径 ─────────────────────────────────────────────────────────────

def test_intent_pattern_matches_local_seek():
    """两处判据必须逐字一致——否则「该不该跳」与「该不该判本地意图」分叉。"""
    assert (local_intent._LOCAL_SIGNAL_RE.pattern
            == local_seek._LOCAL_INTENT_PATTERN.pattern)


# ── 2. 跳过判据 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("domain,query", [
    ("weather_query", "北京天气"),
    ("weather_query", "weather forecast Singapore"),
    ("stock_query", "贵州茅台股价"),
    ("fx_query", "人民币 汇率 中间价"),
    ("macro_data", "中国 GDP 2026"),
    ("financial_news", "美联储 加息 最新消息"),
    ("law_text", "民法典 第 107 条"),
    ("scholar_search", "transformer attention paper"),
])
def test_public_only_domain_skips(domain, query):
    assert local_intent.should_skip_local(domain, query) is True, (domain, query)


@pytest.mark.parametrize("domain,query", [
    ("weather_query", "weather.py 报错"),        # 扩展名
    ("weather_query", "查本地脚本天气接口"),        # 中文本地说法（本地脚本）
    ("stock_query", "本项目股价计算函数"),        # 本工程说法
    ("fx_query", "config.yaml 里的汇率配置"),    # 工程文件
])
def test_public_domain_with_local_signal_does_not_skip(domain, query):
    assert local_intent.should_skip_local(domain, query) is False, (domain, query)


# ── 3. 非公域不跳 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("domain,query", [
    ("general", "Python asyncio error handling"),
    ("skill_search", "找一个本地技能"),
    ("code_search", "grep_app 代码检索"),
    (None, "天气 本地文件"),
])
def test_non_public_domain_never_skips(domain, query):
    assert local_intent.should_skip_local(domain, query) is False, (domain, query)


def test_local_only_domain_list_not_empty():
    """名单非空——空名单会让判据恒 False，跳过机制静默失效。"""
    assert len(local_intent._PUBLIC_ONLY_DOMAINS) >= 20


# ── 4. 显式意图恒尊重 ─────────────────────────────────────────────────────────

def test_explicit_include_local_not_overridden():
    """resolve_include_local 的显式分支不受判据影响（用户点名要搜就搜）。"""
    assert local_seek.resolve_include_local(True, "fast", "北京天气",
                                            "weather_query") is True
    assert local_seek.resolve_include_local(False, "fast", "本地代码",
                                            None) is False


def test_auto_path_uses_domain():
    """公域查询仍跳过；非域信息时默认不跑（信号驱动）。"""
    assert local_seek.resolve_include_local(None, "fast", "北京天气",
                                            "weather_query") is False
    # 2026-11-10 P0-1 翻转：无本地信号时默认不跑本机轨
    assert local_seek.resolve_include_local(None, "fast", "北京天气",
                                            "general") is False
    assert local_seek.resolve_include_local(None, "fast", "北京天气", None) is False


def test_auto_path_runs_with_local_signal():
    """带本地信号的查询默认跑本机轨。"""
    assert local_seek.resolve_include_local(
        None, "fast", "config.yaml 里的 engines 字段", None) is True
    assert local_seek.resolve_include_local(
        None, "fast", "本地文件里有没有 deep research 笔记", None) is True
    assert local_seek.resolve_include_local(
        None, "fast", "我的 .agents/skills 有哪些", None) is True
    assert local_seek.resolve_include_local(
        None, "fast", "~/projects/my-app/notes.md", None) is True
    # 方案一增强：错误类名、私有函数名、工程配置文件、调用语法
    assert local_seek.resolve_include_local(
        None, "fast", "ZeroDivisionError 怎么排查", None) is True
    assert local_seek.resolve_include_local(
        None, "fast", "_run_local_seek 函数在哪", None) is True
    assert local_seek.resolve_include_local(
        None, "fast", "pyproject.toml 里的依赖", None) is True
    assert local_seek.resolve_include_local(
        None, "fast", "resolve_include_local() 实现", None) is True


def test_two_tier_engineering_signals_zero_pollution_on_public_domains():
    """公域白名单优先：即便包含技术弱信号，命中公域答案型白名单仍坚决跳过。"""
    assert local_seek.resolve_include_local(
        None, "fast", "北京天气", "weather_query") is False
    assert local_seek.resolve_include_local(
        None, "fast", "attention mechanism paper", "scholar_search") is False
    assert local_seek.resolve_include_local(
        None, "fast", "美联储 加息 最新消息", "financial_news") is False
    assert local_seek.resolve_include_local(
        None, "fast", "红烧肉 做法", "general") is False


# ── 5. 条数上限 ───────────────────────────────────────────────────────────────

def test_max_hits_capped(monkeypatch):
    """本地命中上限默认 3，且不超过调用方的 max_n。"""
    assert local_seek._max_local_hits() == 3
    monkeypatch.setenv("ARGO_LOCAL_SEEK_MAX_HITS", "7")
    assert local_seek._max_local_hits() == 7
    monkeypatch.setenv("ARGO_LOCAL_SEEK_MAX_HITS", "0")
    assert local_seek._max_local_hits() == 0
    monkeypatch.setenv("ARGO_LOCAL_SEEK_MAX_HITS", "not-a-number")
    assert local_seek._max_local_hits() == 3  # 非法值回落默认


def test_candidate_roots_env_override(monkeypatch, tmp_path):
    """ARGO_LOCAL_SEEK_ROOTS 覆盖默认候选清单。"""
    d1, d2 = tmp_path / "a", tmp_path / "b"
    d1.mkdir()
    d2.mkdir()
    monkeypatch.setenv("ARGO_LOCAL_SEEK_ROOTS", f"{d1}{os_sep()}{d2}")
    roots = local_seek._candidate_targeted_roots()
    assert str(d1) in roots
    # 不存在的目录被过滤
    monkeypatch.setenv("ARGO_LOCAL_SEEK_ROOTS", f"/nonexistent/xyz{os_sep()}{d1}")
    assert local_seek._candidate_targeted_roots() == [str(d1)]


def os_sep() -> str:
    import os
    return os.pathsep


# ── 6. 记账字段 ───────────────────────────────────────────────────────────────

def test_accounting_empty_when_skipped():
    """跳过时只报 skipped，不报 0ms——0ms 会被读成「搜了但很快」。"""
    local_seek._LAST_LOCAL_SEEK_SKIPPED = True
    local_seek._LAST_LOCAL_SEEK_MS = 0
    try:
        acct = local_seek.local_seek_accounting()
        assert acct == {"local_seek_skipped": True}
        assert "local_seek_ms" not in acct
    finally:
        local_seek._LAST_LOCAL_SEEK_SKIPPED = False


def test_accounting_reports_ms_and_count():
    local_seek._LAST_LOCAL_SEEK_SKIPPED = False
    local_seek._LAST_LOCAL_SEEK_MS = 42
    local_seek._LAST_LOCAL_SEEK_COUNT = 2
    try:
        acct = local_seek.local_seek_accounting()
        assert acct == {"local_seek_ms": 42, "local_seek_count": 2}
    finally:
        local_seek._LAST_LOCAL_SEEK_MS = 0
        local_seek._LAST_LOCAL_SEEK_COUNT = 0


if __name__ == "__main__":
    import os  # noqa: F401  (os_sep 用)
    pytest.main([__file__, "-q"])

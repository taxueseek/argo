#!/usr/bin/env python3
"""缓存退化守卫的回归测试（2026-09-27 新增，方案 C1）。

实锤事故：`detecting LLM generated content farm SEO spam` 路由正确
（anysearch + local_bing），但上游某一刻把整句当成单词去查，返回 8 条
「detecting 这个词的词典释义」。这些条目的 rerank_dims.relevance 全部是
**0.1429**（只命中查询里的 detecting 一词），却照常返回、照常排序、照常
写入缓存——于是这次上游抖动被固化，之后每次命中缓存都复现同样的垃圾。

最难察觉的地方：funnel 完全正常（returned 10 → kept 8）。funnel 只数
数量，不看每条像不像答案。已有的登录态守卫挡「不该共享的载荷」，但没有
任何东西挡「上游降级后的残次品」。

本文件锁定的性质：
  1. 整批低相关 → 拒绝写入（核心）；
  2. 单条低相关 → 照常写入（长尾查询本就有弱命中，拦它会误伤真结果）；
  3. 拿不到相关性 → 不判（没有数据不猜，不因缺字段拒绝正常写入）；
  4. 退化批次混有少数正常结果时仍应被拦（上游降级常混 1-2 条真货）。
"""
from __future__ import annotations

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(SCRIPT_DIR, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

os.environ.setdefault("ARGO_STATE_DIR", "/tmp/argo-degraded-guard-test")

from cache_guard import (  # noqa: E402
    DEGRADED_RELEVANCE_FLOOR,
    DegradedCacheRejected,
    assert_not_degraded,
    attempt_cache_write,
    cache_write_rejections,
    is_degraded_results,
)


def _r(rel, **kw):
    """造一条带 rerank_dims.relevance 的结果（与真实落盘结构一致）。"""
    d = {"title": "t", "url": "https://x.com/1"}
    d.update(kw)
    d["rerank_dims"] = {"relevance": rel}
    return d


class TestDegradedGuard:
    def test_real_poisoned_batch_is_rejected(self):
        """核心：实锤事故那批数据（rel 全为 0.1429）必须被拦。"""
        batch = [_r(0.1429) for _ in range(7)] + [_r(0.0)]
        assert is_degraded_results(batch) is True
        with pytest.raises(DegradedCacheRejected):
            assert_not_degraded(batch, context="t")

    def test_normal_batch_passes(self):
        batch = [_r(0.61), _r(0.72), _r(0.55), _r(0.83), _r(0.48)]
        assert is_degraded_results(batch) is False
        assert_not_degraded(batch, context="t")   # 不抛

    def test_single_weak_result_not_rejected(self):
        """单条低相关是正常的（长尾查询），不能因此拒绝整批。"""
        batch = [_r(0.10), _r(0.65), _r(0.70), _r(0.58)]
        assert is_degraded_results(batch) is False
        assert_not_degraded(batch, context="t")

    def test_mostly_low_with_one_good_still_rejected(self):
        """上游降级常混 1-2 条真货；7 低 1 高仍应拦。"""
        batch = [_r(0.12)] * 7 + [_r(0.91)]
        assert is_degraded_results(batch) is True

    def test_missing_relevance_not_judged(self):
        """没有相关性数据就不判——缺字段不等于退化。"""
        batch = [{"title": "x", "url": "https://x.com/%d" % i} for i in range(8)]
        assert is_degraded_results(batch) is False
        assert_not_degraded(batch, context="t")

    def test_too_few_scored_not_judged(self):
        """可判定的条目 <3 时不判：样本太少，比例不可信。"""
        assert is_degraded_results([_r(0.01), _r(0.02)]) is False

    def test_empty_and_malformed_safe(self):
        assert is_degraded_results([]) is False
        assert is_degraded_results(None) is False
        assert is_degraded_results("junk") is False
        assert is_degraded_results([None, 1, "x", _r(0.1)]) is False

    def test_top_level_relevance_also_read(self):
        """未跑 rerank 的引擎直写路径把 relevance 放顶层，也要认。"""
        batch = [{"relevance": 0.11} for _ in range(5)]
        assert is_degraded_results(batch) is True

    def test_floor_boundary(self):
        """恰好等于地板不算低（用 < 而非 <=，避免边界抖动）。"""
        batch = [_r(DEGRADED_RELEVANCE_FLOOR) for _ in range(5)]
        assert is_degraded_results(batch) is False
        batch = [_r(DEGRADED_RELEVANCE_FLOOR - 0.01) for _ in range(5)]
        assert is_degraded_results(batch) is True


class TestAttemptCacheWrite:
    """守卫拒绝只取消写入，不取消本次检索（2026-09-27 修复）。

    实锤：`Apple Inc 10-K annual report 2025` 命中 sec_edgar 后（1 条相关 +
    4 条噪音）触发退化守卫，异常穿透到 CLI 顶层 traceback 退出——已检索到的
    结果连同响应一起丢弃。守卫的意图是「别固化」，不是「别返回」。
    """

    def test_rejection_returns_class_name_instead_of_raising(self):
        def _reject():
            raise DegradedCacheRejected("上游退化")

        assert attempt_cache_write(_reject, context="t") == "DegradedCacheRejected"

    def test_success_returns_none(self):
        calls = []
        assert attempt_cache_write(lambda: calls.append(1), context="t") is None
        assert calls == [1], "写入必须真的执行——守卫不得变成「一律不写」"

    def test_login_rejection_also_swallowed(self):
        """登录态守卫与退化守卫同一处理：两条都是「这次别写」而非「这次别答」。"""
        from cache import LoginCacheRejected

        def _reject():
            raise LoginCacheRejected("登录态载荷")

        assert attempt_cache_write(_reject, context="t") == "LoginCacheRejected"

    def test_unrelated_exception_still_propagates(self):
        """只吞写入守卫的两类异常：其它异常照旧穿透，不把真 bug 静默掉。"""
        def _boom():
            raise RuntimeError("别的毛病")

        with pytest.raises(RuntimeError):
            attempt_cache_write(_boom, context="t")

    def test_rejection_tuple_covers_all_guards(self):
        names = {c.__name__ for c in cache_write_rejections()}
        assert names == {"DegradedCacheRejected", "FailedStateCacheRejected",
                         "LoginCacheRejected"}


class TestRejectionDoesNotKillTheQuery:
    """端到端：combo 写入被拒时，结果照常返回且带可观测信号。"""

    def test_combo_write_rejection_keeps_results(self, tmp_path, monkeypatch):
        import search as search_mod
        from cache import SearchCache
        from search import execute_search
        from stage_timing import StageTiming

        good = [
            {"title": f"Python 教程 {i}", "snippet": "Python 编程入门内容",
             "url": f"https://example.com/{i}"}
            for i in range(3)
        ]

        class _AllowAll:
            def allow(self, eng):
                return True, "closed"

            def get_negative(self, *a, **k):
                return None

            def status(self, eng):
                return {"state": "closed"}

            def record_success(self, *a, **k):
                pass

            def record_failure(self, *a, **k):
                pass

            def set_negative(self, *a, **k):
                pass

            def clear_negative(self, *a, **k):
                pass

        def _reject_set(self, *a, **k):
            raise DegradedCacheRejected("上游退化（注入）")

        monkeypatch.setattr(search_mod, "engine_search",
                            lambda q, e, **k: good)
        monkeypatch.setattr("circuit_breaker.get_breaker",
                            lambda *a, **k: _AllowAll())
        monkeypatch.setattr(SearchCache, "set", _reject_set)

        out = execute_search(
            "Python",
            {"engines_combo": ["t_eng"], "engines": ["t_eng"], "parallel": True,
             "domain": "general_search", "engine": "t_eng"},
            max_results=5, timeout=10, depth="fast",
            cache=SearchCache(db_path=str(tmp_path / "c.db")),
            skip_cache=False, mode="fast", timing=StageTiming(),
        )
        assert out["count"] > 0, "结果不得因写入被拒而丢弃"
        assert out["cache_write_skip"] == "DegradedCacheRejected"
        assert [(o["engine"], o["status"]) for o in out["engine_outcomes"]] == [
            ("t_eng", "ok")], "成功的引擎调用不得被写成 error"

    def test_engine_write_rejection_keeps_engine_healthy(self, tmp_path, monkeypatch):
        """per-engine 写入被拒：引擎仍记 ok，结果不被清空（曾记成 error）。"""
        import search as search_mod
        from cache import SearchCache
        from search import execute_search
        from stage_timing import StageTiming

        good = [
            {"title": f"Python 教程 {i}", "snippet": "Python 编程入门内容",
             "url": f"https://example.com/{i}"}
            for i in range(3)
        ]

        class _AllowAll:
            def allow(self, eng):
                return True, "closed"

            def get_negative(self, *a, **k):
                return None

            def status(self, eng):
                return {"state": "closed"}

            def record_success(self, *a, **k):
                pass

            def record_failure(self, *a, **k):
                pass

            def set_negative(self, *a, **k):
                pass

            def clear_negative(self, *a, **k):
                pass

        def _reject_set_engine(self, *a, **k):
            raise DegradedCacheRejected("上游退化（注入）")

        monkeypatch.setattr(search_mod, "engine_search",
                            lambda q, e, **k: good)
        monkeypatch.setattr("circuit_breaker.get_breaker",
                            lambda *a, **k: _AllowAll())
        monkeypatch.setattr(SearchCache, "set_engine", _reject_set_engine)

        out = execute_search(
            "Python",
            {"engines_combo": ["t_eng"], "engines": ["t_eng"], "parallel": True,
             "domain": "general_search", "engine": "t_eng"},
            max_results=5, timeout=10, depth="fast",
            cache=SearchCache(db_path=str(tmp_path / "c.db")),
            skip_cache=False, mode="fast", timing=StageTiming(),
        )
        assert out["count"] > 0
        outcomes = [(o["engine"], o["status"]) for o in out["engine_outcomes"]]
        assert outcomes == [("t_eng", "ok")], (
            f"per-engine 写入被拒不得改写成 error，实得 {outcomes}")
        assert out["errors"] == []

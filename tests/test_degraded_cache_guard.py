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

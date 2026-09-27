#!/usr/bin/env python3
"""证据回写排序的回归测试（2026-09-27 新增，方案 A-3）。

背景：`verify_results` 抓回正文、算了正文级 absorption，但旧实现只把分数
写进 `post_fetch_absorption` 字段、**不重排**（调用点在排序之后）。于是
「抓取链路已经识别出这是低质正文」这份情报到不了排序器——`--verify` 花了
RTT 却只改展示，不改结果顺序。

`reorder_by_evidence` 补上这个闭环。本文件锁定的核心性质是**只降不升**：
verify 只覆盖 top-k，若正文质量好的条目被加分，等于奖励「恰好被抓取」，
而抓取与否与内容质量无关（采样偏差）。
"""
from __future__ import annotations

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(SCRIPT_DIR, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

os.environ.setdefault("ARGO_STATE_DIR", "/tmp/argo-reorder-test")

from evidence_loop import reorder_by_evidence  # noqa: E402


def _r(url, score, absorption=None, verified=False):
    r = {"url": url, "title": url, "score": score}
    if verified:
        r["has_fetched_evidence"] = True
        r["post_fetch_absorption"] = absorption
    return r


class TestReorderByEvidence:
    def test_low_quality_verified_is_demoted(self):
        """核心：正文低质的已核验条目必须被降权。"""
        res = [_r("https://a.com/seo", 0.90, 0.15, verified=True),
               _r("https://b.com/gov", 0.70, 0.85, verified=True)]
        out = reorder_by_evidence(res)
        assert out["reordered"] is True
        # 软文被降到规范文档之后
        assert res[0]["url"] == "https://b.com/gov", [x["url"] for x in res]

    def test_only_demote_never_promote(self):
        """采样偏差防护：高质量已核验条目**不得**被加分。

        verify 只覆盖 top-k；若高分内容因「被抓到且质量好」而加分，等于
        奖励「恰好被抓取」这件事，与内容质量无关。
        """
        res = [_r("https://a.com/good", 0.60, 0.95, verified=True)]
        out = reorder_by_evidence(res)
        assert out["reordered"] is False
        assert out["adjusted"] == []
        assert res[0]["score"] == 0.60, "高质量条目被提升，违反只降不升"

    def test_neutral_zone_untouched(self):
        """0.5 为中性点：0.5 及以上不减分。

        若以「质量本身」作系数，0.4 分的正常内容会被无端降到 ×0.4——
        而 absorption 的实际分布以 0.3-0.6 为主。
        """
        res = [_r("https://a.com/x", 0.80, 0.50, verified=True),
               _r("https://b.com/y", 0.70, 0.60, verified=True)]
        out = reorder_by_evidence(res)
        assert out["adjusted"] == [], out
        assert [x["score"] for x in res] == [0.80, 0.70]

    def test_unverified_untouched(self):
        """未核验条目不得被改动（它是相对比较的基准）。"""
        res = [_r("https://a.com/nv", 0.80), _r("https://b.com/v", 0.75, 0.1, True)]
        reorder_by_evidence(res)
        by_url = {x["url"]: x for x in res}
        assert by_url["https://a.com/nv"]["score"] == 0.80

    def test_no_evidence_is_noop(self):
        """无任何已核验条目 → 完全不动（逐位可对拍）。"""
        res = [_r("https://a.com/1", 0.9), _r("https://a.com/2", 0.8)]
        before = [x["score"] for x in res]
        out = reorder_by_evidence(res)
        assert out["reordered"] is False
        assert [x["score"] for x in res] == before

    def test_single_result_no_crash(self):
        res = [_r("https://a.com/1", 0.9, 0.2, True)]
        out = reorder_by_evidence(res)
        assert isinstance(out, dict)

    def test_empty_list_safe(self):
        out = reorder_by_evidence([])
        assert out == {"reordered": False, "adjusted": [], "moved": 0}

    def test_malformed_rows_safe(self):
        """脏数据（缺 score / 非 dict / absorption 为 None）不得抛异常。"""
        res = [{"url": "https://a.com/1"}, None, "junk",
               {"url": "https://a.com/2", "score": None,
                "has_fetched_evidence": True, "post_fetch_absorption": 0.1},
               _r("https://a.com/3", 0.9, 0.1, True)]
        out = reorder_by_evidence([x for x in res if isinstance(x, dict)])
        assert isinstance(out, dict)

    def test_weight_zero_disables(self):
        """weight=0 时完全不动（可作全局关闭手段）。"""
        res = [_r("https://a.com/1", 0.9, 0.0, True)]
        out = reorder_by_evidence(res, weight=0.0)
        assert out["reordered"] is False
        assert res[0]["score"] == 0.9

    def test_rerank_dims_observable(self):
        """降权必须留可观测项，否则线上无法定位「为什么这条掉了」。"""
        res = [_r("https://a.com/1", 0.9, 0.1, True)]
        res[0]["rerank_dims"] = {}
        reorder_by_evidence(res)
        assert res[0]["rerank_dims"].get("post_fetch_quality") == 0.1

    def test_strictly_ordered_by_score_after(self):
        """降权后仍按 score 降序（不得留下乱序列表）。"""
        res = [_r("https://a.com/1", 0.9, 0.0, True),
               _r("https://a.com/2", 0.85, 0.9, True),
               _r("https://a.com/3", 0.5, 0.9, True)]
        reorder_by_evidence(res)
        scores = [x["score"] for x in res]
        assert scores == sorted(scores, reverse=True), scores

    def test_negative_score_sorts_last_not_first(self):
        """负分必须排最后，不得被顶到最前（2026-09-27 方案 A 的回归守卫）。

        原实现用 `abs(score)` 作排序键。当前 score 恒非负，abs() 冗余无害；
        但它把「分数绝对值大的更相关」这条**反向**语义固化进了排序器——
        一旦将来某个算子改产出负分（扣分制、或 `score - penalty` 这类改写），
        最不相关的结果会被静默排到第一位，且没有任何测试会红。

        取景要点（第一版用例写错过，这里是踩坑记录）：负分条目若**本身也被
        降权**，|负分| 会随之缩小，abs() 与普通降序碰巧给出同一顺序，测试
        于是「在坏代码上也绿」——空守卫比没有守卫更坏，它让人以为防住了。

        故让负分条目**不被改动**（未核验），另配一条会被降权的正分条目来
        触发重排。未修代码上：abs(-0.9)=0.9 > 0.52 → 负分跳到首位，用例红。
        """
        res = [_r("https://a.com/pos", 0.8, 0.0, True),   # 已核验低质 → 降权
               {"url": "https://b.com/neg", "score": -0.9}]  # 未核验 → 不改动
        out = reorder_by_evidence(res)
        assert out["reordered"] is True, "需要至少一条被降权才触发重排"
        order = [x["url"] for x in res]
        assert order == ["https://a.com/pos", "https://b.com/neg"], order

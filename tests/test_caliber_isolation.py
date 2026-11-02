#!/usr/bin/env python3
"""test_caliber_isolation.py — 口径隔离版 RRF 的名次不变式（离线）。

## 守的是什么缺陷

2026-10-17 的「口径隔离」把 `group_by_caliber` 写成了按**单条结果**分组
（每条包成一个单元素列表）。`merge_within_caliber` 里 `enumerate(group)`
的 `i` 于是恒为 0，同一引擎的所有结果拿到完全相同的 RRF 分：

    anysearch 5 条 → 0.017213 / 0.017213 / 0.017213 / 0.017213 / 0.017213

输出退化成「按引擎权重整段拼接」，引擎内的名次信息归零。默认
`ARGO_RRF_WEIGHTED=True` 让这条路径成为每次搜索的默认路径，而既有测试
只断言「权重被消费」，没有一条断言「同引擎内名次必须递减」。

## 三条不变式

  1. 分组单位是整条引擎列表（同一引擎的 5 条进同一个 group）；
  2. 同引擎内 `_rrf_score` 随名次严格递减；
  3. 跨口径仍按优先级并列（financial 在 general 之前），但各自内部保序。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from caliber_isolation import group_by_caliber, merge_by_caliber  # noqa: E402
from search_rank import rrf_merge  # noqa: E402


def _row(engine: str, index: int, host: str) -> dict:
    return {
        "_engine": engine,
        "source": engine,
        "url": f"https://{host}/{index}",
        "title": f"{engine}-{index}",
        "snippet": f"{engine} snippet {index}",
    }


def _engine_list(engine: str, host: str, count: int = 5) -> list[dict]:
    return [_row(engine, i, host) for i in range(count)]


def _scores_by_source(rows: list[dict]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in rows:
        out.setdefault(r["source"], []).append(float(r["_rrf_score"]))
    return out


class TestGrouping(unittest.TestCase):
    def test_group_keeps_whole_engine_list(self):
        lists = [_engine_list("octen", "a.com"), _engine_list("anysearch", "b.com")]
        groups = group_by_caliber(lists)
        self.assertEqual(list(groups), ["general"])
        self.assertEqual([len(g) for g in groups["general"]], [5, 5],
                         "每个 group 应是整条引擎列表，而不是 10 个单元素列表")

    def test_empty_lists_ignored(self):
        groups = group_by_caliber([[], _engine_list("octen", "a.com")])
        self.assertEqual(len(groups["general"]), 1, "空列表不应产出 group")
        self.assertEqual(len(groups["general"][0]), 5)

    def test_mixed_list_split_by_caliber_preserving_order(self):
        mixed = [_row("octen", 0, "a.com"), _row("eastmoney", 1, "b.com")]
        groups = group_by_caliber([mixed])
        self.assertEqual(len(groups["general"]), 1)
        self.assertEqual(len(groups["financial_data"]), 1)
        self.assertEqual(groups["general"][0][0]["title"], "octen-0")
        self.assertEqual(groups["financial_data"][0][0]["title"], "eastmoney-1")


class TestWeightedRankDecay(unittest.TestCase):
    def test_scores_decrease_with_rank_within_engine(self):
        rows = rrf_merge(
            [_engine_list("octen", "a.com"), _engine_list("anysearch", "b.com")],
            weighted=True,
        )
        by_source = _scores_by_source(rows)
        for source, scores in by_source.items():
            self.assertEqual(len(scores), 5, source)
            self.assertEqual(len(set(scores)), 5,
                             f"{source} 的 5 条同名次同分——名次衰减丢了")
            self.assertEqual(scores, sorted(scores, reverse=True),
                             f"{source} 的分数未随名次递减")

    def test_rank_zero_outranks_rank_one_of_same_engine(self):
        rows = rrf_merge([_engine_list("octen", "a.com")], weighted=True)
        self.assertGreater(rows[0]["_rrf_score"], rows[1]["_rrf_score"])

    def test_interleaves_instead_of_concatenating(self):
        rows = rrf_merge(
            [_engine_list("octen", "a.com"), _engine_list("anysearch", "b.com")],
            weighted=True,
        )
        # 旧实现（每条结果单独成组）会输出 [anysearch×5, octen×5]；
        # 恢复名次衰减后，octen 的第 1 名应挤进前 5。
        self.assertIn("octen", {r["source"] for r in rows[:5]},
                      "两个同口径引擎应交替，而不是整段拼接")


class TestCrossCaliber(unittest.TestCase):
    def test_financial_precedes_general_but_keeps_internal_order(self):
        rows = merge_by_caliber(
            [_engine_list("eastmoney", "fin.com", 3),
             _engine_list("anysearch", "web.com", 3)],
            weighted=True,
        )
        self.assertEqual([r["source"] for r in rows[:3]], ["eastmoney"] * 3)
        fin = [r["_rrf_score"] for r in rows[:3]]
        self.assertEqual(fin, sorted(fin, reverse=True))


class TestCrossCaliberDuplicateUrl(unittest.TestCase):
    def test_same_url_merges_across_calibers(self):
        """同一 URL 被不同口径命中时只交付一条，共识与分数不丢。"""
        web = {
            "_engine": "anysearch", "source": "anysearch",
            "url": "https://zh.wikipedia.org/wiki/Test",
            "title": "Test", "snippet": "short",
            "consensus_engines": ["anysearch"], "_rrf_score": 0.02,
        }
        wiki = {
            "_engine": "wikipedia", "source": "wikipedia",
            "url": "https://zh.wikipedia.org/wiki/Test",
            "title": "Test 词条", "snippet": "更长更完整的摘要",
            "consensus_engines": ["wikipedia"], "_rrf_score": 0.03,
        }
        rows = merge_by_caliber([[web], [wiki]], weighted=True)
        self.assertEqual(len(rows), 1, f"跨口径同 URL 必须合并：{rows}")
        row = rows[0]
        self.assertEqual(set(row["consensus_engines"]),
                         {"anysearch", "wikipedia"})
        self.assertGreater(row["_rrf_score"], 0.0)
        self.assertEqual(row["snippet"], "更长更完整的摘要")
        self.assertEqual(set(row["source"].split("/")),
                         {"anysearch", "wikipedia"})


class TestClassicFallback(unittest.TestCase):
    def test_classic_keeps_per_list_rank(self):
        rows = merge_by_caliber(
            [_engine_list("octen", "a.com", 3),
             _engine_list("anysearch", "b.com", 3)],
            weighted=False,
        )
        self.assertEqual(rows[0]["_rrf_score"], rows[1]["_rrf_score"],
                         "经典 RRF 两个引擎的第 1 名应同分")
        self.assertGreater(rows[0]["_rrf_score"], rows[2]["_rrf_score"],
                           "第 1 名必须高于第 2 名（按列表内名次衰减）")


if __name__ == "__main__":
    import unittest as _u
    _u.main()

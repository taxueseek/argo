#!/usr/bin/env python3
"""否定解析快路径的回归测试（全本地，不联网）。

背景：parse_negation 的否定正则含 [\\u4e00-\\u9fffA-Za-z0-9] 这类两万字符的
字符集，模块级编译 14 条实测 5.5 ms，而 import query_understanding 是**无条件**
发生的（rewrite_query / route.extract_features / execute_search 都会拉它），
纯缓存命中那一档也要付。现在改为「先过 _has_negation_trigger 必要条件，
命中不了就一条正则都不编译」。

前置条件是**必要条件**，所以本文件的核心回归面是**漏答**：任何一条否定正则
能命中的查询，_has_negation_trigger 都必须答 True。漏答会让否定实体（如
「除了百度」的百度）重新污染消歧信号与检索串，而且不会报错——是静默劣化。

覆盖面：7 条模式各一条正样本、连字符标识符负样本（GPT-4o / 2026-09-19 不该
被当否定）、以及随机模糊语料上的「正则命中 ⇒ 前置条件命中」性质检查。
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from query_understanding import (  # noqa: E402
    _NEGATION_SPECS,
    _has_negation_trigger,
    _negation_patterns,
    parse_negation,
)


class TestNegationGate(unittest.TestCase):
    def test_every_spec_declares_trigger(self):
        """触发条件与正则同表：新增一条否定必须同时声明它的必要条件。"""
        for row in _NEGATION_SPECS:
            self.assertEqual(len(row), 4, f"规格行形状变了: {row}")
            self.assertTrue(row[3], f"缺少触发条件: {row[0]}")

    def test_positive_samples_pass_gate(self):
        cases = [
            ("除了百度以外的搜索引擎", "百度"),
            ("不想看恐怖片", "恐怖片"),
            ("不要广告的新闻", "广告"),
            ("排除广告的新闻", "广告"),
            ("python -django framework", "django"),
            ("NOT java tutorial", "java"),
            ("without ads news", "ads"),
        ]
        for query, term in cases:
            self.assertTrue(_has_negation_trigger(query), query)
            exclude, _clean = parse_negation(query)
            self.assertIn(term, exclude, f"{query} → {exclude}")

    def test_hyphenated_identifiers_take_fast_path(self):
        """连字符前是字母数字时不构成否定，必须保留快路径。"""
        for query in ("GPT-4o 价格", "2026-09-19 新闻", "SWE-bench 榜单"):
            self.assertFalse(_has_negation_trigger(query), query)
            self.assertEqual(parse_negation(query), ([], query), query)

    def test_regex_match_implies_gate(self):
        """模糊语料：任一条正则命中，前置条件必须已经答 True（防漏答）。"""
        rng = random.Random(20260919)
        tokens = ["除了", "不想", "不要", "排除", "not", "without", "-",
                  "GPT-4o", "广告", "百度", "的", " ", "新闻", "2026", "x", "以外"]
        patterns, spans = _negation_patterns()
        checked = 0
        for _ in range(3000):
            query = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 6)))
            if any(p.search(query) for p in patterns) or any(p.search(query) for p in spans):
                checked += 1
                self.assertTrue(_has_negation_trigger(query), f"漏答: {query!r}")
        self.assertGreater(checked, 0, "模糊语料没覆盖到任何命中，测试无效")


if __name__ == "__main__":
    unittest.main()

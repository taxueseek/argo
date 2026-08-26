#!/usr/bin/env python3
"""query_enhance 单测：归一化 / 检索变体 / 复杂度门控（离线）

目的：锁定「首轮查询增强」——词形规范化让精确源更好命中、变体生成供多路召回、
复杂度门控决定「简单首轮 / 复杂多轮」。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_query_enhance.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from query_enhance import (  # noqa: E402
    normalize_query,
    retrieval_variants,
    complexity_gate,
    low_complexity,
)


class TestNormalize(unittest.TestCase):
    def test_full_half_width(self):
        self.assertEqual(normalize_query('“GPT-5”，价格'), '"GPT-5",价格')

    def test_full_width_alnum(self):
        # 全角字母数字/符号 → 半角（2026-08 补齐；粘贴文本常见）
        self.assertEqual(normalize_query("ＡＰＩ　１２３"), "API 123")
        self.assertEqual(normalize_query("ｇｐｔ５￥"), "gpt5¥")

    def test_slash_split(self):
        # 仅拆「点号版本」斜杠，保留斜杠语义的查询不拆（2026-08 修复误伤）。
        self.assertEqual(normalize_query("LongCat-2.0/1.6 万亿"), "LongCat-2.0 1.6 万亿")

    def test_slash_not_split_when_semantic(self):
        # 全量拆斜杠会把 URL/分数/年份/日期毁掉 → 现在只拆点号版本，其余不动。
        cases = [
            "2026/08/01 的新闻",
            "1/2 怎么算",
            "https://example.com/a/b",
            "2023/2024 营收对比",
            "红色/蓝色 区别",
        ]
        for q in cases:
            self.assertEqual(normalize_query(q), q, f"应保留斜杠语义: {q}")

    def test_compress_space(self):
        self.assertEqual(normalize_query("a  b   c"), "a b c")

    def test_empty(self):
        self.assertEqual(normalize_query(""), "")


class TestVariants(unittest.TestCase):
    def test_split_hyphen(self):
        vs = retrieval_variants("LongCat-2.0/1.6 万亿")
        self.assertIn("LongCat 2.0 1.6 万亿", vs)

    def test_dedup_case_insensitive(self):
        vs = retrieval_variants("OpenAI API")
        self.assertEqual(len(vs), len({v.lower() for v in vs}))

    def test_empty(self):
        self.assertEqual(retrieval_variants(""), [])


class TestComplexity(unittest.TestCase):
    def test_low_short(self):
        self.assertEqual(complexity_gate("python 教程"), "low")

    def test_high_multi_hop(self):
        self.assertEqual(complexity_gate("OpenAI 和 Google 的区别 与 影响 对比"), "high")

    def test_high_long(self):
        self.assertEqual(complexity_gate("x" * 70), "high")

    def test_medium(self):
        self.assertEqual(
            complexity_gate("python 教程 additional context stuff sdk framework"),
            "medium",
        )

    def test_low_complexity_bool(self):
        self.assertTrue(low_complexity("python 教程"))


if __name__ == "__main__":
    unittest.main()

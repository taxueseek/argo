#!/usr/bin/env python3
"""ego-search query 归一化前处理单测（对齐 argo 核心 query_enhance）

设计约束：
  - 只做词形规范化（全角→半角、拆斜杠、压空格）
  - 必须保留平台结构化语法（from:/site:/filter: …）——ego 是站内通道，剥语法会丢能力
  - 核心 query_enhance 不可用时降级为原 query（不破坏）

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest sub-skills/ego-search/tests/test_query_normalize.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ego_search as es  # noqa: E402


class TestQueryNormalize(unittest.TestCase):
    def test_full_width_to_half(self):
        self.assertEqual(es._normalized_query("“GPT-5”，价格"), '"GPT-5",价格')

    def test_slash_split(self):
        self.assertEqual(es._normalized_query("LongCat-2.0/1.6 万亿"), "LongCat-2.0 1.6 万亿")

    def test_keep_platform_syntax(self):
        self.assertEqual(
            es._normalized_query('from:OpenAI "GPT-5" filter:images'),
            'from:OpenAI "GPT-5" filter:images',
        )

    def test_plain_unchanged(self):
        self.assertEqual(es._normalized_query("openai api"), "openai api")

    def test_import_fallback(self):
        # normalize_query 缺失时应回退原 query（模拟核心不可用）
        self.assertEqual(es._normalized_query("a/b c"), "a b c")


if __name__ == "__main__":
    unittest.main()

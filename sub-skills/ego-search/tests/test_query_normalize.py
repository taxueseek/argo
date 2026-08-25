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

    def test_core_scripts_path_is_argo_root(self):
        # 回归锚：相对路径必须指向 argo 根/scripts（parents[3]）。
        # 曾用 parents[2] 指向 sub-skills/scripts（不存在）→ normalize_query
        # 静默降级为 None，子技能归一化整条失效。
        self.assertTrue(es._CORE_SCRIPTS.exists(), es._CORE_SCRIPTS)
        self.assertEqual(es._CORE_SCRIPTS.name, "scripts")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""watch 噪声感知 diff 测试（纯函数，不联网）。

锁定判据：
  1. 日期/价格/计数/相对时间的变化 → noise，不惊动观察者
  2. 任何文字差异 → substantive
  3. 纯数字变化（3 种 → 5 种）故意判 substantive：宁可多报，不可漏报
  4. 围栏代码块内部的空行不切块
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from watch import _blocks, _trim_contents, diff_text


class TestDiffText(unittest.TestCase):
    def test_identical_is_none(self):
        self.assertEqual(diff_text("a\n\nb", "a\n\nb")["change_type"], "none")

    def test_price_change_is_noise(self):
        d = diff_text("价格 $19.99", "价格 $20.49")
        self.assertEqual(d["change_type"], "noise")
        self.assertIn("price", d["noise_categories"])

    def test_date_change_is_noise(self):
        d = diff_text("更新于 2026-10-27", "更新于 2026-10-28")
        self.assertEqual(d["change_type"], "noise")
        self.assertIn("date", d["noise_categories"])

    def test_cjk_date_change_is_noise(self):
        d = diff_text("发布于 2026年10月27日", "发布于 2026年10月28日")
        self.assertEqual(d["change_type"], "noise")

    def test_count_change_is_noise(self):
        d = diff_text("1,234 views", "1,235 views")
        self.assertEqual(d["change_type"], "noise")
        self.assertIn("count", d["noise_categories"])

    def test_relative_time_change_is_noise(self):
        d = diff_text("3 hours ago", "4 hours ago")
        self.assertEqual(d["change_type"], "noise")

    def test_substantive_word_change(self):
        d = diff_text("支持异步编程", "支持并发编程")
        self.assertEqual(d["change_type"], "substantive")
        self.assertEqual(d["noise_categories"], [])

    def test_bare_number_change_is_substantive(self):
        # 故意不把裸数字算噪声：语义可能真的变了
        d = diff_text("支持 3 种模式", "支持 5 种模式")
        self.assertEqual(d["change_type"], "substantive")

    def test_blocks_reported(self):
        d = diff_text("第一段\n\n旧的第二段", "第一段\n\n新的第二段")
        self.assertEqual(d["change_type"], "substantive")
        self.assertIn("新的第二段", d["added_blocks"])
        self.assertIn("旧的第二段", d["removed_blocks"])


class TestBlocks(unittest.TestCase):
    def test_blank_lines_split_blocks(self):
        self.assertEqual(len(_blocks("a\n\nb\n\nc")), 3)

    def test_fenced_code_blank_line_not_split(self):
        text = "前言\n\n```python\na = 1\n\nb = 2\n```\n\n后记"
        blocks = _blocks(text)
        self.assertEqual(len(blocks), 3)
        self.assertIn("a = 1", blocks[1])
        self.assertIn("b = 2", blocks[1])


class TestTrim(unittest.TestCase):
    def test_only_latest_content_kept(self):
        entry = {"snapshots": [{"content": "old"}, {"content": "new"}]}
        _trim_contents(entry)
        self.assertNotIn("content", entry["snapshots"][0])
        self.assertEqual(entry["snapshots"][1]["content"], "new")


if __name__ == "__main__":
    unittest.main()

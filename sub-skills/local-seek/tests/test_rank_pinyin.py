#!/usr/bin/env python3
"""local-seek 文件名相关性排序 + 拼音首字母单测（离线）

覆盖：
  1. pinyin_initials：中文→拼音首字母（pypinyin 优先 / GB2312 兜底）
  2. _fzf_score：fzf 式文件名评分（smart case / 连续 / 边界 / 全等）
  3. _file_pinyin_bonus：正向（中文→缩写）与反向（缩写→中文文件名）加分
  4. _rank_path_results：按相关性 + mtime 排序
  5. _looks_like_pinyin_abbrev：拼音缩写判定

运行：
  cd ~/.agents/skills/argo
  python3.14 -m pytest sub-skills/local-seek/tests/test_rank_pinyin.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import seek as s  # noqa: E402


class TestPinyin(unittest.TestCase):
    def test_zh_initials(self):
        self.assertEqual(s.pinyin_initials("新建夹"), "xjj")

    def test_non_cjk_kept(self):
        self.assertIn("report", s.pinyin_initials("report_v2.md"))

    def test_empty(self):
        self.assertEqual(s.pinyin_initials(""), "")


class TestFzfScore(unittest.TestCase):
    def test_match_boundary(self):
        self.assertGreater(s._fzf_score("新建夹.pdf", "新"), 0)

    def test_continuous(self):
        self.assertGreater(s._fzf_score("report_v2.md", "rep"), 0)

    def test_no_match(self):
        self.assertEqual(s._fzf_score("zzz.txt", "rep"), 0)

    def test_case_insensitive(self):
        self.assertGreater(s._fzf_score("Report_v2.md", "report"), 0)


class TestPinyinBonus(unittest.TestCase):
    def test_forward(self):
        self.assertEqual(s._file_pinyin_bonus("新建夹.pdf", "新建夹"), 50)

    def test_reverse(self):
        self.assertEqual(s._file_pinyin_bonus("新建夹.pdf", "xjj"), 8)

    def test_none(self):
        self.assertEqual(s._file_pinyin_bonus("other.txt", "xjj"), 0)


class TestRank(unittest.TestCase):
    def test_rank_top_by_relevance(self):
        r = [("a/zzz.txt", 0, ""), ("b/新建夹.pdf", 0, ""), ("c/report_v2.md", 0, "")]  # noqa: E741
        ranked = s._rank_path_results(r, "新")
        self.assertEqual(ranked[0][0], "b/新建夹.pdf")

    def test_abbrev_look(self):
        self.assertTrue(s._looks_like_pinyin_abbrev("xjj"))
        self.assertFalse(s._looks_like_pinyin_abbrev("新"))
        self.assertFalse(s._looks_like_pinyin_abbrev("report"))


if __name__ == "__main__":
    unittest.main()

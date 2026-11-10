#!/usr/bin/env python3
"""test_seek_morphology_and_cluster.py — 验证词形归一、首字母缩写增强、聚簇分块与 FTS5 瘦身门禁。"""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import seek as s
import seek_rank as r
import seek_fts as fts


class TestSeekEnhancements(unittest.TestCase):
    def test_term_stemming(self):
        """验证英文词干提取规则。"""
        self.assertEqual(r._term_stem("queries"), "query")
        self.assertEqual(r._term_stem("searched"), "search")
        self.assertEqual(r._term_stem("running"), "run")
        self.assertEqual(r._term_stem("caching"), "cach")
        # 短词与非纯字母不误伤
        self.assertEqual(r._term_stem("rg"), "rg")
        self.assertEqual(r._term_stem("api"), "api")

    def test_acronym_extraction(self):
        """验证驼峰与下划线命名首字母缩写提取。"""
        self.assertIn("hrc", r._acronyms_of("HedgedRacingCache.py"))
        self.assertIn("ckv", r._acronyms_of("cache_key_vdom.py"))
        self.assertIn("fts", r._acronyms_of("FTS5Index.py"))
        # 过于简短或非字母不生成误导缩写
        self.assertEqual(r._acronyms_of("a.py"), set())

    def test_cluster_results(self):
        """验证同一文件紧邻行的聚簇标记。"""
        results = [
            ("file_a.py", 10, "line 10"),
            ("file_a.py", 12, "line 12"),  # 距上一行 2，同一 cluster
            ("file_a.py", 35, "line 35"),  # 距上一行 23，新 cluster
            ("file_b.py", 36, "line 36"),  # 不同文件，新 cluster
            ("file_b.py", 38, "line 38"),  # 距上一行 2，同一 cluster
        ]
        clusters = s._cluster_results(results)
        self.assertEqual(len(clusters), 5)
        self.assertEqual(clusters[0], clusters[1])
        self.assertNotEqual(clusters[1], clusters[2])
        self.assertNotEqual(clusters[2], clusters[3])
        self.assertEqual(clusters[3], clusters[4])

    def test_fts5_slimming_filters(self):
        """验证 FTS5 瘦身门禁排除大容量与机器生成文件。"""
        self.assertIn(".jsonl", fts._NON_INDEXABLE_EXTS)
        self.assertIn(".csv", fts._NON_INDEXABLE_EXTS)
        self.assertIn(".npy", fts._NON_INDEXABLE_EXTS)
        self.assertIn(".sqlite", fts._NON_INDEXABLE_EXTS)
        # 核心源码与说明文档可索引
        self.assertIn(".py", fts._INDEXABLE_EXTS)
        self.assertIn(".md", fts._INDEXABLE_EXTS)
        self.assertIn(".rs", fts._INDEXABLE_EXTS)
        self.assertLessEqual(fts._MAX_BODY_BYTES, 128 * 1024)
        self.assertLessEqual(fts._MAX_FILE_BYTES, 256 * 1024)


if __name__ == "__main__":
    unittest.main()

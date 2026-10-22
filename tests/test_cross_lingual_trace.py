#!/usr/bin/env python3
"""test_cross_lingual_trace.py — 跨语言长程搜索与任务溯源回归测试。"""

import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from cross_lingual_trace import (
    assess_cross_lingual_need,
    build_cross_lingual_trace_queries,
    synthesize_cross_lingual_evidence,
    _extract_core_entity,
    _classify_source_nature,
)


class TestCrossLingualTrace(unittest.TestCase):
    def test_assess_need_tracing_intent(self):
        assess = assess_cross_lingual_need("Transformer 架构的起源与原始论文出处", results=[{"score": 0.5}])
        self.assertTrue(assess["needed"])
        self.assertTrue(assess["is_tracing"])
        self.assertIn("命中溯源意图关键词", assess["reasons"])

    def test_assess_need_sparse_results(self):
        assess = assess_cross_lingual_need("小众前沿协议", results=[{"score": 0.5}], min_sufficient_results=3)
        self.assertTrue(assess["needed"])
        self.assertTrue(assess["is_sparse"])

    def test_assess_need_no_authoritative(self):
        results = [
            {"score": 0.4, "authority": 0.4, "authority_level": 1},
            {"score": 0.4, "authority": 0.3, "authority_level": 1},
            {"score": 0.4, "authority": 0.2, "authority_level": 0},
        ]
        assess = assess_cross_lingual_need("某海外技术方案", results=results)
        self.assertTrue(assess["needed"])
        self.assertIn("原语言缺乏高置信度一手权威信源", assess["reasons"])

    def test_assess_not_needed_for_sufficient_general_query(self):
        results = [
            {"score": 0.85, "authority": 0.9, "authority_level": 4},
            {"score": 0.8, "authority": 0.85, "authority_level": 4},
            {"score": 0.75, "authority": 0.75, "authority_level": 3},
        ]
        assess = assess_cross_lingual_need("今天北京天气", results=results)
        self.assertFalse(assess["needed"])

    def test_extract_core_entity(self):
        self.assertEqual(_extract_core_entity("CVE-2024-3094 漏洞出处与最早披露"), "CVE-2024-3094")
        self.assertEqual(_extract_core_entity("React Server Components 最初草案"), "React Server Components")

    def test_build_trace_queries(self):
        queries = build_cross_lingual_trace_queries("CVE-2024-3094 溯源", target_lang="en")
        self.assertEqual(len(queries), 3)
        strategies = [q["strategy"] for q in queries]
        self.assertIn("origin_trace", strategies)
        self.assertIn("primary_spec", strategies)
        self.assertIn("cross_check", strategies)
        self.assertTrue(all(q["lang"] == "en" for q in queries))

    def test_classify_source_nature(self):
        self.assertEqual(_classify_source_nature("https://arxiv.org/abs/1706.03762", "Attention Is All You Need"), "academic")
        self.assertEqual(_classify_source_nature("https://github.com/torvalds/linux", "Linux kernel"), "repository")
        self.assertEqual(_classify_source_nature("https://www.rfc-editor.org/rfc/rfc9110.html", "HTTP Semantics"), "standard")
        self.assertEqual(_classify_source_nature("https://nvd.nist.gov/vuln/detail/CVE-2024-3094", "CVE Detail"), "standard")
        self.assertEqual(_classify_source_nature("https://example.com/blog", "Blog post"), "general")

    def test_synthesize_cross_lingual_evidence(self):
        primary = [
            {"title": "国内某媒体报道", "snippet": "2024年3月，社区报告了该漏洞。", "url": "https://news.cn/1"},
        ]
        cross = [
            {"title": "CVE-2024-3094 Official Advisory", "snippet": "First reported in 2024 by Andres Freund.", "url": "https://nvd.nist.gov/vuln/1", "source": "nvd"},
            {"title": "xz/liblzma repository analysis", "snippet": "Original commit back in 2023.", "url": "https://github.com/tukaani-project/xz", "source": "github"},
        ]
        res = synthesize_cross_lingual_evidence(primary, cross, "CVE-2024-3094 溯源")
        self.assertEqual(res["status"], "completed")
        self.assertGreater(len(res["primary_origins"]), 0)
        self.assertIn("https://nvd.nist.gov/vuln/1", [o["url"] for o in res["primary_origins"]])
        self.assertGreater(len(res["provenance_chain"]), 0)


if __name__ == "__main__":
    unittest.main()

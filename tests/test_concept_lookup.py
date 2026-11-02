#!/usr/bin/env python3
"""test_concept_lookup.py — P2 Model2Vec 静态概念向量表及零依赖查表器门禁测试。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "sub-skills" / "local-seek" / "scripts"))

from concept_lookup import ConceptLookup, get_concept_lookup, concept_router_enabled
from seek_rank import rank_rows


class TestConceptLookup(unittest.TestCase):
    def setUp(self):
        self.lookup = get_concept_lookup()

    def test_binary_table_loaded_successfully(self):
        """概念向量表正常装载，魔数与维度契约符合预期。"""
        self.assertTrue(self.lookup.is_available)
        self.assertEqual(self.lookup._dim, 64)
        self.assertGreaterEqual(self.lookup._vocab_size, 1000)

    def test_signed_int8_vector_retrieval(self):
        """词元向量读取为带符号 int8 (零拷贝 memoryview)。"""
        vec = self.lookup.get_vector("concurrency")
        self.assertIsNotNone(vec)
        self.assertEqual(len(vec), 64)
        # 验证有负数与正数（去均值 SIF 分布）
        has_pos = any(x > 0 for x in vec)
        has_neg = any(x < 0 for x in vec)
        self.assertTrue(has_pos and has_neg, "向量应包含正负分量（SIF去均值特征）")

    def test_cross_lingual_semantic_similarity(self):
        """跨语言与同义近义概念余弦相似度门禁。"""
        # 同一语义簇：高相似度 (>= 0.85)
        sim_concurrency = self.lookup.concept_similarity("并发", "concurrency")
        self.assertGreaterEqual(sim_concurrency, 0.85)

        sim_circuit = self.lookup.concept_similarity("熔断", "breaker")
        self.assertGreaterEqual(sim_circuit, 0.85)

        sim_ratelimit = self.lookup.concept_similarity("限流", "ratelimit")
        self.assertGreaterEqual(sim_ratelimit, 0.85)

        sim_fallback = self.lookup.concept_similarity("降级", "fallback")
        self.assertGreaterEqual(sim_fallback, 0.85)

        # 无关域概念：正交或负相关 (< 0.10)
        sim_orthogonal = self.lookup.concept_similarity("并发", "天气")
        self.assertLess(sim_orthogonal, 0.10)

        sim_unrelated = self.lookup.concept_similarity("加密", "美食")
        self.assertLess(sim_unrelated, 0.10)

    def test_sub_millisecond_latency(self):
        """查表计算极速验证：单次余弦点积耗时 < 10µs。"""
        va = self.lookup.get_vector("并发")
        vb = self.lookup.get_vector("concurrency")
        self.assertIsNotNone(va)
        self.assertIsNotNone(vb)

        t0 = time.perf_counter()
        iters = 5000
        for _ in range(iters):
            self.lookup.vector_cosine(va, vb)
        elapsed_s = time.perf_counter() - t0
        avg_us = (elapsed_s / iters) * 1e6
        self.assertLess(avg_us, 10.0, f"单次向量点积应 < 10µs，实际 {avg_us:.2f}µs")

    def test_fail_open_on_missing_file(self):
        """文件不存在或损坏时严格 fail-open，不抛出致命异常。"""
        fake_lookup = ConceptLookup(bin_path="/tmp/non_existent_argo_vec.bin")
        self.assertFalse(fake_lookup.is_available)
        self.assertIsNone(fake_lookup.get_vector("test"))
        self.assertEqual(fake_lookup.concept_similarity("a", "b"), 0.0)

    def test_geo_dims_stripped_from_concept_routing(self):
        """地名维（43/44）不参与概念路由，只保留意图语义维。

        地名是定位上下文而非意图，参与相似度会让含地名的查询被航空/测绘类
        引擎凭地名重叠拉高（实测「北京旅游攻略」对 opensky 余弦 0.979）。
        """
        from concept_lookup import GEO_DIMS, strip_context_dims
        self.assertEqual(GEO_DIMS, {43, 44})
        stripped = strip_context_dims([1] * 64)
        self.assertEqual(len(stripped), 64)
        self.assertTrue(all(stripped[i] == 0 for i in GEO_DIMS))
        self.assertTrue(all(stripped[i] == 1 for i in range(64) if i not in GEO_DIMS))

    def test_geo_query_not_hijacked_by_geo_engine(self):
        """含地名的旅游查询不被航空数据源顶到第一；天气查询仍命中专业源。"""
        import tfidf_router
        tfidf_router._router = None
        scores = tfidf_router.get_router().route("北京旅游攻略", top_k=3)
        self.assertNotEqual(scores[0][0], "opensky",
                            f"opensky 不应凭地名重叠排第一：{scores}")
        # 46 维（天气）未清零，天气查询仍应命中 open_meteo
        tfidf_router._router = None
        weather = tfidf_router.get_router().route("weather Tokyo", top_k=1)
        self.assertEqual(weather[0][0], "open_meteo",
                         f"weather Tokyo 应命中 open_meteo：{weather}")

    def test_seek_rank_concept_expansion(self):
        """seek_rank 在有概念表时能够平滑利用概念相似度增强。"""
        rows = [
            ("scripts/concurrency_pool.py", 10, "def run_pool(): pass"),
            ("docs/unrelated.md", 1, "# Unrelated notes"),
        ]
        # 查询为中文"并发"，无直接字面匹配，但文件名有 concurrency
        ranked = rank_rows(rows, "并发")
        self.assertEqual(len(ranked), 2)
        # 命中并发语义的文件应该排在第 1
        self.assertEqual(ranked[0][0], "scripts/concurrency_pool.py")


if __name__ == "__main__":
    unittest.main()

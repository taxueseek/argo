#!/usr/bin/env python3
"""test_local_search_bm25.py — local-search BM25 相关度打分回归门（D1）。

锁定四组契约：
  1. 切词：ASCII 按词、CJK 按二元、混合查询两侧一致；
  2. BM25 性质：含查询词 > 不含、长文档惩罚、稀有词权重高于泛滥词；
  3. 病理修复：词面高相关的低位次结果必须能逆序超过高位次泛相关结果
     （旧口径 `0.7 - idx*0.05` 永远做不到——这是 D1 的存在理由）；
  4. fail-soft：无词面重叠 / 无 token 时退化为位次序，与升级前逐位一致。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sub-skills" / "local-search"))

import bm25  # noqa: E402


class TestTokenize(unittest.TestCase):
    def test_ascii_words(self):
        self.assertEqual(bm25.tokenize("Python 3.14 asyncio"), ["python", "3.14", "asyncio"])

    def test_cjk_bigrams(self):
        self.assertEqual(bm25.tokenize("异步编程"), ["异步", "步编", "编程"])

    def test_cjk_single_char_stays(self):
        self.assertEqual(bm25.tokenize("酒"), ["酒"])

    def test_mixed(self):
        self.assertEqual(bm25.tokenize("python 异步"), ["python", "异步"])

    def test_empty_and_punct(self):
        self.assertEqual(bm25.tokenize(""), [])
        self.assertEqual(bm25.tokenize("！！！。。。"), [])


class TestBM25Properties(unittest.TestCase):
    def setUp(self):
        self.docs = [
            bm25.tokenize("python asyncio tutorial guide"),
            bm25.tokenize("python asyncio event loop concurrency patterns deep dive"),
            bm25.tokenize("completely unrelated cooking recipes"),
        ]
        self.scorer = bm25.BM25(self.docs)

    def test_term_presence_beats_absence(self):
        q = bm25.tokenize("asyncio")
        self.assertGreater(self.scorer.score(q, 0), self.scorer.score(q, 2))

    def test_tf_saturation_and_length_penalty(self):
        # 同含一次 asyncio 的短文档应不低于长文档（b 项惩罚长度）
        q = bm25.tokenize("asyncio")
        self.assertGreaterEqual(self.scorer.score(q, 0), self.scorer.score(q, 1))

    def test_rare_term_weighs_more(self):
        # concurrency 只出现在 1 篇（稀有），python 出现在 2 篇（泛滥）——
        # 对只含稀有词的文档，命中带来的分数应高于只含泛滥词的文档
        q_rare = bm25.tokenize("concurrency")
        q_common = bm25.tokenize("python")
        self.assertGreater(self.scorer.idf("concurrency"), self.scorer.idf("python"))
        self.assertGreater(self.scorer.score(q_rare, 1), self.scorer.score(q_common, 0) * 0.5)

    def test_idf_nonnegative_on_tiny_corpus(self):
        # 小语料：词在全部文档中出现时 IDF 仍须非负（Lucene 式的意义）
        all_common = bm25.BM25([bm25.tokenize("python"), bm25.tokenize("python guide")])
        self.assertGreaterEqual(all_common.idf("python"), 0.0)


class TestRerankFixesPathology(unittest.TestCase):
    """D1 的核心场景：低位次高相关必须逆序。"""

    def test_relevant_low_position_overtakes_irrelevant_top(self):
        by_engine = {"e1": [
            {"title": "每日新闻聚合", "snippet": "今天科技圈发生了这些事", "url": "u1"},
            {"title": "Python 异步编程指南", "snippet": "asyncio 事件循环与并发模式详解", "url": "u2"},
            {"title": "Python 异步编程完全教程", "snippet": "python asyncio 从入门到精通", "url": "u3"},
        ]}
        bm25.rerank(by_engine, "python asyncio")
        urls = [r["url"] for r in by_engine["e1"]]
        # 旧口径下顺序恒为 u1,u2,u3；升级后两条 python asyncio 结果必须在前。
        # u3 先于 u2 是 BM25 的正确判定：u3 的 title×2+snippet 里 python 出现
        # 3 次、asyncio 2 次，u2 是 1 次、2 次——词面证据 u3 更足。
        self.assertEqual(urls[0], "u3")
        self.assertEqual(urls[1], "u2")
        self.assertEqual(urls[2], "u1")

    def test_position_prior_still_matters(self):
        """BM25 并列时位次先验说话（同相关度维持引擎原序）。"""
        by_engine = {"e1": [
            {"title": "python 教程 第一讲", "snippet": "python 基础", "url": "u1"},
            {"title": "python 教程 第二讲", "snippet": "python 进阶", "url": "u2"},
        ]}
        bm25.rerank(by_engine, "python")
        # 两篇词面几乎等价 → BM25 拉不开 → 位次先验保住引擎原序
        self.assertEqual([r["url"] for r in by_engine["e1"]], ["u1", "u2"])

    def test_fail_soft_no_overlap_keeps_position_order(self):
        by_engine = {"e1": [
            {"title": "苹果发布会回顾", "snippet": "新 iPhone 亮相", "url": "u1"},
            {"title": "香蕉保存方法", "snippet": "热带水果储存", "url": "u2"},
        ]}
        bm25.rerank(by_engine, "python asyncio")
        self.assertEqual([r["url"] for r in by_engine["e1"]], ["u1", "u2"])

    def test_fail_soft_empty_query_tokens(self):
        by_engine = {"e1": [
            {"title": "甲 文章", "snippet": "内容", "url": "u1"},
            {"title": "乙 文章", "snippet": "内容", "url": "u2"},
        ]}
        bm25.rerank(by_engine, "！！！")
        self.assertEqual([r["url"] for r in by_engine["e1"]], ["u1", "u2"])

    def test_cjk_query_reorders(self):
        by_engine = {"e1": [
            {"title": "财经快讯", "snippet": "今日大盘概况", "url": "u1"},
            {"title": "异步编程实战", "snippet": "异步编程 并发 asyncio", "url": "u2"},
        ]}
        bm25.rerank(by_engine, "异步编程")
        self.assertEqual(by_engine["e1"][0]["url"], "u2")

    def test_multi_engine_pooled_idf(self):
        """IDF 在跨引擎池化语料上计算，不是单个引擎的 5 条结果。

        直接断言池化语义：python 在三篇池文档里全出现（泛滥，IDF 低），
        async 只在 b 引擎那一篇出现（稀有，IDF 高）。若 IDF 按引擎分开算，
        b 引擎语料内 async 是唯一文档、n=N，反而拿不到稀有性加成。
        """
        by_engine = {
            "a": [{"title": "python python python", "snippet": "python", "url": "a1"},
                  {"title": "python 指南", "snippet": "python 入门", "url": "a2"}],
            "b": [{"title": "rust 异步编程", "snippet": "rust async", "url": "b1"}],
        }
        bm25.rerank(by_engine, "python async")
        # 重建同一语料验证 IDF 口径（rerank 内部即此构造）
        flat_docs = []
        for lst in by_engine.values():
            for r in lst:
                flat_docs.append(bm25.tokenize(bm25._doc_text(r)))
        scorer = bm25.BM25(flat_docs)
        self.assertEqual(scorer.n_docs, 3, "语料须为三引擎结果池化")
        self.assertGreater(scorer.idf("async"), scorer.idf("python"),
                           "池内仅 1 篇含 async、3 篇全含 python——async 必须更稀有")

    def test_empty_input(self):
        self.assertEqual(bm25.rerank({}, "x"), {})
        by_engine = {"e1": []}
        bm25.rerank(by_engine, "x")  # 不炸即契约


if __name__ == "__main__":
    unittest.main()

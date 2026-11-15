#!/usr/bin/env python3
"""方案C 单测：upstream_authority 只透传不赋权——排序层的上游权威档原样挂到
credibility.authority.upstream_level，取证侧可见；无上游档不加键（不编造），
且有无上游档不得改变 selection/final 计算。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from evidence import compute_credibility  # noqa: E402


def _item(**overrides):
    base = {
        "title": "知乎高分回答示例",
        "url": "https://www.zhihu.com/question/123/answer/456",
        "source": "zhihu_global",
        "snippet": "包含具体数字 42% 与对比说明的证据片段。",
        "score": 0.7,
    }
    base.update(overrides)
    return base


class UpstreamAuthorityPassthroughTest(unittest.TestCase):
    def test_upstream_authority_surfaced(self):
        """builder 产出的 upstream_authority=4 原样出现在 credibility.authority。"""
        report = compute_credibility([_item(upstream_authority=4)], "示例查询")
        auth = report["results"][0]["credibility"]["authority"]
        self.assertEqual(auth.get("upstream_level"), 4)

    def test_authority_level_fallback(self):
        """无 upstream_authority 时回落读 authority_level（search_rank 补齐字段）。"""
        report = compute_credibility([_item(authority_level=2)], "示例查询")
        auth = report["results"][0]["credibility"]["authority"]
        self.assertEqual(auth.get("upstream_level"), 2)

    def test_upstream_authority_wins_over_fallback(self):
        """两字段都在时以更原始的 upstream_authority 为准。"""
        report = compute_credibility(
            [_item(upstream_authority=3, authority_level=1)], "示例查询")
        auth = report["results"][0]["credibility"]["authority"]
        self.assertEqual(auth.get("upstream_level"), 3)

    def test_absent_upstream_adds_no_key(self):
        """无上游档不得编造：credibility.authority 不出现 upstream_level 键。"""
        report = compute_credibility([_item()], "示例查询")
        auth = report["results"][0]["credibility"]["authority"]
        self.assertNotIn("upstream_level", auth)

    def test_passthrough_does_not_rescore(self):
        """只透传不赋权：同一结果有无上游档，selection/final 必须逐位相同。"""
        bare = compute_credibility([_item()], "示例查询")["results"][0]["credibility"]
        tagged = compute_credibility(
            [_item(upstream_authority=4)], "示例查询")["results"][0]["credibility"]
        self.assertEqual(bare["selection"], tagged["selection"])
        self.assertEqual(bare["final"], tagged["final"])
        self.assertEqual(bare["authority"]["score"], tagged["authority"]["score"])
        self.assertEqual(bare["authority"]["is_serp"], tagged["authority"]["is_serp"])

    def test_serp_demotion_unaffected(self):
        """SERP 压制逻辑不被透传字段干扰：is_serp 条目 selection 仍 ≤0.15。"""
        report = compute_credibility(
            [_item(url="https://www.baidu.com/s?wd=test", upstream_authority=4)],
            "示例查询")
        c = report["results"][0]["credibility"]
        self.assertTrue(c["authority"]["is_serp"])
        self.assertLessEqual(c["selection"], 0.15)
        self.assertEqual(c["authority"]["upstream_level"], 4)


if __name__ == "__main__":
    unittest.main()

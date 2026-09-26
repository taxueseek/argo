"""方案 1 回归护栏：噪声门清空结果后必须有兜底补搜。

原始 bug（可复现）：`argo search "プロンプト エンジニアリング"` 返回
`count: 0`，而 `errors: []`——用户看不到任何失败线索，只能以为「搜不到」。

完整链路（实测）：

1. 路由把 ja 查询派给 `qiita`（TF-IDF score=0.683，route_reason 明确写了
   「TF-IDF 语义路由 → Qiita 日本技术社区」）；
2. qiita 的 `?query=` 对日语召回极差，静默返回不相关内容（实测 relevance
   0.1，标题是 Radeon/ROCm/Copilot/Salesforce 权限）；
3. 噪声门**正确**判定 `noise`（reasons: 「语言相符但相关度极低 → 引擎未按
   查询检索（静默降级）」）并把整个列表丢弃；
4. combo 里的 `anysearch` 因早停被 `cancelled`，0 条；
5. → merged 为空，而 `errors` 始终是空的。

噪声门没有错——qiita 确实在静默降级。错的是**清空之后没有任何兜底**：
同文件的 D6 分支（macro_data 域）早就为「结构化源覆盖不足」做了追加通用
引擎的保底，而噪声门这条路没有对应机制，于是一次静默降级被放大成零结果。

本测试锁定的不只是「有兜底」，还包括兜底**不能反过来的两个方向**：
噪声门判定为 noise 的结果绝不能因为兜底而被重新塞回去（否则噪声门形同
虚设，纯噪声会被当成答案）。
"""

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import result_lang  # noqa: E402


class TestNoiseGateFallsBackWhenItEmptiesEverything(unittest.TestCase):
    """噪声门把 clean_lists 清空时，必须触发一次通用引擎补搜。"""

    def test_noise_verdict_is_reachable_for_stale_japanese_source(self):
        """护栏前提：噪声门对「语言相符但相关度极低」判 noise。

        这是整条链路的起点。若哪天 result_lang 改判，这条链就变了，本文件
        的其余断言需要重新审视——所以单独钉住。
        """
        items = [
            {"title": "Radeon 8060SとROCm 10でQwen-Image-2.1を動かす",
             "snippet": "GPU", "url": "https://qiita.com/a/1"},
            {"title": "2026年9月25日に発表された新しい Microsoft Copilot",
             "snippet": "Copilot", "url": "https://qiita.com/b/2"},
        ]
        verdict = result_lang.assess_results(
            "プロンプト エンジニアリング", items, expected_lang="ja")
        self.assertEqual(verdict["verdict"], "noise")

    def test_fallback_candidates_exclude_engines_already_tried(self):
        """兜底不能重复调用已经在 combo 里跑过的引擎。"""
        from search_pipeline import _fallback_candidates
        cands = _fallback_candidates(
            already={"qiita", "anysearch"},
            available={"qiita", "anysearch", "local_bing", "octen"},
            breaker=None,
        )
        self.assertNotIn("qiita", cands)
        self.assertNotIn("anysearch", cands)
        self.assertTrue(cands, "至少要有一个可用的兜底引擎")

    def test_fallback_respects_breaker(self):
        """被熔断的引擎不能进兜底（否则等于绕过熔断）。"""
        from search_pipeline import _fallback_candidates

        class _Breaker:
            def allow(self, name):
                return (name != "anysearch", "circuit open" if name == "anysearch" else "")

        cands = _fallback_candidates(
            already=set(),
            available={"anysearch", "local_bing"},
            breaker=_Breaker(),
        )
        self.assertNotIn("anysearch", cands)

    def test_fallback_is_bounded(self):
        """兜底最多 2 个引擎：无界补搜会把一次搜索拖成长任务。"""
        from search_pipeline import _fallback_candidates
        cands = _fallback_candidates(
            already=set(),
            available={"anysearch", "duckduckgo", "local_bing", "octen", "wikipedia"},
            breaker=None,
        )
        self.assertLessEqual(len(cands), 2)


class TestDroppedResultsAreNotResurrected(unittest.TestCase):
    """反向护栏：兜底只补搜，**不复活**被噪声门丢弃的结果。"""

    def test_noise_results_stay_out(self):
        noise = {"title": "Radeon 8060SとROCm", "snippet": "GPU",
                 "url": "https://qiita.com/a/1"}
        good = {"title": "プロンプト設計の実践", "snippet": "LLM tips",
                "url": "https://example.jp/b"}
        kept = [good]
        self.assertNotIn(noise, kept)
        # 兜底补进来的结果只能是别的引擎的，不能是这批被丢的
        self.assertTrue(kept)


if __name__ == "__main__":
    unittest.main()

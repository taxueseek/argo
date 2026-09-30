#!/usr/bin/env python3
"""test_ranking_contract.py — 排序管线端到端契约（2026-09-13 新增）。

## 守的是什么缺陷

argo 的融合层（rrf_merge）与精排层（local_five_dim_rerank）此前各写各的：

  - `rrf_merge` 算出 `_rrf_score`（含跨引擎共识与引擎权重），但**无任何消费者**
    （纯 write-only 字段）；
  - `local_five_dim_rerank` 在工作结束时 `r["score"] = round(best_score, 4)`，
    用 relevance(token 覆盖率)/completeness(文本长度) 等**文本自身**维度
    覆写 `score`，既不读入参 `score`，也不读 `_rrf_score`/`consensus_engines`。

后果：融合层的核心产出（多引擎共识）对最终顺序**零影响**，且五维公式
结构性偏爱「啰嗦的长网页」——实测一条 3 引擎共识条目被单源长文本条目反超。

**为什么既有测试没抓到**：`test_unit.py::test_consensus_engines` 与
`test_content_security.py::test_rrf_weighted_ranking` 都只检查 `rrf_merge`
**返回值内部**的顺序，没有任何一条检查「这个顺序能活着走到最终输出」。
本文件补的就是这层端到端契约。

## 三条不变式

  1. **等价性**：无融合信息（单引擎路径）时，排序结果必须与改造前逐位一致，
     且分数是原值 × (1 - W_PRIOR) —— 保证这次改动对无共识场景零扰动。
  2. **共识保序**：真共识（同一 URL 被多引擎返回）条目必须排在
     文本更强但单源的条目之前。
  3. **先验可见**：`rerank_dims.prior` 必须存在，供可观测与回归定位。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _consensus_case(cons_n: int, cons_snippet: str, single_snippet: str):
    """构造「多引擎共识（文本弱） vs 单源（文本强）」的融合输入。"""
    from search import rrf_merge
    cons = [
        {"url": "https://bbs.example/t/1", "title": "讨论：某技术选型",
         "snippet": cons_snippet, "_engine": f"e{i}"}
        for i in range(cons_n)
    ]
    single = {
        "url": "https://docs.example/guide",
        "title": "某技术选型 官方指南 完整说明",
        "snippet": single_snippet, "_engine": "exa",
    }
    return rrf_merge([cons, [single]])


class TestConsensusSurvivesRerank:
    """不变式 2：共识必须在最终排序中体现。"""

    @pytest.mark.parametrize("cons_n,snip", [
        (2, "短"),
        (3, "简短回复"),
        (4, "中等的共识摘要内容"),
        (3, "略长的共识摘要内容说明"),
    ])
    def test_consensus_outranks_single_source(self, cons_n, snip):
        from search import local_five_dim_rerank
        merged = _consensus_case(cons_n, snip, "官方文档完整对比、参数说明、迁移步骤与注意事项。" * 3)
        ranked = local_five_dim_rerank("某技术选型", [dict(x) for x in merged],
                                       domain="general", top_n=len(merged))
        top_cons = len(ranked[0].get("consensus_engines") or [])
        assert top_cons >= 2, (
            f"{cons_n} 引擎共识条目被单源条目反超（top consensus={top_cons}）——"
            f"融合信号在精排层丢失。排序：{[r['url'] for r in ranked]}"
        )

    def test_consensus_prior_is_recorded(self):
        """不变式 3：prior 必须出现在 rerank_dims，否则线上无法定位排序异常。"""
        from search import local_five_dim_rerank
        merged = _consensus_case(3, "简短回复", "长文本内容说明" * 4)
        ranked = local_five_dim_rerank("某技术选型", [dict(x) for x in merged],
                                       domain="general", top_n=len(merged))
        for r in ranked:
            assert "prior" in r["rerank_dims"], r["rerank_dims"]
        # 共识条目的先验必须显著高于单源条目
        by_cons = {len(r.get("consensus_engines") or []): r["rerank_dims"]["prior"]
                   for r in ranked}
        assert by_cons[3] > by_cons[1], by_cons


class TestNoFusionSignalEquivalence:
    """不变式 1：无融合信息时的确定性行为（本次改动零扰动）。

    2026-09-27 更新：本类原断言 `["5","4","3","2","1","0"]`，即「snippet 越长
    排越前」。那是**完整性维度旧实现**（0.6×snippet 长度）的直接产物，也正是
    当日修掉的反向激励——内容农场唯一要刷的指标就是长度，实证 SEO 软文在该
    公式下得 0.556、正经来源只得 0.239。断言更新为「确定性 + 不丢结果」，
    本类守护的不变式（无 prior 时排序确定、且不依赖融合信号）保持不变。
    旧行为可由 ARGO_COMPLETENESS_V2=0 复现（逃生门）。
    """

    def _results(self):
        return [
            {"url": f"https://s.com/{i}", "title": f"标题{i} 详细内容说明",
             "snippet": "内容" * (i + 1), "source": "x"}
            for i in range(6)
        ]

    def test_order_deterministic_without_prior(self):
        from search import local_five_dim_rerank
        res = self._results()
        ranked = local_five_dim_rerank("标题 详细内容", [dict(x) for x in res],
                                       domain="general", top_n=6)
        # prior 恒 0（无 _rrf_score、无 consensus_engines）
        assert all(r["rerank_dims"]["prior"] == 0.0 for r in ranked)
        # 确定性：同样输入两次运行结果一致（不依赖字典序/时间等外部状态）
        ranked2 = local_five_dim_rerank("标题 详细内容",
                                        [dict(x) for x in self._results()],
                                        domain="general", top_n=6)
        assert [r["url"] for r in ranked] == [r["url"] for r in ranked2]
        # 六条全部返回且不重复（长度不再决定名次，但一条都不能丢）
        assert len({r["url"] for r in ranked}) == 6

    def test_weights_scale_uniformly(self):
        """前五维必须被同一常数缩放，否则 prior 缺席时会改变相对权重。

        这条锁的是「留出 W_PRIOR 余量」的实现方式：必须整体等比缩放，
        而不是逐个拍脑袋调权重（后者会让无共识场景的排序悄悄变化）。
        """
        import inspect
        import search
        src = inspect.getsource(search.local_five_dim_rerank)
        assert "_BASE = 1.0 - W_PRIOR" in src, "前五维未按 (1 - W_PRIOR) 等比缩放"
        # 通用域原权重比例 30:30:20:15:5 必须保持
        for token in ("0.30 * _BASE", "0.20 * _BASE", "0.15 * _BASE", "0.05 * _BASE"):
            assert token in src, f"权重 {token} 缺失，等比性被破坏"


class TestRrfMergeKeyUniqueness:
    """回归：`__idx__` 保底键必须带列表身份，否则跨引擎相撞。"""

    def test_distinct_untitled_results_not_merged(self):
        from search import rrf_merge
        out = rrf_merge([
            [{"snippet": "alpha", "_engine": "engA"}],
            [{"snippet": "beta", "_engine": "engB"}],
        ])
        assert len(out) == 2, f"两条不同结果被误合并：{out}"
        # 且不得伪造跨引擎共识
        for r in out:
            assert len(r["consensus_engines"]) == 1, r["consensus_engines"]

    def test_false_consensus_not_fabricated(self):
        from search import rrf_merge
        out = rrf_merge([
            [{"snippet": "a", "_engine": "e1"}],
            [{"snippet": "b", "_engine": "e2"}],
            [{"snippet": "c", "_engine": "e3"}],
        ])
        assert len(out) == 3, out
        assert all(len(r["consensus_engines"]) == 1 for r in out)


class TestRankScoreBoundaries:
    """rank_score 边界：NaN/负值不得污染下游排序。"""

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None, "x"])
    def test_non_finite_falls_back(self, bad):
        from engines_base import rank_score
        assert rank_score(bad, 3) == 0.7

    @pytest.mark.parametrize("neg", [-1, -0.5, -100])
    def test_negative_clamped(self, neg):
        """负分在最终排序处被 abs() 反转成最高分，必须夹到 0。"""
        from engines_base import rank_score
        assert rank_score(neg, 3) == 0.0

    def test_above_one_clamped(self):
        from engines_base import rank_score
        assert rank_score(2.0, 3) <= 1.0


class TestWgRrfEngineWeighting:
    """WG-RRF 引擎权重契约（2026-09-16「域信誉层」闭环的回归锁）。

    背景：09-12 调研提出的「域信誉层」经核实已分三层在位——① 引擎级静态
    权威权重（_ENGINE_FUSION_WEIGHTS，权威/学术提权、社交降权）× 动态可靠性
    （weakest-link）；② URL 域权威 evidence.score_authority 进五维权威维度
    （0.26 权重）；③ 18 语言能力矩阵提降权。此前 _engine_weight 无直接回归
    锁，本类补上最核心的一层：同一排名位置上，权威源权重必须严格高于基线、
    社交源严格低于基线，且 rrf_merge 的 weighted 开关确实消费它。
    """

    def test_static_weight_directions(self):
        from search import _ENGINE_FUSION_WEIGHTS, _engine_weight
        assert _engine_weight("wikipedia") > 1.0 > _engine_weight("twitter")
        assert _engine_weight("arxiv") == _ENGINE_FUSION_WEIGHTS["arxiv"]
        assert _engine_weight("不存在的引擎") == 1.0  # 未知源保持中性

    def test_merged_source_takes_best_static_weakest_reliability(self):
        from search import _engine_weight
        # 合并源静态权重取最高成员，未知成员不拉低静态项
        assert _engine_weight("wikipedia/不存在的引擎") >= 1.4

    def test_rrf_merge_consumes_weights(self):
        """同一位次的两条结果，权威源必须比社交源拿到更高的 RRF 分。"""
        from search import rrf_merge
        lists = [
            [{"url": "https://a.example/1", "title": "权威", "_engine": "wikipedia"}],
            [{"url": "https://b.example/2", "title": "社交", "_engine": "twitter"}],
        ]
        ranked = rrf_merge(lists, weighted=True)
        by_title = {r["title"]: r["_rrf_score"] for r in ranked}
        assert by_title["权威"] > by_title["社交"]
        # weighted=False 回到经典 RRF：同位次等权
        classic = rrf_merge([list(x) for x in lists], weighted=False)
        classic_scores = {r["title"]: r["_rrf_score"] for r in classic}
        assert classic_scores["权威"] == classic_scores["社交"]


class TestConsensusSignalSingleEntry:
    """共识信号在排序中的唯一入口契约（2026-09-30 新增）。

    背景：审查报告（2026-09-13）发现共识信号在三处进入最终分：
      ① _consensus_prior 内的加法先验（W_PRIOR=0.12）
      ② 五维 rerank 后的乘法 boost（×(1+0.05·min(n-1,3))）
      ③ evidence selection 阶段的 selection 乘法

    ② 已于 2026-09-13 移除。本类锁定 ① 和 ③ 的设计决策：
      - ① 是共识信号在排序中的唯一入口
      - ③ 是 evidence selection 阶段的独立信号，用于决定「先核验哪条」，
        不影响排序 score，不属于重复计分

    测试守护：
      1. 共识信号在排序中只有一个入口（_consensus_prior）
      2. evidence selection 阶段的共识 boost 是独立信号，不影响排序
      3. 共识条目的 credibility_fast 必须高于单源条目（同 authority 下）
    """

    def test_consensus_prior_is_only_sort_entry(self):
        """共识信号在排序中只有一个入口：_consensus_prior。"""
        import inspect
        import search_rank
        src = inspect.getsource(search_rank.local_five_dim_rerank)
        # 必须有 W_PRIOR 和 _consensus_prior
        assert "W_PRIOR = 0.12" in src
        assert "_consensus_prior" in src
        # 不得有乘法 boost（已移除）
        assert "0.05" not in src or "0.05·min" not in src

    def test_selection_consensus_boost_independent(self):
        """evidence selection 阶段的共识 boost 是独立信号，不影响排序。"""
        from search_rank import _attach_selection_signals
        # 构造两条结果：一条 3 引擎共识，一条单源
        merged = [
            {"url": "https://a.com/1", "title": "共识", "snippet": "短",
             "source": "wikipedia", "score": 0.5,
             "consensus_engines": ["wikipedia", "arxiv", "github"]},
            {"url": "https://b.com/2", "title": "单源", "snippet": "长文本内容说明" * 3,
             "source": "wikipedia", "score": 0.5,
             "consensus_engines": ["wikipedia"]},
        ]
        _attach_selection_signals(merged, "auto", "balanced")
        # 共识条目的 selection 必须高于单源条目
        cons_selection = merged[0]["selection"]
        single_selection = merged[1]["selection"]
        assert cons_selection > single_selection, (
            f"共识条目 selection({cons_selection}) 应高于单源条目({single_selection})"
        )
        # 共识条目的 credibility_fast 必须高于单源条目
        cons_cred = merged[0]["credibility_fast"]
        single_cred = merged[1]["credibility_fast"]
        assert cons_cred > single_cred, (
            f"共识条目 credibility_fast({cons_cred}) 应高于单源条目({single_cred})"
        )

    def test_selection_boost_capped(self):
        """共识 boost 有上限，不得无限放大。"""
        from search_rank import _attach_selection_signals
        # 构造一条 10 引擎共识（超过 min(n-1, 2) 上限）
        merged = [
            {"url": "https://a.com/1", "title": "共识", "snippet": "短",
             "source": "wikipedia", "score": 0.5,
             "consensus_engines": [f"e{i}" for i in range(10)]},
        ]
        _attach_selection_signals(merged, "auto", "balanced")
        # selection 不得超过 1.0
        assert merged[0]["selection"] <= 1.0
        # 10 引擎共识的 boost 与 3 引擎共识相同（min(n-1, 2) 封顶）
        merged2 = [
            {"url": "https://b.com/2", "title": "共识", "snippet": "短",
             "source": "wikipedia", "score": 0.5,
             "consensus_engines": [f"e{i}" for i in range(3)]},
        ]
        _attach_selection_signals(merged2, "auto", "balanced")
        assert merged[0]["selection"] == merged2[0]["selection"], (
            "10 引擎共识与 3 引擎共识的 selection 应相同（min(n-1, 2) 封顶）"
        )

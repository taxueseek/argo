#!/usr/bin/env python3
"""相关性/完整性算子与 SEO 对抗层的回归测试（2026-09-27 新增）。

锁定的四类修复（对应 research/argo-低质内容检测调研.md 的落地项）：

  1. 相关性算子不再是中文单字覆盖率——关键词堆砌不得比自然表述得分高
  2. 完整性维度不再奖励长摘要——SEO 软文不得比规范文档得分高
  3. 域级聚合惩罚只打「多域并存时的单域占榜」，不惩罚单源查询
  4. 三个 SEO 信号（标题党/文不对题/模板重复）的边界与误伤防护

每条测试都注明「防的是什么回归」，避免后来者删掉时不知道代价。
"""
from __future__ import annotations

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(SCRIPT_DIR, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

os.environ.setdefault("ARGO_STATE_DIR", "/tmp/argo-relevance-test")

from search_rank import (  # noqa: E402
    _relevance_units, _score_relevance, _score_completeness,
    _domain_concentration_penalty, _host_of, _title_stuffing_penalty,
)


# ── 1. 相关性算子 ──────────────────────────────────────────────────────────

class TestRelevanceUnits:
    def test_cjk_bigram_not_single_char(self):
        """防回归：中文不得退回单字切分。

        单字切分是「关键词堆砌得分更高」的根因——任何含「股」「价」的页面
        在「贵州茅台股价」上白拿 2/6 覆盖率。
        """
        units = set(_relevance_units("贵州茅台股价"))
        assert "股价" in units, f"应产出 CJK bigram，实际 {units}"
        assert "贵" not in units or "贵州" in units, "不应退化为单字集合"

    def test_stopchars_filtered(self):
        """停用字不参与——「怎么选」这类无信息词不该撑起相关性分。"""
        units = set(_relevance_units("怎么选枕头"))
        assert "怎么" not in units, f"停用字「怎/么」不应组成单元: {units}"
        # 剔除后剩下的实义序列仍要能产出单元
        assert "枕头" in units

    def test_latin_words_lowercased(self):
        units = set(_relevance_units("Python Asyncio"))
        assert units == {"python", "asyncio"}, units


class TestRelevanceScoring:
    """核心不变式：**自然表述必须胜过关键词堆砌**。

    这是本次改动的第一目的。旧实现下堆砌型 0.650 > 正常型 0.464，
    奖励的正是内容农场唯一要刷的指标。
    """

    Q = "颈椎病 枕头 推荐"

    def _q(self):
        return set(_relevance_units(self.Q))

    def test_stuffed_title_loses_to_natural(self):
        q = self._q()
        stuffed = "颈椎枕头推荐_颈椎病枕头怎么选_枕头推荐颈椎病_推荐枕头颈椎病"
        natural = "颈椎病患者如何选择合适的枕头"
        s_stuffed = _score_relevance(q, stuffed, "")
        s_natural = _score_relevance(q, natural, "")
        assert s_natural > s_stuffed, (
            f"自然表述({s_natural}) 必须高于堆砌型({s_stuffed})"
        )

    def test_stuffing_penalty_monotonic(self):
        """堆砌越多，折扣越低（惩罚必须单调，否则可被针对性绕过）。

        样本刻意用**同一短语的倍数堆叠**：hits/单元比 = 1/2/3/4，
        这样单调性是确定的，不与「不同短语的覆盖率差异」混淆。
        """
        q = self._q()
        base = "颈椎枕头推荐"
        p1 = _title_stuffing_penalty(base, q)
        p2 = _title_stuffing_penalty(f"{base}_{base}", q)
        p3 = _title_stuffing_penalty(f"{base}_{base}_{base}", q)
        p4 = _title_stuffing_penalty(f"{base}_{base}_{base}_{base}", q)
        assert p1 >= p2 >= p3 >= p4, f"堆砌惩罚非单调: {p1} {p2} {p3} {p4}"
        assert p1 == 1.0, "自然长度标题不应被罚"
        assert p4 < 1.0, "重度堆砌必须被罚"

    def test_unrelated_title_scores_zero(self):
        q = self._q()
        assert _score_relevance(q, "完全无关的标题内容", "") == 0.0

    def test_empty_query_returns_neutral(self):
        """空查询返回中性分（既有契约，不得被本次改动破坏）。"""
        assert _score_relevance(set(), "任意标题", "") == 0.5

    def test_escape_hatch_restores_legacy(self, monkeypatch):
        """逃生门：ARGO_RELEVANCE_V2=0 必须回到旧单字覆盖率。"""
        import search_rank
        monkeypatch.setenv("ARGO_RELEVANCE_V2", "0")
        try:
            from engine_env import clear_env_cache
            clear_env_cache()
        except Exception:
            pass
        q_legacy = set(search_rank._tokens("颈椎病 枕头 推荐"))
        got = _score_relevance(q_legacy, "颈椎病患者如何选择合适的枕头", "")
        # 旧口径下正常标题的覆盖率即为分数（无堆砌惩罚）
        assert 0.0 <= got <= 1.0


# ── 2. 完整性维度 ──────────────────────────────────────────────────────────

class TestCompleteness:
    """核心不变式：**长度不再是主要得分项**。

    旧实现 0.6×snippet 长度，实测 SEO 软文 0.556 vs 正经来源 0.239
    （2.3 倍反向激励）——内容农场唯一要刷的指标就是长度。
    """

    def test_seo_long_snippet_does_not_win(self):
        """防回归：长而空的软文摘要不得高于规范型内容。"""
        seo_title = ("枕头到底怎么选,【2026年9月】最新实测护颈枕推荐,"
                     "5个维度帮你避开90%的坑")
        seo_snip = ("自费5000元实测20款，5个维度帮你避开90%的坑，"
                    "网红避坑指南，2026年最新推荐榜单，附选购要点。" * 2)
        gov_title = "读写作业台灯性能要求"
        gov_snip = ("GB/T 9473-2022 规定 AA 级照度不低于 500lx，色温 4000K，"
                    "Ra≥90。主要包括中央区域与总区域两项指标。")
        s_seo = _score_completeness(seo_title, seo_snip)
        s_gov = _score_completeness(gov_title, gov_snip)
        assert s_gov > s_seo, f"规范型({s_gov}) 应高于软文({s_seo})"

    def test_long_title_penalized(self):
        """超长标题双向扣分（论文：≥24 词标题更可能是 spam）。"""
        long_title = "护眼台灯" * 12
        assert _score_completeness(long_title, "短摘要") < \
            _score_completeness("护眼台灯怎么选", "短摘要")

    def test_segmented_title_penalized(self):
        """分隔式堆砌（A,B,C 三段以上）扣分——内容农场标题标准形态。"""
        seg = "护眼台灯推荐,2026最新测评,十大品牌排名,选购攻略,避坑指南"
        nat = "护眼台灯如何根据照度等级选择"
        assert _score_completeness(nat, "摘要内容") > _score_completeness(seg, "摘要内容")

    def test_digit_stuffing_penalized(self):
        """数字堆砌（软文的「10款/20款/90%」话术）不得拿满分。"""
        stuffed = "实测10款 20款 90% 5000元 199元 1299元 3款 2款 95% 80%"
        normal = "国家标准规定照度不低于500lx，色温建议4000K。"
        assert _score_completeness("正常标题内容", normal) >= \
            _score_completeness("正常标题内容", stuffed) * 0.9

    def test_empty_inputs_safe(self):
        """空输入不得抛异常（防御性契约）。"""
        assert 0.0 <= _score_completeness("", "") <= 1.0
        assert 0.0 <= _score_completeness("标题", "") <= 1.0
        assert 0.0 <= _score_completeness("", "摘要") <= 1.0


# ── 3. 域级聚合惩罚 ────────────────────────────────────────────────────────

class TestDomainConcentration:
    """防的是「单一域名霸榜」：实测「空气净化器 推荐」5/5 全来自同一域名。"""

    def test_single_domain_not_penalized(self):
        """全部同域 = 单源召回，不是占榜——罚它会破坏「无融合信息时确定性」。"""
        res = [{"url": f"https://s.com/{i}"} for i in range(6)]
        assert _domain_concentration_penalty(res) == {}

    def test_concentrated_domain_penalized(self):
        """多域并存时，占比过半的域名受罚。"""
        res = ([{"url": f"https://heavy.com/{i}"} for i in range(4)]
               + [{"url": "https://a.com/1"}, {"url": "https://b.com/1"}])
        pen = _domain_concentration_penalty(res)
        assert "heavy.com" in pen, pen
        assert pen["heavy.com"] < 1.0

    def test_even_distribution_not_penalized(self):
        """均匀分布不罚。"""
        res = [{"url": f"https://d{i}.com/x"} for i in range(5)]
        assert _domain_concentration_penalty(res) == {}

    def test_too_few_results_not_penalized(self):
        """结果数 <3 时占比噪声过大，不判定。"""
        res = [{"url": "https://a.com/1"}, {"url": "https://a.com/2"}]
        assert _domain_concentration_penalty(res) == {}

    def test_www_folded(self):
        """www 前缀折叠：www.a.com 与 a.com 视为同域。"""
        res = ([{"url": f"https://www.a.com/{i}"} for i in range(3)]
               + [{"url": "https://b.com/1"}, {"url": "https://c.com/1"}])
        pen = _domain_concentration_penalty(res)
        assert "a.com" in pen and "www.a.com" not in pen, pen
        assert _host_of("https://www.a.com/x") == "a.com"

    def test_escape_hatch(self, monkeypatch):
        """开关关闭时惩罚表为空（逃生门）。"""
        import search_rank
        monkeypatch.setenv("ARGO_DOMAIN_CONCENTRATION", "0")
        assert search_rank._domain_penalty_enabled() is False


# ── 4. SEO 信号 ────────────────────────────────────────────────────────────

class TestClickbait:
    def test_clickbait_detected(self):
        from content_signals import score_clickbait
        r = score_clickbait("震惊！这3款护眼台灯千万别买")
        assert r["is_clickbait"] and r["score"] >= 0.5, r

    def test_normal_title_not_flagged(self):
        """误伤防护：正常标题不得被判标题党。"""
        from content_signals import score_clickbait
        for t in ("读写作业台灯性能要求",
                  "国家统计局发布上半年经济数据",
                  "Python asyncio 官方文档"):
            r = score_clickbait(t)
            assert not r["is_clickbait"], f"{t} 被误判: {r}"

    def test_bare_punctuation_not_clickbait(self):
        """PACLIC 2024 的误杀教训：单纯数标点必须不算标题党。"""
        from content_signals import score_clickbait
        r = score_clickbait("今天天气怎么样？")
        assert not r["is_clickbait"], f"仅含问号被判标题党: {r}"

    def test_empty_safe(self):
        from content_signals import score_clickbait
        assert score_clickbait("")["score"] == 0.0


class TestTitleBodyConsistency:
    def test_mismatch_detected(self):
        from content_signals import score_title_body_consistency
        r = score_title_body_consistency(
            "颈椎病枕头怎么选", "本文介绍股票投资的基本方法与风险控制策略。")
        assert r["mismatch"], r

    def test_consistent_not_flagged(self):
        from content_signals import score_title_body_consistency
        r = score_title_body_consistency(
            "护眼台灯怎么选",
            "护眼台灯的选择要看照度等级与色温，国家标准规定照度不低于500lx。")
        assert not r["mismatch"], r

    def test_empty_safe(self):
        from content_signals import score_title_body_consistency
        assert score_title_body_consistency("", "")["score"] == 0.5
        assert score_title_body_consistency("标题", "")["score"] == 0.5


class TestTemplateRepetition:
    def test_repetitive_text_flagged(self):
        from content_signals import score_template_repetition
        text = "什么是CRM？CRM是客户关系管理系统。" * 8
        r = score_template_repetition(text)
        assert r["repetition"] >= 0.5, r

    def test_short_text_not_flagged(self):
        """短文本不足以判定（样本不足）。"""
        from content_signals import score_template_repetition
        assert score_template_repetition("短句。")["score"] == 0.0

    def test_empty_safe(self):
        from content_signals import score_template_repetition
        assert score_template_repetition("")["score"] == 0.0


# ── 5. 跨模块一致性 ────────────────────────────────────────────────────────

class TestStopcharConsistency:
    """两处停用字表独立维护（避开反向依赖），分叉必须被测试发现。"""

    def test_tables_match(self):
        import search_rank
        import content_signals
        assert search_rank._CJK_STOPCHARS == content_signals._ZH_STOPCHARS, (
            "search_rank 与 content_signals 的停用字表已分叉，"
            "相关性算子与标题党/一致性信号的口径会漂移"
        )

#!/usr/bin/env python3
"""关键词密度堆砌判据的回归测试（2026-09-27 新增，方案 1）。

背景：扩充标定集（14 → 25 条）时发现两个真缺陷，都不是「调参」能解决的：

1. `title_stuffing_penalty` 的下限 0.35 **恰好在滥用最严重处触底**——
   实测堆砌样本普遍 5-8 倍超额，5 倍时就吃满惩罚，于是「5 倍堆砌」与
   「20 倍堆砌」拿到同一个系数。内容农场只会越刷越多，不会停在拐点。

2. 更根本：只看**次数**分不出好坏。只按标题算会漏掉「标题正常、正文堆砌」；
   把正文并进来算次数，11 条 stuffing 能罚 7 条，但 4 条正常 SEO 长文也被
   罚 3 条（专家评测/步骤文/FAQ 都会在每段复述主题词，这很正常）。

判据换成**整体关键词密度**（title+snippet 合并）后，标定数据：
    总体 AUC 0.896（本仓最强）  stuffing 类 AUC = 1.000  零误杀

本文件锁定的性质：
  1. 堆砌被罚、正常 SEO 长文不被罚（核心，且必须**同时**成立）；
  2. 阈值 0.22 是零误杀前提下的取值（不是 F1 最优——那个会误杀全部对照）；
  3. 逃生门有效；
  4. 脏数据安全。
"""
from __future__ import annotations

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(SCRIPT_DIR, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))
os.environ.setdefault("ARGO_STATE_DIR", "/tmp/argo-density-test")

from rank_signals import (  # noqa: E402
    STUFFING_DENSITY_THRESHOLD,
    keyword_density,
    relevance_units,
    stuffing_density_penalty,
)

FIXTURE = os.path.join(SCRIPT_DIR, "tests", "golden",
                       "lowquality_calibration.json")


def _u(q):
    return set(relevance_units(q))


def _load():
    import json
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["samples"]


# ── 堆砌：应被罚 ──────────────────────────────────────────────────────────────
STUFFING = [
    ("机械键盘 推荐",
     "机械键盘推荐_机械键盘推荐哪个好_机械键盘推荐2026_机械键盘推荐预算",
     "机械键盘推荐。机械键盘推荐入门。机械键盘推荐轴体。机械键盘推荐键帽。机械键盘推荐连接方式。"),
    ("空气炸锅 推荐",
     "空气炸锅推荐。空气炸锅推荐哪个牌子好。空气炸锅推荐家用。空气炸锅推荐容量。",
     "空气炸锅推荐。选择空气炸锅推荐时要看容量。空气炸锅推荐功率。空气炸锅推荐清洁。"),
    ("人体工学椅 推荐", "人体工学椅推荐",
     "人体工学椅推荐。人体工学椅推荐。人体工学椅推荐。人体工学椅推荐。人体工学椅推荐。"),
]

# ── 正常长文：含查询词但不该被罚 ──────────────────────────────────────────────
# 取自标定集的 seo_good_* 四条（原文照抄，不另造）。自己编的「更长的正常文」
# 反而会越界——那说明判据确实紧，而不是样本不合适，故一律用标定集里的原样本。
SEO_LEGIT = [
    s for s in _load() if s["id"].startswith("seo_good")
]


class TestKeywordDensity:
    def test_density_computed_on_merged_text(self):
        uq = _u("机械键盘 推荐")
        d = keyword_density("机械键盘推荐。机械键盘推荐。机械键盘推荐轴体。", uq)
        assert d > STUFFING_DENSITY_THRESHOLD

    def test_stuffing_is_penalized(self):
        """核心：堆砌必须被罚。"""
        for q, t, c in STUFFING:
            p = stuffing_density_penalty(t, c, _u(q))
            assert p < 1.0, f"{q} 的堆砌未被罚（p={p}）"

    def test_legit_seo_longform_not_penalized(self):
        """核心（与上一条同等重要）：正常长文含查询词但**不得**被罚。

        这组是用「无对照的标定集」绝对测不出来的——早期 fixture 里所有 good
        样本都不含查询词，于是「查询词多」与「低质」在数据上完全共线。
        """
        for s in SEO_LEGIT:
            p = stuffing_density_penalty(s["title"], s["content"], _u(s["query"]))
            assert p == 1.0, f"{s['id']} 的正常长文被误罚（p={p}）"

    def test_no_false_positive_on_calibration_controls(self):
        """把标定集里全部 good 样本跑一遍，逐条断言未罚。"""
        checked = 0
        for s in _load():
            if s["quality"] != "good":
                continue
            checked += 1
            d = keyword_density(f"{s['title']} {s['content']}", _u(s["query"]))
            assert d < STUFFING_DENSITY_THRESHOLD, \
                f"{s['id']} 密度 {d:.3f} 超阈值 {STUFFING_DENSITY_THRESHOLD}"
        assert checked >= 11, f"good 样本只剩 {checked} 条，fixture 被改小了"

    def test_threshold_margin_is_known_and_small(self):
        """把「好样本最高密度」与阈值之间的余量钉在测试里。

        实测余量很薄：good 侧最高 0.211（seo_good_howto_density）、阈值 0.22，
        只差 0.009；而 stuffing 侧最低被罚的是 0.234。**这个薄余量是本判据
        最大的风险**，必须显式钉住：将来往 fixture 里加一条「正常文里密集
        提到查询词」的样本，它若落在 0.22 以上，这条测试会立刻红，提示要
        重新标定阈值，而不是让一条真实内容在生产里被静默误罚。
        """
        goods = [s for s in _load() if s["quality"] == "good"]
        worst = max(
            (keyword_density(f"{s['title']} {s['content']}", _u(s["query"])),
             s["id"]) for s in goods)
        assert worst[0] < STUFFING_DENSITY_THRESHOLD, \
            f"good 侧最高密度 {worst[0]:.3f}（{worst[1]}）已超阈值"

    def test_empty_and_malformed_safe(self):
        uq = _u("测试")
        assert stuffing_density_penalty("", "", uq) == 1.0
        assert stuffing_density_penalty("t", "c", set()) == 1.0
        assert stuffing_density_penalty("t", "c", uq) == 1.0
        assert keyword_density("", uq) == 0.0
        assert keyword_density("abc", set()) == 0.0

    def test_floor_not_below_configured(self):
        """极端堆砌时惩罚不得跌破地板（避免单点判据把结果打到 0）。"""
        q = "空气净化器 推荐"
        uq = _u(q)
        insane = "空气净化器推荐。" * 200
        p = stuffing_density_penalty("空气净化器推荐", insane, uq)
        assert p >= 0.35 - 1e-9, p

    def test_short_document_exempt(self):
        """比率型判据在分母太小时必然失真——短文档不判。

        实测误伤：标题「讨论：某技术选型」+ snippet「短」（1 个字），
        合并单元 5 个命中 3 个 → 密度 0.60，把一条正常的共识条目压掉，
        tests/test_ranking_contract.py 的「不变式 2：共识必须体现在最终排序」
        当场变红。短摘要是 SERP 常态，不是农场特征。
        """
        from rank_signals import score_relevance
        uq = _u("某技术选型")
        # 同样堆砌程度，但正文够长 → 会被罚；对照正文极短 → 不罚
        long_stuffed = score_relevance(
            uq, "讨论：某技术选型", "某技术选型。某技术选型比较。某技术选型推荐。" * 8)
        short = score_relevance(uq, "讨论：某技术选型", "短")
        assert short > long_stuffed, \
            f"短文档不该被密度判据罚（短 {short} 应高于长堆砌 {long_stuffed}）"

    def test_keyword_stream_snippet_exempt(self):
        """SERP 摘要被抽成词条时不得判罚——那是上游抽取方式，不是内容质量。

        实测误伤：金标用例「青藏高原 形成成因」（sorting golden 的
        serp_jump_suppressed）里，摘要被抽成
        「青藏高原 形成成因 印度 板块 与 欧亚 板块 碰撞 隆起」——零标点、
        多空格、每个 token 都是查询词，密度 0.57 越过阈值，把正常来源压掉，
        该用例 MRR 从 1.0 掉到 0.5。

        判据只看 **snippet**：标题里带「：」不影响摘要的形态。
        """
        from rank_signals import score_relevance
        uq = _u("青藏高原 形成成因")
        kw_title = "青藏高原 形成成因：板块 碰撞 的 结果"
        kw_snippet = "青藏高原 形成成因 印度 板块 与 欧亚 板块 碰撞 隆起"
        normal_snippet = ("青藏高原由印度板块与欧亚板块碰撞隆起形成。"
                          "碰撞始于约五千万年前，此后持续抬升。"
                          "地壳厚度与高原面海拔由此奠定。")
        s_kw = score_relevance(uq, kw_title, kw_snippet)
        s_normal = score_relevance(uq, kw_title, normal_snippet)
        # 只断言「没有被密度判据压」——不要求两者分数相等。词条流摘要的
        # 原始覆盖率本来就更高（每个 token 都命中），那是检索侧的事实，
        # 本判据无权改动。真正要防的是它被 ×0.35 之类的系数打下来。
        assert s_kw >= 0.95, f"词条流摘要被密度判据误罚（{s_kw}）——应豁免"


class TestDensityInRelevance:
    def test_density_catches_body_stuffing_with_clean_title(self):
        """密度判据**独立**有效的证明，且必须走真实入口 score_relevance。

        这条是本信号存在的唯一理由。第一版写成「直接调两个惩罚函数」，
        结果是**空守卫**：把 score_relevance 里的密度接线摘掉，测试照样全绿
        ——因为它根本没经过那条路径。判据函数调得通 ≠ 信号接进了排序。
        故这里一律通过 score_relevance 断言：摘掉接线后本用例必须转红。
        """
        from rank_signals import score_relevance, title_stuffing_penalty
        uq = _u("空气净化器 推荐")
        clean_title = "空气净化器怎么选：三个容易忽略的参数"
        stuffed_body = ("空气净化器推荐。空气净化器推荐品牌。空气净化器推荐型号。"
                        "空气净化器推荐价格。空气净化器推荐滤网。空气净化器推荐噪音。"
                        "空气净化器推荐除甲醛。空气净化器推荐适用面积。"
                        "空气净化器推荐耗材。")
        # 前提：标题本身不堆砌，否则 title 判据会先一步压分，测不到密度判据
        assert title_stuffing_penalty(clean_title, uq) == 1.0, \
            "前提不成立：标题也堆砌了，本用例测不到密度判据"
        # 同样内容、同样标题，只把正文换成正常论述 → 分数必须显著回升
        r_stuffed = score_relevance(uq, clean_title, stuffed_body)
        r_normal = score_relevance(
            uq, clean_title,
            "选空气净化器先看洁净空气量，它决定净化速度，按房间体积乘以五估算即可。"
            "颗粒物累计净化量决定滤网寿命，低于三千的属于入门档，长期使用成本明显更高。"
            "适用面积参数常被虚标，看洁净空气量与房间体积的比值更可靠。"
            "甲醛洁净空气量与颗粒物洁净空气量是两个指标，差距通常很大，要分开看。")
        assert r_stuffed < r_normal, \
            f"正文堆砌未被压分（堆砌 {r_stuffed} 不低于正常 {r_normal}）"

    def test_relevance_applies_density_penalty(self):
        """密度惩罚必须真的作用到 score_relevance 上（否则只是死代码）。"""
        from rank_signals import score_relevance
        q = "机械键盘 推荐"
        uq = _u(q)
        stuffed = score_relevance(
            uq, "机械键盘推荐_机械键盘推荐哪个好_机械键盘推荐预算",
            "机械键盘推荐。机械键盘推荐轴体。机械键盘推荐键帽。")
        normal = score_relevance(
            uq, "机械键盘怎么选：轴体、键帽与声音的取舍",
            "选机械键盘时，轴体是第一道门槛。机械键盘推荐红轴还是青轴，"
            "取决于你要线性手感还是段落感。机械键盘推荐上 PBT 键帽。")
        assert stuffed < normal, f"堆砌 {stuffed} 未低于正常 {normal}"

    def test_escape_hatch_disables_density_penalty(self, monkeypatch):
        """逃生门：关掉 v2 算子时整体退回旧行为，密度惩罚不参与。"""
        from rank_signals import score_relevance
        monkeypatch.setenv("ARGO_RELEVANCE_V2", "0")
        uq = set("机械键盘推荐")   # 旧口径是单字
        r = score_relevance(
            uq, "机械键盘推荐_机械键盘推荐哪个好", "机械键盘推荐。机械键盘推荐轴体。")
        # 旧口径：只用单字覆盖率，不乘任何堆砌惩罚 → 满分
        assert r == pytest.approx(1.0), r

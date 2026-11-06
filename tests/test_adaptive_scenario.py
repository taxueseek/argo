#!/usr/bin/env python3
"""test_adaptive_scenario.py — adaptive 的 (domain, lang) 场景分桶。

契约（2026-10-24）：
  1. 不传 domain/lang → 行为与旧口径逐位一致（全局单桶）；
  2. 传 domain/lang 且桶内有可判定样本 → 与全局分加权融合；
  3. 桶为空 → 回退全局分；
  4. 同一引擎可在不同域拿到不同分（「A 域强、B 域弱」可表达）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


@pytest.fixture()
def learner(tmp_path, monkeypatch):
    """隔离状态目录的学习器单例（每用例独立库）。"""
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
    import adaptive
    # 不 do reload：reload 会重执行模块级代码（DB_PATH 改写、单例重置），
    # 而其他模块（route_combo/search_pipeline）持有的是**旧类对象**的引用，
    # 于是全量套件里会出现「record() 不认识 domain 参数」这种只在合集里
    # 复现的失败（2026-10-24 实测）。DB_PATH 在导入时已按 ARGO_STATE_DIR 派生，
    # 这里只需把单例指过去。
    return adaptive.get_learner()


def test_global_score_unchanged_without_bucket(learner):
    """不传 domain/lang 时，全局分只由全局样本决定。"""
    for _ in range(3):
        learner.record("eng", success=True, latency_ms=100, quality=1.0,
                       domain="chem", lang="zh")
    for _ in range(3):
        learner.record("eng", success=False, latency_ms=3000,
                       domain="sports", lang="en")
    # 全局：judged=6, judged_ok=3 → success_rate=0.5；延迟均值 1550 → 1.0
    assert learner.get_score("eng") == 0.5


def test_bucket_blends_with_global(learner):
    """桶向全局分收缩：3 条判定样本时桶权重 3/(3+2)=0.6。"""
    for _ in range(3):
        learner.record("eng", success=True, latency_ms=100, quality=1.0,
                       domain="chem", lang="zh")
    for _ in range(3):
        learner.record("eng", success=False, latency_ms=3000,
                       domain="sports", lang="en")
    chem = learner.get_score("eng", "chem", "zh")
    sports = learner.get_score("eng", "sports", "en")
    assert chem > learner.get_score("eng") > sports
    assert chem == pytest.approx(0.4 * 0.5 + 0.6 * 1.25, abs=1e-3)
    assert sports == pytest.approx(0.4 * 0.5, abs=1e-3)


def test_empty_bucket_falls_back_to_global(learner):
    """桶为空 → 回退全局分，不返回 0。"""
    learner.record("eng", success=True, latency_ms=100, quality=1.0,
                   domain="chem", lang="zh")
    global_score = learner.get_score("eng")
    assert learner.get_score("eng", "news", "ja") == pytest.approx(global_score)


def test_same_engine_different_domains(learner):
    """同一引擎在不同域拿到不同分——分桶的核心价值。"""
    for _ in range(4):
        learner.record("eng", success=True, latency_ms=80, quality=1.0,
                       domain="academic", lang="en")
    for _ in range(4):
        learner.record("eng", success=False, latency_ms=4000,
                       domain="finance_macro", lang="en")
    assert learner.get_score("eng", "academic", "en") > \
        learner.get_score("eng", "finance_macro", "en")


def test_record_backward_compatible(learner):
    """旧调用方（不传 domain/lang）仍可记录，且不影响全局分。"""
    learner.record("legacy", success=True, latency_ms=100)
    assert learner.get_score("legacy") > 0.5


def test_scenario_columns_migrate(tmp_path, monkeypatch):
    """老库（无 domain/lang 列）能被 ALTER 补列，不报错。"""
    import sqlite3
    db = tmp_path / "adaptive.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE engine_perf (engine TEXT, success INTEGER, "
        "latency_ms REAL, cost REAL, created_at REAL, empty INTEGER, quality REAL)"
    )
    conn.execute(
        "INSERT INTO engine_perf VALUES ('e',1,100,0,?,0,1.0)",
        (__import__("time").time(),),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
    import adaptive
    # 不 do reload：reload 会重执行模块级代码（DB_PATH 改写、单例重置），
    # 而其他模块（route_combo/search_pipeline）持有的是**旧类对象**的引用，
    # 于是全量套件里会出现「record() 不认识 domain 参数」这种只在合集里
    # 复现的失败（2026-10-24 实测）。DB_PATH 在导入时已按 ARGO_STATE_DIR 派生，
    # 这里只需把单例指过去。
    learner = adaptive.get_learner()
    cols = [r[1] for r in learner._connect().execute("PRAGMA table_info(engine_perf)")]
    assert "domain" in cols and "lang" in cols


def test_heb_dcb_bucket_empty_demotes_scenario_without_polluting_global(learner):
    """HEB-DCB 正交解耦：场景桶内的持续空结果下调该场景桶分，但不污染全局健康分。"""
    # 引擎在 code 域有正常高质量返回（全局健康分高）
    for _ in range(4):
        learner.record("github", success=True, latency_ms=120, quality=1.0,
                       empty=False, domain="code_search", lang="en")
    g_before = learner.get_score("github")
    assert g_before >= 0.8

    # 同一引擎在 cooking 域持续返回空结果（empty=True）
    for _ in range(6):
        learner.record("github", success=False, latency_ms=150,
                       empty=True, domain="cooking", lang="zh")

    # 1. 全局健康分不受跨域 empty 污染
    assert learner.get_score("github") == pytest.approx(g_before, abs=1e-3)
    # 2. 强域 code_search 分数保持高位
    assert learner.get_score("github", "code_search", "en") >= 0.8
    # 3. 持续空结果的 cooking 域桶分被经验贝叶斯召回因子显著下调
    cooking_score, cooking_n = learner.get_score_with_samples("github", "cooking", "zh")
    assert cooking_n == 6
    assert cooking_score < 0.5


def test_heb_ucb_score_protects_low_sample_but_not_dead_source():
    """HEB-DCB UCB 置信上界：保护低样本边界源（>=0.15），不庇护死源（<0.15）与高样本平庸源。"""
    from adaptive import AdaptiveLearner
    # 死源（<0.15）不获 UCB 补偿
    assert AdaptiveLearner.heb_ucb_score(0.084, 2) == 0.084
    # 低样本（n=2）且未跌破地板（0.24）的垂直源获 UCB 补偿越过 0.3 阈值
    assert AdaptiveLearner.heb_ucb_score(0.24, 2) >= 0.30
    # 随样本数增大（n=50），同分平庸源的 UCB 奖励收敛，不再越过 0.3
    assert AdaptiveLearner.heb_ucb_score(0.24, 50) < 0.30


def test_bucket_weight_grows_with_samples(learner):
    """桶权重 n/(n+2) 随样本单调上升，取代 0.3/0.5/0.75 分段常数。"""
    learner.record("eng", success=False, latency_ms=100, domain="other", lang="en")
    learner.record("eng", success=True, latency_ms=100, domain="other", lang="en")
    prev = None
    for n in range(1, 7):
        learner.record("eng", success=True, latency_ms=100, domain="chem", lang="zh")
        cur = learner.get_score("eng", "chem", "zh")
        if prev is not None:
            assert cur > prev
        prev = cur


def test_bucket_empty_and_ok_mix(learner):
    """场景召回率 = (2 + 有结果) / (2 + 有结果 + 空结果)，只作用于该桶。"""
    for _ in range(2):
        learner.record("eng", success=True, latency_ms=100, domain="d", lang="zh")
    for _ in range(4):
        learner.record("eng", success=False, latency_ms=100, empty=True,
                       domain="d", lang="zh")
    g = learner.get_score("eng")
    s, n = learner.get_score_with_samples("eng", "d", "zh")
    assert n == 6
    # 健康度：桶与全局同为 1.0；召回率 (2+2)/(2+2+4) = 0.5
    assert s == pytest.approx(g * 0.5, abs=1e-3)


def test_marginal_contribution_shapley_shares():
    """独家结果归一个源，共识结果均分；公平份额记 0.5。"""
    from adaptive import marginal_contribution
    merged = [
        {"url": "a", "consensus_engines": ["x"]},
        {"url": "b", "consensus_engines": ["x", "y"]},
        {"url": "c", "consensus_engines": ["x", "y"]},
        {"url": "d", "consensus_engines": ["x"]},
    ]
    out = marginal_contribution(merged, 4, ["x", "y", "z"])
    # 份额 x=3, y=1, z=0；q = 0.5 × 份额/4 × 3，z 取地板 0.01
    assert out == {"x": 1.0, "y": 0.375, "z": 0.01}


def test_marginal_contribution_redundant_sources_not_rewarded():
    """两个源返回完全相同的结果：各记公平份额 0.5（中性），冗余不加分。"""
    from adaptive import marginal_contribution
    merged = [{"url": u, "consensus_engines": ["x", "y"]} for u in "abc"]
    assert marginal_contribution(merged, 3, ["x", "y"]) == {"x": 0.5, "y": 0.5}
    # 单引擎查询没有可比对象：记中性 0.5
    solo = [{"url": u, "consensus_engines": ["x"]} for u in "abc"]
    assert marginal_contribution(solo, 3, ["x"]) == {"x": 0.5}


def test_marginal_contribution_empty_inputs():
    from adaptive import marginal_contribution
    assert marginal_contribution([], 10, ["x"]) == {}
    assert marginal_contribution([{"url": "a", "consensus_engines": ["x"]}], 10, []) == {}
    # 无 consensus_engines 时回退 _engine / source
    assert marginal_contribution([{"url": "a", "source": "x"}], 10, ["x"]) == {"x": 0.5}


def test_never_contributing_source_is_demoted(learner):
    """从不进终榜的源记地板 0.01：SUM(quality)>0，quality_factor 降到 ~0.75 而非中性。"""
    from adaptive import marginal_contribution, quality_factor
    q = marginal_contribution([{"url": "a", "consensus_engines": ["x"]}], 1, ["x", "z"])["z"]
    for _ in range(3):
        learner.record("z", success=True, latency_ms=100, quality=q)
    assert learner.get_score("z") == pytest.approx(quality_factor(0.01, 0.03), abs=1e-3)
    assert learner.get_score("z") < 0.76

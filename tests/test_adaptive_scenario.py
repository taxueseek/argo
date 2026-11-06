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
    """桶样本 ≥3 时权重 0.5；桶分与全局分融合。"""
    for _ in range(3):
        learner.record("eng", success=True, latency_ms=100, quality=1.0,
                       domain="chem", lang="zh")
    for _ in range(3):
        learner.record("eng", success=False, latency_ms=3000,
                       domain="sports", lang="en")
    chem = learner.get_score("eng", "chem", "zh")
    sports = learner.get_score("eng", "sports", "en")
    assert chem > learner.get_score("eng") > sports
    assert chem == pytest.approx(0.875, abs=1e-3)
    assert sports == pytest.approx(0.25, abs=1e-3)


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

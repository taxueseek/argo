#!/usr/bin/env python3
"""test_route_tfidf_weak_score.py — TF-IDF 弱证据不得抢占 primary。

2026-10-03 修复的行为锁：TF-IDF 分支（正则未命中）里，采纳分 0.12 分之上、
强证据线 0.15 之下的语义推荐此前直接当 primary（实测「nanjing food
recommendations」→ usda 营养库 score=0.145 领队，anysearch 殿后）——弱证据
错配占 primary 位，慢源拖墙钟、rerank 被无关结果污染。

修复后：弱证据推荐引擎只作辅源跟跑（combo 里仍并存，能力不减），通用保底
anysearch 领队；>TFIDF_STRONG_SCORE 维持原样（语义推荐领队）。
"""

import os
import sys
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from route import route_query, TFIDF_STRONG_SCORE  # noqa: E402

# 与实测同型：无域 pattern 命中、TF-IDF 弱分推荐
_QUERY = "nanjing food recommendations"


def test_weak_tfidf_pick_rides_behind_general_fallback():
    with patch("route.semantic_route",
               return_value=[("usda", 0.145, "sim=0.145")]):
        out = route_query(_QUERY)
    engines = out["engines"]
    assert engines, "combo 不能为空"
    assert engines[0] != "usda", f"弱分 usda 仍在 primary 位：{engines}"
    assert "usda" in engines, f"弱分推荐应作辅源保留：{engines}"
    assert "anysearch" in engines, f"通用保底应在场：{engines}"
    assert "低分辅源" in out.get("reason", ""), \
        f"reason 应如实标注辅源身份：{out.get('reason')}"


def test_strong_tfidf_pick_still_leads():
    """对照面：强证据语义推荐维持领队（>TFIDF_STRONG_SCORE 行为不变）。"""
    score = TFIDF_STRONG_SCORE + 0.15
    with patch("route.semantic_route",
               return_value=[("usda", score, f"sim={score}")]):
        out = route_query(_QUERY)
    assert out["engines"][0] == "usda", \
        f"强证据推荐应领队：{out['engines']}"
    assert "语义路由" in out.get("reason", ""), out.get("reason")


def test_no_tfidf_pick_falls_back_cleanly():
    """语义推荐缺席时 combo 只剩通用保底（原 [None] 过滤路径不回归）。"""
    with patch("route.semantic_route", return_value=[]):
        out = route_query(_QUERY)
    engines = out["engines"]
    assert engines, "combo 不能为空"
    assert "anysearch" in engines, f"无推荐时应有通用保底：{engines}"
    assert None not in engines, "None 不得混进 combo"

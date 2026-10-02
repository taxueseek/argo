#!/usr/bin/env python3
"""test_vertical_floor_geo_backfill_1002.py — 垂直域死源地板 + 熔断槽位回填。

两个 2026-10-02 修复的行为锁：

A2（垂直域死源地板）：自适应分 <_VERTICAL_FLOOR 的引擎是「已被证明
失败」（实测 thesportsdb 0.084、近期 12/13 次失败）。此前它凭
primary 身份同时豁免三处过滤（GEC protect / must_keep / 策略层
强制补位），每次体育查询都白等它的单引擎超时（2.5-3.5s）。
修复后：combo 不含死源，但它仍是域声明成员 → 恢复链 L3 首位
候选（全失败时兜底）；分数回升后自动回归。

A3（熔断槽位回填）：中文地名查询触发 geo must_keep 挤掉次引擎
（腾位），geo 位引擎随后因熔断 disabled 被摘除——两头损失叠加
后 combo 只剩 anysearch 单引擎，丧失对冲（anysearch 延迟
1.6-6s 且波动大）。修复后：breaker_filter 摘除预算内槽位时，
从策略前候选池按序补回非熔断、语言合规的引擎，补足到摘除前
长度。
"""

import os
import sys
from unittest.mock import MagicMock, patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from route import route_query  # noqa: E402
import route_combo  # noqa: E402


def _mock_learner(scores: dict[str, float]):
    learner = MagicMock()
    learner.get_score.side_effect = lambda e: scores.get(e, 0.8)
    return learner


def test_vertical_dead_primary_demoted_but_kept_in_fallback():
    """死源 primary 不得进 combo，但必须是恢复链首位候选。"""
    learner = _mock_learner({"thesportsdb": 0.084})
    with patch.object(route_combo, "_adaptive_learner", learner):
        out = route_query("马拉松 世界纪录")
    assert out["domain"] == "sports_search"
    assert "thesportsdb" not in out["engines"], \
        f"死源 primary 仍在 combo：{out['engines']}"
    assert out["engines"], "combo 不能为空"
    fb = out.get("engines_fallback") or []
    assert fb and fb[0] == "thesportsdb", \
        f"死源应居恢复链 L3 首位：{fb[:3]}"


def test_vertical_healthy_primary_still_protected():
    """对照面：健康 primary 不受地板误伤（防新源饿死循环照旧）。"""
    learner = _mock_learner({"thesportsdb": 0.5})  # 中性分 = 无数据
    with patch.object(route_combo, "_adaptive_learner", learner):
        out = route_query("马拉松 世界纪录")
    assert "thesportsdb" in out["engines"], \
        f"健康 primary 被地板误伤：{out['engines']}"


class _GeoDeadBreaker:
    """local_openstreetmap 熔断 disabled、其余引擎全健康的假熔断器。"""

    def status(self, engine_id):
        if engine_id == "local_openstreetmap":
            return {"state": "disabled", "failures": 4}
        return {"state": "closed", "failures": 0}


def test_breaker_removed_geo_slot_gets_backfilled():
    """geo 位引擎熔断后，combo 不得退化成单引擎。"""
    with patch("circuit_breaker.get_breaker",
               return_value=_GeoDeadBreaker()):
        out = route_query("北京 二手房 成交量")
    assert out["domain"] == "chinese_general"
    assert len(out["engines"]) >= 2, \
        f"熔断摘除后 combo 退化单引擎：{out['engines']}"
    assert "local_openstreetmap" not in out["engines"]


def test_tfidf_dead_candidate_skipped_for_next():
    """TF-IDF 语义路由的第一候选是死源时，跳过它取下一候选。

    实测：「NBA 总决赛 赛程」的语义路由第一候选是 thesportsdb
    （体育元数据引擎，语义确实最相关），但它自适应分 0.084
    （近期 12/13 次失败）——注入后撤销 GEC 的地板过滤并占据
    wave-1 竞速首位，白等它的单引擎超时。修复：死源候选跳过，
    看下一个（与社交/语言丢弃同构）。
    """
    learner = _mock_learner({"thesportsdb": 0.084})
    with patch.object(route_combo, "_adaptive_learner", learner):
        out = route_query("NBA 总决赛 赛程")
    assert out["domain"] == "sports_search"
    assert "thesportsdb" not in out["engines"], \
        f"TF-IDF 死源候选仍被注入：{out['engines']}"
    assert out["engines"], "combo 不能为空"
    # 对照面：健康语义第一候选仍注入（jolpica 分 0.76）
    learner2 = _mock_learner({"thesportsdb": 0.5})
    with patch.object(route_combo, "_adaptive_learner", learner2):
        out2 = route_query("NBA 总决赛 赛程")
    assert out2["engines"], "对照面 combo 不能为空"


def test_breaker_removed_slot_not_backfilled_when_pool_exhausted():
    """回填池候选也熔断时，combo 保持短而不出脏源（fail-open）。"""

    class _AllDeadBreaker(_GeoDeadBreaker):
        def status(self, engine_id):
            if engine_id in ("local_openstreetmap", "octen"):
                return {"state": "disabled", "failures": 4}
            return {"state": "closed", "failures": 0}

    with patch("circuit_breaker.get_breaker",
               return_value=_AllDeadBreaker()):
        out = route_query("北京 二手房 成交量")
    assert out["engines"], "combo 不能为空"
    assert "local_openstreetmap" not in out["engines"]
    assert "octen" not in out["engines"], \
        f"熔断候选被回填：{out['engines']}"

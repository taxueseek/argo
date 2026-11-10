"""test_state_registry.py — 状态机注册表的机械对拍门禁。

为什么需要：注册表是「新状态机必须显式注册」的强制机制，但注册表本身
也是手写投影——漏一台、写错一个值、routable 子集与词表不一致，都会
让「强制注册」形同虚设。本测试把注册表与词表常量逐值对拍，并验证
未注册状态机 fail-closed。

与 test_state_literal_drift.py 的分工：
- test_state_literal_drift.py：AST 扫 scripts/*.py，检查代码里是否写了裸字面量
- test_state_registry.py：检查注册表投影是否完整、routable 是否一致
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ 在仓库根下；测试从 tests/ 跑时需要把 scripts/ 加进 path
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import engine_states as es  # noqa: E402


class TestRegistryCompleteness:
    """注册表必须覆盖全部 27 台状态机。"""

    # 期望的 27 台状态机名（与 engine_states.py 文件头 §1-§27 一一对应）
    EXPECTED_MACHINES = frozenset({
        "breaker", "config", "env", "admission", "health",
        "outcome", "failure", "fetch_method", "retry",
        "engine_detail", "daemon", "reranker", "plan",
        "batch_probe", "batch_probe_verdict", "numeric",
        "coverage", "probe", "cache", "verification",
        "watch", "envelope", "recovery_level", "conclusion_cap",
        "validation", "root_cause",
    })

    def test_all_machines_registered(self):
        """注册表包含全部期望状态机。"""
        registered = set(es.list_state_machines())
        missing = self.EXPECTED_MACHINES - registered
        extra = registered - self.EXPECTED_MACHINES
        assert not missing, f"未注册的状态机: {sorted(missing)}"
        assert not extra, f"多余的状态机（请更新 EXPECTED_MACHINES）: {sorted(extra)}"

    def test_no_empty_states(self):
        """每台状态机的 states 非空。"""
        for name in es.list_state_machines():
            states = es.get_states(name)
            assert len(states) > 0, f"状态机 '{name}' 的 states 为空"

    def test_routable_subset_of_states(self):
        """routable 必须是 states 的子集。"""
        for name in es.list_state_machines():
            states = es.get_states(name)
            routable = es.get_routable(name)
            assert routable <= states, (
                f"状态机 '{name}' 的 routable 不是 states 的子集: "
                f"routable - states = {sorted(routable - states)}"
            )


class TestRegistryValues:
    """注册表值与词表常量逐值对拍。"""

    def test_breaker_matches(self):
        assert es.get_states("breaker") == es.BREAKER_STATES
        assert es.get_routable("breaker") == es.BREAKER_ROUTABLE_STATES

    def test_config_matches(self):
        assert es.get_states("config") == frozenset({es.CONFIG_ENABLED, es.CONFIG_DISABLED})

    def test_recovery_level_matches(self):
        assert es.get_states("recovery_level") == es.RECOVERY_LEVELS
        assert es.get_cost_order("recovery_level") == es.RECOVERY_LEVEL_TIER

    def test_reranker_matches(self):
        assert es.get_states("reranker") == es.RERANK_STATUSES
        assert es.get_routable("reranker") == frozenset({es.RERANK_OK})

    def test_outcome_contains_skipped_prefix(self):
        """outcome 词表必须包含全部 skipped-* 值。"""
        states = es.get_states("outcome")
        skipped = [s for s in states if s.startswith(es.OUTCOME_SKIPPED_PREFIX)]
        assert len(skipped) >= 4, f"outcome 的 skipped-* 值不足: {skipped}"

    def test_fetch_method_matches(self):
        assert es.get_states("fetch_method") == es.FETCH_METHODS

    def test_validation_matches(self):
        assert es.get_states("validation") == es.VALIDATION_STATUSES


class TestFailClosed:
    """未注册状态机必须 fail-closed。"""

    def test_unknown_machine_raises(self):
        with pytest.raises(es.UnknownStateMachineError):
            es.get_states("definitely_not_a_machine")

    def test_unknown_machine_raises_routable(self):
        with pytest.raises(es.UnknownStateMachineError):
            es.get_routable("definitely_not_a_machine")

    def test_unknown_machine_raises_cost_order(self):
        with pytest.raises(es.UnknownStateMachineError):
            es.get_cost_order("definitely_not_a_machine")

    def test_error_message_lists_registered(self):
        """报错信息必须列出已注册状态机，方便排查。"""
        with pytest.raises(es.UnknownStateMachineError) as exc_info:
            es.get_states("xxx")
        msg = str(exc_info.value)
        assert "已注册" in msg
        assert "breaker" in msg  # 至少包含一台已注册的


class TestCostOrder:
    """成本序只对有意义的机器存在。"""

    def test_recovery_level_order_correct(self):
        order = es.get_cost_order("recovery_level")
        assert order[es.RECOVERY_LEVEL_L0] == -1
        assert order[es.RECOVERY_LEVEL_L1] == 0
        assert order[es.RECOVERY_LEVEL_L2] == 1
        assert order[es.RECOVERY_LEVEL_L3] == 2
        assert order[es.RECOVERY_LEVEL_L4] == 3

    def test_no_cost_order_for_others(self):
        """非成本型状态机的 cost_order 为空。"""
        assert es.get_cost_order("breaker") == {}
        assert es.get_cost_order("validation") == {}

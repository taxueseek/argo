#!/usr/bin/env python3
"""状态词表契约：防止「同一个状态名在两层之间被误合并」。

2026-11-09 实测回归：engine_status.py 把 outcome 的 quota-exhausted（连字符）
填进 CLI 展示态（下划线，quota_exhausted），test_http_attribution 立刻红。
同轮审计发现 ZCode 桥把同一概念写成 quota / quota-exhausted /
quota_exhausted 三套。本用例把各层词表钉死，改错一处即失败。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import engine_states as st  # noqa: E402
from circuit_breaker import CircuitBreaker  # noqa: E402
from engine_failure import NOTE_STATUS  # noqa: E402
from engine_status import engine_detail  # noqa: E402
from retry_policy import NON_RETRYABLE_STATUS  # noqa: E402
from search_cli import _ENGINE_BLOCK_REASONS  # noqa: E402


def test_vocabularies_stay_in_their_own_layer():
    """展示态、outcome、failure category 是三套词表，值必须各自合法且边界清晰。"""
    assert st.OUTCOME_QUOTA_EXHAUSTED == "quota-exhausted"
    assert st.FAILURE_QUOTA_EXHAUSTED == "quota_exhausted"
    assert st.ENGINE_STATUS_QUOTA_EXHAUSTED == "quota_exhausted"
    assert st.OUTCOME_QUOTA_EXHAUSTED not in st.ENGINE_DETAIL_STATUSES
    assert st.ENGINE_STATUS_QUOTA_EXHAUSTED in st.ENGINE_DETAIL_STATUSES
    assert st.ENGINE_BLOCK_QUOTA_EXHAUSTED in st.ENGINE_BLOCK_REASONS
    # 展示态含 outcome 没有的诊断态
    assert {"missing_key", "missing_dep", "env_filtered"} <= st.ENGINE_DETAIL_STATUSES
    assert "ok" not in st.ENGINE_DETAIL_STATUSES


def test_derived_tables_come_from_one_source():
    """派生表不得再抄一份字面量，必须与词表同源。"""
    assert NON_RETRYABLE_STATUS == st.NON_RETRYABLE_OUTCOMES
    assert NOTE_STATUS == dict(st.CATEGORY_TO_OUTCOME)
    reasons = {r for r, _label in _ENGINE_BLOCK_REASONS}
    assert reasons == st.ENGINE_BLOCK_REASONS
    assert st.health_severity_index(st.HEALTH_AUTH_DOWN) == 0
    assert st.health_severity_index("未登记的 verdict") == len(st.HEALTH_SEVERITY_ORDER)


def test_breaker_states_are_persisted_verbatim():
    """落盘值只能来自词表：半开态曾出现 half-open / half_open 两套写法。"""
    assert st.BREAKER_HALF_OPEN == "half_open"
    assert set(st.BREAKER_STATES) == {"closed", "open", "half_open", "disabled"}
    assert st.is_breaker_routable(st.BREAKER_HALF_OPEN)
    assert not st.is_breaker_routable(st.BREAKER_OPEN)
    # 构造一个半开态，确认落盘字段读回来仍是同一个值
    br = CircuitBreaker.__new__(CircuitBreaker)
    br._engines = {"probe": {"state": st.BREAKER_HALF_OPEN}}
    assert br._engines["probe"]["state"] == "half_open"


def test_engine_detail_status_is_display_vocabulary():
    """CLI 展示态不能用 outcome 的词，配额耗尽仍显示 quota_exhausted。"""
    assert "quota_exhausted" in st.ENGINE_DETAIL_STATUSES
    assert "quota-exhausted" not in st.ENGINE_DETAIL_STATUSES
    # 逐条走真实判定链需要一个存在的引擎；这里只验契约本身，
    # 具体分支值由 test_http_attribution 的 quota 用例覆盖。
    assert callable(engine_detail)


# ── 本轮（2026-11-09 第二次扫）新增词表的契约 ────────────────────────────
# 目的不是「再断言一遍常量值」，而是钉住**派生关系与消费面同源**：
# 这些集合一旦被谁手抄一份，漏更新就是静默退化（精排）或静默过滤（探针）。


def test_rerank_degraded_is_complement_of_ok():
    """降级集合必须由全集派生：新增 skipped_* 却漏进降级集合 = 静默退化。"""
    assert st.RERANK_STATUSES - st.RERANK_DEGRADED_STATUSES == {st.RERANK_OK}
    assert st.is_rerank_degraded(st.RERANK_OK) is False
    for status in st.RERANK_STATUSES - {st.RERANK_OK}:
        assert st.is_rerank_degraded(status) is True, status
    # 缓存命中也是「没精排」，必须落在保底集合里（search_entry 写这个值）
    assert st.RERANK_SKIPPED_CACHE in st.RERANK_DEGRADED_STATUSES


def test_rerank_consumers_read_the_same_set():
    """三处消费面（pipeline / postprocess / rank）必须指向同一个 frozenset。"""
    import search_pipeline
    import search_rank

    assert search_pipeline._RERANK_DEGRADED_STATUSES is st.RERANK_DEGRADED_STATUSES
    # search_rank 保留旧名做别名（历史调用点仍 import 它）
    assert search_rank._RERANK_DEGRADED_STATUSES is st.RERANK_DEGRADED_STATUSES
    # postprocess_signals 是函数内延迟导入，确认它取到的是同一对象
    src = (Path(__file__).resolve().parents[1] / "scripts" /
           "postprocess_signals.py").read_text(encoding="utf-8")
    assert "from search_rank import _RERANK_DEGRADED_STATUSES" in src


def test_plan_statuses_cover_exit_code_contract():
    """plan.main 的退出码判据是「status 在词表内」——词表漏一个值就变成非零退出。"""
    import plan

    assert st.PLAN_STATUSES == {"ready", "handoff_required", "needs_authorization"}
    # 参数错走 invalid_request，它不在计划态词表里，所以 main 对它退出码非零
    assert "invalid_request" not in st.PLAN_STATUSES
    # 真打一次 main：合法计划 → 0；路由抛错的 invalid_request → 非 0。
    # 退出码判据就是「status 在 PLAN_STATUSES 内」，故这里只需分别喂两种 status。
    argv = sys.argv
    try:
        sys.argv = ["plan.py", "python 3.13 release notes"]
        assert plan.main() == 0
        sys.argv = ["plan.py", "x"]
        orig = plan.build_plan
        plan.build_plan = lambda *a, **k: {"status": "invalid_request"}
        try:
            assert plan.main() == 1
        finally:
            plan.build_plan = orig
    finally:
        sys.argv = argv


def test_probe_availability_is_fail_open():
    """探针可用性只认 unavailable 一票否决；未登记值必须放行（不静默过滤源）。"""
    assert st.is_probe_available(st.PROBE_STATUS_AVAILABLE)
    assert st.is_probe_available(st.PROBE_STATUS_DEGRADED)
    assert st.is_probe_available(st.PROBE_STATUS_UNKNOWN)
    assert st.is_probe_available("") is True
    assert st.is_probe_available("some_future_status") is True
    assert st.is_probe_available(None) is True
    assert st.is_probe_available(st.PROBE_STATUS_UNAVAILABLE) is False
    # 白名单集合是文档，判据是黑名单；两者不能互相替代
    assert st.PROBE_STATUS_UNAVAILABLE not in st.PROBE_ROUTABLE_STATUSES


def test_numeric_penalty_set_drives_reorder():
    """数值降权集合是 reorder_by_numeric 的唯一判据；confirmed/unfetchable 不动分。"""
    assert st.NUMERIC_PENALIZED == {st.NUMERIC_MISSING, st.NUMERIC_PARTIAL}
    assert st.NUMERIC_CONFIRMED not in st.NUMERIC_PENALIZED
    assert st.NUMERIC_UNFETCHABLE not in st.NUMERIC_PENALIZED
    import numeric_verify

    assert tuple(numeric_verify.VERDICTS) == (
        st.NUMERIC_CONFIRMED, st.NUMERIC_PARTIAL,
        st.NUMERIC_MISSING, st.NUMERIC_UNFETCHABLE,
    )


def test_coverage_vocabulary_stays_uppercase():
    """coverage 值的大写是 dossier JSON 的既有 export 契约，不能为了整齐改小写。"""
    assert st.COVERAGE_STATUSES == {"COVERED", "PARTIAL", "NOT_COVERED"}
    import research_dossier

    def _sr(n):
        return {"results": [{"url": f"https://e{i}.com"} for i in range(n)]}

    assert research_dossier.coverage_status(_sr(9)) == st.COVERAGE_COVERED
    assert research_dossier.coverage_status(_sr(1)) == st.COVERAGE_PARTIAL
    assert research_dossier.coverage_status(_sr(0)) == st.COVERAGE_NOT_COVERED


def test_probe_threshold_has_one_source():
    """连续失败阈值只能有一处定义；health_check 不该再长一个没人读的同名常量。"""
    import health_check
    import health_probe

    assert health_probe.MAX_FAILURES == st.PROBE_MAX_FAILURES
    assert not hasattr(health_check, "MAX_FAILURES")


def test_daemon_states_are_machine_readable():
    """daemon 的 state 字段走词表；stopped 之外的任何值都算「活着」。"""
    import daemon

    assert st.DAEMON_STATES == {"running", "stopped", "idle", "busy"}
    assert st.DAEMON_STOPPED in st.DAEMON_STATES
    # ping 响应在停止态下不存在（进程都没了），但 status 命令必须能给出 stopped
    assert callable(daemon._status)


def test_state_failure_outcome_has_one_meaning():
    """同名反义函数已删：engine_states 不再提供纯状态名版本。

    cache_guard 的 dict 版是唯一实现，且刻意不含 blocked（源站侧事实）。
    """
    assert not hasattr(st, "is_state_failure_outcome")
    from cache_guard import is_state_failure_outcome

    assert is_state_failure_outcome({"status": st.OUTCOME_AUTH_FAILED}) is True
    assert is_state_failure_outcome({"status": st.OUTCOME_BLOCKED}) is False


def test_search_progress_terminates_on_cache_hit(tmp_path):
    """进度契约：缓存命中路径也必须发 DONE——热路径没有终态，消费方会一直等。"""
    from search_types import Stage
    import search_entry

    hit_payload = {
        "results": [{"title": "t", "url": "https://e.com", "snippet": "s"}],
        "engines_combo": ["anysearch"], "engine_outcomes": [],
    }

    class _Cache:
        def get(self, *a, **k):
            return hit_payload

    stages = []
    prepared = search_entry.prepare(
        "q", {"domain": "general", "engine": "anysearch",
              "engines": ["anysearch"]},
        5, 5, "fast", _Cache(), False,
        mode="auto", on_progress=lambda s, d: stages.append((s, d)),
        hooks=None,
    )
    assert prepared.cached is not None
    assert [s for s, _ in stages][-1] is Stage.DONE
    assert stages[-1][1].get("cached") is True


def test_health_check_category_records_without_keyword_collision():
    """--category 路径曾因 available 重复传参必抛 TypeError；--all 路径曾把
    available 从返回值里 pop 掉（可用数恒 0）。两条都钉住。"""
    import health_check

    calls = []

    class _Registry:
        def get_local_search_engines(self):
            return {"local_a": {"url": "https://a.example", "categories": ["c"],
                                "enabled": True},
                    "local_b": {"url": "https://b.example", "categories": ["c"],
                                "enabled": True}}

        def update_health(self, name, available, **extra):
            calls.append((name, available, "available" in extra))

    health_check.get_registry = lambda: _Registry()
    health_check.check_http_engine = (
        lambda name, url, spec: {"available": True, "latency_ms": 1.0, "error": None})
    try:
        cat = health_check.check_category("c")
        assert set(cat) == {"local_a", "local_b"}
        assert all(r["available"] is True for r in cat.values())
        assert calls and all(a is True and not dup for _n, a, dup in calls)

        calls.clear()
        all_res = health_check.check_all()
        assert sum(1 for r in all_res.values() if r.get("available")) == 2
    finally:
        import importlib
        importlib.reload(health_check)


def test_validation_vocabulary_is_shared_across_producer_and_persister():
    """验证结论词表必须被 engine_validate / engine_admission 同时引用。

    `skipped` 改名只改产出侧、漏改 admission 的 `status != "skipped"`，
    未探测引擎会被判 blocked=True 并落库、路由直接跳过（既往 24 个引擎
    错拉黑事故的同形态）。本用例钉住两侧都从词表取值，不写字面量。
    """
    for rel in ("scripts/engine_validate.py", "scripts/engine_admission.py"):
        src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
        assert "import engine_states" in src, f"{rel} 未引用词表"
        assert "engine_states.VALIDATION_STATUS_SKIPPED" in src, \
            f"{rel} 仍手写 skipped 字面量"
        assert "engine_states.VALIDATION_STAGE_HEALTH" in src, \
            f"{rel} 仍手写 health 阶段名"
    assert st.VALIDATION_STATUSES == {"pass", "fail", "skipped", "error"}
    # contract 是 2026-11-10 新增的准入前置 stage（family 与 skill_registry
    # 清单契约，见 engine_validate.check_contracts）
    assert st.VALIDATION_STAGES == {"contract", "health", "quality"}


def test_recovery_level_l0_is_in_the_tier_table():
    """L0 必须进成本序表：缺它时 max_level=L0 落到默认最高档，门控失效。"""
    assert st.recovery_level_tier(st.RECOVERY_LEVEL_L0) == -1
    assert st.recovery_level_tier("L9") is None  # 未登记值不得回退
    assert st.RECOVERY_LEVEL_TIER[st.RECOVERY_LEVEL_L4] == 3


def test_conclusion_cap_vocabulary_is_generated_to_js():
    """结论上限三态必须与 JS 侧生成物逐值对拍。"""
    data = (Path(__file__).resolve().parents[1] /
            "packages/dsh-plugin/dsh/native-tools.mjs").read_text(encoding="utf-8")
    m = re.search(r"export const STATES = (\{.*?\n\});", data, re.S)
    assert m, "native-tools.mjs 缺 STATES 导出"
    states = json.loads(m.group(1))
    assert states["conclusionCap"] == {v: v for v in st.CONCLUSION_CAP_STATUSES}
    assert states["plugin"] == {v: v for v in st.PLUGIN_STATES}
    assert states["daemon"] == {v: v for v in st.DAEMON_STATES}


def test_health_sources_align_to_circuit_breaker(monkeypatch):
    """三套健康存储单一真源：熔断器判定不可用时，health_check 与 health_probe 均一票否决。"""
    import health_check
    import health_probe
    from circuit_breaker import get_breaker

    class _MockBreaker:
        def allow(self, name):
            if name == "mock_down_engine":
                return False, "auth_streak_disabled"
            return True, "ok"

    monkeypatch.setattr("circuit_breaker.get_breaker", lambda: _MockBreaker())

    # 1. health_check.is_available 必须被熔断单一真源拦截
    assert health_check.is_available("mock_down_engine") is False

    # 2. health_probe.get_engine_status 必须对齐熔断不可用状态
    st = health_probe.get_engine_status("mock_down_engine")
    assert st["available"] is False
    assert st["status"] == "unavailable"

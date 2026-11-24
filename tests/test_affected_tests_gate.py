#!/usr/bin/env python3
"""test_affected_tests_gate.py — L2 变更感知选测与坏例自动提案门禁。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import affected_tests  # noqa: E402
import badcase_ledger  # noqa: E402


def _make_mini_repo(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts" / "core_a.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "scripts" / "mid_b.py").write_text("import core_a\nY = core_a.X + 1\n", encoding="utf-8")
    (tmp_path / "scripts" / "dyn_c.py").write_text("Z = 3\n", encoding="utf-8")

    # 直接依赖 core_a（未入册）
    (tmp_path / "tests" / "test_direct.py").write_text("import core_a\n", encoding="utf-8")
    # 经由 mid_b 间接依赖 core_a（未入册）
    (tmp_path / "tests" / "test_transitive.py").write_text("from mid_b import Y\n", encoding="utf-8")
    # 已在 L1 manifest 中（应去重不重复跑）
    (tmp_path / "tests" / "test_in_l1.py").write_text("import core_a\n", encoding="utf-8")
    # 慢测（超单文件阈值应跳过）
    (tmp_path / "tests" / "test_slow_one.py").write_text("import core_a\n", encoding="utf-8")
    # 动态加载（无静态 import，靠 stem 补召回）
    (tmp_path / "tests" / "test_dyn_c_extra.py").write_text("import importlib\n", encoding="utf-8")
    # 无关测试
    (tmp_path / "tests" / "test_unrelated.py").write_text("import json\n", encoding="utf-8")
    return tmp_path


def test_select_affected_tests_transitive_dedupe_and_budget(tmp_path: Path):
    repo = _make_mini_repo(tmp_path)
    plan = affected_tests.select_affected_tests(
        ["scripts/core_a.py"],
        root=repo,
        manifest_entries={"tests/test_in_l1.py"},
        file_durations={
            "tests/test_direct.py": 0.2,
            "tests/test_transitive.py": 0.4,
            "tests/test_slow_one.py": 5.5,
        },
        budget_seconds=2.0,
        max_single_seconds=3.0,
    )
    assert plan["selected"] == ["tests/test_direct.py", "tests/test_transitive.py"]
    assert all(item["file"] == "tests/test_slow_one.py" for item in plan["skipped"])
    assert "tests/test_in_l1.py" not in plan["selected"]
    assert "tests/test_unrelated.py" not in plan["selected"]


def test_select_affected_tests_stem_fallback_for_dynamic_imports(tmp_path: Path):
    repo = _make_mini_repo(tmp_path)
    plan = affected_tests.select_affected_tests(
        ["scripts/dyn_c.py"],
        root=repo,
        manifest_entries=set(),
        file_durations={},
    )
    assert plan["selected"] == ["tests/test_dyn_c_extra.py"]


def test_real_repo_affected_selection_is_fast_and_non_empty():
    t0 = time.monotonic()
    plan = affected_tests.select_affected_tests(["scripts/fetch_v3.py"], budget_seconds=8.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"L2 选测静态扫描过慢: {elapsed:.2f}s"
    assert plan["selected"] or plan["skipped"], "改动 fetch_v3.py 应至少命中一个未入册测试"


def test_badcase_propose_actions_filters_by_threshold_and_explicit_only():
    sample_rows = [
        # 自动路由源 + 跨 3 天 6 次 rate_limited → 应建议 demote_to_explicit_only
        *[
            {"engine": "noisy_src", "root_cause": "rate_limited", "ts": f"2026-11-0{d}T10:00:00Z"}
            for d in (1, 1, 2, 2, 3, 3)
        ],
        # 自动路由源 + 跨 3 天 5 次 unattributed → 应建议 fix_attribution
        *[
            {"engine": "mystery_src", "root_cause": "unattributed", "ts": f"2026-11-0{d}T10:00:00Z"}
            for d in (1, 2, 3, 3, 3)
        ],
        # 已是 explicit_only → 不重复建议降级
        *[
            {"engine": "paid_only", "root_cause": "quota_exhausted", "ts": f"2026-11-0{d}T10:00:00Z"}
            for d in (1, 2, 3, 4, 5)
        ],
        # 仅 1 天集中失败 → 不触发多日门槛
        *[
            {"engine": "one_day_blip", "root_cause": "network", "ts": "2026-11-05T10:00:00Z"}
            for _ in range(8)
        ],
    ]
    engines_cfg = {
        "noisy_src": {"explicit_only": False},
        "mystery_src": {"explicit_only": False},
        "paid_only": {"explicit_only": True},
        "one_day_blip": {"explicit_only": False},
    }
    res = badcase_ledger.propose_actions(
        min_count=5, min_days=3, rows=sample_rows, engines_cfg=engines_cfg,
    )
    by_eng = {p["engine"]: p for p in res["proposals"]}
    assert set(by_eng.keys()) == {"noisy_src", "mystery_src"}
    assert by_eng["noisy_src"]["recommended_action"] == "demote_to_explicit_only"
    assert by_eng["mystery_src"]["recommended_action"] == "fix_attribution"

#!/usr/bin/env python3
"""漂移门的契约：什么算结构回归、什么只记录。

这些用例锁的是「夜间红了代表什么」。阈值写松了没人知道，写紧了会天天误报，
所以把边界钉成数字：少 1 条允许、少 2 条报；零结果必报（交付层死了）；
体积涨再多只进信息项（活网换血是常态，2026-10-28 issue #35 即误报实证）；
新用例不报；`save_baseline` 的外层 `report` 键要能穿透。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import ci_drift_gate  # noqa: E402


def _report(cases: list[dict]) -> dict:
    return {"n_cases": len(cases), "cases": cases, "total_missing": 1}


def _case(cid: str, kept: int = 5, size: int = 3000) -> dict:
    return {"id": cid, "kept": kept, "bytes": size}


def test_same_numbers_pass():
    r = _report([_case("a"), _case("b")])
    assert ci_drift_gate.violations(r, r) == []


def test_one_result_fewer_is_tolerated():
    base = _report([_case("a", kept=5)])
    cur = _report([_case("a", kept=4)])
    assert ci_drift_gate.violations(base, cur) == []


def test_two_results_fewer_is_reported():
    base = _report([_case("a", kept=5)])
    cur = _report([_case("a", kept=3)])
    assert ci_drift_gate.violations(base, cur) == ["a: 结果 3 条，基线 5 条"]


def test_zero_results_is_structural_even_within_floor_slack():
    """基线只有 1 条时地板允许掉到 0，但零结果本身就是交付层死亡，必须报。"""
    base = _report([_case("a", kept=1)])
    cur = _report([_case("a", kept=0)])
    bad = ci_drift_gate.violations(base, cur)
    assert len(bad) == 1 and "零结果" in bad[0]


def test_zero_results_without_baseline_is_reported():
    """新增用例没有基线不算漂移，但零结果例外——录制成功却一条不剩。"""
    base = _report([])
    cur = _report([_case("a", kept=0)])
    assert len(ci_drift_gate.violations(base, cur)) == 1


def test_output_growth_is_informational_not_regression():
    """体积涨 20% 只进信息项不退 1：2026-10-28 四查三换血全是外部漂移。"""
    base = _report([_case("a", size=3000)])
    assert ci_drift_gate.violations(base, _report([_case("a", size=3600)])) == []
    assert ci_drift_gate.drift_notes(base, _report([_case("a", size=3600)])) == []
    cur = _report([_case("a", size=3601)])
    assert ci_drift_gate.violations(base, cur) == []
    notes = ci_drift_gate.drift_notes(base, cur)
    assert len(notes) == 1 and "a" in notes[0] and "20" in notes[0]


def test_new_case_without_baseline_is_not_drift():
    base = _report([_case("a")])
    cur = _report([_case("a"), _case("b", kept=1, size=10)])
    assert ci_drift_gate.violations(base, cur) == []


def test_baseline_case_missing_from_current_is_drift():
    """只剩一条也算「通过」是误判：基线里四条，本次只录到一条，必须报。"""
    base = _report([_case("a"), _case("b"), _case("c"), _case("d")])
    cur = _report([_case("a")])
    bad = ci_drift_gate.violations(base, cur)
    assert len(bad) == 3
    assert all("本次报告里没有这条" in b for b in bad)


def test_baseline_wrapper_is_unwrapped(tmp_path: Path):
    base = {"_meta": {"saved_at": "x"}, "report": _report([_case("a", kept=5)])}
    cur = _report([_case("a", kept=2)])
    p = tmp_path / "base.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    assert ci_drift_gate.violations(ci_drift_gate._load(p), cur) == ["a: 结果 2 条，基线 5 条"]


def test_missing_file_exits_2_not_1(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "ci_drift_gate.py", "--baseline", str(tmp_path / "nope.json"),
        "--current", str(tmp_path / "nope2.json")])
    assert ci_drift_gate.main() == 2
    assert "读不到报告" in capsys.readouterr().err


def test_violation_exits_1(tmp_path: Path, monkeypatch, capsys):
    base = tmp_path / "base.json"
    cur = tmp_path / "cur.json"
    base.write_text(json.dumps({"report": _report([_case("a", kept=5)])}), encoding="utf-8")
    cur.write_text(json.dumps(_report([_case("a", kept=1)])), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "ci_drift_gate.py", "--baseline", str(base), "--current", str(cur)])
    assert ci_drift_gate.main() == 1
    assert "a: 结果 1 条" in capsys.readouterr().err


def test_real_1028_drift_passes_with_notes(monkeypatch, capsys):
    """回归锚：issue #35 当晚的真实 artifact 在新门下必须绿——体积破线降为信息项。

    当晚（2026-10-28）三条查询整批换血、asyncio 体积 +32%，旧门退 1 开了
    issue #35。这份报告固化在 golden 里，锁住「同样的情况以后不再红」。
    """
    fixture = (Path(__file__).resolve().parent.parent
               / "tests" / "golden" / "drift_report_20261028_issue35.json")
    baseline = (Path(__file__).resolve().parent.parent
                / "tests" / "golden" / "drift_baseline_report.json")
    monkeypatch_argv = [
        "ci_drift_gate.py", "--baseline", str(baseline), "--current", str(fixture)]
    monkeypatch.setattr(sys, "argv", monkeypatch_argv)
    assert ci_drift_gate.main() == 0
    out = capsys.readouterr().out
    assert "信息项" in out and "asyncio" in out


def test_pass_exits_0_and_mentions_missing_as_info(tmp_path: Path, monkeypatch, capsys):
    base = tmp_path / "base.json"
    cur = tmp_path / "cur.json"
    base.write_text(json.dumps({"report": _report([_case("a")])}), encoding="utf-8")
    cur.write_text(json.dumps(_report([_case("a")])), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "ci_drift_gate.py", "--baseline", str(base), "--current", str(cur)])
    assert ci_drift_gate.main() == 0
    assert "不算回归" in capsys.readouterr().out

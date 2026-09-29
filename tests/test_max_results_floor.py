"""回归：max_results 必须有下界——负数切片从尾部截，会「要 3 条给 7 条」。

现象（实测复现）：
    _apply_consensus_and_sort(10 条, max_results=-3) -> 7 条
负切片 `[: -3]` 从尾部砍掉 3 条，不是「取前 -3 条」。用户写 `-n -3` 拿到
7 条结果，比「没传 -n」的默认 5 条还多——与「限制条数」的语义完全相反。

更糟的是 `-n -100` 时 kept=0，漏斗把 `kept` 报成塌陷点
（search_output.funnel_collapse），把一个用户笔误归因成「过滤层崩了」——
漏斗唯一的用途就是诚实归因。

覆盖面：CLI argparse 层（源头拦）与 pipeline 层（库/MCP 兜底）。
MCP 以库形式调用 search.py，绕开 argparse，只改 argparse 是不够的。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from search_rank import _apply_consensus_and_sort  # noqa: E402


def _rows(n: int) -> list[dict]:
    return [{"title": str(i), "url": f"u{i}", "snippet": "s", "score": 0.5}
            for i in range(n)]


@pytest.mark.parametrize("n,expected", [
    (5, 5), (0, 0), (-1, 0), (-3, 0), (-100, 0),
])
def test_consensus_sort_never_exceeds_nonnegative_max(n, expected):
    """核心断言：返回条数 == max(max_results, 0)，绝不超过。"""
    out = _apply_consensus_and_sort(_rows(10), max_results=n)
    assert len(out) == expected, f"max_results={n} 返回了 {len(out)} 条"
    assert len(out) <= max(n, 0)


def test_positive_max_results_unchanged():
    """正向路径不得被顺手改坏——这是本测试存在的另一半意义。"""
    assert len(_apply_consensus_and_sort(_rows(10), 5)) == 5
    assert len(_apply_consensus_and_sort(_rows(3), 5)) == 3
    assert _apply_consensus_and_sort(_rows(10), 5) == _apply_consensus_and_sort(
        _rows(10), 5)  # 确定性


def test_cli_rejects_negative_max_results():
    """CLI 层应在 argparse 阶段就拒绝，退出码非 0。"""
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "search.py"), "test", "-n", "-3", "--plan-only"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    assert r.returncode != 0, "负数 -n 被静默接受"
    # 正数必须照常放行（防止把拦截写成「全部拒绝」）
    ok = subprocess.run(
        [sys.executable, str(SCRIPTS / "search.py"), "test", "-n", "3", "--plan-only"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    assert ok.returncode == 0, f"正数 -n 被误拒：{ok.stderr[-300:]}"


def test_plan_only_reports_nonnegative_max_results():
    """库/MCP 路径绕开 argparse，plan 里不得出现负的 max_results。"""
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "search.py"), "test", "-n", "-3",
         "--plan-only", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    txt = r.stdout
    assert '"max_results": -' not in txt.replace("max_results\":-", "max_results\": -")
    try:
        d = json.loads(txt)
    except (ValueError, TypeError):
        return  # argparse 已拦下，plan 根本不产出——由上一条测试覆盖
    plan = d.get("plan", d)
    if isinstance(plan, dict) and "max_results" in plan:
        assert plan["max_results"] >= 0

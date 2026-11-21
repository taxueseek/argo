#!/usr/bin/env python3
"""gate_manifest_check.py — 新增测试文件必须登记快门禁清单（commit 期硬拦）。

背景（2026-11-21 审计）：test_gate_manifest.py 的反向断言只拦「门禁命名」
文件（gate/consistency/drift/… 词根），test_state_machines_plan_b 与
test_query_operators 两个 <1s 文件漏册未被该机制发现——命名判据盖不全。
本检查把拦截点前移到 commit 时刻：git 暂存区新增（A 状态）的
tests/test_*.py 必须出现在 tests/gate_manifest.txt，否则红灯。存量未登记
文件不受影响（由 tests/test_gate_manifest.py 的未登记总数棘轮约束）。

2026-11-21 二次收口：skip 豁免层已取消——13 个 skip 项实测全部 <1s
（142~727ms）却只由 CI 兜底，导致两次「本地绿、全量红」漏库。现在本地
与 CI 共用同一份清单，慢项由 pre-commit 的分片并行消化。

单测：tests/test_gate_manifest.py::TestNewTestFileClassification。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_TEST_FILE_RE = re.compile(r"^tests/test_[a-z0-9_]+\.py$")


def check_new_test_files(staged_files: list[str], manifest_text: str) -> list[str]:
    """返回违规清单：staged 新增 tests/test_*.py 未登记进 manifest（# skip 已取消，不再放行）。"""
    known: set[str] = set()
    for raw in manifest_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        known.add(line)
    return sorted(
        f for f in staged_files
        if _TEST_FILE_RE.match(f) and f not in known
    )


def staged_new_test_files(root: Path) -> list[str]:
    """git 暂存区新增（A 状态）的 tests/test_*.py；非 git 环境返回空（不拦）。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(root), "diff", "--cached", "--name-only",
             "--diff-filter=A", "--", "tests/"],
            capture_output=True, text=True, encoding="utf-8", timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    manifest = root / "tests" / "gate_manifest.txt"
    if not manifest.exists():
        print("[gate-manifest] 缺 tests/gate_manifest.txt，无法判定新增测试归属，门禁视为失败", file=sys.stderr)
        return 1
    staged = staged_new_test_files(root)
    violations = check_new_test_files(staged, manifest.read_text(encoding="utf-8"))
    if violations:
        print("[gate-manifest] 以下新增测试文件未登记 tests/gate_manifest.txt：", file=sys.stderr)
        for f in violations:
            print(f"  - {f}", file=sys.stderr)
        print("  处置：登记进 tests/gate_manifest.txt（本地与 CI 同一份清单，<1s 的", file=sys.stderr)
        print("  回归门应入册；确实慢的也要登记，由 pre-commit 的分片并行消化）。", file=sys.stderr)
        print("  skip 豁免层已于 2026-11-21 取消，不要再新增。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

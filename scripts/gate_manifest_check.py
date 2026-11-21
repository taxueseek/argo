#!/usr/bin/env python3
"""gate_manifest_check.py — 新增测试文件必须登记快门禁或显式豁免（commit 期硬拦）。

背景（2026-11-21 审计）：test_gate_manifest.py 的反向断言只拦「门禁命名」
文件（gate/consistency/drift/… 词根），test_state_machines_plan_b 与
test_query_operators 两个 <1s 文件漏册未被该机制发现——命名判据盖不全。
本检查把拦截点前移到 commit 时刻：git 暂存区新增（A 状态）的
tests/test_*.py 必须出现在 tests/gate_manifest.txt（入册或 # skip 豁免
均可），否则红灯。存量未登记文件不受影响，零误报；「漏登从靠人记变成
直接红」由此覆盖全部新测试文件，而不只是名字带门禁词根的那一半。

单测：tests/test_gate_manifest.py::TestNewTestFileClassification。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_TEST_FILE_RE = re.compile(r"^tests/test_[a-z0-9_]+\.py$")
_SKIP_RE = re.compile(r"^#\s*skip\s+(tests/test_[a-z0-9_]+\.py)\s+")


def check_new_test_files(staged_files: list[str], manifest_text: str) -> list[str]:
    """返回违规清单：staged 新增 tests/test_*.py 既未入册也未登记 # skip。"""
    known: set[str] = set()
    for raw in manifest_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = _SKIP_RE.match(line)
            if m:
                known.add(m.group(1))
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
        print("[gate-manifest] 以下新增测试文件未登记快门禁、也未登记 # skip 豁免：", file=sys.stderr)
        for f in violations:
            print(f"  - {f}", file=sys.stderr)
        print("  处置：在 tests/gate_manifest.txt 登记一行（单文件 <1s 且提交期高频回归才入册），", file=sys.stderr)
        print("  或明确豁免：# skip tests/test_xxx.py <理由>", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""affected_tests.py — L2 变更感知增量选测引擎。

定位（MECE 三层测试分流）：
  - L1 全局硬契约门：tests/gate_manifest.txt（每次 commit 必全跑）；
  - L2 变更感知选测：本脚本按 git 改动模块反查 343 个未入册单测中的受影响子集，
    在严格时间预算内随 pre-commit 自动执行，补齐「本地绿、全量红」盲区；
  - L3 全量回归：pytest tests/ -q（发版前或 CI 运行）。

判定规则（纯标准库 AST，无外部依赖）：
  1. 扫描 scripts/ 与 tests/test_*.py 的静态 import / from-import 关系；
  2. 对改动模块沿反向依赖图做最多 2 跳传递展开，找出导入它们的未入册测试；
  3. 叠加文件名 stem 启发式召回（如 scripts/foo.py → tests/test_foo*.py），
     覆盖通过 importlib 动态加载的测试；
  4. 排除已在 gate_manifest.txt 中的 L1 测试（避免重复执行），按
     tests/.test_durations 历史耗时升序挑选，单文件超阈值或超总预算的慢测
     显式跳过并打印提示，留给 L3 全量回归。
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
TESTS_DIR = ROOT / "tests"
MANIFEST_PATH = TESTS_DIR / "gate_manifest.txt"
DURATIONS_PATH = TESTS_DIR / ".test_durations"


def extract_imported_modules(path: Path) -> set[str]:
    """用 AST 提取 Python 文件中直接导入的顶层/子模块名。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return set()
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                mods.add(name)
                mods.add(name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
            mods.add(node.module.split(".", 1)[0])
            for alias in node.names:
                if alias.name != "*":
                    mods.add(f"{node.module}.{alias.name}")
    return mods


def load_manifest_entries(manifest_path: Path = MANIFEST_PATH) -> set[str]:
    """读取 L1 gate_manifest.txt 中已登记的测试相对路径集合。"""
    if not manifest_path.is_file():
        return set()
    entries: set[str] = set()
    for raw in manifest_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def load_file_durations(durations_path: Path = DURATIONS_PATH) -> dict[str, float]:
    """将 tests/.test_durations 按测试文件聚合为 {rel_path: total_seconds}。"""
    if not durations_path.is_file():
        return {}
    try:
        raw = json.loads(durations_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    totals: dict[str, float] = defaultdict(float)
    if isinstance(raw, dict):
        for nodeid, secs in raw.items():
            if not isinstance(nodeid, str) or not isinstance(secs, (int, float)):
                continue
            rel = nodeid.split("::", 1)[0]
            totals[rel] += float(secs)
    return dict(totals)


def _path_to_module_names(rel_path: str) -> set[str]:
    """将仓内脚本路径转为可能的 import 模块名集合。"""
    p = Path(rel_path)
    if p.suffix != ".py":
        return set()
    parts = p.parts
    names: set[str] = set()
    if len(parts) >= 2 and parts[0] == "scripts":
        sub = parts[1:]
        stem_parts = list(sub[:-1]) + [p.stem]
        if p.stem == "__init__" and len(stem_parts) >= 2:
            stem_parts = stem_parts[:-1]
        dotted = ".".join(stem_parts)
        if dotted:
            names.add(dotted)
            names.add(stem_parts[-1])
            names.add(f"scripts.{dotted}")
    else:
        names.add(p.stem)
    return names


def build_reverse_script_graph(scripts_dir: Path = SCRIPTS_DIR) -> dict[str, set[str]]:
    """构建 scripts/ 内部的反向依赖图：{被导入模块: {导入它的模块, ...}}。"""
    rev: dict[str, set[str]] = defaultdict(set)
    if not scripts_dir.is_dir():
        return rev
    for py in sorted(scripts_dir.rglob("*.py")):
        rel = py.relative_to(scripts_dir.parent).as_posix()
        mod_names = _path_to_module_names(rel)
        if not mod_names:
            continue
        imported = extract_imported_modules(py)
        for dep in imported:
            rev[dep].update(mod_names)
    return rev


def expand_affected_modules(
    changed_files: Iterable[str],
    reverse_graph: dict[str, set[str]],
    *,
    max_hops: int = 2,
) -> tuple[set[str], set[str]]:
    """展开改动文件对应的模块集合及直接改动测试集合。

    返回 (受影响模块名集合, 直接改动的测试文件相对路径集合)。
    """
    seeds: set[str] = set()
    direct_tests: set[str] = set()
    for raw in changed_files:
        norm = Path(raw).as_posix().lstrip("./")
        if norm.startswith("tests/test_") and norm.endswith(".py"):
            direct_tests.add(norm)
        elif norm.startswith("scripts/") and norm.endswith(".py"):
            seeds.update(_path_to_module_names(norm))

    visited = set(seeds)
    queue: deque[tuple[str, int]] = deque((m, 0) for m in seeds)
    while queue:
        mod, hop = queue.popleft()
        if hop >= max_hops:
            continue
        for dependent in reverse_graph.get(mod, ()):
            if dependent not in visited:
                visited.add(dependent)
                queue.append((dependent, hop + 1))
    return visited, direct_tests


def select_affected_tests(
    changed_files: Iterable[str],
    *,
    root: Path = ROOT,
    manifest_entries: set[str] | None = None,
    file_durations: dict[str, float] | None = None,
    budget_seconds: float = 8.0,
    max_single_seconds: float = 3.0,
    max_files: int = 12,
    max_hops: int = 2,
) -> dict[str, object]:
    """根据改动文件列表，筛选应执行的 L2 未入册测试。"""
    scripts_dir = root / "scripts"
    tests_dir = root / "tests"
    manifest = load_manifest_entries(tests_dir / "gate_manifest.txt") if manifest_entries is None else manifest_entries
    durations = load_file_durations(tests_dir / ".test_durations") if file_durations is None else file_durations

    rev_graph = build_reverse_script_graph(scripts_dir)
    affected_mods, direct_tests = expand_affected_modules(
        changed_files, rev_graph, max_hops=max_hops,
    )

    # 改动脚本的原始模块名与 stem（优先于间接传递依赖进入预算）
    direct_mods: set[str] = set()
    changed_stems: set[str] = set()
    for f in changed_files:
        norm = Path(f).as_posix().lstrip("./")
        if norm.startswith("scripts/") and norm.endswith(".py"):
            direct_mods.update(_path_to_module_names(norm))
            if Path(norm).stem != "__init__":
                changed_stems.add(Path(norm).stem)

    direct_candidates: set[str] = set()
    transitive_candidates: set[str] = set()
    for rel in direct_tests:
        if rel not in manifest and (root / rel).is_file():
            direct_candidates.add(rel)

    if (affected_mods or changed_stems) and tests_dir.is_dir():
        for test_file in sorted(tests_dir.glob("test_*.py")):
            rel = f"tests/{test_file.name}"
            if rel in manifest or rel in direct_candidates:
                continue
            stem_hit = any(
                test_file.stem == f"test_{s}" or test_file.stem.startswith(f"test_{s}_")
                for s in changed_stems
            )
            if stem_hit:
                direct_candidates.add(rel)
                continue
            imported = extract_imported_modules(test_file)
            if direct_mods and (imported & direct_mods):
                direct_candidates.add(rel)
            elif affected_mods and (imported & affected_mods):
                transitive_candidates.add(rel)

    # 直接相关测试优先，同层内按历史耗时升序（无历史记录默认估 0.5s）
    scored = sorted(
        direct_candidates | transitive_candidates,
        key=lambda r: (0 if r in direct_candidates else 1, durations.get(r, 0.5), r),
    )
    selected: list[str] = []
    skipped_slow: list[dict[str, object]] = []
    est_total = 0.0

    for rel in scored:
        est = round(durations.get(rel, 0.5), 3)
        if est > max_single_seconds:
            skipped_slow.append({"file": rel, "est_seconds": est, "reason": "single_file_slow"})
            continue
        if len(selected) >= max_files or (selected and est_total + est > budget_seconds):
            skipped_slow.append({"file": rel, "est_seconds": est, "reason": "budget_exceeded"})
            continue
        selected.append(rel)
        est_total += est

    return {
        "selected": selected,
        "skipped": skipped_slow,
        "estimated_seconds": round(est_total, 3),
        "affected_modules_count": len(affected_mods),
    }


def git_changed_files(*, staged: bool = False, diff_ref: str | None = None, root: Path = ROOT) -> list[str]:
    """获取 git 暂存区或相对 ref 的改动文件列表。"""
    if staged:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    elif diff_ref:
        cmd = ["git", "diff", "--name-only", "--diff-filter=ACMR", diff_ref]
    else:
        cmd = ["git", "status", "--porcelain"]
    proc = subprocess.run(
        cmd, cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if proc.returncode != 0:
        return []
    files: list[str] = []
    for line in proc.stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        if not staged and not diff_ref:
            s = s[3:].strip()
        files.append(s)
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L2 变更感知增量选测（补齐未入册单测盲区）")
    ap.add_argument("--staged", action="store_true", help="读取 git staged 改动文件")
    ap.add_argument("--diff-ref", default=None, help="对比指定 git ref 的改动文件（如 HEAD~1）")
    ap.add_argument("--files", nargs="*", default=None, help="显式指定改动文件列表")
    ap.add_argument("--budget", type=float, default=8.0, help="L2 选测总耗时预算（秒，默认 8.0）")
    ap.add_argument("--max-single", type=float, default=3.0, help="单文件最大允许预估耗时（秒）")
    ap.add_argument("--max-files", type=int, default=12, help="单次最多执行的 L2 文件数")
    ap.add_argument("--json", action="store_true", dest="as_json", help="JSON 格式输出")
    ap.add_argument("--run", action="store_true", help="直接用当前解释器执行选中的 L2 测试")
    args = ap.parse_args(argv)

    if args.files is not None:
        changed = list(args.files)
    else:
        changed = git_changed_files(staged=args.staged, diff_ref=args.diff_ref)

    plan = select_affected_tests(
        changed,
        budget_seconds=args.budget,
        max_single_seconds=args.max_single,
        max_files=args.max_files,
    )

    if args.as_json and not args.run:
        print(json.dumps(plan, ensure_ascii=False))
        return 0

    selected: list[str] = plan["selected"]  # type: ignore[assignment]
    skipped: list[dict[str, object]] = plan["skipped"]  # type: ignore[assignment]

    if not args.run:
        for rel in selected:
            print(rel)
        return 0

    if not selected:
        if skipped:
            print(f"[L2-affected] 无预算内增量单测（跳过 {len(skipped)} 个慢测，留给 L3 全量回归）")
        return 0

    print(
        f"[L2-affected] 变更命中 {len(selected)} 个未入册单测"
        f"（预估 {plan['estimated_seconds']}s，跳过 {len(skipped)} 个慢测）: "
        + ", ".join(selected)
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *selected],
        cwd=str(ROOT),
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())

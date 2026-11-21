#!/usr/bin/env python3
"""gate_manifest 一致性门禁：hook 测试清单单真源校验。

scripts/pre-commit 的快门禁清单曾手工枚举在 hook 脚本里，每轮新门禁
都要「漏一道加一道」（hook 头注释史至少 6 轮），且无防漏机制：env/config
对账门里 test_env_alias_consistency / test_env_declaration_consistency /
test_dsh_plugin_env_gate 三道长期不在册——双真源对账类反而漏册。本测试把
清单收口到 tests/gate_manifest.txt 单真源，并反向断言：门禁命名类
（*gate*/*consistency*/*drift*/*snapshot*/*registry*/*contract*/*sync*/*lint*）
的测试必须入册或在同文件登记 skip 豁免——「漏一道」从靠人记变成直接红。
"""

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MANIFEST = TESTS_DIR / "gate_manifest.txt"

# 门禁命名约定：文件 stem 命中任一片段即视为「门禁类」，须入册或登记豁免。
# 新门禁起名遵循该约定即可被自动发现；命名不含这些片段的功能测试不在此列。
GATE_NAME_RE = re.compile(
    r"(gate|consistency|drift|snapshot|registry|contract|sync|lint)")

_SKIP_RE = re.compile(r"^#\s*skip\s+(tests/test_[a-z0-9_]+\.py)\s+(.+)$")


def _load_manifest():
    """返回 (条目列表, {skip 路径: 理由})；注释与空行忽略。"""
    entries, skips = [], {}
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = _SKIP_RE.match(line)
            if m:
                skips[m.group(1)] = m.group(2).strip()
            continue
        entries.append(line)
    return entries, skips


def test_manifest_entries_exist_and_unique():
    entries, _ = _load_manifest()
    assert entries, "gate_manifest.txt 不能为空"
    assert len(entries) == len(set(entries)), "manifest 存在重复条目"
    for rel in entries:
        assert (TESTS_DIR.parent / rel).is_file(), \
            f"manifest 条目文件不存在（路径写错或已删除）: {rel}"


def test_manifest_sorted():
    entries, _ = _load_manifest()
    assert entries == sorted(entries), \
        "manifest 条目必须排序（见文件头约定 1），减少 diff 冲突"


def test_gate_named_tests_are_registered_or_exempted():
    entries, skips = _load_manifest()
    in_manifest = set(entries) | set(skips)
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        rel = f"tests/{path.name}"
        if rel == "tests/test_gate_manifest.py":
            continue
        if GATE_NAME_RE.search(path.stem):
            assert rel in in_manifest, (
                f"门禁命名类测试未入册: {rel} —— 进 tests/gate_manifest.txt "
                "条目，或登记 '# skip ...' 豁免并写明理由")


# 未登记测试总数棘轮（2026-11-21 根因修复）。
#
# 为什么需要：词根判据（上面那条）只盖住名字带 gate/consistency/… 的一半，
# 另一半普通命名的测试可以无限期不登记，只靠 CI 全量分片跑——而本地
# pre-commit 看不到它们。实证代价：本地连续 36 笔未推、CI 零覆盖期间，
# test_http_attribution 与 test_subprocess_encoding_gate 两次「本地绿、
# 全量红」漏进库。
#
# 存量 343 个未登记文件实测合计 ~312s（5.2 分钟），不可能全塞进 20s 红线
# 内的本地 hook，所以这里不追求「清零」，而是把「未登记」从免费变成
# **有限额**：新增测试要么入册、要么让这个数上涨，而它只能减不能增。
# 与 BROAD_SILENT_CATCH_BASELINE / AUTO_EMPTY_PROFILE_BASELINE 同范式。
UNREGISTERED_TEST_BASELINE = 343


def _registered() -> set[str]:
    entries, skips = _load_manifest()
    return set(entries) | set(skips)


def _unregistered() -> list[str]:
    reg = _registered()
    return sorted(
        f"tests/{p.name}" for p in TESTS_DIR.glob("test_*.py")
        if f"tests/{p.name}" not in reg
    )


def test_unregistered_test_count_does_not_grow():
    unreg = _unregistered()
    assert len(unreg) <= UNREGISTERED_TEST_BASELINE, (
        f"未登记测试 {len(unreg)} > 基线 {UNREGISTERED_TEST_BASELINE}："
        "新测试要么进 tests/gate_manifest.txt（<1s 的本地门），要么在 "
        "tests/.test_durations 有耗时记录并靠 CI 兜底；基线只减不增"
    )


def test_skip_entries_point_to_real_files():
    _, skips = _load_manifest()
    for rel, reason in skips.items():
        assert (TESTS_DIR.parent / rel).is_file(), \
            f"skip 豁免指向不存在的文件（防死行累积）: {rel}"
        assert reason, f"skip 豁免必须写明理由: {rel}"


# ── 新增测试文件 commit 期归类检查（scripts/gate_manifest_check.py，2026-11-21）──
# 词根反向断言盖不全（test_state_machines_plan_b / test_query_operators 两个
# <1s 文件漏册实证），拦截点前移到 commit 时刻：staged 新增 tests/test_*.py
# 必须入册或 # skip。此处单测纯函数 check_new_test_files。

import sys

SCRIPTS_DIR_GATE = TESTS_DIR.parent / "scripts"
if str(SCRIPTS_DIR_GATE) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR_GATE))

import gate_manifest_check  # noqa: E402

_MANIFEST_SAMPLE = """
# skip tests/test_slow_heavy.py replay_eval 较重，CI 兜底
tests/test_registered_fast.py
tests/test_gate_named_thing.py
"""


def test_new_test_file_registered_passes():
    violations = gate_manifest_check.check_new_test_files(
        ["tests/test_registered_fast.py", "scripts/foo.py"], _MANIFEST_SAMPLE)
    assert violations == []


def test_new_test_file_skip_exempted_passes():
    violations = gate_manifest_check.check_new_test_files(
        ["tests/test_slow_heavy.py"], _MANIFEST_SAMPLE)
    assert violations == []


def test_new_test_file_unclassified_violates():
    violations = gate_manifest_check.check_new_test_files(
        ["tests/test_state_machines_plan_b.py"], _MANIFEST_SAMPLE)
    assert violations == ["tests/test_state_machines_plan_b.py"]


def test_non_test_files_ignored():
    violations = gate_manifest_check.check_new_test_files(
        ["scripts/new_script.py", "tests/gate_manifest.txt"], _MANIFEST_SAMPLE)
    assert violations == []


def test_current_repo_new_files_would_be_classified():
    """存量自检：本地暂存区当前新增的测试文件必须已归类（本仓真数据零违规）。"""
    root = TESTS_DIR.parent
    staged = gate_manifest_check.staged_new_test_files(root)
    manifest = (TESTS_DIR / "gate_manifest.txt").read_text(encoding="utf-8")
    assert gate_manifest_check.check_new_test_files(staged, manifest) == []

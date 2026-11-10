#!/usr/bin/env python3
"""test_state_literal_drift.py — 已登记状态值不得再以字面量散落。

## 为什么要这道门

`scripts/engine_states.py` 登记了各台状态机的词表，但「登记」不等于「消费面
真的引用」：2026-11-10 审计发现 `fetch_v3` 仍写 `"tls_spoof"` 而词表写
`FETCH_TLS="tls"`、`engine_validate` 与 `engine_admission` 各写一份
`"skipped"`、JS 侧更是一句常量都没引用。改词表不会让任何东西变红，这正是
「隐式状态机」回潮的入口。

本门禁用 AST 扫 `scripts/*.py`，只看**状态字段上下文**（dict 键、赋值目标、
比较左侧），找出那些「值已在词表里、却仍写成字符串字面量」的位置。它不管
未登记的新状态机（那是词表覆盖问题，另由人工审计）；它只钉住已经收口过的
值不许退回去。

## 同名异义白名单

同一个字符串在不同层可以是普通词而非状态值。逐条登记理由，禁止用通配：
- `ok` / `error` / `unknown` / `enabled` / `disabled` 等：既是状态值，也是
  日志文案、HTTP 字段、配置键名，全仓裸写数千处，扫描必然误报。
- `noise` / `good` / `empty`：属于另一台「结果侧语言判定」机，尚未登记进
  `engine_states`，本次不收口，故不纳入已登记值集合。
- `closed`（`matrix_search_eval.py` 的 mock 桩）、`rate_limited`（`paper.py`
  的第三方 API 返回字段）：不是本仓状态机的写入点。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import engine_states as st  # noqa: E402

# 状态字段键名：只有出现在这些键/目标/比较左侧的字符串才算「状态值」。
_STATE_KEYS = frozenset({
    "status", "state", "phase", "stage", "verdict", "outcome", "decision",
    "health", "change_type", "coverage", "fetch_method", "reranker_status",
    "login_state", "level", "strategy", "max_level", "conclusion_cap",
    "access_status", "freshness_cadence", "confidence_tier",
    "corroboration_level", "evidence_tier", "reason",
})

# 已在词表登记、但语义太泛或属于另一台机器的值：不参与扫描。
_EXCLUDED = frozenset({
    # 泛词：日志/HTTP/配置里遍地都是，无法按上下文可靠区分
    "ok", "error", "unknown", "none", "enabled", "disabled", "blocked",
    "partial", "completed", "pass", "fail", "skipped", "free", "low", "api",
    "paid", "high", "medium", "ready", "running", "stopped", "idle", "busy",
    "loading", "unavailable", "auto", "deep", "fast", "budget", "balanced",
    "health", "quality", "all", "general", "http", "parallel", "fulltext",
    "active", "inactive", "empty", "closed", "rate_limited", "open",
    # 另一台「结果侧语言判定」机（result_lang / lang_matrix_probe）尚未登记
    "noise", "good",
})


def _registered_values() -> set[str]:
    vals: set[str] = set()
    for name, value in vars(st).items():
        if name.startswith("_"):
            continue
        if isinstance(value, str):
            vals.add(value)
        elif isinstance(value, (frozenset, set, tuple, list)):
            for item in value:
                if isinstance(item, str):
                    vals.add(item)
        elif isinstance(value, dict):
            for item in value.values():
                if isinstance(item, str):
                    vals.add(item)
    return {v for v in vals
            if re.fullmatch(r"[A-Za-z0-9_?:\-]{2,40}", v)
            and v not in _EXCLUDED}


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _field_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Subscript):
        return _const_str(node.slice)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _state_literals(path: Path) -> list[tuple[int, str]]:
    """返回 (行号, 值)：状态字段上下文里的字符串字面量。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        cands: list[str] = []
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if _const_str(key) in _STATE_KEYS:
                    v = _const_str(value)
                    if v:
                        cands.append(v)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = _const_str(node.value)
            if value and any(_field_key(t) in _STATE_KEYS for t in targets):
                cands.append(value)
        if isinstance(node, ast.Compare) and _field_key(node.left) in _STATE_KEYS:
            for comparator in node.comparators:
                v = _const_str(comparator)
                if v:
                    cands.append(v)
                elif isinstance(comparator, (ast.Tuple, ast.Set, ast.List)):
                    for elt in comparator.elts:
                        v = _const_str(elt)
                        if v:
                            cands.append(v)
        for value in cands:
            out.append((node.lineno, value))
    return out


def test_registered_state_values_are_referenced_not_rewritten():
    """词表里的值不许再以裸字面量出现在状态字段上——改词表必须让这里红。"""
    registered = _registered_values()
    problems = []
    for path in sorted(SCRIPT_DIR.glob("*.py")):
        if path.name in ("engine_states.py",):
            continue
        for lineno, value in _state_literals(path):
            if value in registered:
                problems.append(
                    f"{path.name}:{lineno} 仍手写 {value!r}（应引用 engine_states 常量）")
    assert not problems, (
        "已登记状态值回潮，改词表时这些点不会跟着变：\n  "
        + "\n  ".join(problems))


def test_vocabulary_sets_have_no_duplicate_values_within_a_machine():
    """同一台状态机内不得有两个常量指向同一个值（改名漏改的温床）。"""
    groups = {
        "VALIDATION_STATUSES": st.VALIDATION_STATUSES,
        "CONCLUSION_CAP_STATUSES": st.CONCLUSION_CAP_STATUSES,
        "PLUGIN_STATES": st.PLUGIN_STATES,
        "DAEMON_STATES": st.DAEMON_STATES,
        "RECOVERY_LEVELS": st.RECOVERY_LEVELS,
    }
    for name, values in groups.items():
        assert len(values) == len(set(values)), f"{name} 内有重复值"


def test_fetch_methods_all_covers_the_degraded_set():
    """降级集合必须落在完整全集里：漏一个 method 就静默误判为非降级。"""
    assert st.DEGRADED_FETCH_METHODS <= st.FETCH_METHODS_ALL
    assert st.MARKDOWN_FETCH_METHODS <= st.FETCH_METHODS_ALL
    assert st.is_degraded_fetch_method(st.FETCH_TLS_SPOOF) is True
    assert st.is_degraded_fetch_method(st.FETCH_HTTP_MOBILE) is True
    assert st.is_degraded_fetch_method(st.FETCH_HTTP) is False


@pytest.mark.parametrize("level", sorted(st.RECOVERY_LEVELS))
def test_every_recovery_level_has_a_tier(level):
    """每台登记的等级都要有成本序，避免又出现 L0 落到默认档。"""
    assert st.recovery_level_tier(level) is not None, level

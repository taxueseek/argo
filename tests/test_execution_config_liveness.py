#!/usr/bin/env python3
"""test_execution_config_liveness.py — execution 段「配了不生效」门禁。

背景（2026-09-29）：execution 段连续出现死配置——`max_parallel_engines`
（由 1cf0cb0 接线）、`parallel_timeout`、`retry_on_empty`。后两者全仓无任何
读取点：用户把 `retry_on_empty: false` 改成 `true` 不会有任何变化，也拿不到
任何提示，只能得出「改了没用」的结论。

为什么单独立门而不是并进 `test_usage_doc_gates`：开关门禁扫的是 `ARGO_*`
字符串字面量，配置键走 YAML，是另一条事实链。

判据为什么必须排除 config.py：`DEFAULT_CONFIG["execution"]` 会**重述**同一
批键名，于是「键在代码里出现过」这件事对死键也成立——`parallel_timeout`
正是这样躲过了第一轮人工排查。把 config.py 排除在读取点之外，才让判据指向
「真的有运行时读它」。

范围只到 execution 段下的**标量**键；嵌套段（如 budget）的键由各自策略读取，
不在此门射程。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
CONFIG_YAML = ROOT / "config.yaml"


def _exec_scalar_keys() -> set[str]:
    cfg = yaml.safe_load(CONFIG_YAML.read_text(encoding="utf-8")) or {}
    ex = cfg.get("execution", {}) or {}
    return {k for k, v in ex.items() if not isinstance(v, (dict, list))}


def _referenced(name: str) -> bool:
    """键名是否以字符串字面量出现在「真的会读它」的地方。

    config.py 显式排除：它的 DEFAULT_CONFIG 重述键名，不是读取点。
    """
    pat = re.compile(r'["\']%s["\']' % re.escape(name))
    for f in list(SCRIPTS.glob("*.py")) + list(TESTS.glob("*.py")):
        if f.name == "config.py" or "__pycache__" in f.parts:
            continue
        if pat.search(f.read_text(encoding="utf-8", errors="ignore")):
            return True
    return False


def test_execution_keys_are_read_somewhere():
    dead = sorted(k for k in _exec_scalar_keys() if not _referenced(k))
    assert dead == [], (
        "execution 段有键全仓无读取点（「配了不生效」的假承诺）。"
        "要么接线让它生效，要么删除并说明：\n  " + "\n  ".join(dead))


def test_default_config_has_no_phantom_execution_keys():
    """DEFAULT_CONFIG.execution 不得出现 config.yaml 里没有声明的键。"""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import config as cfgmod

    declared = set(cfgmod.DEFAULT_CONFIG.get("execution", {}))
    phantom = sorted(declared - _exec_scalar_keys())
    assert phantom == [], (
        f"DEFAULT_CONFIG.execution 有 config.yaml 未声明的键（幻影默认值）：{phantom}")

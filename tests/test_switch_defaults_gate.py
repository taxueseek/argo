#!/usr/bin/env python3
"""test_switch_defaults_gate.py — 开关默认值与条数的机械对拍。

## 为什么要这道门

本仓反复出现的低级 bug 有一个共同根因：**同一语义有多处真相，且没有机械
对拍**。已实锤的四例（2026-11-24 逐条复现）：

  1. `argo --help` 的收录数读 `.engine_count` 缓存，缓存键只取 config.yaml
     的 mtime，而引擎声明真源是 `engines/specs/*.yaml`——实际 313、缓存 312、
     catalog 313，`--help` 静默报错数字；
  2. `ARGO_STRAGGLER_GRACE_S` 代码常量 1.5s，usage.md 写「默认 0.5s」
     （0.5 出自一次文档改写，无 A/B 依据）；
  3. `ARGO_SERIAL_STAGGER_S` 同上，代码 0.8s、文档写 0.2s；
  4. `scripts/pre-commit` 打印的条数手抄，manifest 74→76 时不红。

已有的门覆盖了「开关存在性」（test_usage_doc_gates）、「清单登记」
（test_gate_manifest）、「版本号」，但**没有任何一道对拍默认值与计数**。
这正是本文件补的位置。

## 判据（两条，都只做机械比较）

A. **数值型开关的默认值**：usage.md 总表里写了数字默认值的条目，其数字
   必须能在代码里找到对应常量。口径刻意保守——只对拍「显式数字 + 代码里
   同名 env 读取点的字面默认或同名模块常量」，解析不出的条目跳过，避免
   用脆弱启发式制造假红。

B. **pre-commit 打印条数**：hook 的 printf 必须是现算（引用 manifest），
   不允许再出现手写数字——这是第 4 例的直接堵口，也与 AGENTS.md 的
   `test_agents_md_precommit_file_count_matches` 形成双保险。

## 回退验证要求

两条判据都做过回退验证：把默认值改回漂移值 / 把现算改回硬编码，本文件
对应用例必须挂。做法见各用例 docstring。
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
USAGE = (ROOT / "references" / "usage.md").read_text(encoding="utf-8")
PRECOMMIT = (ROOT / "scripts" / "pre-commit").read_text(encoding="utf-8")

# 数值提取：抓完整数字（含小数），允许紧跟单位字母（1.5s / 0.25）。
# 用负向断言排除版本号/路径里的片段（v2.17、3.9）与下划线命名（X_2）。
_NUM_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")

# env 名与模块常量名无推导关系时的显式白名单（值是候选常量名元组）。
# 只登记「确实存在且语义唯一」的映射；推导能命中的不进来。
_CONSTANT_ALIASES: dict[str, tuple[str, ...]] = {
    "ARGO_ROUTE_SAMPLE_RATE": ("_DEFAULT_ROUTE_SAMPLE_RATE",),
}


def _candidate_constant_names(env_name: str) -> tuple[str, ...]:
    """由 env 名推导可能的模块常量名（小写；大小写不敏感查找）。"""
    stem = env_name[len("ARGO_"):].lower()
    cands = [
        "_" + stem,
        "_" + stem + "_s",
        stem,
        stem + "_s",
        "_default_" + stem,
        "_default_" + stem + "_s",
    ]
    return tuple(dict.fromkeys(cands + list(_CONSTANT_ALIASES_LC.get(env_name, ()))))


# 别名表里的名字同样按小写索引查（键是原名，值统一转小写使用）
_CONSTANT_ALIASES_LC = {
    k: tuple(a.lower() for a in v) for k, v in _CONSTANT_ALIASES.items()
}


def _module_constants() -> dict[str, float]:
    """scripts/ 下模块级数值常量（名 → 值）。

    只取字面量赋值：`env_flag("X")` 不带默认实参时，生效值来自这类常量
    （如 `_STRAGGLER_GRACE_S = 1.5` + `_straggler_grace()` 回落），所以
    只扫调用点默认值会漏掉真实默认。

    同时给出**小写索引**：本仓常量大小写不统一（`_STRAGGLER_GRACE_S`、
    `_PRIMARY_GRACE_S` 大写，多数 `_local_seek_grace_s` 小写），而 env 名恒
    大写，按原名推导候选必然一半落空。查找统一走小写键。
    """
    out: dict[str, float] = {}
    lower: dict[str, float] = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[target.id] = float(value)
                lower.setdefault(target.id.lower(), float(value))
    return lower


def _env_default_sites() -> dict[str, set[float]]:
    """代码里 `os.environ.get("ARGO_X", default)` / `env_flag("ARGO_X", default)`
    的字面默认值集合（名为 ARGO_* 的读取点）。

    注意 env_flag/get_env 常常**不带**默认实参（默认在函数内部），所以这类
    调用点产出为空属正常；真正的默认值由模块常量承担，见调用方并集。
    """
    out: dict[str, set[float]] = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get":
                pass
            elif isinstance(func, ast.Name) and func.id in ("env_flag", "get_env"):
                pass
            else:
                continue
            head = node.args[0]
            if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
                continue
            name = head.value
            if not name.startswith("ARGO_"):
                continue
            if len(node.args) < 2:
                continue
            try:
                default = ast.literal_eval(node.args[1])
            except (ValueError, TypeError):
                continue
            if isinstance(default, (int, float)) and not isinstance(default, bool):
                out.setdefault(name, set()).add(float(default))
    return out


class TestSwitchDefaultValues(unittest.TestCase):
    """usage.md 声明的数值默认 ↔ 代码生效默认。"""

    def _doc_rows(self) -> list[tuple[str, str]]:
        rows = []
        for line in USAGE.splitlines():
            if not line.startswith("| `ARGO_") or "默认" not in line:
                continue
            m = re.match(r"\|\s*`(ARGO_[A-Z0-9_]+)`\s*\|[^|]*\|\s*默认\s*([^|]*)\|", line)
            if m:
                rows.append((m.group(1), m.group(2).strip()))
        return rows

    def test_numeric_defaults_match_code(self) -> None:
        """文档写了数字默认值的开关，代码里必须存在同值来源。

        口径：文档侧取该行全部数字；代码侧取三类来源并集——
        (a) 该 env 名读取点的字面默认（`env_flag("ARGO_X", 5)`）；
        (b) 同名候选模块常量（去 ARGO_ 前缀转小写，带/不带 `_S`、`_DEFAULT_`
            前缀等常见变体，见 _candidate_constant_names）；
        (c) 显式登记的白名单映射（命名与 env 名无推导关系的少数派）。
        任一数字命中即通过。找不到任何代码来源的条目**跳过**，不制造假红。

        回退验证：把 usage.md 的尾宽默认改回 0.5s，本条挂。
        """
        constants = _module_constants()
        sites = _env_default_sites()
        mismatches = []
        checked = 0
        for name, text in self._doc_rows():
            doc_nums = [float(x) for x in _NUM_RE.findall(text)]
            if not doc_nums:
                continue
            code_vals: set[float] = set(sites.get(name, set()))
            for cand in _candidate_constant_names(name):
                if cand in constants:
                    code_vals.add(constants[cand])
            if not code_vals:
                continue
            checked += 1
            if not any(any(abs(a - b) < 1e-9 for b in code_vals) for a in doc_nums):
                mismatches.append(f"{name}: 文档 {doc_nums} vs 代码 {sorted(code_vals)}")
        self.assertGreater(checked, 0, "判据空转：一条都没对上，门禁等于没检查")
        self.assertEqual(mismatches, [], "默认值漂移：\n  " + "\n  ".join(mismatches))

    def test_known_drifted_pair_is_fixed(self) -> None:
        """点名锁死已修的两处（防它们被「口径放松」顺手改回去）。"""
        constants = _module_constants()
        self.assertEqual(constants.get("_straggler_grace_s"), 1.5)
        self.assertEqual(constants.get("_primary_grace_s"), 0.8)


class TestPrecommitCountIsComputed(unittest.TestCase):
    """pre-commit 打印的条数必须现算，不允许手抄。"""

    def test_print_count_is_not_hardcoded(self) -> None:
        """printf 里的条数位置必须是 %s（由变量填充），且变量来自 manifest。

        回退验证：把 GATE_COUNT 改回字面量 76，本条挂。
        """
        line = next(
            (ln for ln in PRECOMMIT.splitlines() if "argo 快门禁" in ln and "printf" in ln),
            None,
        )
        self.assertIsNotNone(line, "pre-commit 缺快门禁打印行")
        self.assertIn("%s 条 4 片并行", line, f"打印条数仍是手写数字：{line}")
        self.assertRegex(
            PRECOMMIT,
            r"GATE_COUNT=\$\(grep[^\n]*\$MANIFEST",
            "GATE_COUNT 不是从 manifest 现算的",
        )


if __name__ == "__main__":
    unittest.main()

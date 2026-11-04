#!/usr/bin/env python3
"""lint_stub_targets.py — 测试打桩目标存在性 lint（复发护栏）。

背景：重构搬函数后，测试里的 monkeypatch.setattr / mock.patch 若仍指向旧
模块名，打桩会落在转出副本上成为**空操作**——测试照绿、真行为已变。历史
上至少两例（2026-10-22 `_RETRY_FAST_S` 与 `_seek_py` 打桩旧模块，2026-10-23
CI 假绿第三例），都是全量才红或人工撞见。

本 lint 用 AST 找出 tests/ 里以**字符串字面量**为目标的打桩点，逐个验证
「模块可导入 → 属性链存在」。两条边界如实说明：
  - 打桩目标在「转出副本」上但属性仍在（打桩空转的最隐蔽形态）静态不可
    判定，不在本 lint 范围；那类靠「拆完必须扫 tests 旧模块名打桩点」的
    流程纪律兜底。
  - 模块导入失败按「可选依赖缺失」跳过不计违例——环境缺件是另一类问题；
    只报「模块在、属性不在」这一种确定性错误。
`raising=False` / `create=True` 的创建式打桩是合法用法，显式豁免。
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = SKILL_DIR / "tests"

# 打桩入口：(*, 目标在第一个位置参数的字符串字面量里)
STUB_CALL_NAMES = {"setattr", "patch", "patch_object"}  # patch.object 归一后


def _call_name(node: ast.AST) -> str | None:
    """归一调用名：monkeypatch.setattr → setattr；mock.patch → patch；
    patch.object → patch_object。"""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Attribute):
        if f.attr in ("setattr", "patch"):
            return f.attr
        if f.attr == "object" and isinstance(f.value, ast.Attribute) and f.value.attr == "patch":
            return "patch_object"
    if isinstance(f, ast.Name) and f.id == "patch":
        return "patch"
    return None


def _is_creating_stub(call: ast.Call) -> bool:
    """raising=False / create=True 的创建式打桩：允许造新属性，豁免。"""
    for kw in call.keywords:
        if kw.arg in ("raising", "create") and isinstance(kw.value, ast.Constant):
            if kw.value.value is False and kw.arg == "raising":
                return True
            if kw.value.value is True and kw.arg == "create":
                return True
    return False


def lint_file(path: Path, _resolver=None) -> list[str]:
    """扫描单文件，返回违例（file:line 目标 原因）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []  # 语法错误由 static_lint 门负责，这里不重复报
    violations: list[str] = []
    for node in ast.walk(tree):
        name = _call_name(node)
        if not name or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue  # 对象形态打桩（setattr(obj, "name", …)）是安全形态
        target = first.value
        if "." not in target or not target.strip():
            continue
        if name in ("setattr", "patch") and _is_creating_stub(node):
            continue
        reason = (_resolver or resolve_target)(target)
        if reason:
            try:
                rel = path.relative_to(SKILL_DIR)
            except ValueError:  # 仓外文件（测试正向控制用 tmp_path）：原样呈现
                rel = path
            violations.append(f"{rel}:{node.lineno} {target} {reason}")
    return violations


def resolve_target(target: str) -> str | None:
    """返回违例原因；None = 可验证且通过；模块不可导入返回 None（可选依赖
    缺失按跳过处理，见模块 docstring 的边界说明）。"""
    parts = target.split(".")
    for i in range(len(parts) - 1, 0, -1):
        modname = ".".join(parts[:i])
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue  # 可选依赖缺失/重副作用模块：跳过，不当违例
        cur: Any = mod
        for attr in parts[i:]:
            try:
                cur = getattr(cur, attr)
            except AttributeError:
                return f"模块 {modname} 可导入但属性缺失"
        return None
    return None  # 顶层模块就不可导入：按可选依赖缺失跳过


def lint_tests_dir(tests_dir: Path | None = None) -> list[str]:
    root = tests_dir or TESTS_DIR
    violations: list[str] = []
    for p in sorted(root.glob("test_*.py")):
        violations.extend(lint_file(p))
    return violations


def main() -> int:
    violations = lint_tests_dir()
    if violations:
        for v in violations:
            print(f"❌ {v}", file=sys.stderr)
        print(f"打桩目标 lint：{len(violations)} 条违例", file=sys.stderr)
        return 1
    print("✅ 打桩目标全部可解析")
    return 0


if __name__ == "__main__":
    sys.exit(main())

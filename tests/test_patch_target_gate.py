#!/usr/bin/env python3
"""test_patch_target_gate.py — 打桩点必须打在「读取处」。

## 为什么需要这道门

本仓大量测试用 monkeypatch 换掉模块级入口（668 处 patch/setattr）。这类打桩有一个
**静默失效**模式：名字在目标模块里只是**转出**（`from x import name`），真正读它的
是另一个模块。此时补丁不报错、测试照绿，但隔离已经失效——被测代码用的是真实实现
（真实网络、真实成本系数、真实状态文件）。

2026-09-21 拆模块时被咬到三次：

  1. `search._tokens`（实现搬到 search_rank）——按 AST 属性访问统计裁剪转出清单，
     全量测试红 83 条；
  2. `route._enabled_local_engines`（搬到 route_lang）——单独跑绿、全量跑红；
  3. `search.get_cost_factor`（读取处搬到 search_pipeline）——测试**一直绿**，
     直到把 search.py 的导入删掉才以 AttributeError 暴露：此前那句
     `search.get_cost_factor = lambda _eng: 1.0` 早已是空操作。

第三种最危险：没有失败信号。这道门把它变成失败信号。

## 判据

对每个 `patch("mod.name")` / `patch.object(mod, "name")` / `setattr(mod, "name")`：

  - 若 `mod` 不是本仓模块（标准库/第三方）→ 跳过；
  - 否则：把 `scripts/<mod>.py` 的 **import 行与注释**去掉后，`name` 是否仍以整词
    出现。只出现在 import 行 → 该模块只是转出它，打桩打在转出副本上 → 报错。

## 例外

`EXEMPT` 里的条目要写明理由。**不接受「先放进去」**：真需要豁免，说明该模块确实
以别的方式读到它（例如经 getattr 动态取），并把理由写清楚。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# (模块名, 名字) → 理由。当前为空：全仓 668 处打桩点都打在读取处。
EXEMPT: dict[tuple[str, str], str] = {}


def _patch_sites() -> list[tuple[str, int, str, str]]:
    sites: list[tuple[str, int, str, str]] = []
    files = list((ROOT / "tests").rglob("*.py")) + list(SCRIPTS.rglob("*.py"))
    for path in files:
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not any(k in ast.unparse(node.func) for k in ("patch", "setattr")):
                continue
            for arg in node.args:  # 形式一：字符串 "mod.name"
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        and "." in arg.value):
                    mod, _, name = arg.value.rpartition(".")
                    if name and mod and mod.isidentifier():
                        sites.append((str(path), node.lineno, mod, name))
            # 形式二：模块对象 + 字符串名
            if (len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                target = node.args[0]
                if isinstance(target, ast.Name):
                    modname = target.id
                elif isinstance(target, ast.Attribute):
                    modname = ast.unparse(target).split(".")[-1]
                else:
                    modname = None
                if modname:
                    sites.append((str(path), node.lineno, modname,
                                  node.args[1].value))
    return sites


def _module_body_without_imports(path: Path) -> str:
    """模块源码去掉 import 行与注释（剩下的才算「真的读」）。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    drop: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            drop.update(range(node.lineno, node.end_lineno + 1))
    lines = [l for i, l in enumerate(src.split("\n"), 1) if i not in drop]
    return re.sub(r"#[^\n]*", "", "\n".join(lines))


def test_patch_targets_are_read_by_the_module_they_patch():
    problems = []
    for path, line, mod, name in _patch_sites():
        if (mod, name) in EXEMPT:
            continue
        target = SCRIPTS / f"{mod}.py"
        if not target.is_file():
            continue  # 标准库 / 第三方 / 非本仓模块
        body = _module_body_without_imports(target)
        if not re.search(r"\b" + re.escape(name) + r"\b", body):
            problems.append(
                f"{path}:{line} 打桩 {mod}.{name}——但 {mod}.py 只是转出它"
                f"（名字只出现在 import 行），实现读的是别处：补丁静默失效")
    assert not problems, (
        "打桩点打在转出副本上（测试照绿但隔离失效）。请把打桩改到**读取处**，"
        "或按纪律把该入口按值传入：\n  " + "\n  ".join(problems))


def test_gate_has_teeth(tmp_path, monkeypatch):
    """造一个「目标模块只转出、不读取」的样本，门禁必须报红。"""
    fake_mod = SCRIPTS / "_gate_probe_module.py"
    fake_mod.write_text("from json import dumps  # 只转出，不读取\n", encoding="utf-8")
    try:
        # 直接复用判据（不必真去 patch 一个不存在的名字）
        body = _module_body_without_imports(fake_mod)
        assert not re.search(r"\bdumps\b", body), \
            "判据失效：import 行里的名字没被排除掉"
        fake_mod.write_text("from json import dumps\n\n\ndef f():\n    return dumps({})\n",
                            encoding="utf-8")
        body2 = _module_body_without_imports(fake_mod)
        assert re.search(r"\bdumps\b", body2), "判据失效：真实读取没被认出来"
    finally:
        fake_mod.unlink(missing_ok=True)

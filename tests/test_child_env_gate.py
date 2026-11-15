#!/usr/bin/env python3
"""test_child_env_gate.py — 子进程环境隔离护栏（密钥不随全量继承外泄）。

## 背景

`engine_env.build_child_env` 把 CLI 引擎子进程的环境收敛成
「基础白名单 + 该引擎显式声明的 required_env/allowed_env」。此后仍有一种
形态会绕过它：**显式把全量 os.environ 复制给子进程**（`dict(os.environ)` /
`os.environ.copy()`）。父进程持有多家引擎的 API 密钥，全量继承等于把这些
密钥交给子进程里的代码——若子进程执行的是不可信代码（如 recompute 注入的
研究包脚本），密钥可被 `os.environ` 直接读走。

## 口径（机械，不含人工判断）

- 扫 `scripts/**/*.py`；
- 命中形态：赋值右值为 `dict(os.environ)` / `os.environ.copy()` / `dict(os.environ, ...)`；
- 豁免：右值紧跟 `.pop(...)`/`.setdefault(...)` 代理清理、且所在函数属于
  「内部工具」的登记项——逐条登记在 GRANDFATHERED 并写明理由；
- 新增子进程若确实需要全量环境（如基准测试要复现真实搜索环境），登记时
  必须写明「为何不可信代码不在此列」。

想豁免请改用 `engine_env.build_child_env` 或在本文件登记，不要新写
`dict(os.environ)`。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# 祖父登记：相对路径 → (允许处数, 理由)。
#
# 口径只拦「把全量 os.environ 交给**不可信代码**」。下面两处属内部基准工具：
# 子进程跑的是本仓自己的 `argo` CLI（受测对象），命令由本文件生成、不含外部
# 输入拼进 argv，执行体可信，故全量继承是刻意选择（基准要复现真实运行环境，
# 包括代理与 ARGO_ 开关），不是泄露面。
GRANDFATHERED: dict[str, tuple[int, str]] = {
    "scripts/ab_eval.py": (
        1,
        "A/B 尾延迟基准：--cmd-a/--cmd-b 由调用者给，默认跑本仓 search.py；"
        "全量继承才能复现真实搜索环境（代理/ARGO_*），且只对内部命令注入 "
        "ARGO_USAGE_LOG。子进程不执行不可信代码。",
    ),
    "scripts/bench_general_pool.py": (
        1,
        "通用层选源对拍：子进程跑本仓 argo CLI，命令由本文件生成；"
        "全量继承用于复现真实运行环境（含代理），执行体可信。",
    ),
}


def _is_full_environ_copy(value: ast.AST) -> bool:
    """右值是否为「全量复制 os.environ」形态。"""
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    # dict(os.environ) / dict(os.environ, **kw)
    if isinstance(func, ast.Name) and func.id == "dict":
        return bool(value.args) and _is_environ_expr(value.args[0])
    # os.environ.copy()
    if isinstance(func, ast.Attribute) and func.attr == "copy":
        return _is_environ_expr(func.value)
    return False


def _is_environ_expr(node: ast.AST) -> bool:
    """节点是否为 `os.environ` 或裸 `environ`。"""
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    if isinstance(node, ast.Name) and node.id == "environ":
        return True
    return False


def _full_environ_copies() -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for p in sorted(SCRIPTS.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                value = getattr(node, "value", None)
                if value is None or not _is_full_environ_copy(value):
                    continue
                rel = p.relative_to(ROOT).as_posix()
                rows.append((rel, getattr(node, "lineno", 0), ""))
    return rows


def test_full_environ_copies_are_registered():
    rows = _full_environ_copies()
    unregistered = [
        f"{rel}:{lineno}"
        for rel, lineno, _ in rows
        if rel not in GRANDFATHERED
    ]
    assert not unregistered, (
        "出现未登记的「全量复制 os.environ 给子进程」：\n  "
        + "\n  ".join(unregistered)
        + "\n修法：改用 engine_env.build_child_env（CLI 引擎）或在 "
        "tests/test_child_env_gate.py 的 GRANDFATHERED 登记并写明理由。")

    # 反向：登记数不得超过实际命中数（防死行与虚高，同 module_size_gate 纪律）
    counts: dict[str, int] = {}
    for rel, _lineno, _ in rows:
        counts[rel] = counts.get(rel, 0) + 1
    stale = [f"{rel} 登记 {n} 处但实际 {counts.get(rel, 0)} 处"
             for rel, (n, _why) in GRANDFATHERED.items()
             if counts.get(rel, 0) != n]
    assert not stale, "祖父登记与实际不符：\n  " + "\n  ".join(stale)


def test_gate_has_teeth():
    """判据自检：能识别目标形态、不误伤 build_child_env 与窄口径。"""
    target = ast.parse("env = dict(os.environ)\n")
    (node,) = [n for n in ast.walk(target) if isinstance(n, ast.Assign)]
    assert _is_full_environ_copy(node.value), "dict(os.environ) 未识别"

    copy_form = ast.parse("env = os.environ.copy()\n")
    (node2,) = [n for n in ast.walk(copy_form) if isinstance(n, ast.Assign)]
    assert _is_full_environ_copy(node2.value), "os.environ.copy() 未识别"

    safe = ast.parse("env = build_child_env('twitter', spec)\n")
    (node3,) = [n for n in ast.walk(safe) if isinstance(n, ast.Assign)]
    assert not _is_full_environ_copy(node3.value), "误伤 build_child_env"

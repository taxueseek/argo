#!/usr/bin/env python3
"""静态缺陷检查：拦住「会让代码跑错或静默失效」的写法。

## 为什么需要这道检查

本仓已有一批很讲究的一致性检查（版本四端对账、CommonFlags 契约、环境变量
别名一致性、原生工具 schema 漂移），但**没有任何一道在检查代码本身的静态
缺陷**。2026-09-15 的审查用 ruff F 规则扫 108 个模块，扫出 5 处真缺陷：

- `mcp_transport.py` / `quota.py`：类型注解用了 `Any` 却从未导入。两文件都有
  `from __future__ import annotations`，注解不求值，所以**运行时一直没崩**——
  代价是 `typing.get_type_hints()` 直接抛 NameError、类型检查器对这个模块
  全线失效。这正是它能潜伏至今的原因。
- `cache.py`：`_DOMAIN_MAP` 里 `"english_tech"` 写了两遍。两次值相同，当前
  无行为差异，但日后只改其中一处就会静默失效——在 4522 行 config + 61 个
  spec 的规模下，这类重复靠肉眼几乎不可能发现。

这类缺陷的共同点：**不报错、不崩溃、只是悄悄不生效**。靠 code review 发现
它们的概率极低，应该由检查兜住。

## 收了哪些规则

只收「会跑错 / 会静默失效」的，不收风格类：

| 规则 | 危害 |
|------|------|
| E9 | 语法与 IO 错误 |
| F821 | 未定义名 → 运行时 NameError |
| F822 | `__all__` 里的未定义名 |
| F823 | 局部变量引用前赋值 |
| F811 | 重定义（静默覆盖前一个） |
| F601 | 字典字面量重复键（静默覆盖） |
| F631 | `assert` 一个元组（恒为真，等于没检查） |
| F632 | 用 `==` 比较字面量（多为 `is` 笔误） |
| F701 / F702 | `break` / `continue` 在循环外 |
| F704 / F706 / F707 | `yield` / `return` / `except` 位置非法 |

**刻意不收** F401（未使用导入，存量 106 处）与 F841（未使用变量，17 处）：
`scripts/` 不是包，ruff 无法区分「真未使用」与「被其他模块 re-export」，
一刀切会打断导入链。已抽查确认 F841 多为无害冗余（算了未用），非逻辑错误，
这两类留待人工复核后另行决定。

## 扫描范围

`scripts/` + `tests/` + `bin/argo`。把 `tests/` 纳入是有实际收益的：扩展范围
当天就在 `test_tinyfish_fallback.py` 抓到一处 F821——`_isolate_envfile` 用了
`Path` 却没导入，因为写在 `lambda` 体内而一直没被求值（24 项测试全绿），一旦
有代码路径真的调用 `_envfile_path()` 就会当场 NameError。

## 双引擎设计

- **ruff**（环境有 uv / ruff 时）：覆盖上表全部规则，判定最准。
- **内建 ast**（零依赖）：**总会跑**，至少覆盖字典重复键。

两者都跑，任一报红即失败。内建引擎的意义是：即便在没装 ruff 的机器上，
检查也仍然有效——只是覆盖的规则少一些。

两个引擎各自配了自检用例（`test_gate_has_teeth_*`）：先故意写一个坏样本，
确认检查真的抓得住。少了这一步，检查很容易变成永远通过的摆设。

静态源码扫描，无网络。
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
BIN = ROOT / "bin" / "argo"
TARGETS = [str(SCRIPTS), str(TESTS), str(BIN)]

# 「低于 MIN_PYTHON 就解析不了」的语法扫描器。逻辑独立成模块
# （scripts/min_version_scan.py）而不是塞在本文件里：它有自己的一套 AST/token
# 判据与用例，且 bin/argo 侧也可能用到。这里只做检查装配。
sys.path.insert(0, str(SCRIPTS))
import min_version_scan  # noqa: E402

# 只列「会跑错 / 会静默失效」的规则。基线：本文件落地时全仓零违规。
RULES = "E9,F821,F822,F823,F811,F601,F631,F632,F701,F702,F704,F706,F707"

# ruff 探测顺序：uvx（uv 自带）→ 模块方式 → PATH 上的可执行文件
RUFF_CANDIDATES = (
    ("uvx", "ruff"),
    (sys.executable, "-m", "ruff"),
    ("ruff",),
)


def _resolve_ruff() -> list[str] | None:
    """返回可用的 ruff 调用前缀；都不可用返回 None。"""
    for cand in RUFF_CANDIDATES:
        exe = shutil.which(cand[0]) or (cand[0] if os.path.isabs(cand[0]) else None)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, *cand[1:], "--version"],
                               capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return [exe, *cand[1:]]
    return None


def _ruff_findings(targets: list[str]) -> list[str] | None:
    """跑 ruff，返回违规行列表；ruff 不可用返回 None。

    用 JSON 输出而非 concise：concise 在「无违规」时会打印
    "All checks passed!"，按行解析会把这句话当成一条发现——本检查首次
    落地时就是这么红的（等于把「全绿」误判成「有问题」）。
    """
    prefix = _resolve_ruff()
    if prefix is None:
        return None
    r = subprocess.run(
        [*prefix, "check", *targets, "--select", RULES,
         "--no-cache", "--output-format", "json"],
        capture_output=True, text=True, timeout=300,
    )
    # ruff 无违规时 exit 0；有违规 exit 1；调用异常 exit 2（配置/用法错误）
    if r.returncode not in (0, 1):
        raise AssertionError(f"ruff 调用异常（rc={r.returncode}）：{r.stderr[:400]}")
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as e:
        raise AssertionError(f"ruff 输出无法解析：{e}；stdout={r.stdout[:200]!r}")
    return [
        "{}:{}:{} {} {}".format(
            Path(d.get("filename", "?")).name,
            d.get("location", {}).get("row", "?"),
            d.get("code", "?"),
            d.get("message", ""),
            f"(col {d['location']['column']})" if d.get("location", {}).get("column") else "",
        ).strip()
        for d in data
    ]


def _module_level_names(tree: ast.Module) -> set[str]:
    """模块顶层真正会绑定名字的语句（赋值/def/class/import/try 里的赋值）。"""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.Try):  # try: import X / try: X = ... 的常见形态
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    names.update((a.asname or a.name).split(".")[0]
                                 for a in sub.names)
                elif isinstance(sub, ast.Assign):
                    names.update(t.id for t in sub.targets
                                 if isinstance(t, ast.Name))
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    names.add(sub.target.id)
    return names


def _shadowed_reexports(paths: list[Path]) -> list[str]:
    """内建 ast 检测：模块级既 import 了 X，又自己赋值了 X。

    拆模块后 route.py / search.py 成了「门面」——它们把实现模块的符号转出，
    调用方与测试继续按旧路径引用。这类门面最危险的写法是**同名再定义一份**：
    `from search_rank import _CACHE` 之后又写 `_CACHE = None`，于是门面自己那份
    才是外部看到/打桩改到的，实现模块读的仍是自己那份——测试全绿、行为静默分叉。

    合法的例外只有一种：`try: from X import Y / except ImportError: Y = None`
    这类兜底（import 与赋值同处一个 Try 节点内），本检查已排除。
    """
    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        imported: dict[str, int] = {}
        assigned: dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    imported.setdefault((a.asname or a.name).split(".")[0], node.lineno)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        assigned.setdefault(t.id, node.lineno)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                assigned.setdefault(node.target.id, node.lineno)
            elif isinstance(node, ast.Try):
                t_imported, t_assigned = set(), set()
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        t_imported.update((a.asname or a.name).split(".")[0]
                                          for a in sub.names)
                    elif isinstance(sub, ast.Assign):
                        t_assigned.update(t.id for t in sub.targets
                                          if isinstance(t, ast.Name))
                    elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                        t_assigned.add(sub.target.id)
                for name in t_imported:
                    imported.setdefault(name, node.lineno)
                for name in t_assigned - t_imported:
                    assigned.setdefault(name, node.lineno)
        for name in sorted(set(imported) & set(assigned)):
            problems.append(
                f"{_rel(path)}:{assigned[name]} 本地定义了 {name}"
                f"（第 {imported[name]} 行已从别处导入 → 门面副本会遮蔽实现）")
    return problems


def _globals_without_binding(paths: list[Path]) -> list[str]:
    """内建 ast 检测：`global X` 声明的名字在模块级没有绑定。

    为什么 ruff F821 抓不到：`global X` 本身就是一次「X 在模块级」的声明，
    ruff 据此认为该名已定义，于是 `global _CACHE; return _CACHE`（_CACHE 从未
    赋值）零告警。运行时只在走到那一行 NameError，而这类行常在冷门分支。
    """
    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # 语法错误交由 ruff/E9 报
        bound = _module_level_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Global):
                continue
            for name in node.names:
                if name not in bound:
                    problems.append(
                        f"{_rel(path)}:{node.lineno} global {name}"
                        f"（模块级没有定义 → 走到该行即 NameError）")
    return problems


def _local_import_shadows_global(paths: list[Path]) -> list[str]:
    """内建 ast 检测：函数内的裸 import 遮蔽了同名的模块级绑定。

    这是 2026-09-25 实际发生过的两个 P0（search_output.shape_response 与
    search_pipeline.postprocess）：模块级 `import logging`，函数内**某个 except
    分支**里又写了一次 `import logging`。CPython 在**编译期**就把 `logging`
    判定为该函数的局部变量——不管那次 import 是否执行到。于是同一函数里
    其余 3~5 处 `logging.getLogger(...)` 全部编译成 LOAD_FAST（读局部槽），
    走到就抛 `UnboundLocalError`。

    为什么现有门禁与 ruff 都抓不到，两者是同一个盲区：
      - ruff F821 只查「这个名字全仓都没定义过」。`logging` 在模块级**确实**
        定义过，F821 判为合法。
      - `_globals_without_binding` 只查 `global X` 声明，而这里根本没有 global。
    后果被放大了三倍：这些 `logging.*` 全部位于 `except` 处理器内，处理器里
    再抛异常会**顶替掉原本被 fail-open 吞掉的错误**——一个「增强失败不得让搜索
    失败」的路径，变成搜索整体崩溃。

    判定条件（三条同时满足才报，避免误伤正常局部 import）：
      1. 函数内某处有裸 `import X`（`import X as Y` 不算，不遮蔽原名）；
      2. X 在模块级有绑定（否则它是本就该有的局部，函数内 import 是正确写法）；
      3. X 在该函数内被读取，且**不保证**在 import 之前执行过。
    """
    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # 语法错误交由 ruff/E9 报
        module_bound = _module_level_names(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            imported: dict[str, list[int]] = {}
            reads: list[tuple[int, str]] = []
            for node in ast.walk(fn):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.asname is None:  # `import X as Y` 不遮蔽 X
                            imported.setdefault(alias.name, []).append(node.lineno)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    reads.append((node.lineno, node.id))
            conditional_imports = _imports_inside_branches(fn)
            for name, lines in imported.items():
                if name not in module_bound:
                    continue  # 本来就是局部名，函数内 import 是对的
                first = min(lines)
                # 函数体首部的 import 一定先于后续读取执行（`def f(): import x` 是
                # 最常见也最正确的写法）；只有落进条件分支的 import 才是缺陷——
                # 那种写法下「import 执行到」不再是前提。
                if not any(lineno in conditional_imports for lineno in lines):
                    continue
                exposed = sorted(
                    lineno for lineno, read_name in reads
                    if read_name == name and lineno > first)
                if not exposed:
                    continue
                # 读取全部落在 import 所在的 try 块内 → 该 try 执行到就安全
                if _reads_guarded_by(fn, name, first, exposed):
                    continue
                shown = ", ".join(str(x) for x in exposed[:3])
                more = "…" if len(exposed) > 3 else ""
                problems.append(
                    f"{_rel(path)}:{exposed[0]} {fn.name}() 读 '{name}'，"
                    f"但它只在第 {first} 行的条件分支里被 import 绑定"
                    f"（{shown}{more}）；未走到那行即 UnboundLocalError")
    return problems


def _imports_inside_branches(fn: ast.AST) -> set[int]:
    """函数体内「落进条件/异常/循环分支」的 import 行号集合。

    关键区别：`def f(): import x` 里的 import 是**无条件执行**的，后续读取必然
    在它之后——这是最常见的正确写法。只有当 import 位于 `if` / `try` / `except` /
    `with` / `for` / `while` 内部时，「import 已执行」才不再是前提，遮蔽才会致命。
    2026-09-25 的两个 P0 都属于后者。
    """
    conditional: set[int] = set()

    def visit(node: ast.AST, inside_branch: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                if inside_branch:
                    conditional.add(child.lineno)
            elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For,
                                     ast.While, ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef, ast.Lambda)):
                # 嵌套 def/lambda 是独立作用域，不算本函数的分支
                nested = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                            ast.ClassDef, ast.Lambda))
                visit(child, inside_branch or not nested)
            else:
                visit(child, inside_branch)

    visit(fn, False)
    return conditional


def _reads_guarded_by(fn: ast.AST, name: str, import_line: int,
                      exposed: list[int]) -> bool:
    """exposed 里的读取是否全部被「含该 import 的 try 块」包住。

    `try: import X ... except: X.log()` 里的读取是安全的：要么 import 成功
    （读已绑定），要么抛异常走 except 分支——except 分支若也裸读 X 仍不安全，
    所以这里只认**在 try 体内**的读取。
    """
    guarded: set[int] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        has_import = any(
            isinstance(inner, (ast.Import, ast.ImportFrom))
            and any((a.asname or a.name).split(".")[0] == name for a in inner.names)
            for stmt in node.body for inner in ast.walk(stmt))
        if not has_import or import_line not in {
            inner.lineno for stmt in node.body
            for inner in ast.walk(stmt)
            if isinstance(inner, (ast.Import, ast.ImportFrom))}:
            continue
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load) \
                        and inner.id == name:
                    guarded.add(inner.lineno)
    return bool(guarded) and set(exposed) <= guarded


def _duplicate_dict_keys(paths: list[Path]) -> list[str]:
    """内建 ast 检测：字典字面量里的重复常量键。

    只认字面量常量键——`{k: 1, **other, k2: 2}` 这类动态键不在静态可判范围。
    零依赖引擎：没有任何外部工具时，这道检查依然拦得住最典型的「静默覆盖」。
    """
    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:  # 语法错误本身也是缺陷，交由 ruff/E9 报
            problems.append(f"{path.name}:{e.lineno} 语法错误：{e.msg}")
            continue
        for node in ast.walk(tree):
            # 字典重复键与集合重复元素是同一类问题：后者不会被覆盖（集合自动去重），
            # 但「同一个字面量里写了两遍」通常是复制粘贴留下的意图不明处，
            # 与 F601 是同一类漏检（F601 只管字典）。两者共用一套查重逻辑。
            if isinstance(node, ast.Dict):
                literals = node.keys
                kind = "字典重复键"
                consequence = "后者会覆盖前者，只生效一个"
            elif isinstance(node, ast.Set):
                literals = node.elts
                kind = "集合重复元素"
                consequence = "被自动去重，通常意味着这里想写的东西没写进去"
            else:
                continue
            seen: set[object] = set()
            for key in literals:
                if not isinstance(key, ast.Constant):
                    continue
                if key.value in seen:
                    problems.append(
                        f"{_rel(path)}:{node.lineno} {kind} {key.value!r}"
                        f"（{consequence}）")
                seen.add(key.value)
    return problems


def _authorization_flags_are_explicit(paths: list[Path]) -> list[str]:
    """授权类布尔开关必须写明 `expand=False, strict=True`。

    为什么这道检查要存在（2026-09-15 审查发现）：`env_flag` 走 `get_env`，而
    单名字符串会展开成「原名 + 去前缀裸名」候选链——这是给密钥准备的便利
    （`X_API_KEY` 与 `ARGO_X_API_KEY` 都认）。落在**授权位**上就变成了授权扩张：
    `ARGO_ALLOW_RECOMPUTE` 是「允许 argo 在受限子进程里执行脚本」的放行位，
    展开后环境里任何一个工具随手设的 `ALLOW_RECOMPUTE=1`（少写前缀的一个无关
    变量）就等于替用户放行了。

    另一项 `strict=True`（只认 1/true/yes/on/y/t 这类明确真值）同样必要：默认
    计算方式是「非关即开」，对能力开关没问题（最坏换个行为），对授权位就是「拼错
    的值一律放行」——`ARGO_ALLOW_RECOMPUTE=0x0`、`=maybe`、`=ture` 都会授权。

    判据：调用名 `env_flag`、第一个实参是以 `ARGO_ALLOW_` 开头的字符串字面量
    时，必须同时显式写 `expand=False` 与 `strict=True`。能力开关（超时、并发、
    渲染降级）不受此限——它们的别名容忍度与宽松取值是特性，误触后果只是换个
    行为，不是放行。
    """
    problems: list[str] = []
    want = {"expand": False, "strict": True}
    for path in paths:
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # 语法错误由 ruff/E9 报，这里不重复
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else "")
            if name != "env_flag" or not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            if not first.value.startswith("ARGO_ALLOW_"):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            missing = [
                f"{key}={val}"
                for key, val in want.items()
                if not (isinstance(kw.get(key), ast.Constant)
                        and kw[key].value is val)
            ]
            if missing:
                problems.append(
                    f"{_rel(path)}:{node.lineno} 授权开关 {first.value} 未写明 "
                    f"{'、'.join(missing)}——授权只能认字面名与明确真值")
    return problems


def _shell_var_before_multibyte(paths: list[Path]) -> list[str]:
    """shell 脚本里「变量引用后面紧贴非 ASCII 字符」且没加花括号的写法。

    为什么这是缺陷而不是风格（2026-09-15 实测）：macOS 的 `/bin/sh` 是 bash 3.2
    的 POSIX 模式，它把紧跟 `$PY_VER` 的中文逗号当成标识符的一部分，于是
    `echo "当前 Python 为 $PY_VER，需要 3.10+。"` 在 `set -u` 下报
    `PY_VER<0xEF>: unbound variable`——用户在 `sh scripts/install.sh` 或
    `env -i`（容器、cron、CI 里 LANG 未设）时看到的报错与真实原因毫无关系。
    写成 `${PY_VER}` 在所有 shell 与 locale 下都无歧义，零成本。

    两类脚本的危险面不同，判据也不同：
    - `.sh`：bash 3.2 会把**任何**非 ASCII 字节并入变量名（含标点）→ 全报；
    - `.ps1`：PowerShell 只在后继字符是字母/数字时才会并进标识符
      （`$name中文` 会被解析成另一个变量、静默取到 $null），标点无害 → 只报前者。
    """
    var = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
    problems: list[str] = []
    for path in paths:
        if not path.is_file() or path.suffix not in (".sh", ".ps1"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in var.finditer(line):
                nxt = line[m.end():m.end() + 1]
                if not nxt or ord(nxt) <= 0x7F:
                    continue
                if path.suffix == ".ps1" and not nxt.isalnum():
                    continue        # PowerShell 标识符在标点处结束，无歧义
                why = ("sh/POSIX 模式会把中文并入变量名" if path.suffix == ".sh"
                       else "PowerShell 会解析成另一个变量名")
                problems.append(
                    f"{_rel(path)}:{lineno} 变量引用 {m.group(0)} 后紧贴非 ASCII 字符"
                    f"（{why}）：{line.strip()[:70]}")
    return problems


def _rel(path: Path) -> str:
    """相对仓库根的路径：子目录递归后 path.name 会与顶层同名文件混淆。"""
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def _iter_shell_files() -> list[Path]:
    """shell / PowerShell 脚本清单（含仓库根的 *.sh，如安装脚本副本）。"""
    files = [p for p in sorted(SCRIPTS.glob("*.sh"))]
    files += [p for p in sorted(SCRIPTS.glob("*.ps1"))]
    files += [p for p in sorted(ROOT.glob("*.sh"))]
    return files


def _iter_target_files() -> list[Path]:
    # rglob 而非 glob：子目录（social_engines/、redskill/ 等）是「按域分家」的
    # 引擎自然落脚处，只扫顶层等于漏掉新增出口/新增模块的默认位置——本轮
    # 就是这样漏掉了 8 处绕开出口调度的 urlopen。
    files = [p for p in sorted(SCRIPTS.rglob("*.py")) if "__pycache__" not in p.parts]
    files += [p for p in sorted(TESTS.rglob("*.py")) if "__pycache__" not in p.parts]
    if BIN.is_file():
        files.append(BIN)
    return files


def _envfile_internal_writes(files: list[Path]) -> list[str]:
    """扫描对 engine_env 内部缓存状态的外部写入。

    这两个变量必须同生同灭（见 engine_env.reset_envfile_cache 的说明）。
    外部只改其中一个——尤其是「把签名恢复成旧值」——会让签名与内容错位：
    签名与真实文件对得上，读取函数就直接返回那份**不属于该文件**的缓存，
    本进程内的密钥读取永久失效。现场表现是「单独跑通过、与别的测试同跑
    失败」，排查成本极高（2026-09-15 实测确认）。需要强制重读请用
    engine_env.reset_envfile_cache()。
    """
    problems: list[str] = []
    for path in files:
        if path.name == "engine_env.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for tgt in targets:
                name = getattr(tgt, "attr", None)
                if name in ("_envfile_sig", "_envfile_cache"):
                    problems.append(
                        f"{_rel(path)}:{node.lineno} 直接写 engine_env 的 {name}"
                        f"——两个变量必须一起清，请改用 reset_envfile_cache()")
            # patch.object(engine_env, "_envfile_sig", ...) 是同一类写法，
            # 少了这一支就等于给最常见的测试手法留后门
            if isinstance(node, ast.Call):
                fn = node.func
                if (isinstance(fn, ast.Attribute) and fn.attr == "object"
                        and len(node.args) >= 2
                        and getattr(node.args[0], "id", None) == "engine_env"
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value in ("_envfile_sig",
                                                   "_envfile_cache")):
                    problems.append(
                        f"{_rel(path)}:{node.lineno} 用 patch.object 改 engine_env "
                        f"的 {node.args[1].value}——同样会错位，请改用 "
                        f"reset_envfile_cache()")
    return problems


class TestStaticLintGate(unittest.TestCase):
    """静态缺陷检查本体。"""

    def test_no_external_writes_to_envfile_internals(self):
        """engine_env 的缓存与签名只能由 engine_env 自己改。"""
        problems = _envfile_internal_writes(_iter_target_files())
        self.assertEqual(
            problems, [],
            "外部直接写了 engine_env 的内部缓存状态（签名与内容会错位）：\n  "
            + "\n  ".join(problems))

    def test_no_duplicate_dict_keys(self):
        """零依赖引擎：字典重复键（不依赖 ruff，任何环境都跑）。"""
        problems = _duplicate_dict_keys(_iter_target_files())
        self.assertEqual(
            problems, [],
            "字典字面量存在重复键（后者静默覆盖前者）：\n  "
            + "\n  ".join(problems))

    def test_authorization_flags_use_exact_names(self):
        """授权开关必须 expand=False + strict=True：别名展开与宽松取值都是授权扩张。"""
        problems = _authorization_flags_are_explicit(_iter_target_files())
        self.assertEqual(
            problems, [],
            "授权类开关没写明 expand/strict（少写前缀的变量、拼错的值都可能放行）：\n  "
            + "\n  ".join(problems))

    def test_global_names_have_module_level_bindings(self):
        """函数里 `global X` 声明的名字，模块级必须有定义。

        ruff 的 F821 会放过这一类：`global _X` 让 ruff 认为 _X 是模块级名，
        于是 `def f(): global _X; return _X` 在 _X 从未定义时也判为合法。运行时
        只有在走到那一行才 NameError——而那一行往往在冷门分支里（2026-09-21
        拆 search.py 时踩到：`_SCORE_FLOORS_CACHE` 只在「域相关性地板」首次
        计算时被读到，测试全绿、排序照常，直到某条查询走进那个分支）。
        """
        problems = _globals_without_binding(_iter_target_files())
        self.assertEqual(
            problems, [],
            "`global X` 没有模块级定义（走到那行才 NameError，静态门抓不到）：\n  "
            + "\n  ".join(problems))

    def test_local_import_shadows_global(self):
        """函数内裸 import 遮蔽同名模块级绑定 → 同函数其余读取即 UnboundLocalError。

        2026-09-25 的两个 P0：search_output.shape_response（本地正文索引 /
        域过滤 / include-local 三处 except 处理器）与 search_pipeline.postprocess
        （时间窗过滤 / 恢复 / 本地 rerank 三处）。当时 2854 条测试全绿。
        """
        problems = _local_import_shadows_global(_iter_target_files())
        self.assertEqual(
            problems, [],
            "函数内 import 遮蔽了模块级同名符号，走到未保护的读取即 "
            "UnboundLocalError（fail-open 会变成 fail-closed）：\n  "
            + "\n  ".join(problems))

    def test_shell_vars_braced_before_multibyte(self):
        """shell 脚本里变量引用后紧贴中文必须加花括号（POSIX 模式会把中文并入变量名）。"""
        problems = _shell_var_before_multibyte(_iter_shell_files())
        self.assertEqual(
            problems, [],
            "未加花括号的写法在 sh/POSIX 模式与最小 locale 下会报 unbound variable：\n  "
            + "\n  ".join(problems))

    def test_no_ruff_findings(self):
        """ruff 引擎：覆盖 RULES 全量规则。ruff 不可用时跳过（不阻塞）。"""
        findings = _ruff_findings(TARGETS)
        if findings is None:
            self.skipTest("环境无 ruff（uv / ruff 均不可用），仅内建 ast 引擎生效")
        self.assertEqual(
            findings, [],
            "静态缺陷（会让代码跑错或静默失效）：\n  " + "\n  ".join(findings))

    def test_no_syntax_below_min_python(self):
        """全仓源码不得使用低于 MIN_PYTHON 的语法（本次 P0：job.py 的 PEP 701）。"""
        minver = min_version_scan.read_min_python(BIN)
        problems = min_version_scan.scan_paths(_iter_target_files(), minver)
        self.assertEqual(
            problems, [],
            f"存在最低支持版本 {minver[0]}.{minver[1]} 解析不了的语法"
            "（用户会直接 SyntaxError，且解释器缓存会把它固化）：\n  "
            + "\n  ".join(problems))

    def test_min_python_is_declared_and_consistent(self):
        """MIN_PYTHON 必须可读，且三处带版本判据的落地脚本都与它一致。

        mcp_launch.sh 曾经漏在检查之外：它同样硬编码版本判据、注释里同样写着
        「改一处就要改另外两处」，但只 gate 了 install.sh / install.ps1。于是
        MIN_PYTHON 调整时它会静默落后——把版本抬高的那侧（探测门槛）落单，
        表现是 MCP 客户端日志里的「server 未就绪」，而 CLI 路径一切正常。
        """
        minver = min_version_scan.read_min_python(BIN)
        self.assertEqual(len(minver), 2)
        self.assertGreaterEqual(minver, (3, 8), f"MIN_PYTHON 读不到或过于保守：{minver}")
        want = f"({minver[0]}, {minver[1]})"
        for rel in ("scripts/install.sh", "scripts/install.ps1",
                    "scripts/mcp_launch.sh"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            # 用 assertTrue 而非 assertIn：后者在断言失败时会把整个文件
            # 打进报告（这里都是脚本全文），真正要读的那一行反而被淹没。
            self.assertTrue(
                want in text,
                f"{rel} 的版本判据与 bin/argo 的 MIN_PYTHON={want} 不一致"
                "——各写一份版本号正是本轮 P0 的成因")

    def test_gate_has_teeth_min_python_rule(self):
        """造 3.9 解析不了的样本，规则必须抓住；等价的安全写法不得误报。"""
        # PEP 701：内嵌同类引号（3.12+；3.9~3.11 上是 SyntaxError）
        bad = 'x = f"Bearer {get_env(["A_KEY", "B_KEY"])}"\n'
        found = min_version_scan.scan_text(bad, (3, 9))
        self.assertTrue(found, "PEP 701 样本没被抓住")

        # 等价安全写法：先取变量（这正是本次 P0 的修法）
        good = 'tok = get_env(["A_KEY"])\nx = f"Bearer {tok}"\n'
        self.assertEqual(
            min_version_scan.scan_text(good, (3, 9)), [],
            "安全写法被误报——先取变量再拼接在 3.9 上合法")

        # 外层单引号、内层双引号：3.9 也合法，不得误报
        legal = 'x = f\'{"quoted"}\'\n'
        self.assertEqual(
            min_version_scan.scan_text(legal, (3, 9)), [],
            "异类引号被误报（3.9 合法）")

        # PEP 604 用在真类型上、且不在注解位（无 future import）→ 3.9 会崩
        risky = ("CACHE: dict[str, int] = {}\n"
                 "Handler = Callable | None\n")
        self.assertTrue(
            min_version_scan.scan_text(risky, (3, 9)),
            "PEP604 用在类型上但无 future import 时未报出")

        # 更典型的真实形态：模块级变量注解 + 无 future import
        ann = "CACHE: dict[str, tuple[int, int] | None] = {}\n"
        self.assertTrue(
            min_version_scan.scan_text(ann, (3, 9)),
            "模块级变量注解里的 PEP604 未报出")

        # 真位运算 + 正则交替：均不得误报（这是最容易翻车的一类）
        for safe in ("flags = READ | WRITE\n",
                     'p = re.compile(r"(hrss|rsj)")\n',
                     "fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)\n"):
            self.assertEqual(
                min_version_scan.scan_text(safe, (3, 9)), [],
                f"位运算/正则交替被误报：{safe!r}")

        # 有 future import 时，注解位的 X | None 合法
        with_future = ("from __future__ import annotations\n"
                       "def f(x: int | None) -> str | None:\n    return None\n")
        self.assertEqual(
            min_version_scan.scan_text(with_future, (3, 9)), [],
            "有 future import 的注解被误报")

    # ── 故意造错验证：证明两个引擎都不是恒真的摆设 ────────────────────────────

    def test_gate_has_teeth_ast_engine(self):
        """造一个含重复键的样本，ast 引擎必须抓住。"""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad_sample.py"
            bad.write_text(
                "MAP = {\n"
                "    'alpha': 'x',\n"
                "    'beta': 'y',\n"
                "    'alpha': 'z',\n"
                "}\n",
                encoding="utf-8")
            problems = _duplicate_dict_keys([bad])
        self.assertEqual(len(problems), 1, f"造错样本没被抓住：{problems}")
        self.assertIn("alpha", problems[0])

    def test_gate_has_teeth_shell_var_rule(self):
        """造一个 `$VAR，` 样本，规则必须抓住；加花括号的写法不得误报。"""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.sh"
            bad.write_text(
                'set -u\nPY_VER=3.9\n'
                'echo "当前 Python 为 $PY_VER，需要 3.10+"\n'
                'echo "正确写法 ${PY_VER}，不报"\n',
                encoding="utf-8")
            ps_letter = Path(td) / "bad.ps1"
            ps_letter.write_text('Write-Host "$name中文"\n', encoding="utf-8")
            ps_punct = Path(td) / "ok.ps1"
            ps_punct.write_text('Write-Host "$name，标点无害"\n', encoding="utf-8")
            problems = _shell_var_before_multibyte([bad, ps_letter, ps_punct])
        self.assertEqual(len(problems), 2, f"造错样本没被抓住或误报：{problems}")
        assert any("bad.sh" in p for p in problems) and any("bad.ps1" in p for p in problems)
        assert not any("ok.ps1" in p for p in problems), "PowerShell 标点场景被误报"

    def test_gate_has_teeth_authorization_rule(self):
        """造一个漏写 expand/strict 的授权开关，两种写法都必须被抓住。"""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad_flag.py"
            bad.write_text(
                "from engine_env import env_flag\n\n"
                "def a():\n"
                "    return env_flag('ARGO_ALLOW_RECOMPUTE', default=False)\n\n"
                "def b():\n"
                "    return env_flag('ARGO_ALLOW_RECOMPUTE', default=False, expand=False)\n\n"
                "def ok_capability():\n"
                "    return env_flag('ARGO_FETCH_JINA')\n\n"
                "def ok_auth():\n"
                "    return env_flag('ARGO_ALLOW_RECOMPUTE', default=False,\n"
                "                    expand=False, strict=True)\n",
                encoding="utf-8")
            problems = _authorization_flags_are_explicit([bad])
        self.assertEqual(len(problems), 2, f"造错样本没被抓住：{problems}")
        self.assertTrue(all("ARGO_ALLOW_RECOMPUTE" in p for p in problems))

    def test_no_shadowed_reexports(self):
        """门面模块不得既转出又本地定义同名符号（静默遮蔽实现）。"""
        problems = _shadowed_reexports(_iter_target_files())
        self.assertEqual(
            problems, [],
            "模块级同名重复定义（本地这份会遮蔽转出的实现，打桩与读取分叉）：\n  "
            + "\n  ".join(problems))

    def test_gate_has_teeth_shadowed_reexport(self):
        """造一个「先 import 再本地赋值」的样本，规则必须抓住；
        合法的 try/except ImportError 兜底不得误报。"""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad_shadow.py"
            bad.write_text(
                "from other import _CACHE\n\n_CACHE = None\n",
                encoding="utf-8")
            problems = _shadowed_reexports([bad])
            good = Path(td) / "good_shadow.py"
            good.write_text(
                "try:\n"
                "    from other import _CACHE\n"
                "except ImportError:\n"
                "    _CACHE = None\n",
                encoding="utf-8")
            clean = _shadowed_reexports([good])
        self.assertEqual(len(problems), 1, f"造错样本没被抓住：{problems}")
        self.assertIn("_CACHE", problems[0])
        self.assertEqual(clean, [], f"合法兜底被误报：{clean}")

    def test_gate_has_teeth_global_without_binding(self):
        """造一个 `global X` 但 X 从未在模块级赋值的样本，规则必须抓住；
        正常写法（模块级有定义）不得误报。"""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad_global.py"
            bad.write_text(
                "def f():\n"
                "    global _CACHE\n"
                "    if _CACHE is None:\n"
                "        _CACHE = {}\n"
                "    return _CACHE\n",
                encoding="utf-8")
            problems = _globals_without_binding([bad])
            good = Path(td) / "good_global.py"
            good.write_text(
                "_CACHE = None\n\n\n"
                "def f():\n"
                "    global _CACHE\n"
                "    _CACHE = {}\n"
                "    return _CACHE\n",
                encoding="utf-8")
            clean = _globals_without_binding([good])
        self.assertEqual(len(problems), 1, f"造错样本没被抓住：{problems}")
        self.assertIn("_CACHE", problems[0])
        self.assertEqual(clean, [], f"正常写法被误报：{clean}")

    def test_gate_has_teeth_local_import_shadows_global(self):
        """造两份样本：一份复刻 2026-09-25 的 P0 原文，规则必须抓住；
        一份是正常写法（别名 / 函数首行 import / 本就该是局部名），不得误报。"""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad_shadow.py"
            # 逐字复刻 search_output.shape_response 的缺陷形态
            bad.write_text(
                "import logging\n\n\n"
                "def shape():\n"
                "    try:\n"
                "        x = 1\n"
                "    except Exception:\n"
                "        import logging\n"
                "        logging.getLogger('a').debug('in-try')\n"
                "    try:\n"
                "        y = 2\n"
                "    except Exception:\n"
                "        logging.getLogger('a').debug('out-of-try')\n",
                encoding="utf-8")
            problems = _local_import_shadows_global([bad])

            good = Path(td) / "good_shadow.py"
            # 三种合法写法：别名 import / 函数首行 import / 无模块级绑定的局部名
            good.write_text(
                "import logging\n\n\n"
                "def alias_ok():\n"
                "    import logging as _lg\n"
                "    return _lg.getLogger('a')\n\n\n"
                "def first_stmt_ok():\n"
                "    import json\n"
                "    return json.dumps({})\n\n\n"
                "def local_ok():\n"
                "    import tomllib\n"
                "    return tomllib\n\n\n"
                "def guarded_ok():\n"
                "    try:\n"
                "        import yaml\n"
                "        return yaml.safe_load('a')\n"
                "    except ImportError:\n"
                "        return None\n",
                encoding="utf-8")
            clean = _local_import_shadows_global([good])
        self.assertEqual(len(problems), 1, f"造错样本没被抓住：{problems}")
        self.assertIn("logging", problems[0])
        self.assertIn("shape", problems[0])
        self.assertEqual(clean, [], f"合法写法被误报：{clean}")

    def test_gate_has_teeth_ruff_engine(self):
        """造一个用未导入 `Any` 的样本，ruff 必须抓住（对应本次修复的缺陷）。"""
        if _resolve_ruff() is None:
            self.skipTest("环境无 ruff")
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad_annotation.py"
            bad.write_text(
                "from __future__ import annotations\n\n\n"
                "def f(x: dict[str, Any]) -> None:\n"
                "    pass\n",
                encoding="utf-8")
            findings = _ruff_findings([str(bad)])
        self.assertIsNotNone(findings)
        self.assertTrue(
            any("F821" in ln for ln in findings),
            f"造错样本没被抓住（应报 F821）：{findings}")


if __name__ == "__main__":
    unittest.main()

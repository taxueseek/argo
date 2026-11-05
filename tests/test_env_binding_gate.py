"""test_env_binding_gate.py — 模块导入期 env 绑定护栏（B 类复发护栏之三）。

## 根因链（为什么是第 3 道复发护栏）

290d3d8 的完整叙事：跨用例/跨进程共享的进程级状态在导入期绑定 env，导入后
改写环境（测试临时目录、多工作树、子进程换缓存根）时进程内仍持旧值。同根因
此前已修 4 处（290d3d8 状态路径、ec5b959 熔断器工厂、7190961 测试 env 快照、
310f772 社交子进程 env），2026-11-06 再修 5 处（fetch_v3._IDENTITY_PATH、
job.MCPJOBS_DIR/ENTRY、engine_admission.DEFAULT_ADMISSION_DIR、
local_image.DEFAULT_DB、redskill_engine.CACHE_DIR/CACHE_FILE）。本护栏把
「不许再长」变成机器检查：真要动态解析走 PEP 562 `__getattr__`（290d3d8
模式），要短进程读放函数体内。

## 口径

- 扫 `scripts/**/*.py` 与 `argo_search/**/*.py` 的 Module 直接子节点：
  Assign / AnnAssign / AugAssign 的值、函数定义的默认参数；
- 子树出现 `os.environ` / `os.getenv` / 裸名 `environ`（from os import
  environ）即计一次；
- 只拦直接读：经 `state_path()`/`db_path()` 等解析器的间接绑定是另一笔账，
  不在本护栏口径内（避免误伤成片的模块级路径常量；要收口同样走 290d3d8
  模式逐模块处理）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 祖父登记：相对路径 → (允许处数, 理由)。2026-11-06 起为零——全仓 5 处已全部
# 改 PEP 562 调用期解析。确有必要新增时在此登记并写明理由；登记后归零也要
# 随手清掉（同 module_size_gate 的祖父纪律）。
GRANDFATHERED: dict[str, tuple[int, str]] = {}

_SCAN_ROOTS = ("scripts", "argo_search")


def _env_read(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == "environ":
            return True
        if isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv"):
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id == "getenv":
            return True
    return False


def _module_level_env_reads(tree: ast.Module) -> list[int]:
    """Module 直接子节点里的 env 读绑定行号（赋值与函数默认参数）。"""
    lines: list[int] = []
    for st in tree.body:
        vals: list[ast.AST] = []
        if isinstance(st, (ast.Assign, ast.AugAssign)):
            vals = [st.value]
        elif isinstance(st, ast.AnnAssign) and st.value:
            vals = [st.value]
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            vals = list(st.args.defaults) + [d for d in st.args.kw_defaults if d]
        if any(_env_read(v) for v in vals):
            lines.append(st.lineno)
    return lines


def _scan() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for base in _SCAN_ROOTS:
        for p in sorted((ROOT / base).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            hits = _module_level_env_reads(tree)
            if hits:
                out[str(p.relative_to(ROOT))] = hits
    return out


def test_no_module_level_env_bindings():
    actual = _scan()
    problems: list[str] = []
    for rel, hits in sorted(actual.items()):
        if rel not in GRANDFATHERED:
            problems.append(
                f"{rel}: 模块导入期读 env（第 {','.join(map(str, hits))} 行）。"
                "动态解析走 PEP 562 __getattr__（290d3d8 模式），短进程读放"
                "函数内；确要保留在 GRANDFATHERED 登记并写明理由")
        elif len(hits) > GRANDFATHERED[rel][0]:
            problems.append(
                f"{rel}: env 读 {len(hits)} 处，超过祖父登记 "
                f"{GRANDFATHERED[rel][0]} 处")
    for rel, (n, _reason) in GRANDFATHERED.items():
        got = len(actual.get(rel, ()))
        if got == 0:
            problems.append(f"{rel}: 祖父登记已失效（现 0 处），请随手清掉")
        elif got != n:
            problems.append(f"{rel}: env 读 {got} 处 ≠ 登记 {n} 处，请对齐登记")
    assert not problems, "模块导入期 env 绑定护栏：\n  " + "\n  ".join(problems)


def test_gate_covers_both_roots():
    """口径自检：两个扫描根都真实存在（目录重构时别忘了改这里）。"""
    for base in _SCAN_ROOTS:
        assert (ROOT / base).is_dir(), base

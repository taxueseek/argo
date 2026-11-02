#!/usr/bin/env python3
"""tests/test_circuit_breaker_module_alias.py — 熔断器工厂的模块别名分裂回归。

守的行为：``search_v3`` 与 ``circuit_breaker`` 在任何时刻都指向**同一个**模块
对象。这不是测试洁癖，是生产正确性——

  ``circuit_breaker.STATE_PATH`` 由 ``ARGO_STATE_DIR`` 在导入期派生；
  ``CircuitBreaker`` 的默认路径在 ``def`` 求值期绑定；
  ``get_breaker()`` 是模块级单例，实例存 ``_breaker``。

于是「谁在导入期绑定了 ``get_breaker``」决定了谁和谁说话：

  绑定方（导入期）→ 绑的是**那一刻**的模块对象 A
  调用方（运行期）→ ``import circuit_breaker`` 拿的是**当前**的模块对象 B

A 与 B 不是同一个对象时，单例被拆成两个：``A._breaker`` 与 ``B._breaker``
各持一个实例、各写一个状态文件。实测症状是「记录熔断」与「读取熔断」写读
两个文件——本地 ``local_sogou`` 的 ``record_failure`` 写进 B 的路径，
``search_v3`` 的 ``allow()`` 读 A 的路径，断言永远为假。

触发条件： ``sys.modules.pop("circuit_breaker")`` 后重导入（本仓
``test_argo_paths`` 的 ``test_module_paths_under_state_root`` 正是这么做的，
它用 ``ARGO_STATE_DIR`` 换临时目录来验证路径派生）。

复现（修复前，全套件必红、单跑全绿）::

    pytest tests/test_argo_paths.py \\
           tests/test_local_search_consensus_grace.py::test_circuit_breaker_drops_open_engine

修法： ``search_v3`` 改为**调用期**从 ``sys.modules`` 解析工厂，不绑函数对象。
本文件锁住这条不变量，防止再有人在导入期写回 ``from circuit_breaker import ...``。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "sub-skills" / "local-search", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture
def breaker_modules():
    """确保两侧模块都在场，并在用例后复位（不改动调用方的模块缓存）。"""
    import circuit_breaker  # noqa: F401
    import search_v3  # noqa: F401
    yield
    # 复位：把可能在用例里被 pop 掉的模块重新装回，避免污染同进程的后续用例
    for _name in ("circuit_breaker", "search_v3"):
        if _name not in sys.modules:
            importlib.import_module(_name)


def test_search_v3_resolves_factory_at_call_time(breaker_modules):
    """search_v3 不得在导入期绑定 get_breaker 的函数对象。

    判据是语义的而非文本的：模拟 ``sys.modules.pop`` 后重导入，
    search_v3 解析到的工厂必须**就是**新模块的工厂。
    """
    import circuit_breaker
    import search_v3

    old_module = sys.modules["circuit_breaker"]
    old_factory = search_v3._get_circuit_breaker

    sys.modules.pop("circuit_breaker", None)
    try:
        new_module = importlib.import_module("circuit_breaker")
        assert new_module is not old_module, "前置条件：重导入应产生新模块对象"

        resolved = search_v3._get_circuit_breaker()
        assert resolved is not None, "search_v3 未能解析到熔断器（ ImportError 被吞？）"

        # 核心不变量：调用期解析到的是**当前**模块的单例，不是旧模块的。
        assert resolved is new_module.get_breaker(), (
            "search_v3 解析到的是旧模块对象的单例——"
            "记录熔断与读取熔断会写读两个不同的状态文件"
        )
        assert resolved is not old_module.get_breaker(), (
            "旧模块的单例仍被 search_v3 持有——别名分裂未消除"
        )
    finally:
        # 把旧模块装回去：让后续用例（以及本进程内其他测试）拿到稳定对象
        sys.modules["circuit_breaker"] = old_module


def test_factory_never_binds_module_object_at_import(breaker_modules):
    """静态判据：search_v3 里不得出现导入期 ``from circuit_breaker import``。

    与上一条互补——那条证明「现在是对的」，这条防止「以后被改回去」。
    文本判据在这里是可接受的：要拦的是**一个具体的已知错误写法**，
    而不是某种语义属性。
    """
    src = (ROOT / "sub-skills" / "local-search" / "search_v3.py").read_text(
        encoding="utf-8")
    for lineno, line in enumerate(src.splitlines(), 1):
        # 只认**模块级** import 语句。判据是行首无缩进：
        #   - 缩进的 `import circuit_breaker`（函数内）正是本仓既有做法，合法；
        #   - 注释与 docstring 里出现同名写法也合法（docstring 正是要解释这个坑）。
        if not line or line[0].isspace():
            continue
        is_module_level_import = (
            line.startswith("from circuit_breaker import")
            or line.startswith("import circuit_breaker")
        )
        assert not is_module_level_import, (
            f"search_v3.py:{lineno} 又在模块级绑定 circuit_breaker：{line!r}\n"
            "改为函数内 `import circuit_breaker` 后取 circuit_breaker.get_breaker()，"
            "否则 sys.modules.pop 重导入会让单例分裂成两个。"
        )

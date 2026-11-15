#!/usr/bin/env python3
"""test_cli_engine_interpreter.py — `type: cli` 引擎必须用 argo 自己的解释器。

## 为什么这条契约值得单独立一个门

argo 的解释器是**选出来的**（bin/argo 的 `_probe_python`：版本达标 + 真 import
PyYAML，不达标就 execv 换一个再跑）。`type: cli` 引擎是另一条进程，它若自己从
PATH 现找一个 `python3`，两条进程就可能不是同一个解释器——于是就出现
「argo 本体一切正常、引擎子进程却 ModuleNotFoundError」。

2026-10-21 实测踩中（macOS，无 brew shellenv）：`/opt/homebrew/bin` 不在
非交互式 shell 的 PATH 上，`shutil.which("python3")` 拿到 `/usr/bin/python3`
（3.9，无 PyYAML），而 argo 本体已被 execv 换到 3.14。后果不是崩，而是**静默
降级**——weather/wx.py 等 6 个 spec 的子进程报 ModuleNotFoundError，上游把
`returncode != 0` 的调用归成「0 条结果」，于是：

  - weather 单次耗时 5.8s（帧启动 + 失败）且结果为 0；
  - weather_query 域 p50 被抬到 3071ms，成为本机最慢域。

修法是把解释器来源统一到 `sys.executable`（见 engines_base._build_cli_engine）。
本测试用「PATH 上放一个诱饵 python3」把这条契约钉死：无论 PATH 说什么，引擎
必须跑在 `sys.executable` 上。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import engines_base  # noqa: E402


class TestCliEngineUsesArgoInterpreter(unittest.TestCase):
    """cmd[0] == "python3" 的 spec，其解释器必须解析成 sys.executable。"""

    def _capture(self, spec: dict, which_returns: str | None):
        """跑一次引擎，返回它实际要执行的 argv；不发真实子进程。"""
        seen: dict[str, list[str]] = {}

        def fake_run(cmd, timeout=8, engine_name="?", env=None):
            seen["cmd"] = list(cmd)
            seen["env"] = env
            return ""  # 空 stdout → 解析出 []，本用例只关心 argv

        engine = engines_base._build_cli_engine(spec)
        with patch.object(engines_base, "_run", fake_run), \
                patch.object(shutil, "which", lambda name: which_returns):
            engine("q", 3)
        return seen.get("cmd") or []

    def test_prefers_sys_executable_over_path_python3(self):
        """核心回归：PATH 上有别的 python3 也不能用它。

        `which_returns` 是一个**绝不可能是**真实解释器的诱饵路径——若实现退回
        `shutil.which`，argv[0] 就会是它，断言立刻失败并指出问题。
        """
        argv = self._capture(
            {"cmd": ["python3", "scripts/wx.py"], "search_args": [], "_name": "t"},
            which_returns="/decoy/bin/python3",
        )
        self.assertTrue(argv, "引擎没有产出任何命令，用例前提被破坏")
        self.assertEqual(
            argv[0], sys.executable,
            "cli 引擎没有使用 argo 自己的解释器：PATH 上的 python3 可能缺 PyYAML"
            "（历史 bug：weather 等 6 个 spec 因此静默返回 0 条）",
        )
        self.assertEqual(argv[1:], ["scripts/wx.py"], "其余参数不应被改动")

    def test_absolute_interpreter_in_spec_is_left_alone(self):
        """spec 写了绝对路径就是显式指定，不该被 sys.executable 覆盖。"""
        argv = self._capture(
            {"cmd": ["/opt/venv/bin/python3", "scripts/wx.py"],
             "search_args": [], "_name": "t"},
            which_returns="/decoy/bin/python3",
        )
        self.assertEqual(argv[0], "/opt/venv/bin/python3")

    def test_non_python_cli_engine_untouched(self):
        """非 python 的 cli 引擎（node/rg 等）不得被改写。"""
        argv = self._capture(
            {"cmd": ["node", "cli.js"], "search_args": [], "_name": "t"},
            which_returns="/decoy/bin/python3",
        )
        self.assertEqual(argv[:2], ["node", "cli.js"])


if __name__ == "__main__":
    unittest.main()

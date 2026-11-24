#!/usr/bin/env python3
"""test_help_covers_dispatch.py — `--help --all` 必须列全分发表命令。

## 为什么要这道门（2026-11-24 实测）

8e8347e 把 `--help` 改成三层折叠（核心原语 / 保留工作流 / 专有与运维），
默认视图 28 行、`--all` 44 行。实测发现 `--all` 只列出 31 个命令，而
bin/argo 的分发表有 35 个——`doctor`、`engine`、`settings`、`ui` 四个
缺席。这四个恰是常用运维入口（源健康审计、引擎注册与复活、设置页、
issue #44 承诺的 `argo ui` 唤起词），用户在 `--help` 里找不到就会以为
不存在；而它们全都写在 references/usage.md 里，所以「文档有没有」的既有
门禁（test_usage_doc_gates）对此完全无感——它比的是 usage.md 与分发表，
不比 help 文本。

判据：分发表里的每个命令名都必须出现在 `argo --help --all` 的输出里。
新增命令时若只改分发表而漏了 help 视图，本条直接红。

回退验证：把 `--all` 里任意一行删掉，本条挂（见 test_gate_has_teeth）。
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_ARGO = ROOT / "bin" / "argo"

# 分发表形态：`"name": ("script.py", [...])`
_DISPATCH_RE = re.compile(r'"([a-z][a-z_-]*)":\s*\("')
# help 文本形态：`argo name`（含 local-image / ego-search 这类连字符名）
_HELP_CMD_RE = re.compile(r"argo ([a-z][a-z-]*)")


def _dispatch_commands() -> set[str]:
    return set(_DISPATCH_RE.findall(BIN_ARGO.read_text(encoding="utf-8")))


def _help_all_commands() -> set[str]:
    """从 `_usage_all()` 的源码文本提取命令名（进程内调用，不联网、不受缓存影响）。"""
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("argo_cli_help", str(BIN_ARGO))
    spec = importlib.util.spec_from_loader("argo_cli_help", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return set(_HELP_CMD_RE.findall(mod._usage_all()))


class TestHelpCoversDispatch(unittest.TestCase):
    def test_every_dispatch_command_appears_in_help_all(self) -> None:
        dispatch = _dispatch_commands()
        listed = _help_all_commands()
        self.assertTrue(dispatch, "分发表解析为空——形态漂移了")
        missing = sorted(dispatch - listed)
        self.assertEqual(
            missing, [],
            f"`argo --help --all` 未列出这些分发表命令：{missing}——"
            "用户按 help 找命令，漏一个等于该能力对Agent不可见",
        )

    def test_help_all_is_not_tiny(self) -> None:
        """视图规模下限：折叠后仍应覆盖大部分命令，防「折叠」退化成「隐藏」。"""
        dispatch = _dispatch_commands()
        listed = _help_all_commands()
        self.assertGreaterEqual(
            len(listed), int(len(dispatch) * 0.9),
            f"--all 只列 {len(listed)}/{len(dispatch)} 个命令，视图退化",
        )


class TestGateHasTeeth(unittest.TestCase):
    """自检：判据必须能区分「列全」与「漏一个」，否则恒真。"""

    def test_detector_finds_missing_command(self) -> None:
        dispatch = {"search", "fetch", "doctor"}
        listed = {"search", "fetch"}          # 故意漏 doctor
        self.assertEqual(sorted(dispatch - listed), ["doctor"])

    def test_real_help_all_parses_to_expected_shape(self) -> None:
        listed = _help_all_commands()
        self.assertIn("search", listed)
        self.assertIn("local-image", listed)   # 连字符名不被正则截断
        self.assertNotIn("list-engines", listed)  # 不是分发表条目


if __name__ == "__main__":
    unittest.main()

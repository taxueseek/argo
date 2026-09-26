#!/usr/bin/env python3
"""test_usage_doc_gates.py — 使用文档防漂移门禁。

「文档说的能力必须真存在，真存在的能力必须在文档里」：
  1. bin/argo 分发表每个命令都在 references/usage.md 有提及；
  2. usage.md 里 `argo <cmd>` 引用的命令必须真在分发表（防幻影命令）；
  3. usage.md 提到的 ARGO_* 开关必须代码实存（防幻影开关）。
命令的唯一事实=bin/argo 分发表；开关的唯一事实=源码扫描。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
USAGE = (REPO / "references" / "usage.md").read_text(encoding="utf-8")
BIN_ARGO = (REPO / "bin" / "argo").read_text(encoding="utf-8")


def _dispatch_commands() -> set[str]:
    """bin/argo 分发表（'命令': ("脚本", ...)）的唯一事实提取。

    命令名允许连字符（`local-image`）：提取正则不含 `-` 时，文档里的
    `argo local-image index` 会被截成 `local` 并报「幻影命令」——问题其实
    在提取规则比命名规则窄，不在文档。
    """
    return set(re.findall(r'"([a-z][a-z_-]*)":\s*\("', BIN_ARGO))


class TestUsageDocCommands(unittest.TestCase):
    def test_every_dispatch_command_is_documented(self):
        missing = {c for c in _dispatch_commands()
                   if c != "stats" and not re.search(rf"argo {c}\b", USAGE)}
        self.assertEqual(missing, set(),
                         f"分发表命令未在 usage.md 文档化（补命令节）：{missing}")

    def test_doc_commands_are_real(self):
        doc_cmds = set(re.findall(r"`?argo ([a-z][a-z_-]*)", USAGE))
        phantom = doc_cmds - _dispatch_commands()
        self.assertEqual(phantom, set(),
                         f"usage.md 引用了不存在的命令：{phantom}")


class TestUsageDocSwitches(unittest.TestCase):
    def test_doc_switches_exist_in_code(self):
        import subprocess
        out = subprocess.run(
            ["git", "grep", "-hoE", r'"ARGO_[A-Z_0-9]+"', "--", "scripts/", "bin/argo"],
            capture_output=True, text=True, cwd=REPO).stdout
        code_vars = set(re.findall(r"ARGO_[A-Z_0-9]+", out))
        doc_vars = set(re.findall(r"ARGO_[A-Z_0-9]+", USAGE))
        phantom = doc_vars - code_vars
        self.assertEqual(phantom, set(),
                         f"usage.md 写了代码里不存在的开关：{phantom}")

    def test_switch_table_covers_code_vars(self):
        """代码新增开关必须入表（总表节双向锁定，防表外漂移）。"""
        import subprocess
        out = subprocess.run(
            ["git", "grep", "-hoE", r'"ARGO_[A-Z_0-9]+"', "--", "scripts/", "bin/argo"],
            capture_output=True, text=True, cwd=REPO).stdout
        code_vars = set(re.findall(r"ARGO_[A-Z_0-9]+", out))
        m = re.search(r"## 功能开关总表.*?(?=\n## |\Z)", USAGE, re.S)
        self.assertIsNotNone(m, "usage.md 缺「功能开关总表」节")
        table_vars = set(re.findall(r"ARGO_[A-Z_0-9]+", m.group(0)))
        self.assertEqual(code_vars - table_vars, set(),
                         f"代码有但总表未收录（表是唯一事实）：{code_vars - table_vars}")


if __name__ == "__main__":
    unittest.main()

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


def _code_switches() -> set[str]:
    """扫描源码里实存的 ARGO_* 开关（开关的唯一事实）。

    历史 bug（2026-09-27 实测定位）：原实现是
        subprocess.run(["git", "grep", "-hoE", r'"ARGO_[A-Z_0-9]+"', ...])
    `git grep` **只搜索已跟踪文件**。新建的模块（当次改造拆出的
    scripts/rank_signals.py）在 `git add` 之前是 `??` 状态，门禁看不见它，
    于是把实存于代码的 ARGO_RELEVANCE_V2 / ARGO_COMPLETENESS_V2 /
    ARGO_DOMAIN_CONCENTRATION 判成「usage.md 写了代码里不存在的开关」——
    报错方向恰好与事实相反，把排查引向文档，实际病因在工作区暂存状态。

    换成 Python 自己的遍历：读的是**磁盘真实内容**，与 git 索引状态无关。
    附带两个好处：不必在跑测试前先 git add；`.pyc` 天然被排除（只认 .py）。
    遍历范围沿用原口径（scripts/ + bin/argo），`__pycache__` 显式跳过。
    """
    out: set[str] = set()
    targets = [REPO / "scripts", REPO / "bin" / "argo"]
    for target in targets:
        files = [target] if target.is_file() else sorted(target.rglob("*.py"))
        for f in files:
            if "__pycache__" in f.parts:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            out.update(re.findall(r'"ARGO_[A-Z_0-9]+"', text))
    return {m.strip('"') for m in out}


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
        code_vars = _code_switches()
        doc_vars = set(re.findall(r"ARGO_[A-Z_0-9]+", USAGE))
        phantom = doc_vars - code_vars
        self.assertEqual(phantom, set(),
                         f"usage.md 写了代码里不存在的开关：{phantom}")

    def test_switch_table_covers_code_vars(self):
        """代码新增开关必须入表（总表节双向锁定，防表外漂移）。"""
        code_vars = _code_switches()
        m = re.search(r"## 功能开关总表.*?(?=\n## |\Z)", USAGE, re.S)
        self.assertIsNotNone(m, "usage.md 缺「功能开关总表」节")
        table_vars = set(re.findall(r"ARGO_[A-Z_0-9]+", m.group(0)))
        self.assertEqual(code_vars - table_vars, set(),
                         f"代码有但总表未收录（表是唯一事实）：{code_vars - table_vars}")


if __name__ == "__main__":
    unittest.main()

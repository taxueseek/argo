#!/usr/bin/env python3
"""2026-10-03 审计轮回归测试：四类假阴性 + run_query 契约 + timeout 下传。

守的四个实测缺陷（全部先在真机复现再修）：
  1. run_query 目录不存在分支 print + 裸 return 1，违反 (text, rc) 契约，
     MCP/include-local 进程内调用方解包直接 TypeError。
  2. --since/--until 在 max 截断之后过滤：旧文件占满截断池，新命中被吃掉
     （实测 20 个新文件只报出 8 个）。
  3. build_patterns 中文分支不拆英文多词：「性能 asyncio tutorial」的英文段
     整段化作固定短语，单词条目全部漏掉（eb0ed38 只修了纯英文分支）。
  4. --scope doc 把 md/txt 从 exts 减掉，文档场景恰好漏掉最常搜的笔记。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest sub-skills/local-seek/tests/test_regression_20261003.py -q
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import seek as s  # noqa: E402


def _run_main(argv):
    """跑 seek.main()，捕获 stdout，返回 (exit_code, stdout)。"""
    with mock.patch.object(sys, "argv", ["seek.py"] + argv):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = s.main()
        except SystemExit as e:
            code = e.code or 0
    return code or 0, buf.getvalue()


class TestBuildPatterns(unittest.TestCase):
    """中英混排与纯英文分词（缺陷 3）。"""

    def test_mixed_cjk_english_splits_words(self):
        patterns, fixed = s.build_patterns("性能 asyncio tutorial")
        self.assertIn("asyncio", patterns, "英文段必须按词拆开")
        self.assertIn("tutorial", patterns)
        self.assertIn("性能", patterns)
        self.assertTrue(fixed)

    def test_pure_english_multiword_unchanged(self):
        patterns, _ = s.build_patterns("Python asyncio tutorial")
        self.assertEqual(patterns, ["Python", "asyncio", "tutorial"])

    def test_space_never_a_pattern(self):
        patterns, _ = s.build_patterns("樯橹 灰飞烟灭")
        self.assertNotIn("", patterns)
        self.assertNotIn(" ", patterns)

    def test_cjk_2gram_extension_kept(self):
        patterns, _ = s.build_patterns("数据抓取")
        self.assertIn("数据抓取", patterns)
        self.assertIn("抓取", patterns)


class TestSinceFilterBeforeTruncation(unittest.TestCase):
    """--since 过滤必须发生在截断之前（缺陷 2，真实 rg 端到端）。"""

    def test_new_files_not_swallowed_by_old(self):
        if not s.tool_exists("rg"):
            self.skipTest("本机无 rg")
        with tempfile.TemporaryDirectory() as tmp:
            ancient = 900000000  # 1998 年，稳落任何时间窗之外
            for i in range(40):
                p = Path(tmp) / f"old_{i:02d}.txt"
                p.write_text("目标词命中 here\n")
                os.utime(p, (ancient, ancient))
            for i in range(20):
                (Path(tmp) / f"new_{i:02d}.txt").write_text("目标词命中 here\n")
            code, out = _run_main(
                ["目标词", "--path", tmp, "--since", "1d", "--max", "30", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["count"], 20,
                             "20 个新文件全部在窗口内，一个都不能被旧文件挤掉")

    def test_no_window_returns_all(self):
        if not s.tool_exists("rg"):
            self.skipTest("本机无 rg")
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(35):
                (Path(tmp) / f"f_{i:02d}.txt").write_text("目标词命中 here\n")
            code, out = _run_main(
                ["目标词", "--path", tmp, "--max", "30", "--json"])
            self.assertEqual(json.loads(out)["count"], 30)


class TestScopeDocIncludesMd(unittest.TestCase):
    """--scope doc 必须搜到 md（缺陷 4）。"""

    def test_md_hit_in_doc_scope(self):
        if not s.tool_exists("rg"):
            self.skipTest("本机无 rg")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "notes.md").write_text("检索词在这里\n")
            code, out = _run_main(
                ["检索词", "--path", tmp, "--scope", "doc", "--json"])
            self.assertEqual(code, 0, f"--scope doc 应命中 md，输出：{out}")
            data = json.loads(out)
            self.assertTrue(any("notes.md" in r["path"] for r in data["results"]))


class TestRunQueryContract(unittest.TestCase):
    """run_query 必须始终返回 (text, rc)（缺陷 1）。"""

    def test_nonexistent_dir_returns_tuple(self):
        result = s.run_query(["x", "--path", "/nonexistent-seek-test-xyz"])
        self.assertIsInstance(result, tuple, "返回裸 int 会让进程内调用方解包崩溃")
        text, rc = result
        self.assertIsInstance(text, str)
        self.assertEqual(rc, 1)
        self.assertIn("目录不存在", text)

    def test_cli_still_prints_message(self):
        code, out = _run_main(["x", "--path", "/nonexistent-seek-test-xyz"])
        self.assertEqual(code, 1)
        self.assertIn("目录不存在", out)


class TestTimeoutDownpass(unittest.TestCase):
    """structural/git 路径必须把 time_budget 下传给 run（缺陷 5）。"""

    def test_structural_passes_timeout(self):
        captured = {}
        def fake_run(cmd, timeout=None, **kw):
            captured["timeout"] = timeout
            proc = mock.Mock()
            proc.returncode = 1  # 无命中
            proc.stdout = ""
            return proc
        with mock.patch.object(s, "run", side_effect=fake_run):
            s.structural_search("裸except", "/tmp", [], 5, timeout=5.5)
        self.assertEqual(captured.get("timeout"), 5.5,
                         "structural_search 必须把 timeout 传给 run")

    def test_git_log_passes_timeout(self):
        captured = {}
        def fake_run(cmd, timeout=None, **kw):
            captured["timeout"] = timeout
            proc = mock.Mock()
            proc.returncode = 1
            proc.stdout = ""
            return proc
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.txt"
            f.write_text("x\n")
            with mock.patch.object(s, "run", side_effect=fake_run):
                s.git_log(str(f), timeout=7.5)
        self.assertEqual(captured.get("timeout"), 7.5)


if __name__ == "__main__":
    unittest.main()

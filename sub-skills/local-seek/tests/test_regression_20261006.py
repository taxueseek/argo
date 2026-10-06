#!/usr/bin/env python3
"""2026-10-06 性能轮回归测试：噪声档判定去 syscall + read_lines 单遍读取。

实测背景（cProfile，Documents/GPT 搜 "import" --count，8930 个文件）：
    _is_noise_path 独占 1.52s / 1.91s（80%），内含 35727 次 posix.getcwd、
    103266 次 posix.lstat、35720 次 realpath。根因是每行都做
    `Path(fp).resolve().relative_to(Path(root).resolve())`——resolve() 对每个
    路径段 lstat 且反复 getcwd；而 _apply_noise_floor 还把根 resolve 了两遍、
    每行判了两遍。

修法：纯字符串归一化（normpath + 前缀比较），根只算一次、每行只判一次，
全程零 syscall。本文件锁定两件事：
  1. 语义不变——相对/绝对根、根自己叫 tests/、repos/、日期归档、越界路径
     的判定结果与原实现一致；
  2. 不得回退到 syscall——_is_noise_path 再调 Path.resolve / realpath / lstat
     即报红（这是本轮性能收益的守门用例，比计时断言稳定）。

read_lines 原实现先 open 数行、再 open 读区间（同一文件开两遍，大文件 IO
翻倍），改为单遍流式；本文件锁定区间/越界/空文件三种行为不变。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest sub-skills/local-seek/tests/test_regression_20261006.py -q
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import seek as s  # noqa: E402


class TestNoisePathSemantics(unittest.TestCase):
    """去 syscall 之后，噪声档判定语义必须与原实现逐条一致。"""

    def test_root_named_tests_is_not_noise(self):
        # 搜索根自己叫 tests/ 时整棵树不得被判噪声（原实现的核心理由）
        self.assertFalse(s._is_noise_path("/x/tests/a.py", "/x/tests"))

    def test_src_under_root_is_clean(self):
        self.assertFalse(s._is_noise_path("/x/src/app.py", "/x"))

    def test_repos_under_root_is_noise(self):
        self.assertTrue(s._is_noise_path("/x/repos/lib.py", "/x"))

    def test_date_archive_dir_is_noise(self):
        self.assertTrue(s._is_noise_path("/x/2026-09-26_吸纳/a.md", "/x"))

    def test_relative_root(self):
        # 默认根 "." —— rg 输出相对路径时的常见形态
        self.assertTrue(s._is_noise_path("repos/lib.py", "."))
        self.assertFalse(s._is_noise_path("src/app.py", "."))

    def test_out_of_root_falls_back_to_full_path(self):
        # 不在根之下（软链/越界）：退回整条路径判定
        self.assertTrue(s._is_noise_path("/other/repos/lib.py", "/x"))
        self.assertFalse(s._is_noise_path("/other/src/lib.py", "/x"))

    def test_noise_floor_clean_first(self):
        rows = [("/x/repos/a.py", 0, ""), ("/x/src/b.py", 0, "")]
        out = s._apply_noise_floor(rows, "/x", 30)
        self.assertEqual(out[0][0], "/x/src/b.py",
                         "真源必须排在噪声档之前")

    def test_noise_floor_backfills_when_clean_short(self):
        # 真源不足时噪声档补满（不是排除，保证仍可达）
        rows = [("/x/repos/a.py", 0, ""), ("/x/src/b.py", 0, "")]
        out = s._apply_noise_floor(rows, "/x", 2)
        self.assertEqual(len(out), 2)


class TestNoisePathNoSyscall(unittest.TestCase):
    """性能守门：判定路径不得再触发任何文件系统解析调用。"""

    def test_does_not_call_path_resolve(self):
        with mock.patch.object(Path, "resolve",
                               side_effect=AssertionError("又调 Path.resolve() 了")):
            rows = [(f"/root/repos/f{i}.py", 0, "") for i in range(100)]
            s._apply_noise_floor(rows, "/root", 30)

    def test_does_not_call_realpath_or_lstat(self):
        with mock.patch.object(os.path, "realpath",
                               side_effect=AssertionError("又调 realpath() 了")), \
             mock.patch.object(os, "lstat",
                               side_effect=AssertionError("又调 lstat() 了")):
            rows = [(f"/root/repos/f{i}.py", 0, "") for i in range(100)]
            s._apply_noise_floor(rows, "/root", 30)


class TestReadLinesSinglePass(unittest.TestCase):
    """read_lines 单遍读取：区间/夹取/越界/空文件行为与原实现一致。"""

    def _write(self, tmp, text):
        f = Path(tmp) / "a.txt"
        f.write_text(text, encoding="utf-8")
        return str(f)

    def test_range_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = self._write(tmp, "\n".join(f"L{i}" for i in range(1, 11)) + "\n")
            text, rc = s.read_lines(fp, "3-5")
            self.assertEqual(rc, 0)
            self.assertIn("3: L3", text)
            self.assertIn("5: L5", text)
            self.assertNotIn("6: L6", text)
            self.assertIn("共 10 行", text)

    def test_clamped_to_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = self._write(tmp, "L1\nL2\n")
            text, rc = s.read_lines(fp, "1-99")
            self.assertEqual(rc, 0)
            self.assertIn("第 1-2 行", text)

    def test_start_past_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = self._write(tmp, "L1\n")
            text, rc = s.read_lines(fp, "5-6")
            self.assertEqual(rc, 1)
            self.assertIn("只有 1 行", text)

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = self._write(tmp, "")
            text, rc = s.read_lines(fp, "1-2")
            self.assertEqual(rc, 1)
            self.assertIn("只有 0 行", text)

    def test_bad_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = self._write(tmp, "L1\n")
            text, rc = s.read_lines(fp, "abc")
            self.assertEqual(rc, 1)
            self.assertIn("N-M", text)


if __name__ == "__main__":
    unittest.main()

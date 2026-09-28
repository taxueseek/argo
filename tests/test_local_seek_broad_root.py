#!/usr/bin/env python3
"""--include-local 宽泛根守卫回归测试（2026-09-27）。

守的缺陷：`argo search --include-local` 的本地检索范围完全由「用户在哪个
目录敲命令」决定——子进程继承 cwd，seek.py 默认 `--path .`。实测：

    cwd=/tmp/globtest   62 ms      cwd=/tmp        1170 ms
    cwd=~            >20000 ms     cwd=/           >20000 ms

在 home 或根目录执行时，搜索本体 72 ms，之后本地检索扫全盘跑满 20 s 子进程
超时，整条命令墙钟 **22.6 s**，且 20 s 里一个字的结果都没产出。

修法不是「显式传 --path」（子进程继承 cwd，显式传与默认等价，等于没改），
而是**在范围本身不合理时直接跳过**（`_local_seek_dir()` 返回 None），
并留一条 stderr 说明——「跳过」与「查了没有」含义不同，不能静默。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_local_seek_broad_root.py -q
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import search  # noqa: E402
import local_seek as ls  # noqa: E402

# 守卫与执行器的实现家：2026-09-28（4e1d017）从 search.py 拆到
# scripts/local_seek.py。测试跟着实现走——「打桩点落在读取处」是本仓库
# 的既有纪律（见 search.py 对 search_rank 转出清单的说明）。


class TestLocalSeekDirGuard(unittest.TestCase):
    """_local_seek_dir 的判定：宽泛根 → None，具体目录 → 原样放行。"""

    def _at(self, cwd: str):
        """在指定 cwd 语义下求值（不真 chdir，避免影响其他用例）。"""
        with mock.patch.object(os, "getcwd", return_value=cwd):
            return ls._local_seek_dir()

    def test_home_is_skipped(self):
        home = os.path.realpath(os.path.expanduser("~"))
        self.assertIsNone(self._at(home),
                          "home 是宽泛根：本地检索会扫全盘，必须跳过")

    def test_filesystem_root_is_skipped(self):
        self.assertIsNone(self._at("/"), "根目录必须跳过")

    def test_tmp_is_skipped(self):
        self.assertIsNone(self._at("/tmp"), "/tmp 必须跳过")

    def test_tmp_realpath_is_skipped(self):
        """macOS 上 /tmp 是 /private/tmp 的软链，两种形态都要拦住。"""
        self.assertIsNone(self._at("/private/tmp"),
                          "realpath 形态漏判会导致 /tmp 下的搜索仍扫全盘")

    def test_system_roots_are_skipped(self):
        for d in ("/var", "/usr", "/System", "/Library", "/Applications"):
            self.assertIsNone(self._at(d), f"{d} 是系统根，应跳过")

    def test_project_dir_is_allowed(self):
        d = "/Users/example/projects/demo"
        self.assertEqual(self._at(d), d, "具体项目目录必须放行")

    def test_subdir_of_broad_root_is_allowed(self):
        """宽泛根的子目录是合法范围（如 /System/Library 之外的普通子目录）。"""
        d = "/Users/example/.agents/skills/argo"
        self.assertEqual(self._at(d), d)


class TestRunLocalSeekGuard(unittest.TestCase):
    """_run_local_seek 的端到端行为：跳过时不留噪音、要留说明。"""

    def test_broad_root_skips_without_spawning(self):
        """宽泛根下不得启动子进程——这是 22.6 s 事故的直接防线。"""
        home = os.path.realpath(os.path.expanduser("~"))
        with mock.patch.object(os, "getcwd", return_value=home), \
             mock.patch("subprocess.run") as m_run:
            buf = io.StringIO()
            with redirect_stderr(buf):
                hits = ls._run_local_seek("anything", 5)
            self.assertEqual(hits, [], "跳过时必须返回空列表")
            m_run.assert_not_called()
            self.assertIn("过宽", buf.getvalue(),
                          "跳过必须留可归因的说明，不能静默（否则看起来像「没匹配」）")

    def test_explicit_search_dir_bypasses_guard(self):
        """显式给 search_dir 时不受宽泛根限制（调用方明确指定了范围）。"""
        import seek_locator
        home = os.path.realpath(os.path.expanduser("~"))
        target = "/Users/example/projects/demo"
        with mock.patch.object(os, "getcwd", return_value=home), \
             mock.patch.object(seek_locator, "resolve_seek_py",
                               return_value="/fake/seek.py"), \
             mock.patch.object(os.path, "isfile", return_value=True), \
             mock.patch("subprocess.run") as m_run:
            m_run.return_value = mock.Mock(returncode=1, stdout="")
            ls._run_local_seek("q", 5, search_dir=target)
            self.assertTrue(m_run.called, "显式 search_dir 时必须真的调用 seek.py")
            cmd = m_run.call_args[0][0]
            self.assertIn(target, cmd, "显式 search_dir 必须真的传给 seek.py")

    def test_timeout_does_not_propagate(self):
        """子进程超时必须吞掉——此前 TimeoutExpired 会冒泡到调用方。"""
        import subprocess as sp
        import seek_locator
        with mock.patch.object(os, "getcwd", return_value="/Users/example/proj"), \
             mock.patch.object(seek_locator, "resolve_seek_py",
                               return_value="/fake/seek.py"), \
             mock.patch.object(os.path, "isfile", return_value=True), \
             mock.patch.object(sp, "run",
                               side_effect=sp.TimeoutExpired("cmd", 3)):
            try:
                hits = ls._run_local_seek("q", 5)
            except sp.TimeoutExpired:
                self.fail("TimeoutExpired 冒泡了：本地命中不应让整次搜索承担异常")
            self.assertEqual(hits, [])

    def test_timeout_budget_is_small(self):
        """超时常量必须是「增强项」量级，不能接近搜索总预算。"""
        self.assertLessEqual(ls._LOCAL_SEEK_TIMEOUT_S, 5.0,
                             "本地命中是尾部增强项，超时不该到搜索引擎量级")
        self.assertGreaterEqual(ls._LOCAL_SEEK_TIMEOUT_S, 1.0,
                                "太小会让正常目录的正常查询被误杀")


if __name__ == "__main__":
    unittest.main()

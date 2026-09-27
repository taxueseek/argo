#!/usr/bin/env python3
"""pcre2 判据回归测试（2026-09-27）。

守的缺陷：`pcre2_supported()` 曾用「跑一次匹配看返回码」判定 rg 是否支持 PCRE2：

    proc = run(["rg", "--pcre2", "-e", "x", os.devnull])
    _pcre2_ok = proc is not None and proc.returncode == 0

`/dev/null` 永远没有匹配，rg 无匹配时返回 1，于是判据恒为 False——本机
rg 明明带 +pcre2，却永远被判为「未编译 PCRE2」，所有 look-around /
反向引用查询被拒绝（用户可见的报错：「请简化查询」）。

根因是**判据的观察量与被判定的性质无关**：探测目标的匹配结果说明不了
二进制是否带某个特性。修法改读 `rg --version` 的 features 行。

这组测试把「返回码语义」钉死：只 mock run() 的返回值，覆盖
  - features 行含 +pcre2 → True
  - features 行是 -pcre2 → False
  - rg 不存在（run 返回 None）→ False
  - 只判 features 行，不被「匹配有无」影响（原缺陷的形态）

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest sub-skills/local-seek/tests/test_pcre2_probe.py -q
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import seek as s  # noqa: E402


def _proc(stdout: str, returncode: int = 0):
    p = mock.Mock()
    p.stdout = stdout
    p.returncode = returncode
    return p


class TestPcre2Probe(unittest.TestCase):
    def setUp(self):
        # 模块级缓存必须在每个用例前清掉，否则第一个用例的结果会被后续复用
        self._saved = s._pcre2_ok
        s._pcre2_ok = None
        self.addCleanup(lambda: setattr(s, "_pcre2_ok", self._saved))

    def test_features_line_with_pcre2_is_true(self):
        """正面：features:+pcre2 → 支持（本机 rg 15.0.0 的真实形态）。"""
        out = "ripgrep 15.0.0 (rev 3a612f88b8)\n\nfeatures:+pcre2\nsimd(compile):+NEON\n"
        with mock.patch.object(s, "run", return_value=_proc(out)):
            self.assertTrue(s.pcre2_supported())

    def test_features_line_without_pcre2_is_false(self):
        """反面：features 行明确是 -pcre2 → 不支持，走 grep 回退。"""
        out = "ripgrep 13.0.0\n\nfeatures:-pcre2\n"
        with mock.patch.object(s, "run", return_value=_proc(out)):
            self.assertFalse(s.pcre2_supported())

    def test_missing_rg_is_false(self):
        """rg 不存在时 run() 返回 None → False（不得抛异常）。"""
        with mock.patch.object(s, "run", return_value=None):
            self.assertFalse(s.pcre2_supported())

    def test_does_not_depend_on_match_result(self):
        """核心回归：判据不得再依赖「有没有匹配」。

        原缺陷的形态是 `returncode == 0`——把「无匹配(1)」误读成「不支持」。
        这里给一个**无匹配语义**的返回码 1 但 features 行含 +pcre2，
        新判据必须仍判 True。若有人把它改回看返回码，本用例会红。
        """
        out = "ripgrep 15.0.0\n\nfeatures:+pcre2\n"
        with mock.patch.object(s, "run", return_value=_proc(out, returncode=1)):
            self.assertTrue(s.pcre2_supported(),
                            "判据又依赖返回码了：无匹配(rc=1)被误读成不支持 PCRE2")

    def test_probe_targets_version_not_a_match_run(self):
        """确认探测命令本身是 --version，而不是「跑一次匹配」。"""
        captured = {}

        def _fake(cmd, **kwargs):
            captured["cmd"] = cmd
            return _proc("ripgrep 15.0.0\nfeatures:+pcre2\n")

        with mock.patch.object(s, "run", side_effect=_fake):
            s.pcre2_supported()
        self.assertIn("--version", captured["cmd"],
                      "探测命令必须是 rg --version（读特性声明），不是匹配探测")

    def test_module_cache_avoids_repeated_probe(self):
        """缓存语义：只探一次，第二次不再调 run()。"""
        calls = []

        def _fake(cmd, **kwargs):
            calls.append(cmd)
            return _proc("ripgrep 15.0.0\nfeatures:+pcre2\n")

        with mock.patch.object(s, "run", side_effect=_fake):
            s.pcre2_supported()
            s.pcre2_supported()
            s.pcre2_supported()
        self.assertEqual(len(calls), 1, "模块级缓存失效，重复探测")


class TestPcre2EndToEnd(unittest.TestCase):
    """真机对照：本机 rg 带 +pcre2 时，look-around 查询真的能出结果。

    不断言「一定支持」（别的机器可能没有），而是断言**判据与真机能力一致**：
    先读 rg --version 拿真值，再要求 pcre2_supported() 与之一致。
    """

    def test_agrees_with_real_rg(self):
        try:
            real = subprocess.run(["rg", "--version"], capture_output=True,
                                  text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.skipTest("本机无 rg，跳过真机对照")
        truth = "+pcre2" in (real.stdout or "")
        saved = s._pcre2_ok
        s._pcre2_ok = None
        try:
            self.assertEqual(s.pcre2_supported(), truth,
                             "判据与 rg 自报的特性不一致")
        finally:
            s._pcre2_ok = saved


if __name__ == "__main__":
    unittest.main()

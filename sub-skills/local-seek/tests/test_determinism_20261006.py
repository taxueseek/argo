#!/usr/bin/env python3
"""tests/test_determinism_20261006.py — 宽查询输出确定性回归（方案一，2026-10-06）。

守 v1.4.1 审计遗留的定性修复：
  1. rg 并行遍历发射序不定 →「先到先得」截断拿到任意的 cap 条，同查询
     两次跑结果集与排序都不同。修复后有界堆按 (路径,行号,内容) 选池，
     **同一命中集无论以什么顺序发射，输出逐条一致**。
  2. count 模式并列计数按路径升序决出稳定次序。
  3. grep 兜底路径同款语义。
  4. 锁定 ja/ko 2-gram 既有行为（CJK_RE 覆盖假名/谚文——2026-10-06 实测
     已支持，修正审计记忆里「无分词只能整词匹配」的过时记录）。

打桩纪律：一律 unittest.mock.patch.object（自动恢复）。首版直接给
s.run 赋值且不恢复，把同进程后续测试的真实 rg 全部替换成假输出——
套件 9 红的根因（2026-10-06 实锤，教训记录在此防再犯）。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import seek as s  # noqa: E402


def _proc(stdout: str, rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc,
                                       stdout=stdout, stderr="")


def _matches(n_files: int, lines_per: int = 1) -> list[str]:
    """n_files 个文件的 rg 命中行（路径 f{i:03d}.py 保证字典序可控）。"""
    out = []
    for i in range(n_files):
        for j in range(lines_per):
            out.append(f"src/f{i:03d}.py:{j + 1}:hit {i}-{j}")
    return out


class RgBodyDeterminism(unittest.TestCase):
    """rg 主体路径：池子选择与发射序无关。"""

    def test_output_independent_of_emission_order(self):
        rows = _matches(40)
        # max_results=5 → cap = max(5, min(20, 400)) = 20：命中 40 > cap，
        # 池子选择真正发生截断
        for drop_noise in (True, False):
            with self.subTest(drop_noise=drop_noise):
                with patch.object(s, "run",
                                  return_value=_proc("\n".join(rows) + "\n")):
                    a, err = s.rg_search(["q"], ".", [], [], 0, False, 5,
                                         fixed=True, drop_noise=drop_noise)
                with patch.object(s, "run",
                                  return_value=_proc(
                                      "\n".join(reversed(rows)) + "\n")):
                    b, err2 = s.rg_search(["q"], ".", [], [], 0, False, 5,
                                          fixed=True, drop_noise=drop_noise)
                self.assertIsNone(err)
                self.assertIsNone(err2)
                self.assertEqual(a, b, "同一命中集、不同发射序 → 输出必须一致")

    def test_pool_is_path_smallest_and_sorted(self):
        rows = _matches(40)
        with patch.object(s, "run",
                          return_value=_proc("\n".join(rows) + "\n")):
            out, err = s.rg_search(["q"], ".", [], [], 0, False, 5,
                                   fixed=True, drop_noise=False)
        self.assertIsNone(err)
        self.assertEqual([r[0] for r in out],
                         [f"src/f{i:03d}.py" for i in range(5)])
        self.assertEqual(len(out), 5)

    def test_noise_floor_layering_deterministic(self):
        """clean 在前 noisy 在后的分层语义在确定池子上仍然成立。"""
        rows = (["src/clean_{:03d}.py:1:hit".format(i) for i in range(30)]
                + ["src/repos/lib_{:03d}.py:1:hit".format(i) for i in range(30)])
        outs = []
        for order in (rows, list(reversed(rows))):
            with patch.object(s, "run",
                              return_value=_proc("\n".join(order) + "\n")):
                out, err = s.rg_search(["q"], ".", [], [], 0, False, 5,
                                       fixed=True, drop_noise=True)
            self.assertIsNone(err)
            outs.append(out)
        self.assertEqual(outs[0], outs[1])
        self.assertTrue(all("/repos/" not in r[0] for r in outs[0]),
                        "clean 够数时噪声档不得混入")


class CountTieDeterminism(unittest.TestCase):
    def test_equal_counts_sorted_by_path(self):
        rows = ["src/b.py:5", "src/a.py:5", "src/c.py:2"]
        with patch.object(s, "run",
                          return_value=_proc("\n".join(rows) + "\n")):
            out, err = s.rg_search(["q"], ".", [], [], 0, True, 10,
                                   fixed=True, drop_noise=False)
        self.assertIsNone(err)
        self.assertEqual([r[0] for r in out],
                         ["src/a.py", "src/b.py", "src/c.py"])


class GrepFallbackDeterminism(unittest.TestCase):
    def test_body_and_count_deterministic(self):
        rows = _matches(40)
        for order in (rows, list(reversed(rows))):
            with patch.object(s, "run",
                              return_value=_proc("\n".join(order) + "\n")):
                body, err = s.grep_search("grep", ["q"], ".", [], [], 0,
                                          False, 5, fixed=True)
            self.assertIsNone(err)
            self.assertEqual([r[0] for r in body],
                             [f"src/f{i:03d}.py" for i in range(5)])
        cnt_rows = ["src/b.py:5", "src/a.py:5"]
        with patch.object(s, "run",
                          return_value=_proc("\n".join(cnt_rows) + "\n")):
            cnt, err = s.grep_search("grep", ["q"], ".", [], [], 0,
                                     True, 10, fixed=True)
        self.assertIsNone(err)
        self.assertEqual([r[0] for r in cnt], ["src/a.py", "src/b.py"])


class JaKoBigramLocked(unittest.TestCase):
    """锁定既有行为（实测于 2026-10-06）：CJK_RE 已覆盖假名/谚文。"""

    def test_japanese_compound_gets_bigrams(self):
        pats, literal = s.build_patterns("プロンプトエンジニアリング入門")
        self.assertIn("プロンプトエンジニアリング入門", pats)  # 整词
        self.assertIn("プロ", pats)                            # 首个 2-gram
        self.assertTrue(literal)

    def test_korean_spaced_words_and_compound(self):
        pats, _ = s.build_patterns("프로프트 검색 설정")
        self.assertIn("프로프트", pats)   # 复合词整词
        self.assertIn("프로", pats)       # 复合词 2-gram
        self.assertIn("검색", pats)       # 空格分隔词保持整词


if __name__ == "__main__":
    unittest.main()

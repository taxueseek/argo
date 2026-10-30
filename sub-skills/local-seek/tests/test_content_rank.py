#!/usr/bin/env python3
"""tests/test_content_rank.py — 内容 BM25F 重排 + fzf V2 评分契约（2026-10-07 方案甲）。

守四类不变式：
  A. 重排闸门：逃生门/正则查询/count/context 一律保持 (路径,行号) 原序
  B. 重排质量：文件名命中 > 纯行命中；多词全覆盖 > 单词擦边；噪声档
     分层在重排后依然成立（真源在前）
  C. 确定性：打分只依赖内容，发射序打乱输出逐位一致（2026-10-06 契约延续）
  D. fzf V2：边界/驼峰/连续/gap 惩罚的相对次序与 fzf 口径一致，全等最优

打桩纪律：unittest.mock.patch.object（自动恢复）——直接给 s.run 赋值且
不恢复会把同进程后续测试的真实 rg 顶掉（2026-10-06 实锤教训，见
test_determinism_20261006.py 模块头）。
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
import seek_rank as r  # noqa: E402


def _proc(stdout: str, rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc,
                                       stdout=stdout, stderr="")


class TestRerankGate(unittest.TestCase):
    """A. 闸门：不该重排的场景必须保持 (路径,行号) 原序。"""

    ROWS = [
        ("b/beta.py", 5, "alpha"),
        ("a/alpha.py", 9, "alpha"),
        ("c/gamma.md", 1, "alpha"),
    ]

    def _search(self, raw_query, fixed=True, ctx=0, count=False):
        with patch.object(s, "run", return_value=_proc("a/x.py:1:hit\n")):
            # 直接对池行调用重排闸门（不走 rg，隔离网络与文件系统）
            return s._rerank_content(list(self.ROWS), raw_query, ctx, count, fixed)

    def test_escape_hatch_restores_order(self):
        import os
        old = os.environ.pop("ARGO_SEEK_CONTENT_RERANK", None)
        os.environ["ARGO_SEEK_CONTENT_RERANK"] = "0"
        try:
            self.assertEqual(self._search("alpha"), self.ROWS)
        finally:
            os.environ.pop("ARGO_SEEK_CONTENT_RERANK", None)
            if old is not None:
                os.environ["ARGO_SEEK_CONTENT_RERANK"] = old

    def test_regex_query_not_reranked(self):
        # fixed=False（正则/PCRE2）：词元切分只会产生碎片，闸门必须放行原序
        self.assertEqual(self._search(r"\d{4}", fixed=False), self.ROWS)

    def test_count_and_context_not_reranked(self):
        self.assertEqual(self._search("alpha", count=True), self.ROWS)
        self.assertEqual(self._search("alpha", ctx=3), self.ROWS)

    def test_no_token_query_not_reranked(self):
        self.assertEqual(self._search("!!!"), self.ROWS)


class TestRerankQuality(unittest.TestCase):
    """B. 重排质量：相关度次序的三条核心判据。"""

    def test_filename_match_beats_content_only(self):
        rows = [
            ("docs/notes.md", 12, "支持噪声档降权的说明文字。"),
            ("src/noise_tier.py", 3, "placeholder"),
            ("src/other.py", 8, "噪声相关但文件无关。"),
        ]
        out = r.rank_rows(rows, "noise_tier")
        self.assertEqual(out[0][0], "src/noise_tier.py")

    def test_multi_token_coverage_beats_partial(self):
        rows = [
            ("a/one.md", 1, "只提到缓存一个词。"),
            ("b/both.md", 2, "语义缓存与 minhash 软命中都在这行。"),
        ]
        out = r.rank_rows(rows, "语义缓存 minhash")
        self.assertEqual(out[0][0], "b/both.md")

    def test_verbatim_phrase_beats_bigram_scatter(self):
        # 二元组分不清「噪声档的实现行」与「只有噪声二字的无关行」：
        # 逐字短语加成必须把前者顶上去
        rows = [
            ("a/x.md", 1, "设备噪声很大。"),
            ("b/y.py", 2, "噪声档的阈值定义在此。"),
        ]
        out = r.rank_rows(rows, "噪声档")
        self.assertEqual(out[0][0], "b/y.py")

    def test_ties_break_by_path_line(self):
        rows = [("b.py", 2, "same"), ("a.py", 9, "same"), ("a.py", 1, "same")]
        out = r.rank_rows(rows, "same")
        self.assertEqual([(p, l) for p, l, _ in out],
                         [("a.py", 1), ("a.py", 9), ("b.py", 2)])

    def test_noise_floor_after_rerank(self):
        # 重排改变池内次序后，噪声档分层（真源在前）必须依然成立——
        # 分层发生在 rg_search 的 _apply_noise_floor（重排之后），走全链路验证
        rows = [
            "tmp/x.py:1:noise tier keyword",
            "src/y.py:2:noise tier keyword here",
        ]
        with patch.object(s, "run", return_value=_proc("\n".join(rows) + "\n")):
            out, err = s.rg_search(["noise"], ".", [], [], 0, False, 5,
                                   fixed=True, raw_query="noise tier keyword")
        self.assertIsNone(err)
        self.assertTrue(out, "真源行不应消失")
        self.assertTrue(out[0][0].startswith("src/"),
                        f"真源应排最前，实际: {out[0][0]}")


class TestRerankDeterminism(unittest.TestCase):
    """C. 确定性：打平发射序（2026-10-06 契约在重排开启下延续）。"""

    def test_output_independent_of_emission_order(self):
        rows = [f"src/f{i:03d}.py:{j + 1}:alpha beta hit {i}-{j}"
                for i in range(40) for j in range(1)]
        with patch.object(s, "run", return_value=_proc("\n".join(rows) + "\n")):
            a, err = s.rg_search(["alpha"], ".", [], [], 0, False, 5,
                                 fixed=True, raw_query="alpha beta")
            import random
            shuffled = rows[:]
            random.Random(7).shuffle(shuffled)
            with patch.object(s, "run", return_value=_proc("\n".join(shuffled) + "\n")):
                b, err2 = s.rg_search(["alpha"], ".", [], [], 0, False, 5,
                                      fixed=True, raw_query="alpha beta")
        self.assertIsNone(err)
        self.assertIsNone(err2)
        self.assertEqual(a, b)


class TestFzfV2(unittest.TestCase):
    """D. fzf FuzzyMatchV2 口径。"""

    def test_no_match_is_zero(self):
        self.assertEqual(s._fzf_score("zzz.txt", "rep"), 0)

    def test_smart_case(self):
        self.assertGreater(s._fzf_score("Report_v2.md", "report"), 0)
        # 模式带大写 → 区分大小写：R 匹配、r 不配
        self.assertEqual(s._fzf_score("report.md", "R"), 0)

    def test_boundary_beats_midword(self):
        # bar 在 foo_bar（词边界）> xbarx（词中）
        self.assertGreater(s._fzf_score("foo_bar.py", "bar"),
                           s._fzf_score("xbarx.py", "bar"))

    def test_consecutive_beats_gappy(self):
        self.assertGreater(s._fzf_score("xabc.md", "abc"),
                           s._fzf_score("axbxc.md", "abc"))

    def test_camel_bonus(self):
        self.assertGreater(s._fzf_score("parseHTTPResponse.py", "hr"), 0)

    def test_dp_optimal_alignment(self):
        # xaabc 搜 ab：DP 应发现「跳过词首 a、取边界 b」的对齐优于顺位连打
        # （顺位贪心：a@1 + b@4 有 gap；最优：a@1 + b@4 不如 a@3?——此处锚定
        # 已知反超事实：xaabc 的分必须高于等长无边界对照 axbxc 的同模式分）
        self.assertGreater(s._fzf_score("xaabc", "ab"),
                           s._fzf_score("axbxc", "ab"))

    def test_exact_beats_partial(self):
        self.assertGreater(s._fzf_score("seek.py", "seek.py"),
                           s._fzf_score("seek_helper.py", "seek.py"))

    def test_exact_case_beats_icase(self):
        self.assertGreaterEqual(s._fzf_score("Seek.py", "seek.py"),
                                s._fzf_score("seek.py", "seek.py")
                                )  # 全等大写版含 case bonus，不小于纯 icase


class TestHeadingBoost(unittest.TestCase):
    """E. Markdown 标题行优先加成。"""

    def test_heading_beats_body_mention(self):
        rows = [
            ("docs/guide.md", 50, "在某些配置下可能会使用到 custom_handler 逻辑"),
            ("docs/guide.md", 10, "## custom_handler 架构详解"),
        ]
        ranked = r.rank_rows(rows, "custom_handler")
        self.assertEqual(ranked[0][1], 10, "标题行应排在正文顺带提及之前")


class TestSymbolSearch(unittest.TestCase):
    """F. --symbol 精确词边界匹配。"""

    def test_symbol_flag_adds_word_boundary(self):
        with patch.object(s, "run") as mock_run:
            mock_run.return_value = unittest.mock.MagicMock(returncode=0, stdout="a.py:1:fn foo()\n", stderr="")
            s.rg_search(["foo"], Path("."), [], [], 0, False, 10, fixed=True, symbol=True)
            called_cmd = mock_run.call_args[0][0]
            self.assertIn("-w", called_cmd, "symbol=True 时 rg 命令必须包含 -w 参数")


class TestFrecencyWiring(unittest.TestCase):
    """frecency 默认关闭：不启用时不产生任何行为差异。"""

    def test_disabled_by_default(self):
        import os
        old = os.environ.pop("ARGO_SEEK_FRECENCY", None)
        try:
            import seek_frecency
            self.assertFalse(seek_frecency.enabled())
            self.assertEqual(seek_frecency.boost("/any/path"), 0.0)
        finally:
            if old is not None:
                os.environ["ARGO_SEEK_FRECENCY"] = old

    def test_boost_and_record_roundtrip(self):
        import os
        import tempfile
        old_flag = os.environ.pop("ARGO_SEEK_FRECENCY", None)
        old_db = os.environ.pop("ARGO_SEEK_FRECENCY_DB", None)
        with tempfile.TemporaryDirectory() as td:
            os.environ["ARGO_SEEK_FRECENCY"] = "1"
            os.environ["ARGO_SEEK_FRECENCY_DB"] = str(Path(td) / "f.json")
            try:
                import seek_frecency
                seek_frecency.record(["/a/b.py", "/c/d.py"])
                self.assertGreater(seek_frecency.boost("/a/b.py"), 0.0)
                self.assertEqual(seek_frecency.boost("/z/missing.py"), 0.0)
            finally:
                os.environ.pop("ARGO_SEEK_FRECENCY", None)
                os.environ.pop("ARGO_SEEK_FRECENCY_DB", None)
                if old_flag is not None:
                    os.environ["ARGO_SEEK_FRECENCY"] = old_flag
                if old_db is not None:
                    os.environ["ARGO_SEEK_FRECENCY_DB"] = old_db


if __name__ == "__main__":
    unittest.main()

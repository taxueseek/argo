"""test_seek_p0_p1.py — 零依赖 P0/P1 本地搜索增强单元测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS = TESTS_DIR.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import seek  # noqa: E402
import seek_fts  # noqa: E402
import seek_graph  # noqa: E402
import seek_inspect  # noqa: E402
import seek_rank  # noqa: E402


class TestP0P1LocalSeek(unittest.TestCase):
    def test_fts_identifier_expansion_and_bm25f(self):
        with tempfile.TemporaryDirectory() as d:
            core = Path(d) / "token_budget.py"
            core.write_text("def computeTokenBudget(max_ctx):\n    return max_ctx\n", encoding="utf-8")
            noise = Path(d) / "notes.md"
            noise.write_text("Just a random note mentioning compute and token.\n", encoding="utf-8")

            import os
            orig_state = os.environ.get("ARGO_STATE_DIR")
            orig_fts = os.environ.get("ARGO_LOCAL_FTS")
            os.environ["ARGO_STATE_DIR"] = d
            os.environ["ARGO_LOCAL_FTS"] = "1"
            try:
                info = seek_fts.refresh(d, force=True, budget_s=10.0)
                self.assertEqual(info["complete"], 1)
                hits = seek_fts.content_candidates("token budget", d)
                self.assertIsNotNone(hits)
                self.assertTrue(len(hits) >= 1)
                # BM25F path_terms(5.0) + symbols(3.0) 应把 token_budget.py 排在第一
                self.assertEqual(Path(hits[0][0]).name, "token_budget.py")
            finally:
                if orig_state is None:
                    os.environ.pop("ARGO_STATE_DIR", None)
                else:
                    os.environ["ARGO_STATE_DIR"] = orig_state
                if orig_fts is None:
                    os.environ.pop("ARGO_LOCAL_FTS", None)
                else:
                    os.environ["ARGO_LOCAL_FTS"] = orig_fts

    def test_ast_graph_pagerank_boosts_core_definition(self):
        with tempfile.TemporaryDirectory() as d:
            core = Path(d) / "core_engine.py"
            core.write_text("def dispatch_query(q):\n    return q\n", encoding="utf-8")
            c1 = Path(d) / "caller_a.py"
            c1.write_text("from core_engine import dispatch_query\ndispatch_query('a')\n", encoding="utf-8")
            c2 = Path(d) / "caller_b.py"
            c2.write_text("from core_engine import dispatch_query\ndispatch_query('b')\n", encoding="utf-8")
            noise = Path(d) / "unrelated.py"
            noise.write_text("# dispatch_query mentioned in comment\n", encoding="utf-8")

            scores = seek_graph.rank_files_by_pagerank([str(noise), str(c1), str(c2), str(core)])
            self.assertGreater(scores[str(core)], scores[str(noise)])
            self.assertGreater(scores[str(core)], scores[str(c1)])

    def test_rrf_fuse_monotonicity(self):
        fused = seek_rank.rrf_scores(3, [([2, 0, 1], 1.0), ([2, 1, 0], 0.5)])
        self.assertGreater(fused[2], fused[0])
        self.assertGreater(fused[2], fused[1])

    def test_one_hop_block_resolution_via_cli(self):
        with tempfile.TemporaryDirectory() as d:
            pyf = Path(d) / "sample_mod.py"
            pyf.write_text(
                "def helper_alpha():\n    x = 1\n    return x\n\n"
                "def target_beta(val):\n    # special_marker_xyz\n    return val * 2\n",
                encoding="utf-8",
            )
            out, rc = seek.run_query(["special_marker_xyz", "--path", d, "--agent", "--block"])
            self.assertEqual(rc, 0)
            obj = json.loads(out)
            self.assertGreaterEqual(obj["count"], 1)
            blk = obj["results"][0].get("block")
            self.assertIsNotNone(blk)
            self.assertIn("target_beta", blk["symbol"])
            self.assertEqual(blk["start_line"], 5)
            self.assertEqual(blk["end_line"], 7)


if __name__ == "__main__":
    unittest.main()

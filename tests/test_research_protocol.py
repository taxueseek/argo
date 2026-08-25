#!/usr/bin/env python3
"""工作包交接、可判定门禁、dossier 契约、本地文件入账。"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


class TestWorkPackages(unittest.TestCase):
    def test_parse_and_stage_respects_depends_on(self):
        from research_work_packages import parse_work_packages, stage_work_packages

        pkgs = parse_work_packages([
            {"id": "risk", "question": "量产风险", "depends_on": ["def"]},
            {"id": "def", "question": "定义与分类"},
        ])
        stages, warnings = stage_work_packages(pkgs)
        self.assertEqual(warnings, [])
        self.assertEqual([p["id"] for p in stages[0]], ["def"])
        self.assertEqual([p["id"] for p in stages[1]], ["risk"])

    def test_cycle_falls_into_last_stage(self):
        from research_work_packages import parse_work_packages, stage_work_packages

        pkgs = parse_work_packages([
            {"id": "a", "question": "A", "depends_on": ["b"]},
            {"id": "b", "question": "B", "depends_on": ["a"]},
        ])
        stages, warnings = stage_work_packages(pkgs)
        self.assertEqual(len(stages), 1)
        self.assertTrue(any("成环" in w for w in warnings))

    def test_missing_question_raises(self):
        from research_work_packages import parse_work_packages

        with self.assertRaises(ValueError):
            parse_work_packages([{"id": "x"}])


class TestGates(unittest.TestCase):
    def test_no_sources_fails(self):
        from research_gates import evaluate_dossier_gates

        out = evaluate_dossier_gates({
            "sources": [], "citations": [], "total_sources": 0,
            "coverage_map": [], "fetch_required": False,
        })
        self.assertFalse(out["passed"])
        self.assertEqual(out["conclusion_cap"], "low")
        self.assertEqual(out["failures"][0]["id"], "no_sources")

    def test_no_urls_fails_even_with_total_sources(self):
        from research_gates import evaluate_dossier_gates

        out = evaluate_dossier_gates({
            "sources": [{"title": "无 URL"}],
            "citations": [],
            "total_sources": 3,
            "coverage_map": [], "fetch_required": False,
        })
        self.assertFalse(out["passed"])
        ids = [f["id"] for f in out["failures"]]
        self.assertIn("no_sources", ids)

    def test_uncovered_fails(self):
        from research_gates import evaluate_dossier_gates

        out = evaluate_dossier_gates({
            "sources": [{"url": "https://a.com"}],
            "total_sources": 1,
            "coverage_map": [{"dimension": "定义", "status": "NOT_COVERED"}],
            "fetch_required": False,
        })
        self.assertFalse(out["passed"])
        self.assertEqual(out["failures"][0]["id"], "uncovered_dimensions")

    def test_fetch_required_unverified_fails(self):
        from research_gates import evaluate_dossier_gates

        out = evaluate_dossier_gates({
            "sources": [{"url": "https://a.com"}],
            "total_sources": 1,
            "coverage_map": [{"status": "COVERED"}],
            "fetch_required": True,
        })
        self.assertFalse(out["passed"])
        ids = [f["id"] for f in out["failures"]]
        self.assertIn("fetch_required_unverified", ids)

    def test_fact_conflicts_are_warning(self):
        from research_gates import evaluate_dossier_gates

        out = evaluate_dossier_gates({
            "sources": [{"url": "https://a.com"}],
            "total_sources": 1,
            "coverage_map": [{"status": "COVERED"}],
            "fetch_required": False,
            "fact_alignment": {"fact_conflicts": [{"type": "money"}]},
        })
        self.assertTrue(out["passed"])
        self.assertEqual(out["conclusion_cap"], "medium")
        self.assertEqual(out["warnings"][0]["id"], "fact_conflicts")


class TestDossierContract(unittest.TestCase):
    def test_snippet_is_not_verifiable(self):
        from research_dossier import build_dossier

        collection = {
            "merged_results": [{
                "title": "t", "url": "https://a.com/x?utm_source=x",
                "snippet": "营收 94.9", "source": "eastmoney",
            }],
            "sub_results": [{
                "intent": "事实", "sub_query": "q", "strategy": "direct",
                "results": [{
                    "title": "t", "url": "https://a.com/x?utm_source=x",
                    "snippet": "营收 94.9", "source": "eastmoney",
                }],
            }],
            "engines_used": ["eastmoney"],
            "total_results": 1,
            "elapsed_ms": 1,
        }
        dossier = build_dossier("q", collection, [], mode="fast", depth="fast")
        self.assertEqual(dossier["kind"], "dossier")
        rec = dossier["verification_records"][0]
        self.assertEqual(rec["result"], "unverified_snippet")
        self.assertNotEqual(rec["result"], "verifiable")

    def test_canonical_url_dedup(self):
        from research_dossier import build_dossier

        a = {
            "title": "t1", "url": "https://www.A.com/x/?utm_source=1",
            "snippet": "s", "source": "e1",
        }
        b = {
            "title": "t2", "url": "https://a.com/x",
            "snippet": "s2", "source": "e2",
        }
        collection = {
            "merged_results": [a, b],
            "sub_results": [{
                "intent": "x", "sub_query": "q", "strategy": "direct",
                "results": [a, b],
            }],
            "engines_used": ["e1", "e2"],
            "total_results": 2,
            "elapsed_ms": 1,
        }
        dossier = build_dossier("q", collection, [], mode="fast", depth="fast")
        self.assertEqual(len(dossier["citations"]), 1)

    def test_synthesize_report_alias(self):
        from research import synthesize_report, build_dossier
        self.assertIs(synthesize_report, build_dossier)

    def test_decompose_query_still_exported(self):
        from research import decompose_query, expand_query
        self.assertIs(decompose_query, expand_query)
        out = expand_query("CRISPR 论文", 3)
        self.assertGreaterEqual(len(out), 1)


class TestLocalFileInputs(unittest.TestCase):
    """file_inputs 白名单校验（fail-closed）。"""

    def _write(self, tmp_dir: str, name: str, text: str) -> str:
        p = Path(tmp_dir) / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_parse_with_file_inputs(self):
        from research_work_packages import parse_work_packages
        with tempfile.TemporaryDirectory() as tmp:
            f = self._write(tmp, "data.csv", "a,b\n1,2\n")
            pkgs = parse_work_packages([{
                "id": "wp1", "question": "营收是多少",
                "file_inputs": [{"path": f, "role": "原始数据"}],
            }])
            fi = pkgs[0]["file_inputs"][0]
            self.assertEqual(fi["kind"], "csv")  # 扩展名推断
            self.assertEqual(fi["role"], "原始数据")
            self.assertTrue(fi["path"].endswith("data.csv"))
            self.assertEqual(fi["size"], 8)

    def test_missing_path_raises(self):
        from research_work_packages import parse_work_packages
        with self.assertRaises(ValueError) as cm:
            parse_work_packages([{"id": "x", "question": "q",
                                  "file_inputs": [{"role": "data"}]}])
        self.assertIn("缺少 path", str(cm.exception))

    def test_nonexistent_file_raises(self):
        from research_work_packages import parse_work_packages
        with self.assertRaises(ValueError) as cm:
            parse_work_packages([{"id": "x", "question": "q",
                                  "file_inputs": [{"path": "/no/such/file.csv"}]}])
        self.assertIn("不存在", str(cm.exception))

    def test_directory_rejected(self):
        from research_work_packages import parse_work_packages
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as cm:
                parse_work_packages([{"id": "x", "question": "q",
                                      "file_inputs": [{"path": tmp}]}])
            self.assertIn("不是普通文件", str(cm.exception))

    def test_unsupported_kind_rejected(self):
        from research_work_packages import parse_work_packages
        with tempfile.TemporaryDirectory() as tmp:
            f = self._write(tmp, "data.bin", "x")
            with self.assertRaises(ValueError) as cm:
                parse_work_packages([{"id": "x", "question": "q",
                                      "file_inputs": [{"path": f}]}])
            self.assertIn("不受支持", str(cm.exception))

    def test_no_file_inputs_default_empty(self):
        from research_work_packages import parse_work_packages
        pkgs = parse_work_packages([{"id": "x", "question": "q"}])
        self.assertEqual(pkgs[0]["file_inputs"], [])


class TestLocalSourcesDossier(unittest.TestCase):
    """file_inputs 入账：哈希/血缘，内容不入账。"""

    def _collection(self):
        return {
            "merged_results": [],
            "sub_results": [],
            "engines_used": [],
            "total_results": 0,
            "elapsed_ms": 1,
        }

    def test_local_sources_with_hashes_no_content(self):
        from research_dossier import build_dossier
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "raw.csv"
            f.write_text("营收,2025\n94.9\n", encoding="utf-8")
            expect_hash = hashlib.sha256(f.read_bytes()).hexdigest()
            dossier = build_dossier(
                "q", self._collection(), [],
                file_inputs=[{"path": str(f), "kind": "csv", "role": "数据"}],
            )
        ls = dossier["local_sources"]
        self.assertEqual(len(ls), 1)
        rec = ls[0]
        self.assertEqual(rec["ref"], "[L1]")
        self.assertEqual(rec["type"], "file")
        self.assertEqual(rec["kind"], "csv")
        self.assertEqual(rec["role"], "数据")
        self.assertEqual(rec["sha256"], expect_hash)
        self.assertNotIn("content", rec)  # 内容不入账
        self.assertIn("路径与行号", rec["note"])

    def test_empty_file_inputs_gives_empty_list(self):
        from research_dossier import build_dossier
        dossier = build_dossier("q", self._collection(), [])
        self.assertEqual(dossier["local_sources"], [])

    def test_unreadable_file_skipped_not_fatal(self):
        from research_dossier import build_dossier
        dossier = build_dossier(
            "q", self._collection(), [],
            file_inputs=[{"path": "/no/such/file.csv", "kind": "csv"}],
        )
        self.assertEqual(dossier["local_sources"], [])


class TestLocalPrimaryGate(unittest.TestCase):
    """本地一手文件计入 no_primary_sources 判定。"""

    def test_local_sources_satisfy_primary(self):
        from research_gates import evaluate_dossier_gates
        dossier = {
            "sources": [{"url": "https://a.com/1"}],
            "coverage_map": [],
            "source_grades": {"primary": [], "secondary": ["权威"]},
            "source_leads": [{"evidence_tier": "secondary"}],
            "local_sources": [{"ref": "[L1]"}],
        }
        out = evaluate_dossier_gates(dossier)
        ids = [w["id"] for w in out["warnings"]]
        self.assertNotIn("no_primary_sources", ids)

    def test_no_local_sources_still_warns(self):
        from research_gates import evaluate_dossier_gates
        dossier = {
            "sources": [{"url": "https://a.com/1"}],
            "coverage_map": [],
            "source_grades": {"primary": [], "secondary": ["权威"]},
            "source_leads": [{"evidence_tier": "secondary"}],
        }
        out = evaluate_dossier_gates(dossier)
        ids = [w["id"] for w in out["warnings"]]
        self.assertIn("no_primary_sources", ids)


class TestWorkPackageCollection(unittest.TestCase):
    def test_work_packages_skip_expand(self):
        from research import deep_research

        calls: list[str] = []

        def fake_collect(sub_queries, *a, **kw):
            calls.extend(sq["query"] for sq in sub_queries)
            return {
                "merged_results": [],
                "sub_results": [{
                    "sub_query": sq["query"], "intent": sq["intent"],
                    "strategy": sq["strategy"], "results": [],
                    "package_id": sq.get("package_id"),
                } for sq in sub_queries],
                "engines_used": [],
                "total_results": 0,
                "elapsed_ms": 1,
                "budget_exhausted": False,
                "budget_limit": None,
            }

        with patch("research.collect_sources", side_effect=fake_collect), \
             patch("research.build_plan", create=True):
            report = deep_research(
                "固态电池",
                num_sub_queries=4,
                max_results=1,
                timeout=1,
                depth="fast",
                mode="fast",
                work_packages=[
                    {"id": "def", "question": "定义包"},
                    {"id": "risk", "question": "风险包", "depends_on": ["def"]},
                ],
            )
        self.assertEqual(report["kind"], "dossier")
        self.assertEqual(calls, ["定义包", "风险包"])
        self.assertEqual(report["work_package_stages"], [["def"], ["risk"]])
        self.assertNotIn("query_expansion", report)
        self.assertEqual(report["conclusion_cap"], "low")

    def test_priority_sources_all_engines_propagated(self):
        from research_work_packages import packages_to_sub_queries

        sqs = packages_to_sub_queries([{
            "id": "def",
            "question": "定义包",
            "priority_sources": ["arxiv", "semantic_scholar"],
        }])
        self.assertEqual(sqs[0]["preferred_engines"], ["arxiv", "semantic_scholar"])
        self.assertEqual(sqs[0]["preferred_engine"], "arxiv")


if __name__ == "__main__":
    unittest.main()

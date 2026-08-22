#!/usr/bin/env python3
"""工作包交接、可判定门禁、dossier 契约。"""

from __future__ import annotations

import sys
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

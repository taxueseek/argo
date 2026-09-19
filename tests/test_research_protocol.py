#!/usr/bin/env python3
"""工作包交接、可判定检查、dossier 契约、本地文件入账。"""

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
    """file_inputs 白名单校验（默认拒绝）。"""

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
    """file_inputs 入账：哈希/来源记录，内容不入账。"""

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


class TestResearchEvidenceGateWiring(unittest.TestCase):
    """研究路径的高后果门控必须真的接上（2026-09-19 修复）。

    `_attach_evidence_loop` 从**单条结果**读 `r.get("domain")` 当研究域，但全仓
    没有任何 producer 往结果行写 `domain`（rerank 写的是 authority/absorption/
    evidence_flags 那一组），而 `_search_one` 又把 super_search 返回的路由域整个
    丢掉。于是 domain 恒空 → `is_high_consequence_domain(None)`=False →
    finance/medical/legal 研究的 `fetch_required` 恒为 False，高后果 dossier
    可以拿到 conclusion_cap=high 而从未核验正文。
    """

    def _gate(self, subs):
        from research import _attach_evidence_loop
        report = {}
        _attach_evidence_loop(report, {"sub_results": subs})
        return report.get("fetch_required")

    def _sub(self, domain, intent="x"):
        s = {"sub_query": "q", "intent": intent,
             "results": [{"title": "T", "url": "https://a.example/1",
                          "snippet": "s"}]}
        if domain:
            s["domain"] = domain
        return s

    def test_high_consequence_domain_sets_fetch_required(self):
        for dom in ("stock_query", "financial_news", "medical", "legal"):
            self.assertTrue(self._gate([self._sub(dom)]),
                            f"{dom} 研究应要求核验正文")

    def test_general_domain_does_not_require_fetch(self):
        self.assertFalse(self._gate([self._sub("general_search")]))

    def test_missing_domain_degrades_conservatively(self):
        """没有域信息时不误报（历史形态）。"""
        self.assertFalse(self._gate([self._sub(None)]))

    def test_sub_query_carries_routed_domain(self):
        """_search_one 必须把路由域带下去——否则门控拿不到任何输入。"""
        import inspect
        import research
        src = inspect.getsource(research)
        self.assertIn('"domain": result.get("domain")', src,
                      "_search_one 的 out 里缺 domain，高后果门控会再次失联")


class TestResearchGateVerifyTruthiness(unittest.TestCase):
    """「跑过 --verify」≠「核验成功」（2026-09-19 修复）。

    dossier["verify"] 是 verify_results 的返回 dict，research_cli 在 --verify
    分支里无条件赋值它；全部 fetch 失败时它仍是 {verified: [], ...}，真值判据
    为真。于是「高后果取证尚未核验」这道门在「核验全失败」时反而放行——正是
    它要拦的场景。
    """

    def _gates(self, dossier):
        from research_gates import evaluate_dossier_gates
        return [f["id"] for f in evaluate_dossier_gates(dossier).get("failures", [])]

    def test_all_failed_verify_still_blocks(self):
        ids = self._gates({"fetch_required": True,
                           "evidence_loop": {"verified_count": 0},
                           "verify": {"verified": [], "revision_summary": {}}})
        self.assertIn("fetch_required_unverified", ids)

    def test_successful_verify_unblocks(self):
        ids = self._gates({"fetch_required": True,
                           "evidence_loop": {"verified_count": 0},
                           "verify": {"verified": [{"url": "u"}]}})
        self.assertNotIn("fetch_required_unverified", ids)

    def test_not_run_verify_blocks(self):
        ids = self._gates({"fetch_required": True,
                           "evidence_loop": {"verified_count": 0}})
        self.assertIn("fetch_required_unverified", ids)


class TestCrossVerificationConflicts(unittest.TestCase):
    """交叉验证的冲突检测曾是死代码（2026-09-19 修复）。

    两个坑叠加：读的字段 `authority.source_type` 从未存在（score_authority 的
    返回键是 score/reason/tier/domain/is_serp），且 credibility 只挂在 merged
    上、不挂在各子查询的原始结果上。于是 tiers 恒空 → conflicts 恒空 →
    报告里的「⚠ 混入低证据层级来源」永远不打印。
    """

    def _conflicts(self, urls):
        from research_dossier import _build_cross_verification
        sub = {"intent": "dim", "sub_query": "q",
               "results": [{"title": t, "url": u} for t, u in urls]}
        merged = [{"title": "m", "url": "https://www.gov.cn/a"}]
        return _build_cross_verification(merged, "q", [sub]).get("conflicts") or []

    def test_low_tier_mixed_in_is_detected(self):
        got = self._conflicts([("a", "https://blog.example.com/p"),
                               ("b", "https://www.gov.cn/a")])
        self.assertEqual(len(got), 1, "混入低层级来源应报冲突")

    def test_all_authoritative_is_clean(self):
        got = self._conflicts([("a", "https://www.gov.cn/a"),
                               ("b", "https://docs.python.org/3/")])
        self.assertEqual(got, [], "全权威来源不应报冲突")

#!/usr/bin/env python3
"""recompute 可复算闭环：执行器安全四组 + 入账 + 门禁（P0-2）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from recompute import run_recompute, extract_values  # noqa: E402


def _csv(tmp: str, text: str = "year,revenue\n2024,120\n2025,148\n") -> str:
    p = Path(tmp) / "sales.csv"
    p.write_text(text, encoding="utf-8")
    return str(p)


class TestRunRecompute(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.f = _csv(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unauthorized_skipped(self):
        r = run_recompute("print(1)", [{"path": self.f}])
        self.assertFalse(r["ok"])
        self.assertIn("fail-closed", r.get("skipped_reason", ""))

    def test_authorized_computes(self):
        code = (
            "import csv\n"
            "rows = list(csv.DictReader(open(_ALLOWED[0], encoding='utf-8')))\n"
            "rev = {int(r['year']): float(r['revenue']) for r in rows}\n"
            "print((rev[2025] - rev[2024]) / rev[2024])\n"
        )
        r = run_recompute(code, [{"path": self.f}], allow_exec=True)
        self.assertTrue(r["ok"], r)
        self.assertIn("0.233", r["stdout"])
        self.assertEqual(extract_values(r["stdout"]), [0.233333])  # round(6)

    def test_path_whitelist_enforced(self):
        code = "print(open('/etc/passwd').read())"
        r = run_recompute(code, [{"path": self.f}], allow_exec=True)
        self.assertFalse(r["ok"])
        self.assertNotEqual(r["exit_code"], 0)

    def test_network_disabled(self):
        code = "import urllib.request\nurllib.request.urlopen('https://x.com')"
        r = run_recompute(code, [{"path": self.f}], allow_exec=True)
        self.assertFalse(r["ok"])
        self.assertIn("禁止网络", r["stderr"])

    def test_timeout_kills(self):
        r = run_recompute("import time\ntime.sleep(60)",
                          [{"path": self.f}], allow_exec=True, timeout_s=1)
        self.assertTrue(r.get("timed_out"))
        self.assertLess(r["elapsed_ms"], 10000)

    def test_no_inputs_skipped(self):
        r = run_recompute("print(1)", [], allow_exec=True)
        self.assertFalse(r["ok"])
        self.assertIn("无白名单输入", r.get("skipped_reason", ""))

    def test_extract_values(self):
        self.assertEqual(extract_values("growth=23.33% | 1,234.5 万 | -3.2"),
                         [0.2333, 1234.5, -3.2])


class TestRecomputeContract(unittest.TestCase):
    def test_normalize_recompute(self):
        from research_work_packages import normalize_recompute
        r = normalize_recompute({"script": "print(1)", "expect": "0.23",
                                 "budget": {"timeout_s": 10}})
        self.assertEqual(r["budget"]["timeout_s"], 10)
        self.assertEqual(r["expect"], "0.23")

    def test_missing_script_raises(self):
        from research_work_packages import normalize_recompute
        with self.assertRaises(ValueError):
            normalize_recompute({"expect": "x"})

    def test_package_parse_with_recompute(self):
        from research_work_packages import parse_work_packages
        with tempfile.TemporaryDirectory() as tmp:
            f = _csv(tmp)
            pkgs = parse_work_packages([{
                "id": "wp1", "question": "q",
                "file_inputs": [{"path": f}],
                "recompute": {"script": "print(1)", "budget": {"timeout_s": 5}},
            }])
            self.assertEqual(pkgs[0]["recompute"]["script"], "print(1)")
            self.assertEqual(pkgs[0]["recompute"]["budget"]["timeout_s"], 5)


class TestRecomputeGate(unittest.TestCase):
    def _dossier(self, **kw):
        base = {
            "sources": [{"title": "营收增长", "url": "https://a.com",
                         "snippet": "revenue growth 0.23"}],
            "coverage_map": [],
        }
        base.update(kw)
        return base

    def test_expected_but_skipped_warns(self):
        from research_gates import evaluate_dossier_gates
        out = evaluate_dossier_gates(self._dossier(recompute_expected=True))
        self.assertEqual(out["conclusion_cap"], "medium")

    def test_executed_and_matching_no_warning(self):
        from research_gates import evaluate_dossier_gates
        out = evaluate_dossier_gates(self._dossier(
            recomputed_values=[{"package_id": "wp1", "ok": True,
                                "values": [0.23]}]))
        self.assertEqual(out["conclusion_cap"], "high")

    def test_conflicting_values_warn(self):
        from research_gates import evaluate_dossier_gates
        out = evaluate_dossier_gates(self._dossier(
            recomputed_values=[{"package_id": "wp1", "ok": True,
                                "values": [1.5]}]))
        caps = [w["id"] for w in out["warnings"]]
        self.assertIn("recompute_conflict", caps)
        self.assertEqual(out["conclusion_cap"], "medium")


class TestDossierRecomputeSection(unittest.TestCase):
    def test_recomputed_values_ledger(self):
        from research_dossier import build_dossier
        collection = {
            "merged_results": [], "sub_results": [], "engines_used": [],
            "total_results": 0, "elapsed_ms": 1,
        }
        dossier = build_dossier(
            "q", collection, [],
            recompute_results=[{"package_id": "wp1", "ok": True,
                                "stdout": "0.2333", "elapsed_ms": 42}],
        )
        rv = dossier["recomputed_values"]
        self.assertEqual(len(rv), 1)
        self.assertEqual(rv[0]["ref"], "[R1]")
        self.assertEqual(rv[0]["package_id"], "wp1")
        self.assertEqual(rv[0]["values"], [0.2333])
        self.assertIn("重算", rv[0]["note"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""wide_research 的行为保证：轨道归一/分阶段、账本去重、门禁判定、报告落盘。

插件语义对齐的回归锚：这里的判定必须与 packages/dsh-plugin 的
stageTracks / normalizeSource / buildLedger / evaluateGates 同判——
两条形态共用一套证据语义，测试就是那把尺子。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

from wide_research import (  # noqa: E402
    build_ledger,
    evaluate_gates,
    normalize_source,
    normalize_track_result,
    normalize_tracks,
    persist_report,
    render_report,
    stage_tracks,
)


def _track(tid: str, depends_on: list[str] | None = None) -> dict:
    return {"id": tid, "title": f"t-{tid}", "question": f"q-{tid}?",
            "rationale": "r", "depends_on": depends_on or []}


class TestNormalizeTracks(unittest.TestCase):
    def test_dedupe_and_cap(self):
        raw = [_track("a"), _track("a"), _track("b")] + [_track(f"x{i}") for i in range(12)]
        out = normalize_tracks(raw, 9)
        self.assertEqual(len(out), 9)
        self.assertEqual(out[0]["id"], "a")
        self.assertNotIn("a", [t["id"] for t in out[1:]])

    def test_invalid_rows_dropped_and_id_slugged(self):
        out = normalize_tracks([{"title": "no question"}, {"title": "t", "question": "q", "id": "A B/ c"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "a-b-c")

    def test_string_json_input(self):
        out = normalize_tracks('[{"id":"a","title":"t","question":"q"}]')
        self.assertEqual(out[0]["id"], "a")


class TestStageTracks(unittest.TestCase):
    def test_dependents_run_later(self):
        tracks = [_track("b", ["a"]), _track("a"), _track("c")]
        stages, warnings = stage_tracks(tracks)
        ids = [[t["id"] for t in stage] for stage in stages]
        self.assertEqual(ids[0], ["a", "c"])  # 无依赖并行（id 序）
        self.assertEqual(ids[1], ["b"])
        self.assertEqual(warnings, [])

    def test_missing_dependency_warns_not_crash(self):
        tracks = [_track("a", ["ghost"])]
        stages, warnings = stage_tracks(tracks)
        self.assertTrue(any("ghost" in w for w in warnings))

    def test_cycle_merged_into_last_stage_with_warning(self):
        tracks = [_track("a", ["b"]), _track("b", ["a"])]
        stages, warnings = stage_tracks(tracks)
        self.assertTrue(warnings)
        self.assertEqual(sum(len(s) for s in stages), 2)


class TestSourcesAndLedger(unittest.TestCase):
    def test_http_only_and_confidence_clamp(self):
        ok = normalize_source({"title": "t", "url": "https://x.com/a", "claim": "c", "confidence": "HIGH"})
        self.assertIsNotNone(ok)
        self.assertEqual(ok["confidence"], "high")
        self.assertIsNone(normalize_source({"title": "t", "url": "file:///etc/passwd", "claim": "c"}))
        self.assertIsNone(normalize_source({"title": "t", "url": "javascript:alert(1)", "claim": "c"}))
        self.assertIsNone(normalize_source({"title": "t", "url": "https://x.com/b"}))  # 缺 claim

    def test_ledger_dedupes_url_variants(self):
        results = [
            normalize_track_result({"sources": [
                {"title": "A", "url": "https://x.com/p", "claim": "c1", "confidence": "high"},
            ]}, "t1"),
            normalize_track_result({"sources": [
                {"title": "A2", "url": "https://x.com/p#frag", "claim": "c2"},
                {"title": "B", "url": "https://x.com/p/", "claim": "c3"},
                {"title": "C", "url": "https://y.com/", "claim": "c4"},
            ]}, "t2"),
        ]
        sources, ledger = build_ledger(results)
        self.assertEqual([s["key"] for s in sources], ["S1", "S2"])
        self.assertEqual(ledger[0]["source_keys"], ["S1"])
        self.assertEqual(ledger[1]["source_keys"], ["S2"])

    def test_failed_track_keeps_error(self):
        result = normalize_track_result({"error": "worker died"}, "t9")
        self.assertEqual(result["error"], "worker died")
        self.assertEqual(result["sources"], [])


class TestGates(unittest.TestCase):
    def _src(self, confidence: str = "high") -> dict:
        return {"key": "S1", "title": "t", "url": "https://x.com", "source_type": "web",
                "claim": "c", "confidence": confidence, "limitations": ""}

    def test_clean_run_is_high(self):
        ledger = [{"track_id": "t1", "error": None, "summary": "", "findings": [],
                   "source_keys": ["S1"], "disagreements": [], "gaps": []}]
        gates = evaluate_gates([self._src()], ledger, [], [])
        self.assertTrue(gates["passed"])
        self.assertEqual(gates["conclusion_cap"], "high")

    def test_no_sources_is_low(self):
        gates = evaluate_gates([], [{"track_id": "t1", "error": None, "summary": "",
                                     "findings": [], "source_keys": [], "disagreements": [], "gaps": []}], [], [])
        self.assertFalse(gates["passed"])
        self.assertEqual(gates["conclusion_cap"], "low")
        self.assertIn("no_sources", [f["id"] for f in gates["failures"]])

    def test_partial_failure_warns_but_passes(self):
        ledger = [
            {"track_id": "t1", "error": None, "summary": "", "findings": [],
             "source_keys": ["S1"], "disagreements": [], "gaps": []},
            {"track_id": "t2", "error": "boom", "summary": "", "findings": [],
             "source_keys": [], "disagreements": [], "gaps": []},
        ]
        gates = evaluate_gates([self._src()], ledger, [], [])
        self.assertTrue(gates["passed"])
        self.assertEqual(gates["conclusion_cap"], "medium")
        self.assertIn("partial_track_failure", [w["id"] for w in gates["warnings"]])

    def test_all_failed_is_low(self):
        ledger = [{"track_id": "t1", "error": "boom", "summary": "", "findings": [],
                   "source_keys": [], "disagreements": [], "gaps": []}]
        gates = evaluate_gates([], ledger, [], [])
        self.assertFalse(gates["passed"])
        self.assertEqual(gates["conclusion_cap"], "low")

    def test_low_confidence_only_warns(self):
        ledger = [{"track_id": "t1", "error": None, "summary": "", "findings": [],
                   "source_keys": ["S1"], "disagreements": [], "gaps": []}]
        gates = evaluate_gates([self._src("low")], ledger, [], [])
        self.assertTrue(gates["passed"])
        self.assertEqual(gates["conclusion_cap"], "medium")
        self.assertIn("no_high_confidence_sources", [w["id"] for w in gates["warnings"]])

    def test_high_uncertainty_threshold(self):
        ledger = [{"track_id": "t1", "error": None, "summary": "", "findings": [],
                   "source_keys": ["S1"], "disagreements": [], "gaps": []}]
        caveats = [f"c{i}" for i in range(3)]  # max(3, ceil(1/2)) = 3，踩线即警告
        gates = evaluate_gates([self._src()], ledger, caveats, [])
        self.assertEqual(gates["conclusion_cap"], "medium")
        self.assertIn("high_uncertainty", [w["id"] for w in gates["warnings"]])


class TestReport(unittest.TestCase):
    def test_render_and_persist(self):
        sources = [{"key": "S1", "title": "T", "url": "https://x.com", "source_type": "web",
                    "claim": "c", "confidence": "high", "limitations": ""}]
        text = render_report("Q", "sum", "body", sources, ["w1"])
        self.assertIn("# Wide Research Report", text)
        self.assertIn("[S1] T — https://x.com", text)
        self.assertIn("- w1", text)
        with tempfile.TemporaryDirectory() as tmp:
            path = persist_report(Path(tmp), "问题：测试/特殊 字符", text)
            self.assertTrue(Path(path).exists())
            self.assertIn("问题_测试_特殊_字符", Path(path).name)


class TestCli(unittest.TestCase):
    def test_stage_cli(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "wide_research.py"), "stage",
             "--tracks", json.dumps([_track("a"), _track("b", ["a"])])],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["stages"], [["a"], ["b"]])

    def test_merge_cli_with_missing_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracks_dir = Path(tmp) / "tracks"
            tracks_dir.mkdir()
            (tracks_dir / "t1.json").write_text(json.dumps({
                "track_id": "t1", "summary": "s",
                "sources": [{"title": "A", "url": "https://x.com", "claim": "c", "confidence": "high"}],
                "findings": ["f1"], "disagreements": [], "gaps": [],
            }), encoding="utf-8")
            tracks = json.dumps([{"id": "t1", "title": "T1", "question": "q1"},
                                 {"id": "t2", "title": "T2", "question": "q2"}])
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "wide_research.py"), "merge",
                 "--run-dir", tmp, "--tracks", tracks, "--question", "Q", "--write-report"],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["stats"]["plannedTracks"], 2)
            self.assertEqual(out["stats"]["failedTracks"], 1)  # t2 无产出 → 失败行
            self.assertEqual(out["stats"]["sourceCount"], 1)
            self.assertEqual(out["quality_gate_results"]["conclusion_cap"], "medium")
            self.assertTrue(out["report_path"] and Path(out["report_path"]).exists())

    def test_merge_cli_with_synthesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracks_dir = Path(tmp) / "tracks"
            tracks_dir.mkdir()
            (tracks_dir / "t1.json").write_text(json.dumps({
                "track_id": "t1", "summary": "s",
                "sources": [{"title": "A", "url": "https://x.com", "claim": "c", "confidence": "high"}],
            }), encoding="utf-8")
            synth = Path(tmp) / "synthesis.json"
            synth.write_text(json.dumps({
                "answer": "正文 [S1]", "executive_summary": "摘要",
                "caveats": ["c1", "c2", "c3"], "unanswered_questions": [],
            }), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "wide_research.py"), "merge",
                 "--run-dir", tmp, "--synthesis", str(synth)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertIn("high_uncertainty", [w["id"] for w in out["quality_gate_results"]["warnings"]])
            self.assertEqual(out["caveats"], ["c1", "c2", "c3"])


if __name__ == "__main__":
    unittest.main()

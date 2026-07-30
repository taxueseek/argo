#!/usr/bin/env python3
"""archive_run 离线单测。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import archive_run as ar  # noqa: E402


SAMPLE = {
    "query": "测试查询 archive",
    "status": "completed",
    "mode": "auto",
    "depth": "fast",
    "engine": "mock",
    "engines": ["mock"],
    "engines_used": ["mock"],
    "results": [
        {
            "title": "示例标题",
            "url": "https://example.com/a?utm_source=x",
            "snippet": "这是摘要线索",
            "source": "mock",
            "score": 0.9,
        },
        {
            "title": "重复 URL",
            "url": "https://example.com/a",
            "snippet": "另一摘要",
            "source": "mock2",
        },
    ],
    "count": 2,
    "errors": [],
    "engine_outcomes": [
        {"engine": "mock", "status": "ok", "results_count": 2, "latency_ms": 10}
    ],
    "input_kind": "keyword",
    "limitations": ["snippet is a discovery clue, not verified body text"],
    "candidates": [
        {
            "candidate_id": "web:abc",
            "query": "测试查询 archive",
            "platform": "web",
            "backend": "mock",
            "rank": 1,
            "title": "示例标题",
            "url": "https://example.com/a?utm_source=x",
            "canonical_url": "https://example.com/a",
            "snippet": "这是摘要线索",
            "verification": {"status": "candidate", "opened_original": False},
        }
    ],
    "coverage": [
        {"backend": "mock", "status": "ok", "returned": 2, "truncated": False}
    ],
}


class TestArchiveRun(unittest.TestCase):
    def test_write_and_list_and_show(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "archive"
            meta = ar.write_search_archive(
                dict(SAMPLE),
                root=root,
                tag="unit",
                note="offline",
                source="test",
            )
            self.assertTrue(Path(meta["run_dir"]).is_dir())
            run_dir = Path(meta["run_dir"])
            for name in (
                "run-summary.json",
                "envelope.json",
                "candidates.jsonl",
                "results.jsonl",
                "sources.jsonl",
                "coverage.json",
                "INDEX.md",
            ):
                self.assertTrue((run_dir / name).is_file(), name)

            summary = json.loads((run_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["schema_version"], ar.SCHEMA)
            self.assertTrue(summary["boundaries"]["discovery_only"])
            self.assertFalse(summary["boundaries"]["overwrite"])
            self.assertEqual(summary["counts"]["candidates"], 1)

            # index
            self.assertTrue((root / "index.jsonl").is_file())
            rows = ar.list_runs(root, limit=5, tag="unit")
            self.assertEqual(len(rows), 1)
            self.assertIn("测试查询", rows[0]["query"])

            shown = ar.load_run(run_dir)
            self.assertEqual(shown["summary"]["run_id"], meta["run_id"])

            # 不覆盖：再写一次得到不同目录
            meta2 = ar.write_search_archive(dict(SAMPLE), root=root, tag="unit")
            self.assertNotEqual(meta["run_dir"], meta2["run_dir"])

    def test_no_body_fetch_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = dict(SAMPLE)
            payload["_secret"] = "should-drop"
            payload["cookies"] = "drop-me"
            meta = ar.write_search_archive(payload, root=root)
            env = json.loads(Path(meta["paths"]["envelope"]).read_text(encoding="utf-8"))
            self.assertNotIn("_secret", env)
            self.assertNotIn("cookies", env)


if __name__ == "__main__":
    unittest.main(verbosity=2)

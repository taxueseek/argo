#!/usr/bin/env python3
"""本地打通三通道测试：--include-local 并入 / argo_local_read 白名单预览 / 归档检索。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from archive_run import search_archive  # noqa: E402
from mcp_handlers import _local_read_preview  # noqa: E402


class TestLocalReadPreview(unittest.TestCase):
    def _dir_env(self, d: str):
        os.environ["ARGO_LOCAL_READ_DIRS"] = d
        self.addCleanup(lambda: os.environ.pop("ARGO_LOCAL_READ_DIRS", None))

    def test_reads_text_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._dir_env(tmp)
            f = Path(tmp) / "notes.md"
            f.write_text("# 标题\n正文内容\n", encoding="utf-8")
            content, meta = _local_read_preview(str(f), max_chars=4000)
            self.assertIn("正文内容", content)
            self.assertEqual(meta["is_preview"], False)

    def test_no_whitelist_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("ARGO_LOCAL_READ_DIRS", None)
            f = Path(tmp) / "a.txt"
            f.write_text("x", encoding="utf-8")
            with self.assertRaises(PermissionError) as cm:
                _local_read_preview(str(f))
            self.assertIn("未配置白名单", str(cm.exception))

    def test_outside_whitelist_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            self._dir_env(str(allowed))
            with self.assertRaises(PermissionError):
                _local_read_preview(str(outside))

    def test_binary_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._dir_env(tmp)
            f = Path(tmp) / "data.pdf"
            f.write_bytes(b"%PDF-1.4")
            with self.assertRaises(PermissionError):
                _local_read_preview(str(f))

    def test_line_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._dir_env(tmp)
            f = Path(tmp) / "log.txt"
            f.write_text("".join(f"line {i}\n" for i in range(50)),
                         encoding="utf-8")
            content, meta = _local_read_preview(str(f), line_start=10,
                                                line_end=12)
            self.assertIn("line 10", content)
            self.assertNotIn("line 20", content)
            self.assertEqual(meta["lines"], 3)


class TestRunLocalSeek(unittest.TestCase):
    def test_parses_seek_output(self):
        import search as S

        fake_out = json.dumps({
            "query": "q", "engine": "rg", "count": 2,
            "results": [
                {"path": "/tmp/a.md", "line": 3, "snippet": "hit one"},
                {"path": "/tmp/b.md", "line": 12, "snippet": "hit two"},
            ],
        })

        class _R:
            returncode = 0
            stdout = fake_out
            stderr = ""

        with patch.object(S.sys, "executable", "/usr/bin/python3"), \
             patch.object(S, "sys", S.sys), \
             patch("search._run_local_seek") as m:
            # 直接测试解析逻辑：monkeypatch 掉 subprocess 路径后调用真实实现
            pass

        # 直接用真实实现 + mock subprocess.run
        with patch.object(S, "sys", S.sys), \
             patch("subprocess.run", return_value=_R()) as mr, \
             patch("os.path.exists", return_value=True):
            # _run_local_seek 内 SCRIPT_DIR 定位 seek.py 走 os.path.exists 分支
            hits = S._run_local_seek("q", max_n=5)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["source"], "local_files")
        self.assertTrue(hits[0]["url"].endswith("#3"))


class TestSearchArchive(unittest.TestCase):
    def _index(self, root: Path, rows: list[dict]):
        (root / "index.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )

    def test_search_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._index(root, [
                {"run_id": "r1", "packed_at": "2026-08-01T10:00:00",
                 "query": "固态电池 硫化物", "tag": "research",
                 "source": "argo_research", "counts": {"results": 5},
                 "run_dir": str(root / "runs")},
                {"run_id": "r2", "packed_at": "2026-08-20T10:00:00",
                 "query": "公司营收增速", "tag": "research",
                 "source": "argo_research", "counts": {"results": 3},
                 "run_dir": str(root / "runs")},
            ])
            hits = search_archive("固态电池", root=root)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["run_id"], "r1")

    def test_time_window_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._index(root, [
                {"run_id": "r-old", "packed_at": "2026-07-01T10:00:00",
                 "query": "固态电池", "tag": None, "counts": {}},
                {"run_id": "r-new", "packed_at": "2026-08-10T10:00:00",
                 "query": "固态电池", "tag": None, "counts": {}},
            ])
            hits = search_archive("固态电池", root=root, since="2026-08-01")
            self.assertEqual([h["run_id"] for h in hits], ["r-new"])

    def test_no_index_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(search_archive("x", root=Path(tmp)), [])


if __name__ == "__main__":
    unittest.main()

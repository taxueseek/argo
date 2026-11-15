#!/usr/bin/env python3
"""test_p2_followup.py — 剩余两条 P2 的回归门禁。

1. JEV 本地隔离改用白名单：只有 http/https 公网 URL 可进 JEV 队列，
   其余形态（data: / localhost / 无 ./ 的相对路径 / 正斜杠盘符）一律拦。
2. engine_validate 的 --fix 变成预览态：不显式 --apply 绝不改写 Spec；
   --apply 也要过置信度门槛，低置信度建议不落盘。

两条都用「删掉修复就挂」的行为判据，不测函数是否存在。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import engine_validate  # noqa: E402
import semantic_evidence as se  # noqa: E402


class TestLocalTargetWhitelist(unittest.TestCase):
    """白名单判据：只有 http/https 放行，其余一律当本地拦下。"""

    def test_non_http_schemes_and_hostless_paths_are_blocked(self):
        blocked = (
            "data:text/plain,secret",
            "localhost:8080/notes.md",
            "Users/me/notes.md",
            "C:/Users/x/a.txt",
            "ftp://example.com/private.txt",
            "ws://example.com/socket",
            "//server/share/secret.md",
            "file:///etc/passwd",
            "~/notes.md",
            "./relative.md",
            "notes.md",
        )
        for p in blocked:
            self.assertTrue(se.is_local_target(p), f"私域目标被当成公网放行: {p!r}")

    def test_only_http_and_https_pass(self):
        for u in ("https://example.com/x", "http://example.com/x",
                  "HTTPS://EXAMPLE.COM/X", "https://r.jina.ai/https://example.com"):
            self.assertFalse(se.is_local_target(u), f"公网 URL 被误拦: {u!r}")

    def test_existing_local_forms_still_blocked(self):
        """回归：原黑名单已覆盖的形态不许因改白名单而漏。"""
        for p in ("file:///Users/x/secret.md", "/Users/x/s.md", "/home/x/s.md",
                  "~/notes.md", "./relative.md", ".\\local.md", "../parent.md",
                  "C:\\Users\\x\\a.doc", "/private/tmp/x"):
            self.assertTrue(se.is_local_target(p), f"本地路径未被拦截: {p!r}")


class TestFixIsPreviewOnly(unittest.TestCase):
    """--fix 与 --diagnose 只读输出补丁建议，绝不自动改写 Spec 真源。"""

    def test_fix_outputs_suggested_patch_without_writing_spec(self):
        diag = {
            "ok": False,
            "drift_detected": True,
            "stage": "field",
            "confidence": 0.95,
            "issues": ["标题字段失效"],
            "suggested_patch": {"item_title": "headline"},
        }
        spec = {"type": "http", "output_map": {"item_title": "title"}}
        tmp_root = Path(tempfile.mkdtemp(prefix="argo-p2-spec-"))
        spec_dir = tmp_root / "engines" / "specs"
        spec_dir.mkdir(parents=True)
        spec_path = spec_dir / "probe.yaml"
        spec_path.write_text("engine_id: probe\noutput_map:\n  item_title: title\n",
                             encoding="utf-8")
        try:
            with patch.object(engine_validate, "ROOT", tmp_root), \
                    patch.object(engine_validate, "_get_spec", return_value=spec), \
                    patch.object(engine_validate, "probe_engine_raw", return_value={"x": 1}), \
                    patch("engines_schema_drift.diagnose_schema_drift", return_value=diag):
                report = engine_validate.validate_engine(
                    "probe", stage="health", diagnose=True)
            self.assertFalse(report.get("fix_applied"))
            self.assertEqual(report.get("suggested_patch"), {"item_title": "headline"})
            self.assertIn("item_title: title", spec_path.read_text(encoding="utf-8"))
        finally:
            _cleanup(tmp_root)


def _cleanup(tmp_root):
    import shutil
    shutil.rmtree(tmp_root, ignore_errors=True)


class TestCliFlags(unittest.TestCase):
    """CLI 保留只读 --fix / --diagnose，不再暴露自动改写真源的 --apply。"""

    def test_main_help_exposes_fix_without_apply(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "engine_validate.py"), "--help"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--fix", proc.stdout)
        self.assertIn("--diagnose", proc.stdout)
        self.assertNotIn("--apply", proc.stdout)


if __name__ == "__main__":
    unittest.main()

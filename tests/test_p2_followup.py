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


class TestFixIsPreviewByDefault(unittest.TestCase):
    """--fix 不写盘；--apply 才写，且低置信度建议不落盘。"""

    def _report(self, *, apply: bool, confidence: float):
        diag = {
            "ok": False,
            "drift_detected": True,
            "stage": "field",
            "confidence": confidence,
            "issues": ["标题字段失效"],
            "suggested_patch": {"item_title": "headline"},
        }
        spec = {"type": "http", "output_map": {"item_title": "title"}}
        # 真实拼接路径是 ROOT/engines/specs/<engine_id>.yaml；夹具按这个形状
        # 建目录，否则 apply_schema_patch 读不到文件、异常被吞掉，
        # 测试会误报成「实现没落盘」。
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
                    patch("engines_parsers.diagnose_schema_drift", return_value=diag), \
                    patch("engines_parsers.apply_schema_patch", wraps=_fake_apply) as applied:
                report = engine_validate.validate_engine(
                    "probe", stage="health", diagnose=True, apply=apply)
            return report, spec_path, applied
        except Exception:
            import shutil
            shutil.rmtree(tmp_root, ignore_errors=True)
            raise

    def test_fix_without_apply_never_writes_spec(self):
        """用户没说 --apply 时，报告里必须带补丁预览但零写盘。"""
        report, spec_path, applied = self._report(apply=False, confidence=0.95)
        try:
            self.assertFalse(report.get("fix_applied"), "预览态不得声称已落盘")
            self.assertEqual(report.get("suggested_patch"), {"item_title": "headline"})
            applied.assert_not_called()
            self.assertIn("item_title: title", spec_path.read_text(encoding="utf-8"))
        finally:
            _cleanup(spec_path.parent.parent.parent)

    def test_apply_writes_spec_on_high_confidence(self):
        report, spec_path, applied = self._report(apply=True, confidence=0.95)
        try:
            self.assertTrue(report.get("fix_applied"))
            applied.assert_called_once()
            self.assertIn("item_title: headline", spec_path.read_text(encoding="utf-8"))
        finally:
            _cleanup(spec_path.parent.parent.parent)

    def test_apply_refuses_low_confidence_patch(self):
        """启发式选出的 0.80 建议不该自动改写唯一真源 Spec。"""
        report, spec_path, applied = self._report(apply=True, confidence=0.80)
        try:
            self.assertFalse(report.get("fix_applied"))
            self.assertTrue(report.get("fix_blocked_low_confidence"))
            applied.assert_not_called()
            self.assertIn("item_title: title", spec_path.read_text(encoding="utf-8"))
        finally:
            _cleanup(spec_path.parent.parent.parent)


def _cleanup(tmp_root):
    import shutil
    shutil.rmtree(tmp_root, ignore_errors=True)


def _fake_apply(spec_file, patch):
    p = Path(spec_file)
    text = p.read_text(encoding="utf-8")
    for k, v in patch.items():
        text = text.replace(f"{k}: title", f"{k}: {v}")
    p.write_text(text, encoding="utf-8")
    return True, "patched"


class TestCliFlags(unittest.TestCase):
    """CLI 必须同时认 --fix（预览）与 --apply（落盘）。"""

    def test_main_help_exposes_apply(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "engine_validate.py"), "--help"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--apply", proc.stdout)
        self.assertIn("--fix", proc.stdout)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""test_seek_cross_platform.py — 跨平台（Windows / Linux 裸机）开箱即用与来源隔离测试。

验证：
1. 无外部工具（无 rg / grep / fd）时，纯 Python 标准库保底能检索内容和文件名（零依赖开箱即用）；
2. 适配器输出包含严格的来源隔离标记（[本机] 前缀、source_type='local'、is_local=True、file:// URI）。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS = ROOT / "sub-skills" / "local-seek" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "scripts"))

import seek as s  # noqa: E402
import local_seek_adapter as adapter  # noqa: E402


def _run_seek(argv: list[str]) -> tuple[int, str]:
    with mock.patch.object(sys, "argv", ["seek.py"] + argv):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = s.main()
        except SystemExit as e:
            code = e.code or 0
    return code or 0, buf.getvalue()


class TestCrossPlatformZeroDeps(unittest.TestCase):
    """模拟无外部 CLI 工具的纯 Python 环境（Windows / Linux 裸机）。"""

    def test_content_search_pure_python_fallback(self):
        """无 rg 且无 grep 时，纯 Python walk 能正确命中文件内容。"""
        with tempfile.TemporaryDirectory() as tmp:
            test_file = Path(tmp) / "demo.py"
            test_file.write_text("def find_me_everywhere():\n    return 42\n", encoding="utf-8")

            with mock.patch.object(s, "tool_exists", lambda name: False), \
                 mock.patch.object(s, "resolve_grep", lambda: None):
                code, out = _run_seek(["find_me_everywhere", "--path", tmp, "--json"])

            self.assertEqual(code, 0, f"纯 Python 保底应返回 0，实际输出: {out}")
            data = json.loads(out)
            self.assertEqual(data.get("engine"), "py-walk", "无外部工具应降级到 py-walk")
            results = data.get("results") or []
            self.assertTrue(any("find_me_everywhere" in str(r) for r in results), "应成功命中目标内容")

    def test_filename_search_pure_python_fallback(self):
        """无 fd 时，纯 Python walk 能按文件名正确检索。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "target_doc.txt").write_text("content", encoding="utf-8")

            with mock.patch.object(s, "tool_exists", lambda name: False):
                code, out = _run_seek(["target_doc", "--path", tmp, "--filename", "--json"])

            self.assertEqual(code, 0, f"无 fd 时文件名搜索应返回 0，实际输出: {out}")
            data = json.loads(out)
            self.assertEqual(data.get("engine"), "py-walk")
            results = data.get("results") or []
            self.assertTrue(any("target_doc.txt" in str(r) for r in results), "应找到目标文件")


class TestSourceIsolation(unittest.TestCase):
    """验证本机私域与公网来源的隔离性。"""

    def test_adapter_tags_local_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "secret.py"
            f.write_text("LOCAL_CONFIG = 'xyz'\n", encoding="utf-8")

            with mock.patch.object(sys, "argv", ["local_seek_adapter.py", "LOCAL_CONFIG", "--path", tmp]):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = adapter.main()

            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            results = data.get("results") or []
            self.assertTrue(results, "适配器应产出结果")
            item = results[0]
            self.assertTrue(item.get("is_local"), "必须标记 is_local=True")
            self.assertEqual(item.get("source_type"), "local", "source_type 必须是 local")
            self.assertEqual(item.get("channel"), "local_filesystem", "channel 必须是 local_filesystem")
            self.assertTrue(str(item.get("title")).startswith("[本机]"), "标题必须带 [本机] 前缀")
            self.assertTrue(str(item.get("url")).startswith("file:///"), "URL 必须是 file:/// 格式")


if __name__ == "__main__":
    unittest.main()

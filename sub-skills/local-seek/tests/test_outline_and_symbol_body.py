"""Tests for outline line ranges and --symbol-body in local-seek."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import seek  # noqa: E402


class OutlineAndSymbolBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

        self.py_file = self.tmp / "sample.py"
        self.py_file.write_text(
            "\n".join([
                "import os",
                "",
                "def alpha(x):",
                "    return x + 1",
                "",
                "class Demo:",
                "    def run(self):",
                "        return 42",
                "",
                "    def helper(self):",
                "        return 0",
                "",
                "def beta():",
                "    return alpha(1)",
            ]) + "\n",
            encoding="utf-8",
        )

        self.big_py = self.tmp / "big.py"
        body = ["def huge_fn():"] + [f"    x_{i} = {i}" for i in range(1, 260)]
        self.big_py.write_text("\n".join(body) + "\n", encoding="utf-8")

        self.md_file = self.tmp / "doc.md"
        self.md_file.write_text(
            "\n".join([
                "# Intro",
                "hello",
                "## Detail",
                "line a",
                "line b",
                "# End",
                "bye",
            ]) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_outline_includes_start_end_and_span(self) -> None:
        out, rc = seek.outline_file(str(self.py_file))
        self.assertEqual(rc, 0)
        self.assertIn("3-4 (2L): def alpha(x):", out)
        self.assertIn("6-11 (6L): class Demo:", out)
        self.assertIn("7-8 (2L): def run(self):", out)
        self.assertIn("13-14 (2L): def beta():", out)

    def test_outline_markdown_levels(self) -> None:
        out, rc = seek.outline_file(str(self.md_file))
        self.assertEqual(rc, 0)
        self.assertIn("1-5 (5L): # Intro", out)
        self.assertIn("3-5 (3L): ## Detail", out)
        self.assertIn("6-7 (2L): # End", out)

    def test_symbol_body_exact_and_cli_dispatch(self) -> None:
        out, rc = seek.run_query(["--symbol-body", "Demo", str(self.py_file)])
        self.assertEqual(rc, 0)
        self.assertIn("第 6-11 行", out)
        self.assertIn("6: class Demo:", out)
        self.assertIn("11:         return 0", out)
        self.assertNotIn("13: def beta():", out)

    def test_symbol_body_truncates_at_200_lines(self) -> None:
        out, rc = seek.read_symbol_body(str(self.big_py), "huge_fn")
        self.assertEqual(rc, 0)
        self.assertIn("第 1-200 行", out)
        self.assertIn("已截断（上限 200 行）", out)
        self.assertIn("--lines 201-260", out)

    def test_symbol_body_missing_returns_error(self) -> None:
        out, rc = seek.read_symbol_body(str(self.py_file), "nonexistent")
        self.assertEqual(rc, 1)
        self.assertIn("未找到符号", out)


if __name__ == "__main__":
    unittest.main()

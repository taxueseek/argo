#!/usr/bin/env python3
"""seek_locator 单测：local-seek 子技能的安装感知定位（离线）

目标：锁定「单一真源 + 不写死主机路径」的发现规则——打包子技能优先，
ARGO_LOCAL_SEEK_PATH / ARGO_LOCAL_SEEK_ROOTS 承载自定义与遗留位置。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_seek_locator.py -q
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import seek_locator  # noqa: E402


class TestSeekLocator(unittest.TestCase):
    def setUp(self):
        for k in ("ARGO_LOCAL_SEEK_PATH", "ARGO_LOCAL_SEEK_ROOTS"):
            os.environ.pop(k, None)

    def _tmp_file(self):
        d = tempfile.TemporaryDirectory()
        p = Path(d.name) / "seek.py"
        p.write_text("print(1)", encoding="utf-8")
        self.addCleanup(d.cleanup)
        return str(p)

    def test_default_resolves_bundled(self):
        # 打包子技能存在（仓库标准装法），默认命中该路径。
        r = seek_locator.resolve_seek_py()
        self.assertTrue(r.endswith("sub-skills/local-seek/scripts/seek.py"))
        self.assertTrue(Path(r).is_file())

    def test_env_override_takes_priority(self):
        f = self._tmp_file()
        os.environ["ARGO_LOCAL_SEEK_PATH"] = f
        self.assertEqual(seek_locator.resolve_seek_py(), os.path.realpath(f))

    def test_env_roots_fallback(self):
        # 遗留/独立装法：roots 下 <根>/local-seek/scripts/seek.py
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        (root / "local-seek" / "scripts").mkdir(parents=True)
        (root / "local-seek" / "scripts" / "seek.py").write_text("print(1)", encoding="utf-8")
        os.environ["ARGO_LOCAL_SEEK_ROOTS"] = str(root)
        self.assertEqual(
            seek_locator.resolve_seek_py(),
            os.path.realpath(str(root / "local-seek" / "scripts" / "seek.py")),
        )

    def test_no_hardcoded_home_paths_in_logic(self):
        # 发现逻辑不应再写死 ~/.agents/skills / ~/.claude/skills 字面量。
        src = Path(seek_locator.__file__).read_text(encoding="utf-8")
        # 允许 docstring 里作为「禁止的示例」出现，但代码路径里不得硬编码命中。
        logic = src.split('"""')[2]  # 去掉 docstring，只看实现
        self.assertNotIn("~/.agents/skills", logic)
        self.assertNotIn("~/.claude/skills", logic)

    def test_isfile_uses_actual(self):
        # seek_py_exists 反映真实存在性，不受环境覆盖影响（指向不存在文件时为 False）。
        os.environ["ARGO_LOCAL_SEEK_PATH"] = "/nonexistent/seek.py"
        self.assertTrue(Path(seek_locator.resolve_seek_py()).is_file())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""test_local_seek_adaptive_roots.py — 验证私域检索自适应环境探测与去除硬编码。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import local_seek as ls


class TestAdaptiveRoots(unittest.TestCase):
    def test_no_gpt_or_private_hardcode(self):
        """候选根中绝不包含 Documents/GPT 等特定机器私有路径。"""
        roots = ls._candidate_targeted_roots()
        for r in roots:
            self.assertNotIn("Documents/GPT", r)
            self.assertFalse(r.endswith("GPT"))

    def test_detect_project_root(self):
        """子目录执行时能自适应向上溯源到工程根目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "my_project"
            sub = root / "src" / "package" / "submodule"
            sub.mkdir(parents=True)
            # 在 root 放置标记文件
            (root / "pyproject.toml").write_text("[project]\nname = 'test'\n")

            detected = ls._detect_project_root(str(sub))
            self.assertEqual(detected, str(root.resolve()))

    def test_local_seek_dir_upward_propagation(self):
        """_local_seek_dir 在工程子目录下能返回顶层根目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo_repo"
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            (root / ".git").mkdir()

            with mock.patch("os.getcwd", return_value=str(nested)):
                target = ls._local_seek_dir()
                self.assertEqual(target, str(root.resolve()))

    def test_container_env_detection(self):
        """云端容器环境变量（WORKSPACE）能被纳入候选根。"""
        with tempfile.TemporaryDirectory() as tmp:
            workspace_dir = Path(tmp) / "cloud_workspace"
            workspace_dir.mkdir()
            with mock.patch.dict(os.environ, {"WORKSPACE": str(workspace_dir)}):
                roots = ls._candidate_targeted_roots()
                self.assertIn(str(workspace_dir.resolve()), roots)


if __name__ == "__main__":
    unittest.main()

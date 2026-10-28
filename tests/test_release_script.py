#!/usr/bin/env python3
"""test_release_script.py — 发版脚本自身的门禁。

三条契约：
1. 改号规则覆盖全部机器声明：在真实仓库的拷贝上 bump 一次，release_check 必须全绿；
2. 内容清单必须能拦住「只改号不写文案」——这正是 v2.9.8 漏 README 的形态；
3. 版本守卫：只允许递增的三段版本号。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import release  # noqa: E402
import release_check  # noqa: E402

# bump 会碰的文件 + release_check 的输入；只拷这些，避免整仓 36MB
FIXTURE_FILES = sorted({rel for rel, *_ in release.rules("9.9.9", "9.9.10")})


def _fixture(tmp: Path, ver: str | None = None) -> Path:
    """在 tmp 造一棵最小检出：拷真实文件，版本统一成 ver。"""
    ver = ver or release.current_version(ROOT)
    for rel in FIXTURE_FILES:
        dst = tmp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    (tmp / "docs").mkdir(exist_ok=True)
    shutil.copy2(ROOT / f"docs/RELEASE_NOTES_v{ver}.md",
                 tmp / f"docs/RELEASE_NOTES_v{ver}.md")
    return tmp


class TestReleaseScript(unittest.TestCase):
    def test_bump_makes_machine_declarations_green(self):
        with tempfile.TemporaryDirectory() as d:
            root = _fixture(Path(d))
            old = release.current_version(root)
            release.bump(root, "2.11.0", git_checks=False)
            problems = release_check.check(root)
            # 改号只负责机器声明；发布表行属于内容，bump 不写，正好五语各一条
            self.assertEqual(len(problems), 5, problems)
            for p in problems:
                self.assertIn("发布记录表缺", p)
            self.assertEqual(release.current_version(root), "2.11.0")
            # 机器声明全部换新，不留旧号
            for rel, o, n, _ in release.rules(old, "2.11.0"):
                self.assertNotIn(o, (root / rel).read_text(encoding="utf-8"), rel)

    def test_bump_scaffolds_notes_and_content_check_catches_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = _fixture(Path(d))
            release.bump(root, "2.11.0", git_checks=False)
            notes = root / "docs/RELEASE_NOTES_v2.11.0.md"
            self.assertTrue(notes.is_file(), "bump 应生成发布说明骨架")
            problems = release.content_problems(root, "2.11.0")
            self.assertTrue(any("骨架" in p for p in problems), problems)
            # 五语各 3 条（最近更新 / 表行 / 能力段；能力标题由 bump 机械跟进）
            for rel in release_check.README_LANGS:
                self.assertTrue(any(p.startswith(rel) for p in problems), rel)
            self.assertGreaterEqual(len(problems), 1 + 3 * 5)

    def test_bump_refuses_same_or_lower_version(self):
        with tempfile.TemporaryDirectory() as d:
            root = _fixture(Path(d))
            with self.assertRaises(release.ReleaseError):
                release.bump(root, "2.10.0", git_checks=False)
            with self.assertRaises(release.ReleaseError):
                release.bump(root, "2.9.9", git_checks=False)

    def test_bump_refuses_bad_version_shape(self):
        with tempfile.TemporaryDirectory() as d:
            root = _fixture(Path(d))
            for bad in ("2.11", "v2.11.0", "2.11.0-rc1", "2.11.0.1"):
                with self.assertRaises(release.ReleaseError, msg=bad):
                    release.bump(root, bad, git_checks=False)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root = _fixture(Path(d))
            before = {rel: (root / rel).read_text(encoding="utf-8") for rel in FIXTURE_FILES}
            release.bump(root, "2.11.0", dry=True, git_checks=False)
            for rel, text in before.items():
                self.assertEqual((root / rel).read_text(encoding="utf-8"), text, rel)
            self.assertFalse((root / "docs/RELEASE_NOTES_v2.11.0.md").exists())

    def test_current_repo_is_release_ready(self):
        """当前仓库的内容清单必须全绿——不然下一次发版第一步就卡住。"""
        self.assertEqual(release.check(ROOT), [])


if __name__ == "__main__":
    unittest.main()

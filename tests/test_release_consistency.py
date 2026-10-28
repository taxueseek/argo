#!/usr/bin/env python3
"""test_release_consistency.py — 发布一致性预检的行为门（钉住 release_check）。

两条契约：
1. 当前仓库必须一致（版本漂移/漏写发布说明/漏登 npm 白名单 → 提交时红）；
2. 漂移必须被**报出来**（负向夹具：改一处版本号、删一行徽标、漏一份
   RELEASE_NOTES 登记，三种都要被点名）——只测「不抛异常」的测试等于没测。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import release_check  # noqa: E402


def _write_fixture(root: Path, ver: str = "9.9.9", drift: dict | None = None) -> None:
    """在 root 下造一棵最小可检树；drift 覆盖个别文件的版本号。"""
    drift = drift or {}

    def v(rel: str) -> str:
        return drift.get(rel, ver)

    (root / "pyproject.toml").write_text(
        f'[project]\nname = "argo-search"\nversion = "{ver}"\n', encoding="utf-8")
    (root / "package.json").write_text(json.dumps({
        "name": "argo-search",
        "version": v("package.json"),
        "files": [f"docs/RELEASE_NOTES_v{ver}.md"],
    }), encoding="utf-8")
    (root / "packages").mkdir()
    (root / "packages/dsh-plugin").mkdir()
    (root / "packages/dsh-plugin/package.json").write_text(json.dumps({
        "name": "@taxueseek/argo-dsh",
        "version": v("packages/dsh-plugin/package.json"),
    }), encoding="utf-8")
    (root / "packages/marketplace.json").write_text(json.dumps({
        "plugins": [{"name": "argo-zcode", "version": v("packages/marketplace.json")}],
    }), encoding="utf-8")
    (root / "server.json").write_text(json.dumps({
        "name": "io.github.taxueseek/argo",
        "version": v("server.json"),
        "packages": [{
            "registryType": "npm",
            "identifier": "argo-search",
            "version": drift.get("server.json#packages", v("server.json")),
        }],
    }), encoding="utf-8")
    (root / "packages/argo-zcode/.zcode-plugin").mkdir(parents=True)
    (root / "packages/argo-zcode/.zcode-plugin/plugin.json").write_text(json.dumps({
        "name": "argo-zcode",
        "version": v("packages/argo-zcode/.zcode-plugin/plugin.json"),
    }), encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts/mcp_transport.py").write_text(
        f'ARGO_MCP_VERSION = "{v("scripts/mcp_transport.py")}"\n', encoding="utf-8")
    (root / "argo_search").mkdir()
    (root / "argo_search/__init__.py").write_text(
        f'__version__ = "{v("argo_search/__init__.py")}"\n', encoding="utf-8")
    (root / "SKILL.md").write_text(
        f"---\nname: argo\nversion: {v('SKILL.md')}\n---\n", encoding="utf-8")
    for rel in release_check.README_LANGS:
        (root / rel).write_text(
            f"![version](https://img.shields.io/badge/version-{v(rel)}-informational)\n"
            f"| **v{v(rel)}** | notes |\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / f"docs/RELEASE_NOTES_v{ver}.md").write_text("# notes\n", encoding="utf-8")


class TestReleaseConsistency(unittest.TestCase):
    def test_current_repo_is_consistent(self):
        self.assertEqual(release_check.check(ROOT), [],
                         "仓库版本声明漂移；先让 `python3 scripts/release_check.py` 归零")

    def test_package_json_version_drift_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, drift={"package.json": "9.9.8"})
            problems = release_check.check(root)
        self.assertTrue(any("package.json" in p and "9.9.8" in p for p in problems),
                        problems)

    def test_server_json_package_version_drift_is_reported(self):
        """MCP 注册清单只改顶层版本、漏改 packages[] 时也要点名。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, drift={"server.json#packages": "9.9.8"})
            problems = release_check.check(root)
        self.assertTrue(
            any("server.json[argo-search]" in p and "9.9.8" in p for p in problems),
            problems)

    def test_mcp_transport_version_drift_is_reported(self):
        """MCP 握手自报版本（历史漂移点）漏改时要点名。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, drift={"scripts/mcp_transport.py": "9.9.8"})
            problems = release_check.check(root)
        self.assertTrue(
            any("mcp_transport.py" in p and "9.9.8" in p for p in problems), problems)

    def test_missing_readme_badge_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            # 把英文 README 的徽标换成旧版本，模拟「发版只改了主 README」
            readme = root / "README.en.md"
            readme.write_text(
                readme.read_text(encoding="utf-8").replace(
                    "badge/version-9.9.9-", "badge/version-9.9.8-"),
                encoding="utf-8")
            problems = release_check.check(root)
        self.assertTrue(any("README.en.md" in p for p in problems), problems)

    def test_unlisted_release_notes_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            # 盘上多一份旧发布说明但白名单没登记（npm 包会缺文件）
            (root / "docs/RELEASE_NOTES_v9.9.0.md").write_text("# old\n", encoding="utf-8")
            problems = release_check.check(root)
        self.assertTrue(any("RELEASE_NOTES_v9.9.0.md" in p for p in problems), problems)

    def test_missing_notes_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            (root / "docs/RELEASE_NOTES_v9.9.9.md").unlink()
            problems = release_check.check(root)
        self.assertTrue(any("发布说明不存在" in p for p in problems), problems)

    def test_non_checkout_root_is_reported_not_crash(self):
        """--root 指错目录要给人话，不是 FileNotFoundError 穿出去。"""
        with tempfile.TemporaryDirectory() as tmp:
            problems = release_check.check(Path(tmp))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("不像 argo 检出", problems[0])

    def test_missing_readme_is_reported_not_crash(self):
        """检出被裁剪（缺某个语种 README）同样只记一条问题，不炸。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            (root / "README.ko.md").unlink()
            problems = release_check.check(root)
        self.assertTrue(any("README.ko.md" in p and "读取失败" in p for p in problems),
                        problems)


if __name__ == "__main__":
    unittest.main()

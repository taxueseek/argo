#!/usr/bin/env python3
"""link_source.py 宿主入口校验/重建回归测试（全本地，不联网）。

背景（2026-09-14 实测）：resolve_targets() 会先对目标做 resolve()，把「已经
间接指向来源的 symlink」折叠成来源本体。后果有两个：
  1. link_one 看到目标是来源本体 → 打印 [skip] 而不是把入口重建为直连；
  2. check_targets 报 [ok]，无法区分直连与「隔了一层」的链接——校验常年绿灯，
     实际入口挂在中转链接上，中间那环被删改就成断链（历史上踩过）。
关键不变量：**规范化目标路径时不得跟随末级符号链接**。
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import link_source  # noqa: E402


def _make_repo(root: Path) -> Path:
    """造一个「像 argo 来源」的目录（含 link_source 的软校验标记文件）。"""
    repo = root / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "search.py").write_text("# stub\n", encoding="utf-8")
    (repo / "SKILL.md").write_text("# stub\n", encoding="utf-8")
    return repo


def _run(fn, *args, **kwargs):
    """跑一个会打印的函数，返回 (返回码, stdout)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kwargs)
    return code, buf.getvalue()


class TestHopsToSource(unittest.TestCase):
    """跳数判定：0 本体 / 1 直连 / >1 间接 / -1 未指向来源。"""

    def test_source_itself_is_zero(self):
        with TemporaryDirectory() as td:
            repo = _make_repo(Path(td))
            with patch.object(link_source, "SOURCE", repo):
                self.assertEqual(link_source._hops_to_source(repo), 0)

    def test_direct_link_is_one(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            entry = root / "entry"
            entry.symlink_to(repo, target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo):
                self.assertEqual(link_source._hops_to_source(entry), 1)

    def test_indirect_link_is_two(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            mid = root / "mid"
            mid.symlink_to(repo, target_is_directory=True)
            entry = root / "entry"
            entry.symlink_to(mid, target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo):
                self.assertEqual(link_source._hops_to_source(entry), 2)

    def test_relative_link_counts_as_one(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            entry = root / "entry"
            entry.symlink_to(Path("repo"), target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo):
                self.assertEqual(link_source._hops_to_source(entry), 1)

    def test_unrelated_path_is_minus_one(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            other = root / "other"
            other.mkdir()
            with patch.object(link_source, "SOURCE", repo):
                self.assertEqual(link_source._hops_to_source(other), -1)

    def test_broken_symlink_is_minus_one(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            dangling = root / "dangling"
            dangling.symlink_to(root / "nope", target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo):
                self.assertEqual(link_source._hops_to_source(dangling), -1)

    def test_symlink_loop_is_minus_one(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            a, b = root / "a", root / "b"
            a.symlink_to(b, target_is_directory=True)
            b.symlink_to(a, target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo):
                self.assertEqual(link_source._hops_to_source(a), -1)


class TestResolveTargetsKeepsSymlink(unittest.TestCase):
    """目标路径必须是「链接自己」，不能被 resolve 折叠成来源。"""

    def test_symlinked_target_not_collapsed(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            mid = root / "mid"
            mid.symlink_to(repo, target_is_directory=True)
            entry = root / "entry"
            entry.symlink_to(mid, target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo), \
                 patch.object(link_source, "LOCAL_INSTALLS", root / "none.yaml"), \
                 patch.dict(os.environ, {"ARGO_LINK_TARGETS": ""}):
                targets = link_source.resolve_targets([entry])
            self.assertEqual(targets, [entry])
            self.assertNotEqual(targets[0], repo)

    def test_env_and_cli_deduped_after_normalize(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            entry = root / "entry"
            with patch.object(link_source, "SOURCE", repo), \
                 patch.object(link_source, "LOCAL_INSTALLS", root / "none.yaml"), \
                 patch.dict(os.environ,
                            {"ARGO_LINK_TARGETS": f"{entry}:{entry}/"}):
                targets = link_source.resolve_targets([entry, Path(str(entry) + "/")])
            self.assertEqual(targets, [entry])


class TestLinkOneRebuildsIndirect(unittest.TestCase):
    """间接链接必须被重建为直连（只替换 symlink，不动任何目录内容）。"""

    def test_indirect_is_relinked_to_direct(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            mid = root / "mid"
            mid.symlink_to(repo, target_is_directory=True)
            entry = root / "entry"
            entry.symlink_to(mid, target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo):
                code, out = _run(link_source.link_one, entry,
                                 dry_run=False, force=False)
            self.assertEqual(code, 0)
            self.assertIn("[relink]", out)
            self.assertTrue(entry.is_symlink())
            self.assertEqual(entry.resolve(), repo.resolve())
            self.assertEqual(os.readlink(entry), str(repo.resolve()))
            with patch.object(link_source, "SOURCE", repo):
                self.assertEqual(link_source._hops_to_source(entry), 1)
            self.assertTrue(mid.is_symlink())  # 中间那环不被动

    def test_indirect_dry_run_touches_nothing(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            mid = root / "mid"
            mid.symlink_to(repo, target_is_directory=True)
            entry = root / "entry"
            entry.symlink_to(mid, target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo):
                code, out = _run(link_source.link_one, entry,
                                 dry_run=True, force=False)
            self.assertEqual(code, 0)
            self.assertIn("[dry]", out)
            self.assertEqual(os.readlink(entry), str(mid))

    def test_direct_link_untouched(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            entry = root / "entry"
            entry.symlink_to(repo, target_is_directory=True)
            raw = os.readlink(entry)
            with patch.object(link_source, "SOURCE", repo):
                code, out = _run(link_source.link_one, entry,
                                 dry_run=False, force=False)
            self.assertEqual(code, 0)
            self.assertIn("[ok]", out)
            self.assertEqual(os.readlink(entry), raw)

    def test_source_dir_skipped(self):
        with TemporaryDirectory() as td:
            repo = _make_repo(Path(td))
            with patch.object(link_source, "SOURCE", repo):
                code, out = _run(link_source.link_one, repo,
                                 dry_run=False, force=False)
            self.assertEqual(code, 0)
            self.assertIn("[skip]", out)

    def test_foreign_dir_without_force_is_refused(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            foreign = root / "foreign"
            foreign.mkdir()
            (foreign / "keep.txt").write_text("data", encoding="utf-8")
            with patch.object(link_source, "SOURCE", repo):
                code, _ = _run(link_source.link_one, foreign,
                               dry_run=False, force=False)
            self.assertEqual(code, 1)
            self.assertTrue((foreign / "keep.txt").exists())
            self.assertFalse(foreign.is_symlink())

    def test_force_refuses_non_argo_dir(self):
        """--force 的软校验：不像 argo 副本的目录不许被整目录迁走。"""
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            foreign = root / "foreign"
            foreign.mkdir()
            (foreign / "keep.txt").write_text("data", encoding="utf-8")
            with patch.object(link_source, "SOURCE", repo):
                code, _ = _run(link_source.link_one, foreign,
                               dry_run=False, force=True)
            self.assertEqual(code, 1)
            self.assertTrue((foreign / "keep.txt").exists())

    def test_broken_link_replaced_with_force(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            entry = root / "entry"
            entry.symlink_to(root / "gone", target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo):
                code, _ = _run(link_source.link_one, entry,
                               dry_run=False, force=True)
            self.assertEqual(code, 0)
            self.assertEqual(entry.resolve(), repo.resolve())

    def test_missing_target_created(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            entry = root / "nested" / "entry"
            with patch.object(link_source, "SOURCE", repo):
                code, out = _run(link_source.link_one, entry,
                                 dry_run=False, force=False)
            self.assertEqual(code, 0)
            self.assertIn("[link]", out)
            self.assertEqual(entry.resolve(), repo.resolve())


class TestCheckTargets(unittest.TestCase):
    """--check 必须能区分直连 / 间接 / 未指向。"""

    def test_direct_passes(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            entry = root / "entry"
            entry.symlink_to(repo, target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo):
                code, out = _run(link_source.check_targets, [entry])
            self.assertEqual(code, 0)
            self.assertIn("[ok]", out)

    def test_indirect_fails_with_hint(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            mid = root / "mid"
            mid.symlink_to(repo, target_is_directory=True)
            entry = root / "entry"
            entry.symlink_to(mid, target_is_directory=True)
            with patch.object(link_source, "SOURCE", repo):
                code, out = _run(link_source.check_targets, [entry])
            self.assertEqual(code, 1)
            self.assertIn("[warn]", out)
            self.assertIn("2 跳", out)

    def test_unrelated_fails(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            other = root / "other"
            other.mkdir()
            with patch.object(link_source, "SOURCE", repo):
                code, out = _run(link_source.check_targets, [other])
            self.assertEqual(code, 1)
            self.assertIn("[miss]", out)

    def test_no_targets_fails(self):
        code, out = _run(link_source.check_targets, [])
        self.assertEqual(code, 1)
        self.assertIn("[fail]", out)


class TestSubLinks(unittest.TestCase):
    """sub_links 子技能直连与越界防护。"""

    def test_sub_links_link_and_check(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            sub_src = repo / "sub-skills" / "argo-wide-research"
            sub_src.mkdir(parents=True)
            (sub_src / "SKILL.md").write_text("# wide\n", encoding="utf-8")
            dst = root / "host" / "argo-wide-research"
            local_yaml = repo / "installs.local.yaml"
            local_yaml.write_text(
                f"sub_links:\n  {dst}: sub-skills/argo-wide-research\n",
                encoding="utf-8",
            )
            with patch.object(link_source, "SOURCE", repo), \
                 patch.object(link_source, "LOCAL_INSTALLS", local_yaml):
                subs = link_source._load_local_sub_links()
                self.assertEqual(subs, [(dst, sub_src.resolve())])
                code, _ = _run(link_source.link_one, dst, dry_run=False, force=False, source=sub_src)
                self.assertEqual(code, 0)
                chk_code, out = _run(link_source.check_targets, [], subs)
                self.assertEqual(chk_code, 0)
                self.assertIn("[ok]", out)

    def test_sub_links_rejects_escape_and_absolute(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = _make_repo(root)
            outside = root / "outside"
            outside.mkdir()
            local_yaml = repo / "installs.local.yaml"
            local_yaml.write_text(
                "sub_links:\n"
                f"  {root / 'a'}: ../outside\n"
                f"  {root / 'b'}: {outside}\n",
                encoding="utf-8",
            )
            with patch.object(link_source, "SOURCE", repo), \
                 patch.object(link_source, "LOCAL_INSTALLS", local_yaml):
                self.assertEqual(link_source._load_local_sub_links(), [])


if __name__ == "__main__":
    unittest.main()

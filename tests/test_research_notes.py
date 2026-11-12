#!/usr/bin/env python3
"""research_notes 离线单测：归档目录、索引、契约骨架、search_archive 并入。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import research_notes as rn  # noqa: E402
import archive_run as ar  # noqa: E402


class StudyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_slug_from_url(self):
        self.assertEqual(
            rn._slugify("https://hy.tencent.com/research/hyra2?langVersion=zh"),
            "hyra2",
        )
        self.assertEqual(rn._slugify("https://example.com/a/"), "a")
        self.assertEqual(rn._slugify(""), "note")

    def test_study_creates_note_and_index(self):
        out = self.root / "study" / "hyra2" / "hyra2-研究.md"
        rc = rn.main([
            "study",
            "https://hy.tencent.com/research/hyra2?langVersion=zh",
            "--title", "Hyra 2.0 研究",
            "--slug", "hyra2",
            "--root", str(self.root),
            "--archive-text", "三个月前，我们发布了 Hyra 1.0。",
            "--related", "ernie-evolve",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(out.is_file())
        text = out.read_text(encoding="utf-8")
        # 五段式齐全
        for heading, _ in rn.SECTIONS:
            self.assertIn(heading, text)
        # 头部固定字段
        self.assertIn("研究对象：https://hy.tencent.com/research/hyra2", text)
        self.assertIn("证据口径", text)
        self.assertIn("关联：ernie-evolve", text)
        # 索引写入
        idx = self.root / "research-index.jsonl"
        self.assertTrue(idx.is_file())
        row = json.loads(idx.read_text(encoding="utf-8").strip())
        self.assertEqual(row["kind"], "study")
        self.assertEqual(row["slug"], "hyra2")
        self.assertEqual(row["related"], "ernie-evolve")
        self.assertTrue(row["archive_sha256"])

    def test_explore_has_candidate_section(self):
        out = self.root / "explore" / "auto-research" / "auto-research-探索.md"
        rc = rn.main([
            "explore", "自动化科研的下一步是什么",
            "--slug", "auto-research",
            "--root", str(self.root),
        ])
        self.assertEqual(rc, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("## 5. 候选方向", text)
        for heading, _ in rn.SECTIONS:
            self.assertIn(heading, text)

    def test_search_index_filters_by_kind(self):
        rn.main([
            "study", "https://example.com/a", "--slug", "alpha",
            "--root", str(self.root),
        ])
        rn.main([
            "explore", "beta 问题", "--slug", "beta",
            "--root", str(self.root),
        ])
        self.assertEqual(len(rn.search_index("alpha", root=self.root)), 1)
        self.assertEqual(len(rn.search_index("beta", root=self.root, kind="explore")), 1)
        self.assertEqual(len(rn.search_index("beta", root=self.root, kind="study")), 0)
        # 子串命中 slug
        self.assertEqual(len(rn.search_index("alp", root=self.root)), 1)

    def test_no_index_when_root_unwritable(self):
        # 索引写入失败不应阻断主流程（返回 indexed False，不抛异常）
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "research-index.jsonl").mkdir()  # 目录占位，写文件必失败
            meta = rn.append_index({"slug": "x"}, root=root)
            self.assertFalse(meta["indexed"])


class SearchArchiveMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_include_notes_merges_note_index(self):
        rn.main([
            "study", "https://example.com/hyra2", "--slug", "hyra2",
            "--root", str(self.root),
        ])
        hits = ar.search_archive("hyra2", root=self.root, include_notes=True)
        note_hits = [h for h in hits if h.get("source") == "research_note"]
        self.assertEqual(len(note_hits), 1)
        self.assertEqual(note_hits[0]["note_kind"], "study")
        self.assertTrue(note_hits[0]["run_dir"].endswith("hyra2-研究.md"))

    def test_without_flag_no_note_hits(self):
        rn.main([
            "study", "https://example.com/hyra2", "--slug", "hyra2",
            "--root", str(self.root),
        ])
        hits = ar.search_archive("hyra2", root=self.root)
        self.assertEqual([h for h in hits if h.get("source") == "research_note"], [])

    def test_note_index_missing_is_safe(self):
        hits = ar.search_archive("nothing", root=self.root, include_notes=True)
        self.assertEqual(hits, [])

    def test_include_notes_finds_notes_under_archive_root(self):
        """真实调用路径：`--include-notes` 传的是 archive_root（<base>/archive），
        笔记索引在 <base>/research-index.jsonl。8ec0501 之前归档根就是笔记根，
        改共用基目录后这里会静默空结果——回归用例锁住它。
        """
        rn.main([
            "study", "https://example.com/hyra2", "--slug", "hyra2",
            "--root", str(self.root),
        ])
        # 模拟生产路径：归档根 = 笔记根/archive
        archive_root = self.root / "archive"
        archive_root.mkdir()
        hits = ar.search_archive("hyra2", root=archive_root, include_notes=True)
        note_hits = [h for h in hits if h.get("source") == "research_note"]
        self.assertEqual(len(note_hits), 1, "archive_root 口径应能命中笔记")

    def test_include_notes_dedupes_across_candidate_roots(self):
        """两个候选根都有同一条笔记时只出一行。"""
        rn.main([
            "study", "https://example.com/hyra2", "--slug", "hyra2",
            "--root", str(self.root),
        ])
        archive_root = self.root / "archive"
        archive_root.mkdir()
        # 在 archive 下也放一份索引（模拟分设或历史残留）
        (archive_root / "research-index.jsonl").write_text(
            (self.root / "research-index.jsonl").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        hits = ar.search_archive("hyra2", root=archive_root, include_notes=True)
        note_hits = [h for h in hits if h.get("source") == "research_note"]
        self.assertEqual(len(note_hits), 1, "两个根都命中同一条时要去重")

    def test_include_notes_works_when_roots_are_split(self):
        """ARGO_RESEARCH_ROOT 与 ARGO_ARCHIVE_ROOT 分设两棵树时仍能命中。

        共用基目录只是默认布局，用户完全可以把笔记与归档分到两个卷（例如
        归档进冷存储、笔记留本地）。此时 archive_root.parent 不是笔记根，
        必须显式取笔记根，不能靠「父目录」推断。
        """
        note_root = self.root / "vol" / "research"
        archive_root = self.root / "vol" / "archive"
        rn.main([
            "study", "https://example.com/split", "--slug", "split",
            "--root", str(note_root),
        ])
        archive_root.mkdir(parents=True)
        old = os.environ.get("ARGO_RESEARCH_ROOT")
        os.environ["ARGO_RESEARCH_ROOT"] = str(note_root)
        try:
            hits = ar.search_archive("split", root=archive_root, include_notes=True)
        finally:
            if old is None:
                os.environ.pop("ARGO_RESEARCH_ROOT", None)
            else:
                os.environ["ARGO_RESEARCH_ROOT"] = old
        note_hits = [h for h in hits if h.get("source") == "research_note"]
        self.assertEqual(len(note_hits), 1, "分设两棵树时也要能命中笔记")


class CrossEnvironmentRootTest(unittest.TestCase):
    """跨环境（容器 / 无持久盘 / 多机）归档根兜底。

    核心判据：工作区可锚定时行为**完全不变**（项目级归档跟着仓库走）；
    只有完全无标记的目录才退回用户级文档目录，避免结论随容器销毁。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._cwd = Path.cwd()
        self.addCleanup(lambda: os.chdir(self._cwd))
        # 环境变量逐用例快照还原：_isolated_module 改 HOME、honors_env 设
        # ARGO_RESEARCH_ROOT，都只在本用例内生效，不泄漏给同进程后续用例
        # （2026-11-11 评审：靠各用例自律清理是顺序地雷）。
        self._env_snapshot = dict(os.environ)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env_snapshot)

    def _isolated_module(self, tmp_path: Path):
        """在被隔离的环境变量与 cwd 下重新导入 argo_paths（模块级缓存会串味）。"""
        import importlib
        import argo_paths
        for var in ("ARGO_RESEARCH_ROOT", "ARGO_ARCHIVE_ROOT", "ARGO_STATE_DIR",
                    "XDG_DOCUMENTS_DIR", "HOME", "USERPROFILE"):
            os.environ.pop(var, None)
        os.environ["HOME"] = str(tmp_path / "home")
        os.environ["XDG_DOCUMENTS_DIR"] = str(tmp_path / "docs")
        importlib.reload(argo_paths)
        return argo_paths

    def test_workspace_with_marker_keeps_project_root(self):
        """有 .git 的目录：照旧落项目内 research/，不碰用户级兜底。"""
        ws = Path(self.tmp.name) / "repo"
        ws.mkdir()
        (ws / ".git").mkdir()
        os.chdir(ws)
        root = rn.resolve_root()
        self.assertEqual(root, ws.resolve() / "research")

    def test_markerless_dir_falls_back_to_user_docs(self):
        """无标记的临时目录（容器 workdir、/tmp）：退回用户级文档目录。"""
        bare = Path(self.tmp.name) / "bare"
        bare.mkdir()
        os.chdir(bare)
        # 清掉可能影响判定的环境变量
        for var in ("ARGO_RESEARCH_ROOT", "ARGO_ARCHIVE_ROOT"):
            os.environ.pop(var, None)
        root = rn.resolve_root()
        self.assertNotEqual(root, bare.resolve() / "research")
        self.assertIn("argo-research", str(root))

    def test_explicit_dir_beats_fallback(self):
        bare = Path(self.tmp.name) / "bare2"
        bare.mkdir()
        os.chdir(bare)
        for var in ("ARGO_RESEARCH_ROOT", "ARGO_ARCHIVE_ROOT"):
            os.environ.pop(var, None)
        explicit = Path(self.tmp.name) / "explicit"
        self.assertEqual(rn.resolve_root(str(explicit)), explicit.resolve())

    def test_env_beats_fallback(self):
        bare = Path(self.tmp.name) / "bare3"
        bare.mkdir()
        os.chdir(bare)
        os.environ["ARGO_RESEARCH_ROOT"] = str(Path(self.tmp.name) / "envroot")
        self.assertEqual(
            rn.resolve_root(), (Path(self.tmp.name) / "envroot").resolve())

    def test_research_root_is_independent_of_state_root(self):
        """缓存目录可清，结论目录不可丢：两者必须不同源。"""
        ap = self._isolated_module(Path(self.tmp.name))
        state = ap.state_root()
        research = ap.research_root()
        self.assertNotEqual(state, research)
        self.assertNotIn(".cache", str(research))

    def test_research_root_honors_env(self):
        ap = self._isolated_module(Path(self.tmp.name))
        os.environ["ARGO_RESEARCH_ROOT"] = "/mnt/vol/research"
        self.assertEqual(ap.research_root(), Path("/mnt/vol/research"))

    def test_archive_root_falls_back_too(self):
        """归档根与笔记根用同一套兜底口径，不一边丢一边不丢。"""
        bare = Path(self.tmp.name) / "bare4"
        bare.mkdir()
        os.chdir(bare)
        for var in ("ARGO_RESEARCH_ROOT", "ARGO_ARCHIVE_ROOT"):
            os.environ.pop(var, None)
        a_root = ar.resolve_archive_root()
        self.assertIn("argo-research", str(a_root))
        self.assertTrue(str(a_root).endswith("archive"))


class TestWorkspaceResearchBundle(unittest.TestCase):
    """工作区原生归档与自包含资产包（Dossier Bundle）测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws_dir = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def test_find_workspace_root_by_git_marker(self):
        # 模拟工程子目录
        sub_dir = self.ws_dir / "src" / "deep_module"
        sub_dir.mkdir(parents=True)
        (self.ws_dir / ".git").mkdir()

        found = rn.find_workspace_root(sub_dir)
        self.assertEqual(found, self.ws_dir)

    def test_resolve_root_defaults_to_docs_research_if_docs_present(self):
        (self.ws_dir / "pyproject.toml").touch()
        (self.ws_dir / "docs").mkdir()

        import unittest.mock as mock
        with mock.patch("pathlib.Path.cwd", return_value=self.ws_dir):
            root = rn.resolve_root()
            self.assertEqual(root, self.ws_dir / "docs" / "research")

    def test_study_bundle_creates_manifest_sources_and_judgement(self):
        bundle_root = self.ws_dir / "docs" / "research"
        rc = rn.main([
            "study",
            "https://arxiv.org/abs/2401.0001",
            "--title", "Diffusion Models Review",
            "--slug", "diff-review",
            "--dir", str(bundle_root),
            "--archive-text", "Deep learning generative models review text...",
        ])
        self.assertEqual(rc, 0)

        bundle_dir = bundle_root / "study" / "diff-review"
        self.assertTrue(bundle_dir.is_dir())
        # 1. 00_判断稿.md 生成
        judgement = bundle_dir / "00_判断稿.md"
        self.assertTrue(judgement.is_file())
        self.assertIn("## 0. 结论速览", judgement.read_text(encoding="utf-8"))

        # 2. sources 目录与原文存储
        sources_dir = bundle_dir / "sources"
        self.assertTrue(sources_dir.is_dir())
        source_files = list(sources_dir.glob("*.md"))
        self.assertEqual(len(source_files), 1)

        # 3. manifest.json 自包含证据账本
        manifest_file = bundle_dir / "manifest.json"
        self.assertTrue(manifest_file.is_file())
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest["slug"], "diff-review")
        self.assertEqual(manifest["judgement_file"], "00_判断稿.md")
        self.assertEqual(len(manifest["sources"]), 1)
        self.assertTrue(manifest["sources"][0]["sha256"])

    def test_single_physical_note_and_symlink_sync(self):
        bundle_root = self.ws_dir / "docs" / "research"
        rc = rn.main([
            "study",
            "https://arxiv.org/abs/2401.0002",
            "--title", "Symlink Test",
            "--slug", "symlink-test",
            "--dir", str(bundle_root),
            "--archive-text", "Initial source text",
        ])
        self.assertEqual(rc, 0)
        bundle_dir = bundle_root / "study" / "symlink-test"
        judgement = bundle_dir / "00_判断稿.md"
        legacy = bundle_dir / "symlink-test-研究.md"

        self.assertTrue(judgement.is_file())
        self.assertTrue(legacy.is_file())
        self.assertTrue(legacy.is_symlink())

        # 编辑 00_判断稿.md，软链即时同步，消除物理副本漂移
        with judgement.open("a", encoding="utf-8") as f:
            f.write("\n追加关键洞察\n")
        self.assertIn("追加关键洞察", legacy.read_text(encoding="utf-8"))

        # 原文 sources 验证：sources 下为唯一实体，根目录下为相对软链
        sources_dir = bundle_dir / "sources"
        source_files = list(sources_dir.glob("*.md"))
        self.assertEqual(len(source_files), 1)
        source_real = source_files[0]
        source_link = bundle_dir / source_real.name
        self.assertTrue(source_real.is_file())
        self.assertTrue(source_link.is_symlink())


class WorkspaceRootAndBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def test_git_marker_wins_over_nested_package_json(self):
        """repo/.git 必须压过 repo/frontend/package.json，否则归档落进子包。"""
        (self.ws / ".git").mkdir()
        frontend = self.ws / "frontend"
        frontend.mkdir()
        (frontend / "package.json").write_text("{}", encoding="utf-8")
        self.assertEqual(rn.find_workspace_root(frontend), self.ws)

    def test_fresh_workspace_notes_and_archive_share_one_base(self):
        """首次使用不得分裂成 research/ 与 docs/research/archive/ 两棵树。"""
        (self.ws / ".git").mkdir()
        import unittest.mock as mock
        with mock.patch("pathlib.Path.cwd", return_value=self.ws):
            notes = rn.resolve_root()
            archive = ar.resolve_archive_root()
        self.assertEqual(notes, self.ws / "research")
        self.assertEqual(archive, self.ws / "research" / "archive")


class TestArchiveRunProjectStandard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws_dir = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def test_resolve_archive_root_prefers_docs_research_archive(self):
        (self.ws_dir / ".git").mkdir()
        (self.ws_dir / "docs" / "research").mkdir(parents=True)

        import unittest.mock as mock
        with mock.patch("pathlib.Path.cwd", return_value=self.ws_dir):
            root = ar.resolve_archive_root()
            self.assertEqual(root, self.ws_dir / "docs" / "research" / "archive")

    def test_resolve_archive_root_preserves_legacy_data_archive_if_exists(self):
        (self.ws_dir / ".git").mkdir()
        legacy = self.ws_dir / "数据" / "argo-search-archive"
        legacy.mkdir(parents=True)

        import unittest.mock as mock
        with mock.patch("pathlib.Path.cwd", return_value=self.ws_dir):
            root = ar.resolve_archive_root()
            self.assertEqual(root, legacy)


class NoOverwriteOnRerunTest(unittest.TestCase):
    """同 slug 重跑必须换目录，不能覆盖旧笔记、sources 原文与 manifest。

    回退验证：把 `_cmd_study` / `_cmd_explore` 里的 `_unique_dir(...)` 换回
    `root / "kind" / slug` 后，本组用例会挂（旧文件被覆盖、只剩 1 个索引行）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _run_study(self, archive_text: str):
        return rn.main([
            "study", "https://example.com/a", "--slug", "dup",
            "--title", "同一 slug", "--dir", str(self.root),
            "--archive-text", archive_text,
        ])

    def test_rerun_same_slug_keeps_both_dirs(self):
        self.assertEqual(self._run_study("第一版原文"), 0)
        self.assertEqual(self._run_study("第二版原文"), 0)
        dirs = sorted(p.name for p in (self.root / "study").iterdir())
        self.assertEqual(len(dirs), 2, f"重跑后应有两个目录，实际：{dirs}")
        self.assertEqual(dirs[0], "dup")
        self.assertTrue(dirs[1].startswith("dup-"), dirs)

    def test_old_note_sources_and_manifest_survive_rerun(self):
        self._run_study("第一版原文")
        first_dir = self.root / "study" / "dup"
        first_note = first_dir / "00_判断稿.md"
        first_source = next((first_dir / "sources").glob("dup_原文版_*.md"))
        self.assertTrue(first_note.is_file())
        self._run_study("第二版原文")
        # 旧笔记、旧原文与旧 manifest 都还在，内容未被改写
        self.assertTrue(first_note.is_file())
        self.assertIn("第一版原文", first_source.read_text(encoding="utf-8"))
        self.assertTrue((first_dir / "manifest.json").is_file())
        # 新目录里是第二版
        new_dir = self.root / "study" / sorted(
            p.name for p in (self.root / "study").iterdir())[1]
        self.assertIn("第二版原文",
                      next((new_dir / "sources").glob("dup_原文版_*.md")).read_text(encoding="utf-8"))

    def test_index_has_one_row_per_run(self):
        self._run_study("第一版原文")
        self._run_study("第二版原文")
        lines = (self.root / "research-index.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        sha = sorted(json.loads(x)["archive_sha256"] for x in lines)
        self.assertNotEqual(sha[0], sha[1], "两次原文不同，sha256 应不同")
        self.assertTrue(all(sha), "两次都应登记了原文 sha256")


class BodyExcerptSearchTest(unittest.TestCase):
    """`note list` 要能命中笔记正文里的结论，不只是元数据。

    回退验证：把 `search_index` 的 `body_excerpt` 匹配与现场补算去掉，
    test_list_matches_note_body 会挂（正文里的关键词搜不到）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_list_matches_note_body(self):
        note = self.root / "explore" / "topic" / "00_判断稿.md"
        rn.main([
            "explore", "某个开放问题", "--slug", "topic", "--dir", str(self.root),
        ])
        # 模拟 Agent 把结论写进正文（登记时的摘录还是骨架提示）
        with note.open("a", encoding="utf-8") as f:
            f.write("\n已验证：这个方向的瓶颈在检索召回，不在生成质量。\n")
        # 不重新登记，写完即可搜到
        self.assertEqual(len(rn.search_index("召回", root=self.root)), 1)
        self.assertEqual(len(rn.search_index("检索召回", root=self.root)), 1)
        # 元数据命中的老行为仍在
        self.assertEqual(len(rn.search_index("topic", root=self.root)), 1)

    def test_search_ignores_missing_note_file(self):
        rn.main([
            "explore", "会被删掉的问题", "--slug", "gone", "--dir", str(self.root),
        ])
        (self.root / "explore" / "gone" / "00_判断稿.md").unlink()
        # 文件没了不该抛异常，元数据仍能命中
        self.assertEqual(len(rn.search_index("gone", root=self.root)), 1)

    def test_body_excerpt_skips_headings_and_comments(self):
        p = self.root / "note.md"
        p.write_text("# 标题\n\n## 0. 结论速览\n\n<!-- 提示注释 -->\n\n真正的正文首行\n", encoding="utf-8")
        self.assertEqual(rn._body_excerpt(p), "真正的正文首行")

    def test_body_excerpt_skips_header_fields(self):
        note = self.root / "explore" / "ex" / "00_判断稿.md"
        rn.main([
            "explore", "问题正文", "--slug", "ex", "--dir", str(self.root),
        ])
        with note.open("a", encoding="utf-8") as f:
            f.write("\n[已验证] 结论：检索召回是瓶颈。\n")
        excerpt = rn._body_excerpt(note)
        self.assertIn("检索召回", excerpt)
        # 头部固定字段（研究对象等）不算正文
        self.assertNotIn("研究对象", excerpt)


class ExploreBundleEntryPointTest(unittest.TestCase):
    """CLI 与 research --auto-note 共用 create_explore_bundle，结构必须一致。"""

    def test_bundle_has_judgement_manifest_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = rn.create_explore_bundle(
                root=root, slug="auto", title="自动笔记", problem="某个问题",
                related="run:abc",
            )
            kind_dir = Path(bundle["bundle_dir"])
            self.assertTrue((kind_dir / "00_判断稿.md").is_file())
            self.assertTrue((kind_dir / "manifest.json").is_file())
            self.assertTrue((kind_dir / "auto-探索.md").is_symlink())
            manifest = json.loads((kind_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["judgement_file"], "00_判断稿.md")
            self.assertEqual(manifest["problem"], "某个问题")
            rows = (root / "research-index.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0])["related"], "run:abc")


if __name__ == "__main__":
    unittest.main()

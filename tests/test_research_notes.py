#!/usr/bin/env python3
"""research_notes 离线单测：验证方案二标准单文件 Markdown 研报、Frontmatter、索引与工作区锚定。"""

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
            rn.slugify("https://hy.tencent.com/research/hyra2?langVersion=zh"),
            "hyra2",
        )
        self.assertEqual(rn.slugify("https://example.com/a/"), "a")
        self.assertEqual(rn.slugify(""), "note")

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

        # YAML Frontmatter 验证
        self.assertTrue(text.startswith("---\n"))
        self.assertIn('title: "Hyra 2.0 研究"', text)
        self.assertIn('kind: "study"', text)
        self.assertIn('slug: "hyra2"', text)

        # 五段式契约健全
        for heading, _ in rn.SECTIONS:
            self.assertIn(heading, text)

        # 正文固定字段与附录内嵌验证
        self.assertIn("研究对象：https://hy.tencent.com/research/hyra2", text)
        self.assertIn("关联：ernie-evolve", text)
        self.assertIn("## 附录：参考原文", text)
        self.assertIn("三个月前，我们发布了 Hyra 1.0。", text)

        # 索引写入
        idx = self.root / "research-index.jsonl"
        self.assertTrue(idx.is_file())
        row = json.loads(idx.read_text(encoding="utf-8").strip())
        self.assertEqual(row["kind"], "study")
        self.assertEqual(row["slug"], "hyra2")
        self.assertEqual(row["related"], "ernie-evolve")

    def test_study_explicit_output_flag(self):
        custom_out = self.root / "custom_reports" / "my_deep_study.md"
        rc = rn.main([
            "study",
            "https://example.com/deep",
            "--title", "自定义路径研报",
            "-o", str(custom_out),
            "--root", str(self.root),
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(custom_out.is_file())
        self.assertIn("自定义路径研报", custom_out.read_text(encoding="utf-8"))

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
        self.assertEqual(len(rn.search_index("alp", root=self.root)), 1)

    def test_no_index_when_root_unwritable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "research-index.jsonl").mkdir()
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


class CrossEnvironmentRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._cwd = Path.cwd()
        self.addCleanup(lambda: os.chdir(self._cwd))
        self._env_snapshot = dict(os.environ)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env_snapshot)

    def _isolated_module(self, tmp_path: Path):
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
        ws = Path(self.tmp.name) / "repo"
        ws.mkdir()
        (ws / ".git").mkdir()
        os.chdir(ws)
        root = rn.resolve_root()
        self.assertEqual(root, ws.resolve() / "research")

    def test_markerless_dir_falls_back_to_user_docs(self):
        bare = Path(self.tmp.name) / "bare"
        bare.mkdir()
        os.chdir(bare)
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


class WorkspaceRootAndBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def test_git_marker_wins_over_nested_package_json(self):
        (self.ws / ".git").mkdir()
        frontend = self.ws / "frontend"
        frontend.mkdir()
        (frontend / "package.json").write_text("{}", encoding="utf-8")
        self.assertEqual(rn.find_workspace_root(frontend), self.ws)

    def test_fresh_workspace_notes_and_archive_share_one_base(self):
        (self.ws / ".git").mkdir()
        import unittest.mock as mock
        with mock.patch("pathlib.Path.cwd", return_value=self.ws):
            notes = rn.resolve_root()
            archive = ar.resolve_archive_root()
        self.assertEqual(notes, self.ws / "research")
        self.assertEqual(archive, self.ws / "research" / "archive")


class BodyExcerptSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_list_matches_note_body(self):
        note = self.root / "explore" / "topic" / "topic-探索.md"
        rn.main([
            "explore", "某个开放问题", "--slug", "topic", "--dir", str(self.root),
        ])
        with note.open("a", encoding="utf-8") as f:
            f.write("\n已验证：这个方向的瓶颈在检索召回，不在生成质量。\n")
        self.assertEqual(len(rn.search_index("召回", root=self.root)), 1)
        self.assertEqual(len(rn.search_index("检索召回", root=self.root)), 1)
        self.assertEqual(len(rn.search_index("topic", root=self.root)), 1)

    def test_body_excerpt_skips_headings_and_comments(self):
        p = self.root / "note.md"
        p.write_text("# 标题\n\n## 0. 结论速览\n\n<!-- 提示注释 -->\n\n真正的正文首行\n", encoding="utf-8")
        self.assertEqual(rn._body_excerpt(p), "真正的正文首行")

    def test_body_excerpt_skips_horizontal_rule_after_frontmatter(self):
        """frontmatter 结束后的水平线 "---" 不是正文（2026-11-15 研报实跑
        发现：骨架与填实两种形态 excerpt 恒为 "---"，索引检索拿到的是横线）。"""
        p = self.root / "note_fm.md"
        p.write_text(
            "---\ntitle: x\n---\n\n# 标题\n\n---\n\n生态分四层且本轮全部命中。\n",
            encoding="utf-8")
        self.assertEqual(rn._body_excerpt(p), "生态分四层且本轮全部命中。")


class ExploreBundleEntryPointTest(unittest.TestCase):
    def test_bundle_entrypoint_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = rn.create_explore_bundle(
                root=root, slug="auto", title="自动笔记", problem="某个问题",
                related="run:abc",
            )
            self.assertIn("note", bundle)
            note_path = Path(bundle["note"])
            self.assertTrue(note_path.is_file())
            content = note_path.read_text(encoding="utf-8")
            self.assertIn('title: "自动笔记"', content)
            self.assertIn("## 0. 结论速览", content)
            rows = (root / "research-index.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0])["related"], "run:abc")


if __name__ == "__main__":
    unittest.main()

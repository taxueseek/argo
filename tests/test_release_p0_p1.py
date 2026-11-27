"""tests/test_release_p0_p1.py — 发版流程 P0/P1 增强单元测试（覆盖范围/红线/单源派生/JSON审计）。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import release  # noqa: E402
import release_audit  # noqa: E402


def test_leakage_scanner_blocks_paths_and_internal_details():
    bad_note = (
        "# Argo v2.19.0\n\n"
        "- 修复 /Users/alice/dev/argo 下的路径问题\n"
        "- 新增 scripts/seek_graph.py 与 `_expand_identifiers`\n"
        "- 优化 bm25(content, 5.0, 3.0, 1.0) 查询\n"
    )
    hits = release_audit.scan_text_leakage(bad_note, "docs/RELEASE_NOTES_v2.19.0.md")
    assert len(hits) == 4
    assert any("绝对路径" in h for h in hits)
    assert any("内部脚本路径" in h for h in hits)
    assert any("内部私有符号" in h for h in hits)
    assert any("底层实现参数" in h for h in hits)


def test_leakage_scanner_allows_clean_user_facing_notes():
    clean_note = (
        "<!-- 注释里的 scripts/foo.py 不应误报 -->\n"
        "# Argo v2.19.0\n\n"
        "## Highlights (English / TL;DR)\n\n"
        "- **What's New**: Added `--yaml` compact output (-40% size) and `--block` one-hop reads (-47.5% latency).\n"
    )
    hits = release_audit.scan_text_leakage(clean_note, "docs/RELEASE_NOTES_v2.19.0.md")
    assert hits == []


def test_leakage_gate_skips_legacy_v2_18_0():
    root = Path(__file__).resolve().parent.parent
    assert release_audit.scan_release_leakage(root, "2.18.0", release.README_LANGS) == []


def test_sync_from_release_notes_idempotent():
    with tempfile.TemporaryDirectory() as d:
        r = Path(d)
        (r / "docs").mkdir()
        (r / "docs/RELEASE_NOTES_v2.19.0.md").write_text(
            "# Argo v2.19.0\n\n"
            "## Highlights (English / TL;DR)\n\n"
            "- **What's New**: Compact YAML output and faster local code search.\n\n"
            "---\n\n"
            "# Argo v2.19.0 发布说明（中文）\n\n"
            "**版本**：2.19.0\n"
            "**定位**：本地搜索精度提速 + Agent 紧凑输出\n\n"
            "## 这次更新有什么（通俗版）\n\n"
            "- **本地搜索更快更准**：跨词符号检索 0.55 ms 命中\n"
            "- **紧凑 YAML**：输出体积减少 40%+\n",
            encoding="utf-8",
        )
        (r / "docs/CHANGELOG.md").write_text(
            "# Argo 变更日志\n\n## v2.18.0\n\n**定位**：旧版\n",
            encoding="utf-8",
        )
        (r / "README.md").write_text(
            "### v2.18.0: Old\n\n| **v2.18.0** | Old |\n",
            encoding="utf-8",
        )
        (r / "README.zh.md").write_text(
            "### v2.18.0：旧版\n\n| **v2.18.0** | 旧版 |\n",
            encoding="utf-8",
        )
        langs = ("README.md", "README.zh.md")
        acts1 = release_audit.sync_from_release_notes(r, "2.19.0", langs, dry=False)
        assert any("派生插入 ## v2.19.0" in a for a in acts1)
        assert "## v2.19.0" in (r / "docs/CHANGELOG.md").read_text(encoding="utf-8")
        assert "### v2.19.0:" in (r / "README.md").read_text(encoding="utf-8")
        assert "| **v2.19.0** |" in (r / "README.zh.md").read_text(encoding="utf-8")

        # 二次执行幂等跳过
        acts2 = release_audit.sync_from_release_notes(r, "2.19.0", langs, dry=False)
        assert all("跳过" in a for a in acts2)


def test_check_json_report_on_real_repo(capsys):
    rc = release.main(["check", "--json"])
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["version"] == release.current_version(release.ROOT)
    assert "coverage" in report
    assert report["coverage"]["commits_since_tag"] >= 1
    assert "gates" in report
    assert rc in (0, 1)

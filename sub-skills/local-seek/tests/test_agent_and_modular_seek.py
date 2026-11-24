#!/usr/bin/env python3
"""test_agent_and_modular_seek.py — 方案二（local-seek 模块化、--agent 契约、互斥 Fail-Fast 与 handoff_hint）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import seek  # noqa: E402
import seek_git  # noqa: E402
import seek_inspect  # noqa: E402


def test_modular_split_exports_and_line_reduction():
    assert hasattr(seek_inspect, "outline_file")
    assert hasattr(seek_inspect, "read_symbol_body")
    assert hasattr(seek_inspect, "read_lines")
    assert hasattr(seek_git, "git_log")
    assert hasattr(seek_git, "git_blame")
    # seek.py 保持同名 re-export 兼容既有测试
    assert seek.outline_file is seek_inspect.outline_file
    assert seek.read_symbol_body is seek_inspect.read_symbol_body


def test_agent_flag_compact_json_and_max_cap(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("\n".join(f"def fn_{i}(): return 'needle_{i}'" for i in range(30)), encoding="utf-8")
    out, rc = seek.run_query(["needle_", "--path", str(tmp_path), "--agent"])
    assert rc == 0
    # 紧凑单行 JSON（无缩进换行）
    assert "\n" not in out
    data = json.loads(out)
    assert data["count"] <= 20
    assert data["results"]


def test_inspect_supports_json_for_workbench(tmp_path):
    f = tmp_path / "demo.py"
    f.write_text("def alpha():\n    return 42\n\ndef beta():\n    return 99\n", encoding="utf-8")

    out_o, rc_o = seek.run_query(["--outline", str(f), "--json"])
    assert rc_o == 0
    do = json.loads(out_o)
    assert do["ok"] is True and do["count"] == 2
    assert do["entries"][0]["start"] == 1 and do["entries"][0]["end"] == 2

    out_s, rc_s = seek.run_query(["--symbol-body", "beta", str(f), "--agent"])
    assert rc_s == 0
    ds = json.loads(out_s)
    assert ds["ok"] is True and ds["symbol"] == "beta"
    assert "return 99" in ds["content"]


def test_mutually_exclusive_modes_fail_fast(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    for argv in (
        ["--outline", "--filename", str(f)],
        ["--symbol-body", "x", "--count", str(f)],
        ["--git-log", "--structural", str(f)],
    ):
        out, rc = seek.run_query(argv)
        assert rc == 2, f"{argv} 应返回退出码 2，实际 {rc}"
        assert "互斥" in out


def test_zero_hit_json_returns_structured_handoff_hint(tmp_path):
    (tmp_path / "a.py").write_text("hello = 1\n", encoding="utf-8")
    out, rc = seek.run_query(["definitely_nonexistent_token_xyz", "--path", str(tmp_path), "--agent"])
    assert rc == 1
    data = json.loads(out)
    assert data["count"] == 0 and data["results"] == []
    assert "handoff_hint" in data
    assert "argo search" in data["handoff_hint"]["web_fallback"]

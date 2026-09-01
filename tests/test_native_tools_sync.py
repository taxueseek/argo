#!/usr/bin/env python3
"""test_native_tools_sync.py — DSH 原生工具规格与 schema 真源的漂移门禁。

packages/dsh-plugin/dsh/native-tools.mjs 由 scripts/gen_native_tools.py 从
mcp_tools.py 生成。本测试保证两者一致：改 mcp_tools.py 而忘记重新生成时，
此处必须红。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

GEN = ROOT / "scripts" / "gen_native_tools.py"
OUT = ROOT / "packages" / "dsh-plugin" / "dsh" / "native-tools.mjs"


def _parse_mjs(text: str) -> dict:
    m = re.search(r"export const NATIVE_TOOLS = (.*);\s*$", text, re.S)
    assert m, "native-tools.mjs 缺少 NATIVE_TOOLS 导出"
    return json.loads(m.group(1))


def test_generated_file_matches_source():
    r = subprocess.run([sys.executable, str(GEN), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or "gen_native_tools --check 失败"


def test_covers_all_tools_except_research():
    from mcp_tools import TOOLS
    data = _parse_mjs(OUT.read_text(encoding="utf-8"))
    expected = {t["name"] for t in TOOLS} - {"argo_research"}
    assert set(data) == expected, (
        f"生成表与 mcp_tools.py 工具面不一致: "
        f"缺 {expected - set(data)}, 多 {set(data) - expected}")


def test_allowed_matches_properties():
    data = _parse_mjs(OUT.read_text(encoding="utf-8"))
    for name, spec in data.items():
        props = spec["parameters"].get("properties", {})
        assert spec["allowed"] == list(props.keys()), name
        assert spec["parameters"].get("additionalProperties") is False, name


def test_research_not_in_native_table():
    data = _parse_mjs(OUT.read_text(encoding="utf-8"))
    assert "argo_research" not in data, \
        "argo_research 是分钟级编排，不应出现在 CLI 单发规格表"

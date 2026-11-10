#!/usr/bin/env python3
"""test_native_tools_sync.py — DSH 侧生成物与 Python 真源的漂移检查。

packages/dsh-plugin/dsh/native-tools.mjs 由 scripts/gen_native_tools.py 生成，
含两张表：NATIVE_TOOLS（来自 mcp_tools.py）与 STATES（来自 engine_states.py）。
本测试保证生成物与两侧真源一致：改任一真源而忘记重新生成时，此处必须红。
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


def _parse_export(text: str, name: str) -> dict:
    """解析单个 `export const <name> = {...};`，不吞掉后续导出。

    此前 `_parse_mjs` 用 `(.*);\\s*$` 贪婪匹配到文件末尾，新增 STATES 导出后
    三个用例同时 JSONDecodeError——解析器必须先按导出名定位再取该段 JSON。
    """
    m = re.search(r"export const " + re.escape(name) + r" = (\{.*?\n\});",
                  text, re.S)
    assert m, f"native-tools.mjs 缺少 {name} 导出"
    return json.loads(m.group(1))


def _parse_mjs(text: str) -> dict:
    return _parse_export(text, "NATIVE_TOOLS")


def _parse_states(text: str) -> dict:
    return _parse_export(text, "STATES")


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


def test_states_match_python_vocabulary():
    """STATES 必须与 engine_states.py 逐个值对拍（跨侧状态词表漂移门禁）。

    此前 DSH 面板的 ready/loading/unavailable、wide-research 的
    conclusion_cap 三态、ZCode 桥的 daemon 四态全是字面量，改 Python 词表
    不会让任何东西变红。生成器把词表投影到 JS，本用例把它钉住。
    """
    import engine_states as st

    data = _parse_states(OUT.read_text(encoding="utf-8"))
    assert set(data) == {"plugin", "daemon", "conclusionCap"}
    assert data["plugin"] == {v: v for v in st.PLUGIN_STATES}
    assert data["daemon"] == {v: v for v in st.DAEMON_STATES}
    assert data["conclusionCap"] == {v: v for v in st.CONCLUSION_CAP_STATUSES}


def test_states_generator_check_covers_vocabulary():
    """--check 必须同时覆盖两张表：改 engine_states.py 不重新生成即红。"""
    r = subprocess.run([sys.executable, str(GEN), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or "gen_native_tools --check 失败"
    assert "状态词表" in r.stdout, "生成器输出未覆盖 STATES 对拍"

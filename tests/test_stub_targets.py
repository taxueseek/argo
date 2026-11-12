"""打桩目标存在性 lint 的门禁测试：真仓零违例 + 正向控制。

正向控制复刻历史 bug 形状（2026-10-22/23 三例打桩空转的可静态判定子类：
模块可导入但属性已搬走），并钉住两条豁免边界——创建式打桩与对象形态打桩
不算违例，防止 lint 误伤合法用法后被 --no-verify 绕过。
"""

from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lint_stub_targets import lint_file, lint_tests_dir, resolve_target  # noqa: E402

import textwrap  # noqa: E402


def test_real_tests_zero_stub_violations():
    """真仓测试必须零违例——重构搬属性撞上打桩旧名时，门禁在这里红。"""
    violations = lint_tests_dir()
    assert violations == [], "\n".join(violations)


def test_missing_attr_on_importable_module_is_violation(tmp_path):
    """正向控制：模块在、属性不在（_RETRY_FAST_S 搬家类）必须被点名。"""
    f = tmp_path / "test_synthetic_stub.py"
    f.write_text(
        textwrap.dedent(
            """
            def test_x(monkeypatch):
                monkeypatch.setattr("config.definitely_not_an_attr_xyz", 1, raising=True)
            """
        ).strip(),
        encoding="utf-8",
    )
    v = lint_file(f)
    assert len(v) == 1 and "definitely_not_an_attr_xyz" in v[0] and "属性缺失" in v[0]


def test_object_form_and_creating_stub_are_exempt(tmp_path):
    """对象形态与 raising=False / create=True 是合法形态，不得误报。"""
    f = tmp_path / "test_synthetic_stub2.py"
    f.write_text(
        textwrap.dedent(
            """
            import os

            def test_obj(monkeypatch):
                monkeypatch.setattr(os.path, "join", lambda *a: "")
                monkeypatch.setattr("config.brand_new_attr", 1, raising=False)

            def test_create(mocker):
                mocker.patch("config.brand_new_attr2", create=True)
            """
        ).strip(),
        encoding="utf-8",
    )
    assert lint_file(f) == []


def test_unimportable_module_is_skipped():
    """可选依赖缺失的模块不可判 → 跳过不报（边界如实钉住）。"""
    assert resolve_target("definitely_no_such_module_xyz.attr") is None


def test_resolve_existing_target_passes():
    """模块与属性都在 → None（正向通过例）。"""
    assert resolve_target("config.load_config") is None


def _parse_version(v_str: str) -> tuple[int, ...]:
    clean = v_str.lstrip("v").strip()
    return tuple(int(x) for x in clean.split(".") if x.isdigit())


def test_retired_stubs_have_sunset_version_and_not_expired():
    """生命周期门禁：已退役的代码存根必须显式声明日落版本与原因，且不得超期存留。

    为什么需要：退役模块如果无限期保留为存根，日后没人知道何时可以物理删除，
    导致死代码长期滞留。声明 __sunset_version__ 后，一旦版本推进到该版本，
    门禁直接红，倒逼开发者物理清理。
    """
    import tomllib
    root = SCRIPTS.parent
    pyproj = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    current_ver = _parse_version(pyproj["project"]["version"])

    import re
    sunset_decl = re.compile(r"^__sunset_version__\s*=", re.M)
    stubs_found = []
    for py in sorted(SCRIPTS.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        if "架构归一退役" in text or sunset_decl.search(text):
            stubs_found.append(py)
            assert "__sunset_version__" in text, f"{py.name} 声明退役却缺少 __sunset_version__"
            assert "__deprecation_reason__" in text, f"{py.name} 缺少 __deprecation_reason__"

            m = re.search(r'__sunset_version__\s*=\s*["\']([^"\']+)["\']', text)
            assert m, f"{py.name} __sunset_version__ 格式非法"
            sunset_ver = _parse_version(m.group(1))

            assert current_ver < sunset_ver, (
                f"{py.name} 已到日落版本 {m.group(1)}（当前版本 {pyproj['project']['version']}），"
                f"根据生命周期契约，该存根文件必须彻底物理删除！"
            )

    assert len(stubs_found) >= 1, "未找到任何退役存根，需确认存根标识"


def test_sunset_gate_has_teeth():
    """自检：如果当前版本超过或等于日落版本，门禁必须红。"""
    curr = _parse_version("2.16.0")
    sunset = _parse_version("v2.16.0")
    assert not (curr < sunset), "当前版本 >= 日落版本时判定未触发"
    curr_old = _parse_version("2.14.0")
    assert curr_old < sunset, "正常未到期版本误判"


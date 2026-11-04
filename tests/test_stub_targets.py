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

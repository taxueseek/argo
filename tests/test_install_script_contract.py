#!/usr/bin/env python3
"""test_install_script_contract.py — install.sh 安装脚本契约。

## 为什么需要

install.sh 是「装好就能用」的唯一入口，但它此前没有测试，三类真实缺陷都靠
偶然发现：

1. **重跑安装时 uv venv 报错退出**（2026-11-09 实测）：`uv venv` 在 .venv 已
   存在时 exit=2（提示加 --clear），而安装场景下 .venv 存在是常态（用户重跑）。
   旧实现 `uv venv ... || return 1` 直接整段失败，回落到 pip 路径重建 venv，
   解释器版本从 3.14 漂到系统 3.15——「版本一致性」被自己破坏。
2. **uv sync 的盲区**：它只对照 lock 与 venv 的「已装记录」，不对照磁盘真实
   状态。手工 `pip uninstall pyyaml` 后 `uv sync` 报「Checked 17 packages」
   但包仍缺（实测）。装完必须真 import 一次。
3. **bash 变量紧跟多字节字符**：`$var（` 在 bash 3.2 / dash 下被解析成
   `$var<0xEF>`，`set -u` 时 unbound variable。已有 static_lint_gate 覆盖，
   本文件不重复。

本文件锁第 1、2 类——它们不会让任何现有门禁变红，只在用户重跑安装时静默降级。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "scripts" / "install.sh"
INSTALL_PS1 = ROOT / "scripts" / "install.ps1"


def _bash_syntax_ok(path: Path) -> bool:
    r = subprocess.run(["bash", "-n", str(path)], capture_output=True)
    return r.returncode == 0


def test_install_sh_has_valid_bash_syntax():
    assert INSTALL_SH.is_file(), "install.sh 不见了"
    assert _bash_syntax_ok(INSTALL_SH), "install.sh 有 bash 语法错误（bash -n）"


def test_uv_venv_is_skipped_when_venv_exists():
    """重跑安装时不能因 uv venv 报错而整段失败。

    判据：`uv venv` 必须出现在 `if [[ ! -x "$venv_dir/bin/python" ]]` 判断**之后**，
    且两者之间不能有 `fi`（即必须在同一个 then 块内）。
    写成裸 `uv venv ... || return 1` 的形态即红——那正是 2026-11-09 的缺陷。
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    guard = text.find('if [[ ! -x "$venv_dir/bin/python" ]]; then')
    assert guard >= 0, (
        "缺「.venv/bin/python 不存在」的前置判断："
        "重跑安装时 uv venv 会因 venv 已存在而 exit=2，导致整段落回 pip 路径"
    )
    call = text.find('uv venv', guard)
    assert call >= 0, "判断之后没有 uv venv 调用"
    between = text[guard:call]
    assert "fi" not in between, (
        "uv venv 不在「venv 不存在」判断的 then 块内——重跑安装时仍会触发 uv venv 报错"
    )


def test_uv_sync_verifies_real_import():
    """uv sync 后必须真 import 一次，缺则 --reinstall。

    判据：脚本里存在 `import yaml` 的验证分支，且该分支带 `--reinstall`。
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "import yaml" in text, "uv sync 后没有真 import 验证（uv sync 的 Checked 不可信）"
    assert "--reinstall" in text, (
        "uv sync 后缺包时没有 --reinstall 兜底：venv 记录与磁盘不一致时静默缺包"
    )


def test_no_bare_variable_before_multibyte():
    """bash/dash 下 `$var` 紧跟多字节字符必须写成 `${var}`。

    这条与 static_lint_gate 的同名检查同口径，这里再锁一次 install.sh 专档——
    因为它是用户执行的第一个脚本，报错时用户还没有任何上下文可推断原因。
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    pat = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7F])")
    hits = [(i, m.group(0)) for i, line in enumerate(text.splitlines(), 1)
            for m in pat.finditer(line)]
    assert not hits, (
        "install.sh 里 $var 紧跟多字节字符未加花括号（bash 3.2/dash 会解析成 "
        f"另一个变量名）：{hits[:5]}"
    )


def test_install_sh_is_self_contained():
    """自包含：不 source 仓内其他 bash 片段（除它自己）。

    规范要求 scripts/ 自包含或清晰声明依赖。install.sh 是唯一入口，
    拆成 source 片段会让「curl | bash」路径断裂。
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    src = re.findall(r'^\s*(?:source|\.)\s+([^\s;]+)', text, re.M)
    assert not src, f"install.sh 不应 source 其他文件（会破坏 curl|bash 路径）：{src}"


def test_uninstall_does_not_touch_real_directories():
    """卸载只删符号链接，且必须先判定 -L。

    误删用户真实目录的代价不可逆，判据必须写在代码里。
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert '[[ -L "$_target"' in text, (
        "卸载时未先判定符号链接（-L）——有误删真实目录的风险"
    )
    # 不能调用不存在的接口（link_source.py 只有 --to/--check/--dry-run/--force）。
    # 只匹配「命令调用」形态，注释里提到 --undo 不算。
    assert not re.search(r"(?:run\s+)?python3[^\n]*link_source\.py[^\n]*--undo", text), (
        "link_source.py 没有 --undo；卸载应自己删符号链接"
    )


def test_dry_run_does_not_execute():
    """--dry-run 必须真的不执行任何改系统的动作。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "DRY_RUN" in text
    # run() 函数必须存在且在 DRY_RUN 时 return
    assert re.search(r'if \[\[ "\$DRY_RUN" == "1" \]\]; then', text), (
        "缺 dry-run 分支：--dry-run 会真的执行安装"
    )


def test_install_ps1_has_uv_path_with_explicit_python():
    """install.ps1 与 install.sh 同口径：uv 路径显式传 --python。"""
    assert INSTALL_PS1.is_file(), "install.ps1 不见了"
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "--python" in text, "install.ps1 的 uv 调用未显式传 --python（会被 UV_PYTHON 覆盖）"
    assert ".python-version" in text, "install.ps1 未读 .python-version"


if __name__ == "__main__":
    sys.exit(subprocess.run([sys.executable, "-m", "pytest", __file__, "-q"]).returncode)

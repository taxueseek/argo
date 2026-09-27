#!/usr/bin/env python3
"""噪声档降权的回归测试（2026-09-27 新增，方案 C3）。

实测背景（Documents/GPT 搜 "import"，rg 61899 处命中）：
    vendor/生成物/归档  7% + 测试/fixture/benchmark  27% + tmp/  7% = 41%
搜代码时，四成命中是「不想看到的东西」。原来的 DEFAULT_EXCLUDES 挡掉了
node_modules/dist 之类，但漏了三类最大的：repos/（克隆仓）、tests|fixtures|
benchmark、tmp/。

**为什么是降权不是排除**：排除 = 这些内容永远搜不到。搜「某个克隆仓里怎么
写的」「我的测试怎么写的」是真实且常见的用法，排除会让工具在这些查询上
直接回答「未找到匹配」——那比返回噪声更糟。降权 + 无命中自动回落，两者兼得。

本文件锁定的性质：
  1. 默认搜不到噪声档内容（降权生效）；
  2. 噪声档独有内容**仍可达**（回落生效，不制造「明明有却搜不到」）；
  3. --include-noise 显式关闭降权；
  4. 回落不影响正常命中（有真源时不去噪声档白跑一趟）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SEEK = Path(__file__).resolve().parent.parent / "scripts" / "seek.py"


def _have(*tools) -> bool:
    import shutil
    return all(shutil.which(t) for t in tools)


def _run(args, cwd):
    return subprocess.run([sys.executable, str(SEEK)] + args, cwd=cwd,
                          capture_output=True, text=True, timeout=120)


def _make_tree(root: Path):
    """构造一棵含「真实源」与「噪声档」两类内容的最小目录树。

    标记词刻意选 `zzmarker_*`：pytest 的 tmp_path 目录名会带测试函数名
    （如 `test_noise_tier_is_deprioritiz0`），若标记词是 `pytest` 之类，
    路径本身就会命中，测试变成在验证「路径里有这个词」而非「噪声档降权」。
    这是第一版用例写错的地方。
    """
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("import os\nimport sys\n", encoding="utf-8")
    (root / "repos" / "thirdparty").mkdir(parents=True)
    (root / "repos" / "thirdparty" / "lib.py").write_text(
        "import zzzarker_clonelib\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text(
        "import zzzarker_fixture\n", encoding="utf-8")


def test_noise_tier_is_deprioritized_and_reachable(tmp_path):
    if not _have("rg"):
        import pytest
        pytest.skip("rg 未安装")
    _make_tree(tmp_path)
    proj = str(tmp_path)

    # 噪声档独有内容：搜得到（相对根判定，本例的 tests/ 在根之下），
    # 且**不触发**回落重搜——它本来就没被排除，只是被排到真源之后。
    r = _run(["zzzarker_fixture", "--path", proj], tmp_path)
    assert "test_x.py" in r.stdout, r.stdout
    assert "噪声档" not in r.stdout, "没被排除就不该多跑一趟回落"

    # 正常源：不受降权影响
    r2 = _run(["import", "--path", proj], tmp_path)
    assert "app.py" in r2.stdout, r2.stdout


def test_deprioritized_means_source_wins(tmp_path):
    """同一查询同时命中真源与噪声档时，真源必须排在前面。"""
    if not _have("rg"):
        import pytest
        pytest.skip("rg 未安装")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("import shared_token\n", encoding="utf-8")
    (tmp_path / "repos").mkdir()
    (tmp_path / "repos" / "copy.py").write_text("import shared_token\n", encoding="utf-8")
    proj = str(tmp_path)

    r = _run(["shared_token", "--path", proj, "--max", "1"], tmp_path)
    assert "real.py" in r.stdout, r.stdout
    assert "copy.py" not in r.stdout, r.stdout


def test_include_noise_flag_is_explicit(tmp_path):
    if not _have("rg"):
        import pytest
        pytest.skip("rg 未安装")
    _make_tree(tmp_path)
    proj = str(tmp_path)
    r = _run(["zzzarker_fixture", "--path", proj, "--include-noise"], tmp_path)
    assert "test_x.py" in r.stdout, r.stdout
    # 显式开启时不应再回落（那会多跑一趟）
    assert "噪声档" not in r.stdout, r.stdout

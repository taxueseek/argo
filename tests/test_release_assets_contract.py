#!/usr/bin/env python3
"""test_release_assets_contract.py — Release 资产清单的机械契约。

## 为什么要这道门

2026-11-10（v2.14.0 发版后）审视资产清单时发现：仓库额外打了一个
`argo-v<ver>.tar.gz`（运行时子集，不含 tests），而 GitHub 每个 Release 页面
**自动生成** Source code (tar.gz) 与 (zip)，「要源码」的需求已被平台覆盖。
同类项目（psf/requests、pandas、astral-sh/uv）的 Release 资产都是 wheel +
sdist 两件套，没有一个手动传运行时源码包。

删掉它之后需要一道门锁住，否则下版会有人再加回来——理由通常是「万一有人
要下载源码」，而那个需求 GitHub 已经覆盖了。

## 锁三件事

1. `build_release_assets.sh` 不再产出运行时源码包（`argo-v<ver>.tar.gz`）
2. 每个资产配一个独立 `.sha256`（照 astral-sh/uv 格式），不再产出合并的
   `SHA256SUMS.txt`——合并清单是单点，它损坏则所有校验失效
3. `MANIFEST.in` 排除 `tests/`（sdist 服务「从源码构建」，不服务「跑测试」）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_SH = ROOT / "scripts" / "build_release_assets.sh"
MANIFEST = ROOT / "MANIFEST.in"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_no_runtime_source_tarball():
    """不再产出 argo-v<ver>.tar.gz 运行时源码包。

    回退测试：把 `tar -czf "$TARBALL"` 那段加回来，这条会挂。
    """
    sh = _read(BUILD_SH)
    assert "argo-v$VERSION.tar.gz" not in sh, (
        "不该再产出 argo-v<ver>.tar.gz：GitHub 自动生成 Source code 已覆盖"
        "「要源码」需求，运行时子集包没有安装路径触达它"
    )
    # 也不该有 tar -czf 打源码包的动作（wheel/sdist 由 python -m build 产出）
    assert 'tar -czf "$TARBALL"' not in sh, (
        "不该用 tar 打 Release 资产包；wheel 与 sdist 由 `python -m build` 产出"
    )


def test_per_asset_sha256_not_combined_manifest():
    """每个资产配独立 .sha256，不再产出合并的 SHA256SUMS.txt。

    回退测试：把 `sha256sum * > SHA256SUMS.txt` 加回来，这条会挂。
    2026-11-10 首次写的断言是 `'> SHA256SUMS.txt' not in sh`，回退时把目标
    写成 `"$DIST_DIR/SHA256SUMS.txt"` 就绕过了——断言必须匹配真实写法。
    """
    sh = _read(BUILD_SH)
    # 必须显式删掉旧格式（它可能残留在上一次构建的 dist/ 里）
    assert 'rm -f "$DIST_DIR"/SHA256SUMS.txt' in sh, (
        "该显式 rm -f 掉旧格式的合并清单，否则上一次构建的残留会被 CI 传上去"
    )
    # 任何把 SHA256SUMS.txt 当输出目标的写法都算违规（含带路径前缀的变体）
    assert not re.search(r'>\s*"?\$DIST_DIR/?SHA256SUMS\.txt', sh), (
        "不该产出合并清单 SHA256SUMS.txt：它是单点，损坏则所有校验失效；"
        "逐个资产的 .sha256 可以边下边校（照 astral-sh/uv 格式）"
    )
    assert '.sha256' in sh, "该为每个资产生成独立 .sha256"
    # 格式：`<sha256>  <filename>`（两空格，sha256sum 原生输出）
    assert re.search(r'sha256sum\s+"\$f"\s*>\s*"\$f\.sha256"', sh), (
        "该用 `sha256sum \"$f\" > \"$f.sha256\"` 的逐资产格式"
    )


def test_sdist_excludes_tests():
    """sdist 排除 tests/：它服务「从源码构建」，不服务「跑测试」。

    回退测试：从 MANIFEST.in 删掉 recursive-exclude tests，这条会挂。
    """
    mf = _read(MANIFEST)
    assert "recursive-exclude tests" in mf, (
        "MANIFEST.in 该排除 tests/：测试代码解包后约 4 MB，占 sdist 三分之一；"
        "想跑测试的人用 git clone（能拿到完整仓库与 CI 配置）"
    )


def test_sdist_includes_docs_and_agents():
    """sdist 要带 docs/ 与 AGENTS.md——它们是 agent 装完包要读的。

    与 package.json 白名单同口径：SKILL.md 引用 COMMIT_STANDARD.md，
    装包后若不在就是死链。
    """
    mf = _read(MANIFEST)
    for name in ("AGENTS.md", "docs/"):
        assert name in mf, f"MANIFEST.in 该包含 {name}"

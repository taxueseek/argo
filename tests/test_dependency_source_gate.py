#!/usr/bin/env python3
"""test_dependency_source_gate.py — 依赖真源对账护栏。

## 为什么需要这道门

2026-11-09 之前，依赖声明散在两处且互不相通：

- `pyproject.toml` 的 `[project.optional-dependencies].fast` 只写了运行期两件
  （curl_cffi / rapidfuzz），开发档与「装全档」根本没有声明，只在
  `requirements-dev.txt` / `requirements-optional.txt` 里；
- CI 读 requirements，`uv lock` 式的可复现解析不存在，`requirements-*.txt`
  里除了 pytest 家族全是浮动版本（`curl_cffi` 不写 `>=` 也不写 `==`）。

后果是「开发、安装与版本一致性」没有单一真源：同一件依赖在 pyproject 与
requirements 里可以各自漂移，而没有任何机制会红。

## 收口后的口径

- **真源**：`pyproject.toml`（运行期 dependencies、optional-dependencies fast/full、
  dependency-groups dev）+ `uv.lock`（钉死解析结果）+ `.python-version`（钉死
  解释器版本）。
- **派生清单**：`requirements-dev.txt` / `requirements-optional.txt` 保留为
  「pip 用户的可读清单 + 文档引用 + CI extras 的缓存 key 输入」，内容必须与
  真源一致。
- 本文件锁三件事：
  1. `uv.lock` 存在且其 `requires-python` 与 pyproject 一致；
  2. 两份 requirements 清单里的每个依赖名都能在 pyproject 的对应段落里找到
     （防「清单里有、真源里没有」——装了却不在锁里）；
  3. pyproject 的 fast/full/dev 三段里的依赖名都在 uv.lock 里出现
     （防「真源改了、lock 没重算」——CI 用 `--frozen`，lock 过时会在安装期
     直接失败，本地却可能照旧跑绿）。

## 为什么判据是「名字对账」而不是「全文一致」

requirements 清单里写了版本号（`pytest==9.1.1`）而 pyproject 里也写了
（`pytest==9.1.1; python_version >= '3.10'`），逐字节比对会被 marker 与注释
噪声淹没。名字对账回答的是真正会出问题的那一问：「这个依赖在不在真源里」。
版本漂移由 uv.lock 与 `uv lock --check` 负责（见 test_uv_lock_is_fresh）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # 3.9/3.10 档：tomli 由 uv.lock 解析（marker: python_full_version < 3.11）
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"
PYTHON_VERSION = ROOT / ".python-version"

# 清单文件 → 它对应的 pyproject 段落（解析后取依赖名集合）
MANIFEST_TO_SECTION = {
    "requirements-dev.txt": "dev",
    "requirements-optional.txt": "full",
}

# 依赖名 = 行首第一个 token；版本约束（>=、==）与环境标记（; python_version...）
# 都算「后续内容」，不参与名字提取。
_SPEC_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _section_names(section: str) -> set[str]:
    """取 pyproject 里某段的依赖名集合。

    `dev` 在 [dependency-groups] 下，`fast`/`full` 在 [project.optional-dependencies]
    下——两者层级不同，但都是「名字列表」，合并取并集后按段名区分。
    """
    data = _pyproject()
    if section == "dev":
        entries = (data.get("dependency-groups") or {}).get("dev") or []
    else:
        entries = (data.get("project", {}).get("optional-dependencies") or {}).get(section) or []
    names = set()
    for e in entries:
        if not isinstance(e, str):
            continue
        m = _SPEC_RE.match(e)
        if m:
            names.add(_canon(m.group(1)))
    return names


def _canon(name: str) -> str:
    """PEP 503 归一化：下划线与连字符等价，比较时统一成连字符小写。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _manifest_names(path: Path) -> set[str]:
    """读清单里的依赖名（跳过注释与空行）。"""
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _SPEC_RE.match(s)
        if m:
            names.add(_canon(m.group(1)))
    return names


def test_uv_lock_exists_and_matches_requires_python():
    assert UV_LOCK.is_file(), (
        "缺 uv.lock：依赖真源声明在 pyproject.toml，但解析结果没有落盘，"
        "CI 的 --frozen 会直接失败。生成：uv lock"
    )
    lock = UV_LOCK.read_text(encoding="utf-8")
    m = re.search(r'^requires-python\s*=\s*"([^"]+)"', lock, re.M)
    assert m, "uv.lock 里读不到 requires-python"
    py = _pyproject()
    want = py["project"]["requires-python"]
    assert m.group(1) == want, (
        f"uv.lock 的 requires-python（{m.group(1)}）与 pyproject 的 {want} 不一致："
        "lock 过期或 pyproject 改了下限没重算。跑 uv lock"
    )


def test_manifest_deps_exist_in_pyproject():
    """清单里的每个依赖必须在 pyproject 对应段落里——否则「装了却不在锁里」。"""
    problems = []
    for rel, section in MANIFEST_TO_SECTION.items():
        path = ROOT / rel
        assert path.is_file(), f"清单不见了：{rel}"
        want = _section_names(section)
        got = _manifest_names(path)
        missing = sorted(got - want)
        if missing:
            problems.append(
                f"{rel} 里的 {missing} 不在 pyproject 的 [{section}] 段："
                "清单是派生件，改依赖请改 pyproject.toml 再重新生成清单"
            )
    assert not problems, "\n".join(problems)


def test_pyproject_deps_are_locked():
    """pyproject 三段里的依赖都必须出现在 uv.lock——否则 lock 过期。

    名字用 uv.lock 里的包名做归一化匹配；uv.lock 同时含多版本时（3.9 档与
    3.10+ 档各一份）只要求「至少出现过一次」。
    """
    lock = UV_LOCK.read_text(encoding="utf-8")
    locked = {_canon(m) for m in re.findall(r'^name\s*=\s*"([^"]+)"', lock, re.M)}
    problems = []
    for section in ("fast", "full", "dev"):
        for name in sorted(_section_names(section)):
            if _canon(name) not in locked:
                problems.append(
                    f"[{section}] 的 {name} 不在 uv.lock 里：lock 未重算。跑 uv lock"
                )
    assert not problems, "\n".join(problems)


def test_python_version_file_pins_one_version():
    """.python-version 是解释器版本的真源：必须只写一个版本，且与 CI 一致。

    为什么必须单一：uv 允许写多版本列表，但那样「钉版」就退化成了「候选」，
    与 .venv 重建时的选择结果相关——同一份代码在不同机器上可能落到不同版本。
    """
    assert PYTHON_VERSION.is_file(), (
        "缺 .python-version：解释器版本没有真源，uv venv/uv sync 会按当前 "
        "PATH 决定版本"
    )
    content = PYTHON_VERSION.read_text(encoding="utf-8").strip()
    assert content and "\n" not in content, (
        f".python-version 应只钉一个版本，当前内容：{content!r}"
    )
    # CI 与 pre-commit 都读它，所以这里只做「存在且单一」的契约；
    # 具体版本号的对账在 test_ci_workflows 里（避免本文件依赖 workflows 内容）。
    assert re.match(r"^\d+\.\d+$", content), (
        f".python-version 内容应是 '主.次'（如 3.14），当前：{content!r}"
    )


@pytest.mark.parametrize("rel", sorted(MANIFEST_TO_SECTION))
def test_manifests_are_marked_derived(rel):
    """两份清单必须在头部声明自己是派生件——否则下一个人会去改清单。

    这是文档契约，不是机械对账：清单漂移时 CI 会红，但红的原因要能从清单
    自己的头部读出来，而不是靠翻测试代码。
    """
    head = (ROOT / rel).read_text(encoding="utf-8")[:600]
    assert "派生清单" in head and "pyproject.toml" in head, (
        f"{rel} 头部未声明「派生清单」与真源位置"
    )

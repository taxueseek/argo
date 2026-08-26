#!/usr/bin/env python3
"""seek_locator.py — local-seek 子技能的「安装感知」定位（单一真源，不写死主机路径）。

本地搜索（argo_local_search / --include-local）需要 local-seek/scripts/seek.py。
local-seek 随 argo 打包在 sub-skills/ 下（安装感知：寻址锚点是本文件的真实安装位置），
也可能作为独立 skill 装在别处。此模块把发现逻辑收敛到一处，供 search.py 与
mcp_handlers.py 共用，消除两处各自硬编码 __file__ 相对路径与 ~/.agents/skills 等
主机路径的重复（Fowler: Duplicated Code 反面却是各自为政、易漂移）。

对齐 SKILL.md 纪律「禁止在产品代码写死主机 skill 路径」：不再出现
~/.agents/skills / ~/.claude/skills 这类字面量，改由环境变量承载自定义/遗留位置。

查找顺序（命中即返回，全部未命中则返回打包位置，由调用方据此 fail 提示）：
  1. ARGO_LOCAL_SEEK_PATH      显式指定 seek.py 的绝对/相对路径（任意自定义位置）
  2. <ARGO_ROOT>/sub-skills/local-seek/scripts/seek.py
                               —— 打包子技能，锚点为本模块的安装位置（symlink 会解析回真源）
  3. ARGO_LOCAL_SEEK_ROOTS     os.pathsep 分隔的候选根目录，逐个检查
                               <根>/local-seek/scripts/seek.py（承载遗留/独立装法）
"""

from __future__ import annotations

import os
from typing import Iterator

ARGO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUNDLED = os.path.join(ARGO_ROOT, "sub-skills", "local-seek", "scripts", "seek.py")


def _candidates() -> Iterator[str]:
    """按优先级产出候选 seek.py 路径（仅路径，未校验存在性）。

    优先级：显式文件覆盖 > 显式候选根 > 打包位置（默认）。
    显式配置（ARGO_LOCAL_SEEK_PATH / ARGO_LOCAL_SEEK_ROOTS）代表用户点名了
    装在哪，理应先于总是存在的打包子技能；打包位置只作兜底。
    """
    override = (os.environ.get("ARGO_LOCAL_SEEK_PATH") or "").strip()
    if override:
        yield os.path.realpath(os.path.expanduser(override))
    roots = (os.environ.get("ARGO_LOCAL_SEEK_ROOTS") or "").strip()
    if roots:
        for root in roots.split(os.pathsep):
            root = root.strip()
            if not root:
                continue
            base = os.path.realpath(os.path.expanduser(root))
            candidate = os.path.join(base, "local-seek", "scripts", "seek.py")
            if os.path.isfile(candidate):
                yield candidate
    yield _BUNDLED


def resolve_seek_py() -> str:
    """返回 local-seek/scripts/seek.py 的绝对路径（命中即返回，未命中返回打包位置）。"""
    for cand in _candidates():
        if cand and os.path.isfile(cand):
            return cand
    return _BUNDLED


def seek_py_exists() -> bool:
    """本地搜索能力是否可用（seek.py 实际存在）。"""
    return os.path.isfile(resolve_seek_py())


if __name__ == "__main__":  # pragma: no cover
    import sys
    print(resolve_seek_py())

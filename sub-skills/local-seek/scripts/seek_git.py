#!/usr/bin/env python3
"""seek_git.py — local-seek 历史层（文件提交历史与单行 blame）。

从 seek.py 拆出：独立封装 git 只读查询，runner 支持注入以兼容既有单测 monkeypatch。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable


def _default_run(cmd: list[str], timeout: float | None = 30) -> Any:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout or 30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_log(
    path: str | Path,
    n: int = 10,
    timeout: float | None = None,
    *,
    runner: Callable[..., Any] | None = None,
) -> tuple[str, int]:
    """输出文件的最近提交历史（git log --oneline）。"""
    run_fn = runner or _default_run
    p = Path(path).expanduser()
    if not p.is_file():
        return f"local-seek: {path} 不是文件", 1
    proc = run_fn(["git", "-C", str(p.parent), "log", "--oneline", f"-{n}", "--", p.name],
                  timeout=timeout)
    if proc is None:
        return "local-seek: git 执行失败", 1
    if proc.returncode == 128 or (proc.returncode == 0 and not proc.stdout.strip()):
        inside = run_fn(["git", "-C", str(p.parent), "rev-parse", "--is-inside-work-tree"])
        if inside is None or inside.returncode != 0:
            return f"local-seek: {p.name} 不在 git 仓库中", 1
        return f"local-seek: {p.name} 无提交历史", 0
    lines = proc.stdout.splitlines()
    return "\n".join([f"local-seek: {p.name} 最近 {len(lines)} 条提交"] + lines), 0


def git_blame(
    path: str | Path,
    line: str | int,
    timeout: float | None = None,
    *,
    runner: Callable[..., Any] | None = None,
) -> tuple[str, int]:
    """输出文件第 N 行的 blame 信息（谁在哪个提交改的）。"""
    run_fn = runner or _default_run
    p = Path(path).expanduser()
    if not p.is_file():
        return f"local-seek: {path} 不是文件", 1
    proc = run_fn(["git", "-C", str(p.parent), "blame", "-L", f"{line},{line}", "--", p.name],
                  timeout=timeout)
    if proc is None or proc.returncode != 0:
        return f"local-seek: git blame 失败（{p.name} 可能在 git 仓库外或行号无效）", 1
    return f"local-seek: {p.name} 第 {line} 行\n{proc.stdout.strip()}", 0

#!/usr/bin/env python3
"""hot_state.py — argo 热生效基建（代码指纹自重启 + 文件热读监视器）。

设计（2026-08-30）：变更的三类生效边界各自处理——
  代码/配置   → 指纹自检 + os.execv 自重启（stdio fd 跨 exec 继承，客户端连接
                不断；磁盘状态天然存活）。Python 进程内热替换不可靠（单例/线程/
                模块缓存），进程是唯一可靠的重载边界。
  密钥        → engine_env 直读 ~/.config/argo/env（本模块 HotFile 提供 mtime 缓存）
  数据配置    → config.py 已有 mtime 热读；quota.py / engines.py registry 补齐

护栏：ARGO_NO_AUTORELOAD=1 关闭自重启；指纹防循环；检查节流 1s。
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent.parent

# 代码 + 声明式配置的指纹范围（相对 SKILL_DIR）
_CODE_GLOBS = (
    "scripts/**/*.py",
    "scripts/mcp_launch.sh",
    "config.yaml",
    "backends/*.json",
    "backends/*.yaml",
    "engines/specs/*.yaml",
    "sub-skills/local-search/parse_maps.yaml",
)


def fingerprint(paths: list[Path]) -> str:
    """路径集合的指纹：(相对路径, mtime_ns, size) 排序后 sha1 前 16 位。"""
    items = []
    for p in sorted(set(paths)):
        try:
            st = p.stat()
        except OSError:
            continue
        items.append(f"{p.name}|{st.st_mtime_ns}|{st.st_size}")
    return hashlib.sha1("\n".join(items).encode()).hexdigest()[:16]


def code_paths() -> list[Path]:
    out: list[Path] = []
    for g in _CODE_GLOBS:
        out.extend(p for p in _SKILL_DIR.glob(g) if p.is_file())
    return out


_lock = threading.Lock()
_last_fingerprint: str | None = None
_last_check: float = 0.0


def should_reload(min_interval: float = 1.0) -> bool:
    """距上次检查超过 min_interval 才真正 stat 一轮；指纹变化返回 True。"""
    global _last_fingerprint, _last_check
    if os.environ.get("ARGO_NO_AUTORELOAD", "").strip() in ("1", "true", "yes"):
        return False
    now = time.monotonic()
    if now - _last_check < min_interval:
        return False
    _last_check = now
    fp = fingerprint(code_paths())
    with _lock:
        if _last_fingerprint is None:
            _last_fingerprint = fp  # 首次基线，不触发
            return False
        if fp != _last_fingerprint:
            _last_fingerprint = fp
            return True
    return False


def self_exec() -> bool:
    """原地换映像重启：stdio fd 0/1/2 跨 exec 继承，客户端连接不断。

    磁盘状态（quota.json / 熔断 / sqlite 缓存）天然存活。Windows 的
    execv 语义不可靠且入口有 -X utf8 重启路径，跳过。
    返回是否真正执行（调用方 execv 成功不会返回）。
    """
    if os.name == "nt":
        return False
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else __file__
    try:
        os.execv(sys.executable, [sys.executable, "-u", script, *sys.argv[1:]])
    except Exception:
        return False
    return False  # pragma: no cover — execv 成功不会走到这里


class HotFile:
    """单文件 mtime+size 监视器：changed() 首次调用建立基线返回 False，
    之后签名变化返回 True（供调用方重读）。线程安全。"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._sig: tuple | None = None

    def _sig_now(self) -> tuple:
        try:
            st = self._path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return ()

    def changed(self) -> bool:
        with self._lock:
            sig = self._sig_now()
            first = self._sig is None
            changed = (not first) and sig != self._sig
            self._sig = sig
            return changed

    def reset(self) -> None:
        with self._lock:
            self._sig = None

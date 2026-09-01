#!/usr/bin/env python3
"""argo_paths.py — argo 本地状态目录的单一真源。

背景：此前 11 个模块各自拼 ~/.cache/unified-search，构造方式分裂成 4 种
（Path.home()/".cache"/...、expanduser("~/.cache/...")、字面量字符串、
config 默认值），导致 config.yaml 的 cache.db_path 管不住 quota.json、
health.db 等文件，测试也难以整体隔离。

现在所有状态路径统一由本模块派生：
  - 根目录可被 ARGO_STATE_DIR 覆盖（测试隔离 / 只读环境 / XDG 迁移）
  - 未设置时回落到 config.yaml 的 cache.db_path 所在目录，保持向后兼容
  - 各模块只声明「文件名」，不再各自拼目录

注意：User-Agent 里的 unified-search@local 是邮箱标识，与状态目录无关，
不在此处管理。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

# 环境变量覆盖：优先级最高，用于测试隔离与只读环境
ENV_STATE_DIR = "ARGO_STATE_DIR"

# 历史默认目录（也是 config.yaml 中 db_path 的默认前缀）
_LEGACY_ROOT = "~/.cache/unified-search"

# 缓存配置段未就绪时的兜底（config 不可用、PyYAML 缺失等场景）
_FALLBACK_ROOT = _LEGACY_ROOT


def _config_db_path() -> str | None:
    """从 config.yaml 读 cache.db_path；不可用时返回 None。

    config 模块本身可能不可用（PyYAML 缺失 / 配置文件损坏），
    此处必须 fail-open，否则路径派生会连带崩溃。
    """
    try:
        from config import get_cache_config
        cfg = get_cache_config()
        db_path = cfg.get("db_path")
        return str(db_path) if db_path else None
    except Exception:
        return None


def state_root() -> Path:
    """返回 argo 本地状态根目录（已 expanduser，不保证存在）。

    优先级：
      1. ARGO_STATE_DIR 环境变量
      2. config.yaml cache.db_path 的父目录（保证与主缓存同域）
      3. 历史默认 ~/.cache/unified-search
    """
    override = os.environ.get(ENV_STATE_DIR, "").strip()
    if override:
        return Path(os.path.expanduser(override))

    db_path = _config_db_path()
    if db_path:
        expanded = os.path.expanduser(db_path)
        parent = os.path.dirname(expanded)
        if parent:
            return Path(parent)
    return Path(os.path.expanduser(_FALLBACK_ROOT))


def state_path(*parts: str) -> Path:
    """返回状态根目录下的路径（不自动创建目录）。

    state_path("health.db")        → <root>/health.db
    state_path("admission", "x.json") → <root>/admission/x.json
    """
    return state_root().joinpath(*parts)


def ensure_state_dir(*parts: str) -> Path:
    """返回状态根目录下的子目录，并确保其存在。

    目录不可创建时（只读挂载 / 权限受限）fail-open 返回目标路径——
    由调用方在写入时处理，避免 import 期就崩。
    """
    d = state_path(*parts)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def legacy_root() -> Path:
    """历史默认目录（仅用于迁移/兼容判断，新代码请用 state_root）。"""
    return Path(os.path.expanduser(_LEGACY_ROOT))


def db_path() -> Path:
    """主缓存库路径。

    与 state_path("cache.db") 的差别只在「config.yaml 里用户显式改写了
    db_path」这一种情况：此时尊重用户的显式配置，不放回状态根目录。

    ARGO_STATE_DIR 一旦设置，一律优先——它是测试隔离与只读环境的硬开关，
    不能被磁盘上的 config.yaml 盖掉（否则 env 形同虚设）。
    """
    if os.environ.get(ENV_STATE_DIR, "").strip():
        return state_path("cache.db")
    raw = _config_db_path()
    if raw:
        return Path(os.path.expanduser(raw))
    return state_path("cache.db")

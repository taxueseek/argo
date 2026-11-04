#!/usr/bin/env python3
"""engine_registry.py — Argo 统一引擎注册中心

合并主路径引擎 + local-search 子引擎，使 route.py 可以直接路由到
local_bing、local_baidu 等子引擎，消除 local_search 黑盒。

向后兼容：local_search 仍作为 fallback alias 保留。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# 本地状态目录唯一来源（env ARGO_STATE_DIR → config cache.db_path 父目录 → 旧路径）
import argo_paths as _paths

# 这里曾有 logger = logging.getLogger(...) + setLevel/addHandler 四行，但全模块
# 一次都没调用过它——本模块只做声明合并与健康状态读写，没有需要记的运行时事件。
# 代价却不是零：`import logging` 会连带 traceback → dataclasses → inspect →
# _colorize（CPython 3.13+ 实测 12.5 ms），而 route 在模块级导入本模块，
# 于是每一次进程启动（含纯缓存命中的那一档）都要付这笔钱去买一个没人用的 logger。
# 真要记日志时再按需 import，别让路由路径为它买单。

ARGO_DIR = Path(__file__).resolve().parent.parent
LOCAL_SEARCH_DIR = ARGO_DIR / "sub-skills" / "local-search"
LOCAL_SEARCH_CONFIG = LOCAL_SEARCH_DIR / "config.yaml"


def __getattr__(name: str):
    """`HEALTH_STATE_PATH` 每次访问按当前 ARGO_STATE_DIR 解析（PEP 562）。

    与 circuit_breaker 同一口径：导入期常量会与改写后的 `ARGO_STATE_DIR` 脱节，
    而读写必须落同一个文件。保留此名以维持「可读、可 patch」的既有契约。
    """
    if name == "HEALTH_STATE_PATH":
        return _paths.state_path("argo_engine_health.json")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ── YAML mtime 缓存（route 热路径会高频调用 list_local_engines） ──────────────
_yaml_cache: dict[str, Any] | None = None
_yaml_mtime: float = -1.0
_yaml_path_key: str = ""


def _load_yaml(path: Path) -> dict:
    """按 mtime 缓存 YAML；同一文件重复解析是 route 最大 CPU 开销。"""
    global _yaml_cache, _yaml_mtime, _yaml_path_key
    path_key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if (
        _yaml_cache is not None
        and _yaml_path_key == path_key
        and mtime == _yaml_mtime
    ):
        return _yaml_cache
    try:
        from yaml_load import load as _yaml_parse
        data = _yaml_parse(path) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    _yaml_cache = data
    _yaml_mtime = mtime
    _yaml_path_key = path_key
    return data


class EngineRegistry:
    """统一引擎注册表：主引擎 + local-search 子引擎。"""

    def __init__(self):
        self._health: dict = {}
        # 读模块属性（可被 monkeypatch），每次构造按当前 ARGO_STATE_DIR 解析。
        # 用 `globals().get`：裸名查找不触发 PEP 562 `__getattr__`。
        self._health_path = globals().get("HEALTH_STATE_PATH") or _paths.state_path(
            "argo_engine_health.json")
        self._local_names_cache: list[str] | None = None
        self._local_names_mtime: float = -1.0
        self._load_health()

    def _health_state_path(self) -> Path:
        """健康状态路径：实例属性 → 模块属性（可 patch）→ 按当前环境解析。

        收口成一处，是为了让绕过 `__init__` 的构造（测试用 `__new__` 直接注入
        `_health`）也能拿到正确路径——否则 `_save_health` 会因缺 `_health_path`
        抛错并被 `except` 吞掉，表现为「静默不写盘」。
        """
        return (getattr(self, "_health_path", None)
                or globals().get("HEALTH_STATE_PATH")
                or _paths.state_path("argo_engine_health.json"))

    def _load_health(self):
        path = self._health_state_path()
        if path.exists():
            try:
                self._health = json.loads(path.read_bytes())
            except Exception:
                self._health = {}

    def _save_health(self):
        try:
            # 原子写收编（此前 write_text 直写）：截断即写帧内崩溃会让整个
            # 健康门状态丢失——"全体失忆"。同族事故已在
            # sub-skills/local-search/engine_registry 与 local_seek 修过两例，
            # 这是漏网的第三例；mkstemp 唯一 tmp + os.replace，失败自清理。
            _paths.atomic_write_json(self._health_state_path(), self._health, indent=None)
        except Exception:
            pass

    def get_local_search_engines(self) -> dict[str, dict]:
        """获取 local-search 子引擎配置（带 local_ 前缀）。"""
        cfg = _load_yaml(LOCAL_SEARCH_CONFIG)
        engines = cfg.get("engines", {})
        result = {}
        for name, spec in engines.items():
            if not isinstance(spec, dict):
                continue
            merged = dict(spec)
            merged["name"] = name
            merged["_source"] = "local-search"
            health = self._health.get(name, {})
            merged["available"] = self._is_available(spec, health)
            merged["consecutive_failures"] = health.get("consecutive_failures", 0)
            result[name] = merged
        return result

    def _is_available(self, spec: dict, health: dict) -> bool:
        if not spec.get("enabled", True):
            return False
        if not health:
            return True
        return bool(health.get("available", True))

    def list_local_engines(self, available_only: bool = False) -> list[str]:
        """列出 local-search 子引擎名（mtime 缓存全量列表）。"""
        # 全量列表可缓存；available_only 需结合 health 过滤
        if not available_only:
            try:
                mtime = LOCAL_SEARCH_CONFIG.stat().st_mtime
            except OSError:
                mtime = -1.0
            if (
                self._local_names_cache is not None
                and mtime == self._local_names_mtime
            ):
                return list(self._local_names_cache)

        engines = self.get_local_search_engines()
        names = []
        for name, spec in engines.items():
            if available_only and not spec.get("available", True):
                continue
            names.append(name)

        if not available_only:
            try:
                self._local_names_mtime = LOCAL_SEARCH_CONFIG.stat().st_mtime
            except OSError:
                self._local_names_mtime = -1.0
            self._local_names_cache = list(names)
        return names

    def update_health(self, name: str, available: bool, **extra):
        """更新引擎健康状态并持久化。"""
        record = self._health.setdefault(name, {})
        record["last_checked"] = time.time()
        record["available"] = available
        if available:
            record["consecutive_failures"] = 0
            record["last_ok"] = time.time()
        else:
            record["consecutive_failures"] = record.get("consecutive_failures", 0) + 1
        for k, v in extra.items():
            record[k] = v
        self._save_health()

    def is_available(self, name: str) -> bool:
        health = self._health.get(name, {})
        return bool(health.get("available", True))


# 单例
_registry: EngineRegistry | None = None


def get_registry() -> EngineRegistry:
    global _registry
    if _registry is None:
        _registry = EngineRegistry()
    return _registry

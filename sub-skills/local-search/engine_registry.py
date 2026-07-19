#!/usr/bin/env python3
"""engine_registry.py — local-search 引擎注册中心（唯一真源）

读取 sub-skills/local-search/config.yaml 与 parse_maps.yaml，维护 24+ 本地引擎的
元数据、分类与可用状态。新增引擎只需改 YAML，无需改代码。
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore

logger = logging.getLogger("local_search.engine_registry")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler())

SKILL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SKILL_DIR / "config.yaml"
PARSE_MAPS_PATH = SKILL_DIR / "parse_maps.yaml"

# 领域分类（与 config.yaml 中 engines[*].category 对应）
DEFAULT_CATEGORIES = [
    "web_general",
    "chinese",
    "academic",
    "news",
    "code",
    "reference",
    "vertical",
]

# 反爬/拦截标记（用于 health_check 快速判定）
ANTI_BOT_MARKERS = [
    "captcha", "recaptcha", "robot", "robots", "cloudflare", "challenge",
    "blocked", "verification", "please verify", "access denied",
    "too many requests", "rate limit",
]


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise ImportError("缺少 PyYAML，请安装：pip install pyyaml")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"加载 YAML 失败 {path}: {e}")
        return {}


def _default_health_state_path() -> Path:
    base = Path(os.path.expanduser("~/.cache/unified-search"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "local_search_health.json"


class EngineRegistry:
    """本地搜索引擎注册表。"""

    def __init__(
        self,
        config_path: Path | str | None = None,
        parse_maps_path: Path | str | None = None,
        health_state_path: Path | str | None = None,
    ):
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self.parse_maps_path = Path(parse_maps_path) if parse_maps_path else PARSE_MAPS_PATH
        self.health_state_path = Path(health_state_path) if health_state_path else _default_health_state_path()
        self._config_mtime: float = 0.0
        self._parse_mtime: float = 0.0
        self._config: dict[str, Any] = {}
        self._parse_maps: dict[str, Any] = {}
        self._health: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """加载配置、解析映射和健康状态缓存。"""
        cfg = _load_yaml(self.config_path)
        maps = _load_yaml(self.parse_maps_path)
        self._config = cfg
        self._parse_maps = maps
        try:
            self._config_mtime = self.config_path.stat().st_mtime
            self._parse_mtime = self.parse_maps_path.stat().st_mtime
        except OSError:
            pass
        self._health = self._load_health()

    def _load_health(self) -> dict[str, Any]:
        if not self.health_state_path.exists():
            return {}
        try:
            with self.health_state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"加载健康状态失败: {e}")
        return {}

    def _save_health(self) -> None:
        try:
            self.health_state_path.parent.mkdir(parents=True, exist_ok=True)
            with self.health_state_path.open("w", encoding="utf-8") as f:
                json.dump(self._health, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存健康状态失败: {e}")

    def reload_if_changed(self) -> bool:
        """如果 YAML 文件发生变更则热重载，返回是否重载。"""
        try:
            cfg_mtime = self.config_path.stat().st_mtime
            parse_mtime = self.parse_maps_path.stat().st_mtime
        except OSError:
            return False
        if cfg_mtime != self._config_mtime or parse_mtime != self._parse_mtime:
            self._load()
            return True
        return False

    def force_reload(self) -> None:
        """强制重载所有配置。"""
        self._load()

    # ── 配置访问 ───────────────────────────────────────────────────────────────

    @property
    def settings(self) -> dict[str, Any]:
        return self._config.get("settings", {})

    @property
    def engines(self) -> dict[str, dict[str, Any]]:
        return self._config.get("engines", {})

    @property
    def parse_maps(self) -> dict[str, Any]:
        return self._parse_maps

    def get_engine(self, name: str) -> dict[str, Any] | None:
        """获取指定引擎的完整配置（含运行时可用状态）。"""
        spec = self.engines.get(name)
        if spec is None:
            return None
        merged = dict(spec)
        merged["name"] = name
        health = self._health.get(name, {})
        merged["available"] = self._is_available(name, spec, health)
        merged["last_checked"] = health.get("last_checked")
        merged["consecutive_failures"] = health.get("consecutive_failures", 0)
        return merged

    def _is_available(self, name: str, spec: dict[str, Any], health: dict[str, Any]) -> bool:
        """综合 enabled 与健康状态判定可用性。"""
        if not spec.get("enabled", True):
            return False
        # 没有健康记录时默认可用（由 health_check 负责探针）
        if not health:
            return True
        return bool(health.get("available", True))

    def list_engines(
        self,
        category: str | None = None,
        available_only: bool = False,
        enabled_only: bool = False,
    ) -> list[str]:
        """列出引擎名，支持按分类、可用性、启用状态过滤。"""
        names: list[str] = []
        for name, spec in self.engines.items():
            if enabled_only and not spec.get("enabled", True):
                continue
            cats = spec.get("categories", [])
            if isinstance(cats, str):
                cats = [cats]
            if category and category not in cats:
                continue
            if available_only:
                eng = self.get_engine(name)
                if not eng or not eng.get("available", True):
                    continue
            names.append(name)
        return names

    def list_categories(self) -> list[str]:
        """返回实际出现的分类（保留顺序）。"""
        seen: set[str] = set()
        cats: list[str] = []
        for spec in self.engines.values():
            for c in spec.get("categories", []):
                if isinstance(c, str) and c not in seen:
                    seen.add(c)
                    cats.append(c)
        # 确保默认分类存在
        for c in DEFAULT_CATEGORIES:
            if c not in seen:
                cats.append(c)
        return cats

    # ── 可用性管理 ───────────────────────────────────────────────────────────────

    def get_health(self, name: str) -> dict[str, Any]:
        return dict(self._health.get(name, {}))

    def update_availability(self, name: str, available: bool, **extra: Any) -> None:
        """更新单个引擎的可用状态，并持久化。"""
        now = time.time()
        record = self._health.setdefault(name, {})
        record["last_checked"] = now
        record["available"] = available
        if available:
            record["consecutive_failures"] = 0
            record["last_ok"] = now
            record["fail_reason"] = None
        else:
            record["consecutive_failures"] = record.get("consecutive_failures", 0) + 1
            if extra.get("fail_reason"):
                record["fail_reason"] = extra["fail_reason"]
        for k, v in extra.items():
            if k not in ("last_checked", "available", "consecutive_failures"):
                record[k] = v
        self._save_health()

    def bulk_update_availability(self, updates: dict[str, dict[str, Any]]) -> None:
        """批量更新可用状态。"""
        for name, data in updates.items():
            self.update_availability(name, data.get("available", False), **data)

    def is_available(self, name: str) -> bool:
        eng = self.get_engine(name)
        if eng is not None:
            return bool(eng.get("available", True))
        # 引擎不在配置中，但健康记录存在时直接取记录状态
        health = self._health.get(name, {})
        return bool(health.get("available", True))


@functools.lru_cache(maxsize=1)
def get_registry() -> EngineRegistry:
    return EngineRegistry()


def get_engine(name: str) -> dict[str, Any] | None:
    return get_registry().get_engine(name)


def list_engines(
    category: str | None = None,
    available_only: bool = False,
    enabled_only: bool = False,
) -> list[str]:
    return get_registry().list_engines(category, available_only, enabled_only)


def update_availability(name: str, available: bool, **extra: Any) -> None:
    get_registry().update_availability(name, available, **extra)


def list_categories() -> list[str]:
    return get_registry().list_categories()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="local-search 引擎注册表调试")
    parser.add_argument("--list", action="store_true", help="列出所有引擎")
    parser.add_argument("--category", default=None, help="按分类过滤")
    parser.add_argument("--available", action="store_true", help="仅可用引擎")
    parser.add_argument("--engine", default=None, help="查看单个引擎")
    parser.add_argument("--reload", action="store_true", help="强制重载")
    args = parser.parse_args()

    reg = get_registry()
    if args.reload:
        reg.force_reload()

    if args.engine:
        print(json.dumps(reg.get_engine(args.engine), ensure_ascii=False, indent=2))
    elif args.list or args.category or args.available:
        names = reg.list_engines(category=args.category, available_only=args.available)
        print(json.dumps(names, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            "categories": reg.list_categories(),
            "engines": {n: reg.get_engine(n) for n in reg.list_engines()},
        }, ensure_ascii=False, indent=2, default=str))

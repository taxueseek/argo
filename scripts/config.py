#!/usr/bin/env python3
"""
config.py — Unified Search v2 配置加载器

职责：
  - 从项目根目录的 config.yaml 加载统一配置
  - 支持热加载（按 mtime 缓存）
  - 使用 PyYAML 解析（缺失时给出明确安装提示）
  - 将 ~ 展开为实际用户目录
  - 提供类型化访问接口（引擎列表、域规则、成本分级、预算配置）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ── 路径 ──────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
# 外置引擎声明目录：engines/*.yaml（不含 plugins/、templates/、_ 前缀）
ENGINES_DIR = Path(__file__).parent.parent / "engines"


# ── 默认配置 ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 2,
    "engines": {
        "anysearch": {
            "enabled": True, "type": "cli",
            "cmd": ["python3", "~/.agents/skills/anysearch-skill/scripts/anysearch_cli.py"],
            "search_args": ["search", "{query}", "--max_results", "{n}"],
            "env": {},
        },
    },
    "domains": [
        {
            "name": "general_search", "desc": "通用搜索（兜底）",
            "patterns": [], "primary": "anysearch",
            "fallback": "anysearch", "parallel": True,
        },
    ],
    "cache": {"enabled": True, "db_path": "~/.cache/unified-search/cache.db", "ttl": 3600, "max_size_mb": 200},
    "execution": {"default_timeout": 8, "parallel_timeout": 6, "max_parallel_engines": 3, "retry_count": 0},
    "budget": {
        "fast": {"max_cost_per_query": 0.0, "allow_paid": False},
        "auto": {"max_cost_per_query": 0.01, "allow_paid": True},
        "deep": {"max_cost_per_query": 1.0, "allow_paid": True},
        "budget": {"max_cost_per_query": 0.005, "allow_paid": False, "quota_threshold": 0.2},
    },
    "output": {"format": "auto", "include_scores": True, "include_routing_decision": True},
}


# ── YAML 加载 ─────────────────────────────────────────────────────────────────

def _require_yaml():
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError as e:
        raise ImportError("缺少 PyYAML，请安装：pip install pyyaml") from e


def _load_yaml(text: str) -> dict[str, Any]:
    yaml = _require_yaml()
    parsed = yaml.safe_load(text)
    return parsed if isinstance(parsed, dict) else {}


# ── 配置加载与缓存 ─────────────────────────────────────────────────────────────

_config_cache: dict[str, Any] | None = None
_config_mtime: float = 0.0
_config_load_error: str | None = None


def _expand_value(value: Any) -> Any:
    """递归展开字符串中的 ~ 为用户目录。"""
    if isinstance(value, str):
        return os.path.expanduser(value)
    if isinstance(value, list):
        return [_expand_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_value(v) for k, v in value.items()}
    return value


def _resolve_relative_paths(config: dict[str, Any]) -> dict[str, Any]:
    """将 cli 引擎 cmd/domain_search 中的相对路径解析为 config.yaml 所在目录的绝对路径。"""
    base = CONFIG_PATH.parent
    for name, spec in config.get("engines", {}).items():
        if not isinstance(spec, dict) or spec.get("type") != "cli":
            continue
        for key in ("cmd", "domain_search"):
            if key not in spec:
                continue
            items = spec[key]
            if not isinstance(items, list):
                continue
            resolved: list[Any] = []
            for item in items:
                if isinstance(item, str) and item and not item.startswith(("/", "~", "http://", "https://")) and not item.startswith("{"):
                    candidate = base / item
                    if candidate.exists() or ("/" in item or "\\" in item):
                        resolved.append(str(candidate.resolve()))
                    else:
                        resolved.append(item)
                else:
                    resolved.append(item)
            spec[key] = resolved
    return config


def _validate_engine_paths(config: dict[str, Any]) -> dict[str, Any]:
    """验证引擎 CLI 路径，不存在则标记为 disabled。"""
    import logging as _logging
    _log = _logging.getLogger("unified_search.config")
    for name, spec in config.get("engines", {}).items():
        if not isinstance(spec, dict) or spec.get("type") != "cli":
            continue
        cmd = spec.get("cmd", [])
        if not cmd or cmd[0] in ("npx", "node"):
            continue
        cmd_path_str = cmd[-1]
        if cmd_path_str.startswith("--"):
            continue
        cmd_path = Path(cmd_path_str).expanduser()
        if not cmd_path.exists():
            spec["enabled"] = False
    return config


def _load_external_engine_specs() -> dict[str, dict[str, Any]]:
    """加载 engines/*.yaml 与 engines/specs/*.yaml 外置声明。

    文件名（去后缀）默认为 engine_id；文件内 engine_id / name 可覆盖。
    跳过：_ 前缀文件。
    """
    result: dict[str, dict[str, Any]] = {}
    if not ENGINES_DIR.is_dir():
        return result
    try:
        yaml = _require_yaml()
    except ImportError:
        return result

    def _load_dir(directory: Path) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.yaml")):
            if path.name.startswith("_"):
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            # 允许 {engine_id, ...} 或直接是 engine spec
            engine_id = data.get("engine_id") or data.get("name") or path.stem
            if not isinstance(engine_id, str) or not engine_id:
                continue
            spec = dict(data)
            spec.pop("engine_id", None)
            # name 字段若是展示名且与 id 不同，保留；type 必须有
            if "type" not in spec and "url" in spec:
                spec["type"] = "http"
            if "enabled" not in spec:
                spec["enabled"] = True
            spec["_external_spec"] = str(path)
            result[engine_id] = spec

    _load_dir(ENGINES_DIR)
    _load_dir(ENGINES_DIR / "specs")
    return result


def _merge_external_engines(config: dict[str, Any]) -> dict[str, Any]:
    """外置 YAML 覆盖/新增引擎；cost_tiers 可选追加。"""
    external = _load_external_engine_specs()
    if not external:
        return config
    engines = config.setdefault("engines", {})
    if not isinstance(engines, dict):
        engines = {}
        config["engines"] = engines
    for eid, spec in external.items():
        # 外置优先覆盖同名（便于本地试验）
        base = dict(engines.get(eid) or {})
        base.update(spec)
        engines[eid] = base
        # 可选：写入 cost_tier 列表
        tier = spec.get("cost_tier")
        if tier and isinstance(tier, str):
            tiers = config.setdefault("cost_tiers", {})
            if isinstance(tiers, dict):
                lst = list(tiers.get(tier) or [])
                if eid not in lst:
                    lst.append(eid)
                    tiers[tier] = lst
    return config


def _external_engines_mtime() -> float:
    latest = 0.0
    if not ENGINES_DIR.is_dir():
        return latest
    for path in ENGINES_DIR.rglob("*.yaml"):
        if "plugins" in path.parts:
            continue
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            pass
    return latest


def load_config(force: bool = False) -> dict[str, Any]:
    """加载配置，支持热加载；合并 engines/*.yaml 外置声明。"""
    global _config_cache, _config_mtime, _config_load_error
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        if _config_cache is None:
            base = _validate_engine_paths(_expand_value(json.loads(json.dumps(DEFAULT_CONFIG))))
            _config_cache = _merge_external_engines(base)
        return _config_cache

    ext_mtime = _external_engines_mtime()
    combined_mtime = max(mtime, ext_mtime)

    if not force and _config_cache is not None and combined_mtime == _config_mtime:
        return _config_cache

    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        parsed = _load_yaml(text)
        if parsed:
            expanded = _expand_value(parsed)
            resolved = _resolve_relative_paths(expanded)
            resolved = _merge_external_engines(resolved)
            _config_cache = _validate_engine_paths(resolved)
            _config_mtime = combined_mtime
            _config_load_error = None
        elif _config_cache is None:
            base = _validate_engine_paths(_resolve_relative_paths(_expand_value(json.loads(json.dumps(DEFAULT_CONFIG)))))
            _config_cache = _merge_external_engines(base)
    except ImportError as e:
        _config_load_error = str(e)
        import sys as _sys
        print(f"[unified-search] PyYAML 未安装，使用内置默认配置。安装：pip install pyyaml。原因：{e}", file=_sys.stderr)
        if _config_cache is None:
            base = _validate_engine_paths(_resolve_relative_paths(_expand_value(json.loads(json.dumps(DEFAULT_CONFIG)))))
            _config_cache = _merge_external_engines(base)
        return _config_cache
    except Exception as e:
        _config_load_error = str(e)
        if _config_cache is None:
            base = _validate_engine_paths(_resolve_relative_paths(_expand_value(json.loads(json.dumps(DEFAULT_CONFIG)))))
            _config_cache = _merge_external_engines(base)
    return _config_cache


def last_load_error() -> str | None:
    return _config_load_error


# ── 类型化访问接口 ─────────────────────────────────────────────────────────────

def get_engines(config: dict[str, Any] | None = None,
                *,
                routable_only: bool = False) -> dict[str, dict[str, Any]]:
    """返回启用的引擎配置字典。

    routable_only=True 时额外过滤：
      - ARGO_ENABLE/DISABLE_ENGINES
      - 缺 API Key
      - admission blocked
    """
    cfg = config if config is not None else load_config()
    engines = cfg.get("engines", {})
    result = {name: spec for name, spec in engines.items() if spec.get("enabled", True)}
    if not routable_only:
        return result
    try:
        from engine_env import is_engine_allowed_by_env, env_ready
        from engine_admission import is_blocked
    except ImportError:
        return result
    filtered: dict[str, dict[str, Any]] = {}
    for name, spec in result.items():
        if not is_engine_allowed_by_env(name):
            continue
        if not env_ready(name, spec):
            continue
        if is_blocked(name):
            continue
        filtered[name] = spec
    return filtered


def get_domains(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """返回按优先级排序的域规则列表。"""
    cfg = config if config is not None else load_config()
    return cfg.get("domains", [])


def get_cache_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else load_config()
    return cfg.get("cache", DEFAULT_CONFIG["cache"])


def get_execution_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else load_config()
    return cfg.get("execution", DEFAULT_CONFIG["execution"])


def get_output_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else load_config()
    return cfg.get("output", DEFAULT_CONFIG["output"])


def get_cost_tiers(config: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """返回成本分级：从引擎声明的 cost_tier 字段聚合（唯一真源 config.yaml engines 段）。

    自 v2.6 起 cost_tiers 不再是独立配置段，新增引擎只需在 engines 段声明
    cost_tier 字段，此处自动归入对应分级。
    """
    cfg = config if config is not None else load_config()
    tiers: dict[str, list[str]] = {}
    for name, spec in cfg.get("engines", {}).items():
        if not isinstance(spec, dict):
            continue
        tier = spec.get("cost_tier", "free")
        tiers.setdefault(tier, []).append(name)
    return tiers


def get_budget_config(mode: str = "auto") -> dict[str, Any]:
    """返回预算模式配置。"""
    cfg = load_config()
    budgets = cfg.get("execution", {}).get("budget", DEFAULT_CONFIG["budget"])
    return budgets.get(mode, budgets.get("auto", {}))


def get_cost_factor(engine: str) -> float:
    """获取引擎的 cost_factor：free=1.0, low=0.7, paid=0.3。

    由 get_cost_tiers 聚合结果驱动；未声明 cost_tier 的引擎按 free 兜底。
    """
    tiers = get_cost_tiers()
    if engine in tiers.get("free", []):
        return 1.0
    if engine in tiers.get("low", []):
        return 0.7
    if engine in tiers.get("paid", []):
        return 0.3
    return 1.0  # 未分级默认为 free


# ── CLI 调试用 ─────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="Unified Search v2 配置查看器")
    parser.add_argument("--engines", action="store_true", help="显示引擎配置")
    parser.add_argument("--domains", action="store_true", help="显示域规则")
    parser.add_argument("--cost-tiers", action="store_true", help="显示成本分级")
    parser.add_argument("--check", action="store_true", help="检查配置")
    args = parser.parse_args()
    cfg = load_config(force=True)
    if args.engines:
        print(json.dumps(get_engines(cfg), ensure_ascii=False, indent=2))
    elif args.domains:
        print(json.dumps(get_domains(cfg), ensure_ascii=False, indent=2))
    elif args.cost_tiers:
        print(json.dumps(get_cost_tiers(cfg), ensure_ascii=False, indent=2))
    elif args.check:
        err = last_load_error()
        print(json.dumps({
            "path": str(CONFIG_PATH), "ok": err is None, "error": err,
            "engines": list(get_engines(cfg).keys()),
            "domains": [d.get("name") for d in get_domains(cfg)],
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()

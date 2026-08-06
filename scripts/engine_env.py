#!/usr/bin/env python3
"""engine_env.py — 引擎密钥与启用策略（环境变量优先）

规范：
  - 敏感信息只走环境变量，禁止硬编码
  - 新名优先：ARGO_<ENGINE>_API_KEY / ARGO_<NAME>
  - 兼容旧名：TAVILY_API_KEY、EXA_API_KEY、QWEATHER_KEY 等
  - 启用控制：ARGO_ENABLE_ENGINES / ARGO_DISABLE_ENGINES（逗号分隔）

对外：
  resolve_env_name / get_env / expand_placeholders
  required_env_for / missing_env_for / env_ready
  parse_engine_list_env / is_engine_allowed_by_env
"""

from __future__ import annotations

import os
import re
import json
from typing import Any

# 引擎 → 候选环境变量（从左到右优先）
# 第一项为推荐新名（ARGO_ 前缀），后续为历史兼容名
KNOWN_ENV_ALIASES: dict[str, list[str]] = {
    "tavily": ["ARGO_TAVILY_API_KEY", "TAVILY_API_KEY"],
    "bocha": ["ARGO_BOCHA_API_KEY", "BOCHA_API_KEY"],
    "bocha_ai": ["ARGO_BOCHA_API_KEY", "BOCHA_API_KEY"],
    "brave": ["ARGO_BRAVE_API_KEY", "BRAVE_API_KEY"],
    "byted": ["ARGO_BYTED_API_KEY", "ARGO_WEB_SEARCH_API_KEY", "WEB_SEARCH_API_KEY"],
    "exa": ["ARGO_EXA_API_KEY", "EXA_API_KEY"],
    "octen": ["ARGO_OCTEN_API_KEY", "OCTEN_API_KEY"],
    "felo": ["ARGO_FELO_API_KEY", "FELO_API_KEY"],
    "metaso": ["ARGO_METASO_API_KEY", "METASO_API_KEY"],
    "qweather": ["ARGO_QWEATHER_KEY", "QWEATHER_KEY"],
    "github": ["ARGO_GITHUB_TOKEN", "GITHUB_TOKEN"],
    "wolframalpha": ["ARGO_WOLFRAM_APPID", "WOLFRAM_APPID"],
    "zhihu": ["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"],
    "zhihu_global": ["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"],
    "zhihu_hot": ["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"],
    "anysearch": ["ARGO_ANYSEARCH_API_KEY", "ANYSEARCH_API_KEY"],  # 可选
    "weread": ["ARGO_WEREAD_API_KEY", "WEREAD_API_KEY"],  # 微信读书 Agent Gateway
    "em_miaoxiang": ["ARGO_EASTMONEY_APIKEY", "EASTMONEY_APIKEY"],  # 东财妙想（可选）
}

# 占位符名 → 候选 env（用于 config 中 {TAVILY_API_KEY} 展开）
PLACEHOLDER_ALIASES: dict[str, list[str]] = {
    "TAVILY_API_KEY": ["ARGO_TAVILY_API_KEY", "TAVILY_API_KEY"],
    "BOCHA_API_KEY": ["ARGO_BOCHA_API_KEY", "BOCHA_API_KEY"],
    "BRAVE_API_KEY": ["ARGO_BRAVE_API_KEY", "BRAVE_API_KEY"],
    "WEB_SEARCH_API_KEY": ["ARGO_BYTED_API_KEY", "ARGO_WEB_SEARCH_API_KEY", "WEB_SEARCH_API_KEY"],
    "EXA_API_KEY": ["ARGO_EXA_API_KEY", "EXA_API_KEY"],
    "OCTEN_API_KEY": ["ARGO_OCTEN_API_KEY", "OCTEN_API_KEY"],
    "FELO_API_KEY": ["ARGO_FELO_API_KEY", "FELO_API_KEY"],
    "METASO_API_KEY": ["ARGO_METASO_API_KEY", "METASO_API_KEY"],
    "QWEATHER_KEY": ["ARGO_QWEATHER_KEY", "QWEATHER_KEY"],
    "GITHUB_TOKEN": ["ARGO_GITHUB_TOKEN", "GITHUB_TOKEN"],
    "WOLFRAM_APPID": ["ARGO_WOLFRAM_APPID", "WOLFRAM_APPID"],
    "ZHIHU_ACCESS_SECRET": ["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"],
    "ANYSEARCH_API_KEY": ["ARGO_ANYSEARCH_API_KEY", "ANYSEARCH_API_KEY"],
    "WEREAD_API_KEY": ["ARGO_WEREAD_API_KEY", "WEREAD_API_KEY"],
}

_PLACEHOLDER_RE = re.compile(r"\{([A-Z_][A-Z0-9_]*)\}")
# 动态生成的非密钥占位符，不算 required_env
_NON_SECRET_PLACEHOLDERS = {"QUERY", "N", "TIMESTAMP", "MODE", "DEPTH"}

# 可选密钥：有则更好，缺失不阻断自动路由
OPTIONAL_ENV_ENGINES: set[str] = {
    "github",       # GITHUB_TOKEN 仅提高限频
    "anysearch",    # ANYSEARCH_API_KEY 可选
}


def get_env(names: str | list[str], default: str = "") -> str:
    """按优先级读取第一个非空环境变量。"""
    if isinstance(names, str):
        names = [names]
    for name in names:
        if not name:
            continue
        val = os.environ.get(name)
        if val is not None and str(val).strip() != "":
            return str(val)
    return default


def resolve_env_name(engine_id: str, logical: str = "api_key") -> list[str]:
    """返回某引擎逻辑密钥的候选环境变量名列表。"""
    aliases = KNOWN_ENV_ALIASES.get(engine_id)
    if aliases:
        return list(aliases)
    upper = engine_id.upper().replace("-", "_")
    if logical == "api_key":
        return [f"ARGO_{upper}_API_KEY", f"{upper}_API_KEY"]
    upper_logic = logical.upper()
    return [f"ARGO_{upper}_{upper_logic}", f"{upper}_{upper_logic}"]


def expand_placeholders(value: Any) -> Any:
    """递归展开字符串中的 {ENV_NAME}，支持 ARGO_ 别名。"""
    if isinstance(value, str):
        def _sub(m: re.Match[str]) -> str:
            key = m.group(1)
            if key in _NON_SECRET_PLACEHOLDERS:
                return m.group(0)
            candidates = PLACEHOLDER_ALIASES.get(key, [f"ARGO_{key}", key])
            resolved = get_env(candidates, "")
            return resolved if resolved else m.group(0)

        return _PLACEHOLDER_RE.sub(_sub, value)
    if isinstance(value, list):
        return [expand_placeholders(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_placeholders(v) for k, v in value.items()}
    return value


def _placeholders_in_spec(spec: dict[str, Any]) -> list[str]:
    """从引擎 spec 中提取 {ENV} 占位符名（不含非密钥）。"""
    try:
        blob = json.dumps(spec, ensure_ascii=False, default=str)
    except Exception:
        blob = str(spec)
    found = []
    for m in _PLACEHOLDER_RE.finditer(blob):
        key = m.group(1)
        if key in _NON_SECRET_PLACEHOLDERS:
            continue
        if key not in found:
            found.append(key)
    return found


def required_env_for(engine_id: str, spec: dict[str, Any] | None = None) -> list[str]:
    """推断引擎必填环境变量（返回「逻辑/主推荐」名列表，用于展示与检查）。

    优先级：
      1. spec.required_env（显式列表，元素可以是 str 或候选列表）
      2. KNOWN_ENV_ALIASES
      3. 从 spec 占位符推断
      4. 专用 type 映射
    """
    spec = spec or {}
    if engine_id in OPTIONAL_ENV_ENGINES and not spec.get("require_api_key"):
        # 仍可在 detail 中展示 optional，但不作为 required
        return []

    explicit = spec.get("required_env")
    if explicit:
        out: list[str] = []
        for item in explicit:
            if isinstance(item, list) and item:
                out.append(item[0])
            elif isinstance(item, str) and item:
                out.append(item)
        return out

    if engine_id in KNOWN_ENV_ALIASES:
        return [KNOWN_ENV_ALIASES[engine_id][0]]

    # type 级默认
    type_defaults = {
        "exa": ["ARGO_EXA_API_KEY"],
        "octen": ["ARGO_OCTEN_API_KEY"],
        "qweather": ["ARGO_QWEATHER_KEY"],
    }
    t = str(spec.get("type", ""))
    if t in type_defaults:
        return type_defaults[t]

    placeholders = _placeholders_in_spec(spec)
    # 把占位符映射到推荐 ARGO 名
    mapped: list[str] = []
    for p in placeholders:
        candidates = PLACEHOLDER_ALIASES.get(p, [f"ARGO_{p}", p])
        mapped.append(candidates[0])
    return mapped


def missing_env_for(engine_id: str, spec: dict[str, Any] | None = None) -> list[str]:
    """返回缺失的环境变量（用候选链检查：任一候选有值即视为满足）。"""
    spec = spec or {}
    if engine_id in OPTIONAL_ENV_ENGINES and not spec.get("require_api_key"):
        return []

    missing: list[str] = []

    explicit = spec.get("required_env")
    if explicit:
        for item in explicit:
            if isinstance(item, list):
                candidates = item
                display = item[0] if item else "?"
            else:
                display = str(item)
                if display in PLACEHOLDER_ALIASES:
                    candidates = PLACEHOLDER_ALIASES[display]
                elif display.startswith("ARGO_"):
                    legacy = display[len("ARGO_"):]
                    candidates = [display, legacy]
                else:
                    candidates = [f"ARGO_{display}", display]
            if not get_env(candidates):
                missing.append(display)
        return missing

    if engine_id in KNOWN_ENV_ALIASES:
        aliases = KNOWN_ENV_ALIASES[engine_id]
        if not get_env(aliases):
            missing.append(aliases[0])
        return missing

    t = str((spec or {}).get("type", ""))
    type_map = {
        "exa": KNOWN_ENV_ALIASES["exa"],
        "octen": KNOWN_ENV_ALIASES["octen"],
        "qweather": KNOWN_ENV_ALIASES["qweather"],
    }
    if t in type_map:
        if not get_env(type_map[t]):
            missing.append(type_map[t][0])
        return missing

    for p in _placeholders_in_spec(spec or {}):
        candidates = PLACEHOLDER_ALIASES.get(p, [f"ARGO_{p}", p])
        if not get_env(candidates):
            missing.append(candidates[0])
    return missing


def env_ready(engine_id: str, spec: dict[str, Any] | None = None) -> bool:
    return len(missing_env_for(engine_id, spec)) == 0


def parse_engine_list_env(var_name: str) -> set[str] | None:
    """解析逗号分隔引擎列表；未设置返回 None。"""
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return None
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_engine_allowed_by_env(engine_id: str) -> bool:
    """ARGO_ENABLE_ENGINES 白名单 / ARGO_DISABLE_ENGINES 黑名单。

    - 仅设置 ENABLE：不在名单内 → False
    - DISABLE 命中 → False
    - 都未设置 → True
    """
    enabled = parse_engine_list_env("ARGO_ENABLE_ENGINES")
    disabled = parse_engine_list_env("ARGO_DISABLE_ENGINES")
    if disabled and engine_id in disabled:
        return False
    if enabled is not None and engine_id not in enabled:
        return False
    return True


def env_status_for(engine_id: str, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """单引擎 env 状态摘要（供 list/validate）。"""
    required = required_env_for(engine_id, spec)
    missing = missing_env_for(engine_id, spec)
    return {
        "required_env": required,
        "missing_env": missing,
        "env_ready": len(missing) == 0,
        "allowed_by_env": is_engine_allowed_by_env(engine_id),
    }

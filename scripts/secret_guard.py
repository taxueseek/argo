#!/usr/bin/env python3
"""secret_guard.py — 输出流自动脱敏拦截器（防止真实凭据反射/外泄给 Agent 与 LLM）。

第一性原理：
  任何流向终端、JSON 或 MCP 工具返回的文本/结构化数据，都必须经过
  脱敏拦截，杜绝三类泄露：
    1. 已配置的真实凭据（从 os.environ 与 envfile 收集的所有有效 API Key/Token）；
    2. URL 参数中的敏感认证（?key=..., &token=..., &secret=...）；
    3. 通用高熵 API 凭据签名模式（sk-..., tvly-..., ghp_..., Bearer ...）。
"""

from __future__ import annotations

import os
import re
from typing import Any

# 通用高熵 API Key 签名正则
_GENERIC_KEY_PATTERNS = [
    # OpenAI / DeepSeek / 通用 sk- 开头
    re.compile(r"\b(sk-[a-zA-Z0-9_\-]{20,})\b"),
    # Tavily tvly- 开头
    re.compile(r"\b(tvly-[a-zA-Z0-9_\-]{20,})\b"),
    # GitHub Token (ghp_, github_pat_)
    re.compile(r"\b(ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{40,})\b"),
    # HTTP Bearer 认证头残留
    re.compile(r"(?i)\bBearer\s+([a-zA-Z0-9_\-\.]{24,})\b"),
    # 典型键值对形式
    re.compile(r"(?i)(?:api_key|access_token|secret_key|api_secret)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-]{24,})['\"]?"),
]

# URL query 敏感参数脱敏
_URL_PARAM_PATTERN = re.compile(
    r"([?&](?:api_?key|access_?token|token|key|secret|password|auth)=)[^& \t\r\n\"'>]+",
    re.I,
)

# 避免误杀的非密钥常见词
_WHITELIST = {
    "undefined", "null", "false", "true", "none", "password", "secret", "bearer",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
}


def get_active_secrets() -> set[str]:
    """收集当前环境中加载的真实有效长密钥字符串集合（去重、过滤短串）。"""
    secrets: set[str] = set()
    # 1. 进程环境变量
    for k, v in os.environ.items():
        if not v or len(v) < 8:
            continue
        k_upper = k.upper()
        if (k_upper.startswith("ARGO_") or
            k_upper.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_APIKEY")) or
            "API_KEY" in k_upper or "ACCESS_KEY" in k_upper):
            if v.lower() not in _WHITELIST:
                secrets.add(v.strip())

    # 2. envfile 存储的密钥
    try:
        from engine_env import _envfile_load
        file_env = _envfile_load()
        for _k, v in file_env.items():
            if v and len(v) >= 8 and v.lower() not in _WHITELIST:
                secrets.add(v.strip())
    except Exception:
        pass

    return secrets


def redact_text(text: str, extra_secrets: set[str] | None = None) -> str:
    """对单段文本执行深度脱敏替换。"""
    if not text or not isinstance(text, str):
        return text

    out = text

    # 1. 替换已知真实有效凭据
    active_secrets = get_active_secrets()
    if extra_secrets:
        active_secrets.update(extra_secrets)

    for sec in sorted(active_secrets, key=len, reverse=True):
        if sec in out:
            out = out.replace(sec, "[REDACTED_API_KEY]")

    # 2. 替换通用高熵 API Key 签名
    for pat in _GENERIC_KEY_PATTERNS:
        out = pat.sub("[REDACTED_SECRET]", out)

    # 3. 替换 URL query 参数中的敏感参数
    out = _URL_PARAM_PATTERN.sub(r"\1[REDACTED]", out)

    return out


def redact_data(data: Any, extra_secrets: set[str] | None = None) -> Any:
    """递归对复杂数据结构（dict / list / str）进行全量脱敏。"""
    if isinstance(data, str):
        return redact_text(data, extra_secrets=extra_secrets)
    if isinstance(data, list):
        return [redact_data(item, extra_secrets=extra_secrets) for item in data]
    if isinstance(data, dict):
        return {k: redact_data(v, extra_secrets=extra_secrets) for k, v in data.items()}
    return data

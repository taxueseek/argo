#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态专业搜索安全边界：URL 校验。

浏览器导航威胁模型 ≠ 服务端 SSRF 全量 DNS 解析：
  - 默认：拦危险 scheme + 明显本机/内网主机名 + 字面私有 IP
  - 严格模式 EGO_SEARCH_STRICT_SSRF=1：复用 argo url_safety 全量 DNS 检查
  - ARGO_ALLOW_PRIVATE_URLS=1：放行私有目标（本地调试）
"""
from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_ARGO_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if _ARGO_SCRIPTS.is_dir() and str(_ARGO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ARGO_SCRIPTS))

_LOCALHOST = {"localhost", "localhost.localdomain", "ip6-localhost"}
_PRIVATE_SUFFIXES = (
    ".local", ".internal", ".lan", ".home.arpa", ".corp", ".intranet",
)


def allow_private() -> bool:
    return os.environ.get("ARGO_ALLOW_PRIVATE_URLS", "").strip().lower() in {
        "1", "true", "yes",
    }


def strict_ssrf() -> bool:
    return os.environ.get("EGO_SEARCH_STRICT_SSRF", "").strip().lower() in {
        "1", "true", "yes",
    }


def _is_literal_private_host(host: str) -> bool:
    h = (host or "").strip().rstrip(".").lower()
    if not h:
        return True
    if h in _LOCALHOST:
        return True
    if any(h.endswith(s) for s in _PRIVATE_SUFFIXES):
        return True
    # 裸主机名（无点）视为本机/内网风格
    if "." not in h and ":" not in h:
        return True
    try:
        ip = ipaddress.ip_address(h)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        )
    except ValueError:
        return False


def validate_browser_url(url: str, *, context: str = "url") -> dict[str, Any]:
    """校验浏览器将打开的 URL。Returns {ok, error?, url}."""
    if not url or not isinstance(url, str):
        return {"ok": False, "error": f"{context}: empty_url", "url": url or ""}
    u = url.strip()
    try:
        p = urlparse(u)
    except Exception as e:
        return {"ok": False, "error": f"{context}: bad_url {e}", "url": u}
    if p.scheme not in ("http", "https"):
        return {
            "ok": False,
            "error": f"{context}: scheme_not_allowed ({p.scheme or 'none'})",
            "url": u,
        }
    if not p.netloc:
        return {"ok": False, "error": f"{context}: missing_host", "url": u}

    host = p.hostname or ""
    if allow_private():
        return {"ok": True, "url": u}

    if strict_ssrf():
        try:
            from url_safety import is_safe_fetch_url  # type: ignore
            if not is_safe_fetch_url(u):
                return {
                    "ok": False,
                    "error": f"{context}: blocked_by_strict_ssrf",
                    "url": u,
                }
            return {"ok": True, "url": u}
        except ImportError:
            pass

    if _is_literal_private_host(host):
        return {
            "ok": False,
            "error": (
                f"{context}: private_or_local_host_blocked "
                f"(ARGO_ALLOW_PRIVATE_URLS=1 to allow)"
            ),
            "url": u,
        }
    return {"ok": True, "url": u}

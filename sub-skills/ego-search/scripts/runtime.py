#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双运行时探测：ego lite（ego-browser）与 WebBridge（扩展桥）。

完全态：任一可用即可做登录态专业搜索。
优先序：ego（隔离更好）→ webbridge（用户浏览器会话更广）→ none。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any, Literal

RuntimeName = Literal["ego", "webbridge", "none"]

EGO_BIN = os.environ.get("EGO_SEARCH_EGO_BIN", "ego-browser")
WEBBRIDGE_URL = os.environ.get(
    "EGO_SEARCH_WEBBRIDGE_URL", "http://127.0.0.1:10086/command"
)
WEBBRIDGE_START = os.path.expanduser(
    os.environ.get(
        "EGO_SEARCH_WEBBRIDGE_START",
        "~/.kimi-webbridge/bin/kimi-webbridge",
    )
)
PROBE_TIMEOUT = float(os.environ.get("EGO_SEARCH_PROBE_TIMEOUT", "3"))


def ego_available() -> dict[str, Any]:
    """探测 ego-browser 是否在 PATH 且可启动。"""
    path = shutil.which(EGO_BIN)
    if not path:
        return {"available": False, "bin": EGO_BIN, "path": None, "error": "not_found"}
    try:
        proc = subprocess.run(
            [EGO_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
        ver = (proc.stdout or proc.stderr or "").strip().splitlines()[:3]
        ok = proc.returncode == 0 or bool(ver)
        return {
            "available": bool(ok),
            "bin": EGO_BIN,
            "path": path,
            "version_lines": ver,
            "error": None if ok else f"exit_{proc.returncode}",
        }
    except (OSError, subprocess.TimeoutExpired) as e:
        return {
            "available": False,
            "bin": EGO_BIN,
            "path": path,
            "error": str(e),
        }


def webbridge_available(*, try_start: bool = False) -> dict[str, Any]:
    """探测 WebBridge 本地桥是否在线。try_start 时幂等 start（永不 stop）。"""
    if try_start:
        _try_start_webbridge()
    try:
        body = json.dumps(
            {"action": "list_tabs", "args": {}, "session": "ego-search-probe"},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            WEBBRIDGE_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
        # 协议：{ok, data:{success, tabs}} 或 data 直接 success
        ok = bool(data.get("ok", True))
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        if isinstance(inner, dict) and inner.get("success") is False:
            ok = False
        return {
            "available": ok,
            "url": WEBBRIDGE_URL,
            "error": None if ok else "bridge_error",
            "sample": {"ok": data.get("ok"), "keys": list(data.keys())[:6]},
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        return {
            "available": False,
            "url": WEBBRIDGE_URL,
            "error": str(e),
        }


def _try_start_webbridge() -> None:
    start_bin = WEBBRIDGE_START
    if not os.path.isfile(start_bin) and not shutil.which(start_bin):
        return
    try:
        subprocess.run(
            [start_bin, "start"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def detect_runtimes(*, try_start_webbridge: bool = False) -> dict[str, Any]:
    """汇总双运行时状态并给出 preferred。"""
    ego = ego_available()
    wb = webbridge_available(try_start=try_start_webbridge)
    if ego.get("available"):
        preferred: RuntimeName = "ego"
    elif wb.get("available"):
        preferred = "webbridge"
    else:
        preferred = "none"
    any_ok = preferred != "none"
    return {
        "ego": ego,
        "webbridge": wb,
        "preferred": preferred,
        "login_search_ready": any_ok,
        "install_hints": _install_hints(ego, wb),
    }


def _install_hints(ego: dict, wb: dict) -> list[str]:
    hints: list[str] = []
    if not ego.get("available"):
        hints.append(
            "安装/启用 ego lite 并完成 onboarding，使 ego-browser 在 PATH 上可用"
            "（见 sub-skills/ego-search/references/install.md）"
        )
    if not wb.get("available"):
        hints.append(
            "安装 WebBridge 扩展并启动本地桥："
            f"`{WEBBRIDGE_START} start`（扩展需已连接）"
        )
    if ego.get("available") or wb.get("available"):
        hints.append(
            "任一运行时可用即可做登录态专业搜索；"
            "常规 argo 检索与登录态分区隔离，汇总分析阶段再融合"
        )
    return hints


def resolve_runtime(explicit: str | None = None) -> RuntimeName:
    """解析 --runtime auto|ego|webbridge。"""
    choice = (explicit or "auto").lower().strip()
    info = detect_runtimes(try_start_webbridge=(choice in ("auto", "webbridge")))
    if choice == "auto":
        return info["preferred"]  # type: ignore[return-value]
    if choice == "ego":
        return "ego" if info["ego"].get("available") else "none"
    if choice in ("webbridge", "wb", "extension"):
        return "webbridge" if info["webbridge"].get("available") else "none"
    return info["preferred"]  # type: ignore[return-value]

#!/usr/bin/env python3
"""engine_fingerprint.py — 引擎环境指纹（驱动熔断自愈与回归差分）。

第一性原理：引擎熔断/自动禁用记录的是「上次在那个环境下失败了」。当用户
升级了外部 CLI 工具（如 xhs/yt-dlp）、改了代理配置、补齐或更换了 API 密钥、
或更新了 engines/specs/<engine>.yaml 后，历史失败证据已经过时——指纹一变，
熔断器应立即允许重新探测，而不是让旧的 auto_disabled 锁死 1 小时。

设计约束：
  - 零网络请求、零子进程（CLI 依赖用 shutil.which + stat(mtime_ns, size)，毫秒级）
  - 绝不输出或存储任何密钥/代理明文，只输出 16 位 sha256 摘要
  - 所有环境读取在函数调用期完成（禁止导入期读 os.environ）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

_SPECS_DIR = Path(__file__).resolve().parent.parent / "engines" / "specs"
_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


def _spec_hash(engine_id: str, spec: dict[str, Any] | None = None) -> str:
    yaml_path = _SPECS_DIR / f"{engine_id}.yaml"
    try:
        raw = yaml_path.read_bytes()
        return hashlib.sha256(raw).hexdigest()[:12]
    except OSError:
        pass
    if isinstance(spec, dict) and spec:
        blob = json.dumps(spec, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:12]
    return ""


def _proxy_hash() -> str:
    parts = [f"{k}={os.environ.get(k, '')}" for k in _PROXY_ENV_KEYS if os.environ.get(k)]
    if not parts:
        return ""
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _key_fp(engine_id: str) -> str:
    try:
        from quota import get_quota_manager
        return str(get_quota_manager()._engine_key_fp(engine_id) or "")
    except (ImportError, OSError, RuntimeError, ValueError):
        return ""


def _dep_hash(spec: dict[str, Any] | None) -> str:
    if not isinstance(spec, dict):
        return ""
    requires = spec.get("requires")
    if not requires:
        return ""
    if isinstance(requires, str):
        items = [requires]
    elif isinstance(requires, list):
        items = [x for x in requires if isinstance(x, str)]
    else:
        return ""
    sigs: list[str] = []
    for req in sorted(set(items)):
        # 支持 "bin:xhs" 或裸名 "xhs"；非二进制声明（如 python 包）跳过
        if req.startswith("py:") or req.startswith("python:"):
            continue
        cmd = req.split(":", 1)[1] if req.startswith("bin:") else req
        path = shutil.which(cmd)
        if not path:
            sigs.append(f"{cmd}=missing")
            continue
        try:
            st = os.stat(path)
            sigs.append(f"{cmd}={path}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            sigs.append(f"{cmd}={path}")
    if not sigs:
        return ""
    return hashlib.sha256("\n".join(sigs).encode("utf-8")).hexdigest()[:12]


def _load_spec_safe(engine_id: str) -> dict[str, Any] | None:
    try:
        from config import load_config
        engines = (load_config() or {}).get("engines") or {}
        sp = engines.get(engine_id)
        return sp if isinstance(sp, dict) else None
    except (ImportError, OSError, RuntimeError, ValueError):
        return None


def compute_engine_fingerprint(
    engine_id: str,
    spec: dict[str, Any] | None = None,
) -> str:
    """计算引擎当前环境指纹（16 位 sha256）。"""
    sp = spec if spec is not None else _load_spec_safe(engine_id)
    payload = {
        "engine": engine_id,
        "spec": _spec_hash(engine_id, sp),
        "key": _key_fp(engine_id),
        "proxy": _proxy_hash(),
        "dep": _dep_hash(sp),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]

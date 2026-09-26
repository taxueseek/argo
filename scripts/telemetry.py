#!/usr/bin/env python3
"""telemetry.py — argo 最小观测遥测（append-only JSONL）

纪律（开发做减法）：
  - 不做平台：只有「追加一条」「读最近 N 条」两个操作
  - 失败静默：任何异常都吞掉返回 False，绝不拖累搜索主路径
  - 目录可注入：ARGO_TELEMETRY_DIR 覆盖（测试隔离），默认 <状态目录>/telemetry/
  - 总开关：ARGO_TELEMETRY=0 / false 完全关闭
  - 脱敏：query 等敏感字段由调用方截断，本模块不放大

Schema（每条一行 JSON，UTF-8）：
  {"ts": "...", "stream": "recovery", "version": 1, ...业务字段}
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# 本地状态目录唯一来源（env ARGO_STATE_DIR → config cache.db_path 父目录 → 旧路径）
import argo_paths as _paths
from engine_env import env_flag  # 布尔开关统一判断（见 env_flag 的说明）

_STREAM_VERSION = 1

# 单流体积上限与回缩后保留的行数。
#
# why（2026-09-17 实测）：append-only 而无任何轮转，三个流已涨到 771 KB
# （recovery 383 KB / route 350 KB / merge 38 KB）且只增不减。本模块唯一的
# 读取方是 tail（只看最近若干条），旧记录没有读者，所以超限时直接回缩到最近
# _KEEP_LINES 行，不另存归档——归档是「留给会来取的人」，这里没有这样的人。
_MAX_BYTES = 1024 * 1024
_KEEP_LINES = 2000


def _trim_if_oversized(path: Path) -> None:
    """超过上限就把文件回缩到最近 _KEEP_LINES 行；任何失败静默。

    先 stat 再决定要不要读：绝大多数调用只付一次 stat 的代价。
    """
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        # 走 argo_paths 的原子写唯一来源，避免回缩中途崩溃留下半截文件
        _paths.atomic_write_text(path, "\n".join(lines[-_KEEP_LINES:]) + "\n")
    except Exception:
        return


def _telemetry_dir() -> Path:
    # ARGO_TELEMETRY_DIR 优先（测试隔离）；未设置时由唯一来源派生
    override = os.environ.get("ARGO_TELEMETRY_DIR", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return _paths.state_path("telemetry")


def stream_dir() -> Path:
    """遥测目录（公开读出口：stats 等读者需要展示数据在哪）。"""
    return _telemetry_dir()


def _enabled() -> bool:
    return env_flag("ARGO_TELEMETRY")


def emit(stream: str, record: dict[str, Any]) -> bool:
    """追加一条观测记录到 <telemetry_dir>/<stream>.jsonl。

    失败静默返回 False，绝不抛异常；记录内会补 ts / stream / version。
    """
    if not _enabled():
        return False
    try:
        line = json.dumps(
            {
                "ts": datetime.now().astimezone().isoformat(timespec="microseconds"),
                "stream": stream,
                "version": _STREAM_VERSION,
                **record,
            },
            ensure_ascii=False,
        )
        d = _telemetry_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{stream}.jsonl"
        _trim_if_oversized(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception:
        return False


def tail(stream: str, n: int = 10) -> list[dict[str, Any]]:
    """读取最近 n 条记录（供分析与测试）。读失败返回空列表。"""
    try:
        d = _telemetry_dir()
        lines = (d / f"{stream}.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(x) for x in lines[-n:]]
    except Exception:
        return []

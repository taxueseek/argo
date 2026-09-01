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

# 本地状态目录单一真源（env ARGO_STATE_DIR → config cache.db_path 父目录 → 旧路径）
import argo_paths as _paths

_STREAM_VERSION = 1


def _telemetry_dir() -> Path:
    # ARGO_TELEMETRY_DIR 优先（测试隔离）；未设置时由单一真源派生
    override = os.environ.get("ARGO_TELEMETRY_DIR", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return _paths.state_path("telemetry")


def _enabled() -> bool:
    return os.environ.get("ARGO_TELEMETRY", "1").strip() not in ("0", "false", "False")


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
        with open(d / f"{stream}.jsonl", "a", encoding="utf-8") as f:
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

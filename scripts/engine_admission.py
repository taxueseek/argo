#!/usr/bin/env python3
"""engine_admission.py — 引擎生产准入状态

状态文件目录（默认）：
  ~/.cache/unified-search/admission/<engine_id>.json

字段：
  engine_id, admitted_at, stages_passed, quality_score,
  avg_latency_ms, blocked, reason, updated_at

路由规则：
  - blocked=true → 自动路由跳过
  - 用户 --engine 强制指定仍可调用（调试）
  - 无 admission 文件 → 默认放行（向后兼容存量引擎）
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_ADMISSION_DIR = Path(
    os.path.expanduser(os.environ.get(
        "ARGO_ADMISSION_DIR",
        "~/.cache/unified-search/admission",
    ))
)


def admission_dir() -> Path:
    d = DEFAULT_ADMISSION_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def admission_path(engine_id: str) -> Path:
    safe = engine_id.replace("/", "_").replace("..", "_")
    return admission_dir() / f"{safe}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_admission(engine_id: str) -> dict[str, Any] | None:
    path = admission_path(engine_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_admission(engine_id: str, record: dict[str, Any]) -> dict[str, Any]:
    path = admission_path(engine_id)
    out = {
        "engine_id": engine_id,
        "admitted_at": record.get("admitted_at"),
        "stages_passed": list(record.get("stages_passed") or []),
        "quality_score": record.get("quality_score"),
        "avg_latency_ms": record.get("avg_latency_ms"),
        "blocked": bool(record.get("blocked", False)),
        "reason": record.get("reason") or "",
        "updated_at": _now_iso(),
        "health": record.get("health"),
        "quality": record.get("quality"),
    }
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def set_blocked(engine_id: str, blocked: bool, reason: str = "") -> dict[str, Any]:
    current = load_admission(engine_id) or {"engine_id": engine_id, "stages_passed": []}
    current["blocked"] = blocked
    current["reason"] = reason
    if not blocked and not current.get("admitted_at"):
        current["admitted_at"] = _now_iso()
    return save_admission(engine_id, current)


def record_validation(
    engine_id: str,
    *,
    stages_passed: list[str],
    quality_score: float | None = None,
    avg_latency_ms: float | None = None,
    blocked: bool | None = None,
    reason: str = "",
    health: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
    admit: bool = False,
) -> dict[str, Any]:
    """写入验证结果；admit=True 且未 blocked 时标记准入时间。"""
    current = load_admission(engine_id) or {}
    merged_stages = list(dict.fromkeys(
        list(current.get("stages_passed") or []) + list(stages_passed or [])
    ))
    if blocked is None:
        # 默认：health 失败则 block；通过则 unblock
        blocked = "health" not in stages_passed and bool(stages_passed)
        if "health" in (stages_passed or []):
            blocked = False
        if health is not None and not health.get("ok", False) and health.get("status") != "skipped":
            blocked = True

    admitted_at = current.get("admitted_at")
    if admit and not blocked:
        admitted_at = _now_iso()
    elif blocked:
        # 保持历史 admitted_at，但 blocked 生效
        pass

    record = {
        "admitted_at": admitted_at,
        "stages_passed": merged_stages,
        "quality_score": quality_score if quality_score is not None else current.get("quality_score"),
        "avg_latency_ms": avg_latency_ms if avg_latency_ms is not None else current.get("avg_latency_ms"),
        "blocked": blocked,
        "reason": reason or current.get("reason") or "",
        "health": health if health is not None else current.get("health"),
        "quality": quality if quality is not None else current.get("quality"),
    }
    return save_admission(engine_id, record)


def is_blocked(engine_id: str, default: bool = False) -> bool:
    """是否被准入系统拉黑。无记录时 default=False（兼容存量）。"""
    rec = load_admission(engine_id)
    if rec is None:
        return default
    return bool(rec.get("blocked", False))


def is_admitted(engine_id: str) -> bool:
    rec = load_admission(engine_id)
    if rec is None:
        return False
    return bool(rec.get("admitted_at")) and not bool(rec.get("blocked"))


def list_admissions() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    d = admission_dir()
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("engine_id"):
                result[data["engine_id"]] = data
            else:
                result[path.stem] = data if isinstance(data, dict) else {}
        except Exception:
            continue
    return result


def filter_routable(engine_ids: list[str] | set[str]) -> list[str]:
    """过滤掉 blocked 引擎，保持原顺序。"""
    out = []
    for e in engine_ids:
        if is_blocked(e):
            continue
        out.append(e)
    return out

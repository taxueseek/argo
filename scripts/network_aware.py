#!/usr/bin/env python3
"""
network_aware.py — 网络环境感知（v2.7 新增）

利用 adaptive 引擎表现数据 + 运行时实测，推断当前网络环境并给出
自适应策略建议。核心洞察：
  - adaptive.get_score 已经综合了 success × latency × cost，
    是天然的「网络传感器」（慢网 → 延迟高 → 分数低）
  - 不同网络环境应使用不同策略：慢网放大超时预算、快网收紧、
    受限网优先本地引擎

纯 stdlib，无外部依赖。任何失败都返回中性建议，不阻断搜索。
"""

from __future__ import annotations

import time
from typing import Any

# 引擎延迟采样窗口（秒）
_LATENCY_WINDOW = 24 * 3600

# 网络环境判定阈值（平均延迟，毫秒）
_FAST_THRESHOLD_MS = 800.0     # 平均 < 0.8s → 快网
_SLOW_THRESHOLD_MS = 3000.0    # 平均 > 3s → 慢网

# 超时缩放范围
_TIMEOUT_SCALE_MIN = 0.8
_TIMEOUT_SCALE_MAX = 1.8

_cache: dict[str, Any] = {}


def _avg_latency(learner: Any, engines: list[str]) -> float | None:
    """从 adaptive 数据估计引擎平均延迟（无数据返回 None）。"""
    latencies: list[float] = []
    for e in engines:
        try:
            # 用 score 反推延迟不可靠；直接查 adaptive 的 per-engine 平均延迟
            score = learner.get_score(e)
            # score 已含 latency_factor，但需原始延迟 → 查 db
            lat = _latency_from_db(e)
            if lat is not None:
                latencies.append(lat)
        except Exception:
            continue
    if not latencies:
        return None
    return sum(latencies) / len(latencies)


def _latency_from_db(engine: str) -> float | None:
    """直接查 adaptive.db 的 AVG(latency_ms)（绕过 score 的合成因子）。"""
    try:
        from adaptive import DB_PATH, WINDOW_DAYS
        import sqlite3
        cutoff = time.time() - WINDOW_DAYS * 86400
        conn = sqlite3.connect(str(DB_PATH), timeout=2)
        try:
            row = conn.execute(
                "SELECT AVG(latency_ms) FROM engine_perf "
                "WHERE engine = ? AND created_at > ?",
                (engine, cutoff),
            ).fetchone()
            lat = row[0] if row and row[0] is not None else None
            return float(lat) if lat else None
        finally:
            conn.close()
    except Exception:
        return None


def network_profile(engines: list[str]) -> dict[str, Any]:
    """推断当前网络环境，返回策略建议。

    Returns:
        {
          "network": "fast" | "normal" | "slow" | "unknown",
          "avg_latency_ms": float | None,
          "timeout_scale": float,      # 超时缩放（0.8 ~ 1.8）
          "prefer_local": bool,        # 受限网偏好本地引擎
        }
    """
    now = time.time()
    cached = _cache.get("profile")
    if cached and now - cached["_ts"] < 60:  # 60s 缓存
        return cached["data"]

    profile: dict[str, Any] = {
        "network": "unknown",
        "avg_latency_ms": None,
        "timeout_scale": 1.0,
        "prefer_local": False,
    }
    try:
        from adaptive import get_learner
        learner = get_learner()
        avg = _avg_latency(learner, engines or [])
        if avg is None:
            # 无数据：用健康探针的延迟
            avg = _probe_latency(engines)
        if avg is not None:
            profile["avg_latency_ms"] = round(avg, 1)
            if avg < _FAST_THRESHOLD_MS:
                profile["network"] = "fast"
                profile["timeout_scale"] = _TIMEOUT_SCALE_MIN
            elif avg > _SLOW_THRESHOLD_MS:
                profile["network"] = "slow"
                profile["timeout_scale"] = _TIMEOUT_SCALE_MAX
                profile["prefer_local"] = True
            else:
                profile["network"] = "normal"
                profile["timeout_scale"] = 1.0
    except Exception:
        pass

    _cache["profile"] = {"_ts": now, "data": profile}
    return profile


def _probe_latency(engines: list[str]) -> float | None:
    """无 adaptive 数据时，用 health_probe 的 last_latency_ms 估计。"""
    try:
        from health_probe import get_engine_status
        lats = []
        for e in engines:
            st = get_engine_status(e)
            lat = st.get("last_latency_ms")
            if isinstance(lat, (int, float)) and lat > 0:
                lats.append(float(lat))
        if lats:
            return sum(lats) / len(lats)
    except Exception:
        pass
    return None


def adjusted_timeout(base_timeout: int, engines: list[str]) -> int:
    """按网络环境调整超时预算。

    慢网放大（最多 1.8×，避免误杀慢网下正常的引擎），
    快网收紧（0.8×，更快响应），正常网不变。
    """
    profile = network_profile(engines)
    scale = profile.get("timeout_scale", 1.0)
    return max(1, int(base_timeout * scale))


def should_prefer_local(engines: list[str]) -> bool:
    """受限网偏好本地引擎（零成本、少外部依赖）。"""
    profile = network_profile(engines)
    return bool(profile.get("prefer_local"))

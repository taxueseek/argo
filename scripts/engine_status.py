#!/usr/bin/env python3
"""engine_status.py — 引擎状态聚合（list-engines --detail）

输出每引擎：
  enabled / type / cost_tier / env_ready / missing_env /
  allowed_by_env / blocked / admitted / routable / health summary
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from config import load_config, get_cost_tiers  # noqa: E402
from engine_env import env_status_for, is_engine_allowed_by_env  # noqa: E402
from engine_admission import load_admission, is_blocked, is_admitted  # noqa: E402


def _cost_tier_of(engine_id: str, tiers: dict[str, list[str]]) -> str:
    for tier in ("paid", "api", "low", "free"):
        if engine_id in (tiers.get(tier) or []):
            return tier
    return "free"


def _all_engine_specs(cfg: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    cfg = cfg if cfg is not None else load_config()
    engines = cfg.get("engines") or {}
    return {k: v for k, v in engines.items() if isinstance(v, dict)}


def engine_detail(engine_id: str, spec: dict[str, Any] | None = None,
                  tiers: dict[str, list[str]] | None = None) -> dict[str, Any]:
    if spec is None:
        specs = _all_engine_specs()
        spec = specs.get(engine_id) or {}
    tiers = tiers if tiers is not None else get_cost_tiers()
    env = env_status_for(engine_id, spec)
    adm = load_admission(engine_id)
    blocked = is_blocked(engine_id)
    admitted = is_admitted(engine_id)
    config_enabled = bool(spec.get("enabled", True))
    allowed = env["allowed_by_env"]
    env_ok = env["env_ready"]
    # 自动路由可用条件
    routable = (
        config_enabled
        and allowed
        and env_ok
        and not blocked
    )
    status = "ready"
    if not config_enabled:
        status = "disabled"
    elif not allowed:
        status = "env_filtered"
    elif not env_ok:
        status = "missing_key"
    elif blocked:
        status = "blocked"
    elif admitted:
        status = "admitted"
    else:
        status = "ready"  # 未验证但可用（兼容）

    return {
        "engine_id": engine_id,
        "enabled": config_enabled,
        "type": spec.get("type", "cli"),
        "cost_tier": _cost_tier_of(engine_id, tiers),
        "status": status,
        "env_ready": env_ok,
        "required_env": env["required_env"],
        "missing_env": env["missing_env"],
        "allowed_by_env": allowed,
        "blocked": blocked,
        "admitted": admitted,
        "routable": routable,
        "admission": {
            "admitted_at": (adm or {}).get("admitted_at"),
            "stages_passed": (adm or {}).get("stages_passed") or [],
            "quality_score": (adm or {}).get("quality_score"),
            "avg_latency_ms": (adm or {}).get("avg_latency_ms"),
            "reason": (adm or {}).get("reason") or "",
        } if adm else None,
    }


def list_engines_detail(
    *,
    routable_only: bool = False,
    include_disabled: bool = True,
) -> list[dict[str, Any]]:
    cfg = load_config()
    tiers = get_cost_tiers(cfg)
    specs = _all_engine_specs(cfg)
    rows = []
    for name in sorted(specs.keys()):
        row = engine_detail(name, specs[name], tiers)
        if not include_disabled and not row["enabled"]:
            continue
        if routable_only and not row["routable"]:
            continue
        rows.append(row)
    return rows


def list_routable_engine_ids() -> list[str]:
    return [r["engine_id"] for r in list_engines_detail(routable_only=True, include_disabled=False)]


def format_engines_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        f"{'ENGINE':<22} {'STATUS':<14} {'TIER':<6} {'TYPE':<14} {'ROUTABLE':<8} ENV",
        "-" * 90,
    ]
    for r in rows:
        env_note = "ok" if r["env_ready"] else ",".join(r["missing_env"][:2]) or "missing"
        lines.append(
            f"{r['engine_id']:<22} {r['status']:<14} {r['cost_tier']:<6} "
            f"{str(r['type']):<14} {str(r['routable']):<8} {env_note}"
        )
    lines.append(f"\n总计 {len(rows)} · routable={sum(1 for r in rows if r['routable'])}")
    return "\n".join(lines)


def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Argo 引擎状态")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--routable-only", action="store_true")
    parser.add_argument("--engine", help="只看单个引擎")
    args = parser.parse_args()
    if args.engine:
        rows = [engine_detail(args.engine)]
    else:
        rows = list_engines_detail(routable_only=args.routable_only)
    if args.json:
        print(json.dumps(rows if len(rows) != 1 else rows[0], ensure_ascii=False, indent=2))
    else:
        print(format_engines_table(rows))


if __name__ == "__main__":
    _cli()

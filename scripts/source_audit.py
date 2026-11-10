#!/usr/bin/env python3
"""source_audit.py — 源健康度审计（`argo doctor` 的实现）。

## 为什么需要

300 个源里，真正进自动路由的只有 ~250 个，其中 15 个已停用、30 个显式专用。
接入源时只有「准入」，没有「退出」——低价值源只增不减，每个都要维护
spec、派生件、文档、测试。

本模块用**真实使用数据**（usage_log）给出去留建议，而不是靠猜。

## 判据（第一性原理：价值 = 命中率 × 结果质量 / 成本）

- `route_hits`：过去 N 天被路由命中的次数
- `result_rate`：命中后返回非空结果的比例
- `value_score`：route_hits × result_rate（命中率 × 交付率）
- `cost`：总调用成本（按 spec.cost_per_call 累加）

去留规则：
- 90 天 route_hits == 0 且非显式专用 → 建议停用（demote）
- 90 天 value_score < 5 且非显式专用 → 建议降级为 explicit_only（review）
- 30 天 result_rate < 20% 且 route_hits > 0 → 建议隔离（quarantine）
- 需密钥且本机未配置 → 提示（info）

## 输出

`argo doctor` 输出三类：建议动作（demote/review/quarantine）、提示（info）、
健康摘要。`--fix` 自动执行 demote（停用）与 quarantine（隔离），
review 只提示不改（降级需要人工判断是否还有垂直价值）。
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _usage_dir() -> Path:
    """使用日志目录（ARGO_USAGE_LOG_DIR 可覆盖）。"""
    override = os.environ.get("ARGO_USAGE_LOG_DIR")
    if override:
        return Path(override)
    try:
        import usage_log
        return usage_log.usage_log_dir()
    except Exception:
        return Path.home() / ".cache" / "unified-search" / "usage_log"


def _load_query_log(days: int) -> list[dict]:
    """读最近 N 天的 query 流日志。"""
    d = _usage_dir()
    path = d / "query.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now().timestamp() - days * 86400
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = r.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.timestamp() < cutoff:
                continue
        except Exception:
            pass
        rows.append(r)
    return rows


def _load_route_log(days: int) -> list[dict]:
    """读最近 N 天的 route 流日志。"""
    d = _usage_dir()
    path = d / "route.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now().timestamp() - days * 86400
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = r.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.timestamp() < cutoff:
                continue
        except Exception:
            pass
        rows.append(r)
    return rows


def _engine_usage(days: int = 90) -> dict[str, dict[str, Any]]:
    """统计每个引擎的使用情况。"""
    queries = _load_query_log(days)
    routes = _load_route_log(days)

    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "route_hits": 0, "calls": 0, "results": 0, "errors": 0,
        "domains": set(),
    })

    # route 流：被路由命中
    for r in routes:
        engines = r.get("engines") or r.get("engines_used") or []
        if isinstance(engines, str):
            engines = [engines]
        for e in engines:
            stats[e]["route_hits"] += 1
            dom = r.get("domain")
            if dom:
                stats[e]["domains"].add(dom)

    # query 流：实际调用与结果。
    # 口径注意：engine_results 字段 2026-10-26 才加入（v2.13.0），
    # 历史日志里 92% 是 null——不能拿它算交付率。改用 count（最终结果数）
    # 作为「这次调用有没有交付」的粗信号：count > 0 且该引擎在 engines_used
    # 里，就记一次交付。
    for r in queries:
        used = r.get("engines_used") or []
        if isinstance(used, str):
            used = [used]
        count = r.get("count") or 0
        er = r.get("engine_results") or {}
        for e in used:
            stats[e]["calls"] += 1
            # 优先用 engine_results（精确），缺失时用 count > 0（粗）
            n = er.get(e)
            if isinstance(n, int):
                if n > 0:
                    stats[e]["results"] += 1
            elif count > 0:
                stats[e]["results"] += 1
        for f in (r.get("failures") or []):
            if isinstance(f, dict) and f.get("engine"):
                stats[f["engine"]]["errors"] += 1

    return dict(stats)


def audit(days: int = 90, verbose: bool = False) -> dict[str, Any]:
    """审计全部源，返回建议清单与摘要。"""
    try:
        import engines
        all_specs = engines.get_all_engine_specs() if hasattr(engines, "get_all_engine_specs") else {}
    except Exception:
        all_specs = {}

    if not all_specs:
        # 兜底：直接读 specs 目录
        from yaml_load import loads as yaml_loads
        spec_dir = Path(__file__).parent.parent / "engines" / "specs"
        for f in spec_dir.glob("*.yaml"):
            try:
                d = yaml_loads(f.read_text(encoding="utf-8"))
                if isinstance(d, dict) and d.get("engine_id"):
                    all_specs[d["engine_id"]] = d
            except Exception:
                continue

    usage = _engine_usage(days)

    demote: list[dict] = []      # 建议停用
    review: list[dict] = []      # 建议降级 explicit_only
    quarantine: list[dict] = []  # 建议隔离
    info: list[dict] = []        # 提示

    for name, spec in sorted(all_specs.items()):
        explicit = bool(spec.get("explicit_only"))
        enabled = spec.get("enabled", True)
        cost_per_call = spec.get("cost_per_call", 0.0) or 0.0

        u = usage.get(name, {})
        route_hits = u.get("route_hits", 0)
        calls = u.get("calls", 0)
        results = u.get("results", 0)
        errors = u.get("errors", 0)

        result_rate = (results / calls) if calls > 0 else 0.0
        value_score = route_hits * result_rate

        record = {
            "engine": name,
            "explicit_only": explicit,
            "enabled": enabled,
            "route_hits": route_hits,
            "calls": calls,
            "results": results,
            "errors": errors,
            "result_rate": round(result_rate, 3),
            "value_score": round(value_score, 2),
            "cost_per_call": cost_per_call,
            "domains": sorted(u.get("domains", set())),
        }

        if not enabled:
            continue

        # 需密钥且未配置 → 提示
        # required_env 形态：['KEY'] 或 [['KEY_A', 'KEY_B']]（别名链，任一就绪即可）
        required_env = spec.get("required_env") or []
        if isinstance(required_env, str):
            required_env = [required_env]
        missing_group = []
        for group in required_env:
            names = group if isinstance(group, list) else [group]
            if not any(os.environ.get(n) for n in names):
                missing_group.append(names)
        if missing_group and not explicit:
            shown = ', '.join('/'.join(g[:3]) for g in missing_group[:3])
            record["reason"] = f"需密钥未配置: {shown}"
            info.append(record)
            continue

        if explicit:
            continue  # 显式专用源不进自动路由，不参与去留判定

        if route_hits == 0:
            record["reason"] = f"{days} 天 0 次路由命中"
            demote.append(record)
        elif value_score < 5:
            record["reason"] = f"价值分 {value_score} < 5（命中 {route_hits} 次，交付率 {result_rate:.0%}）"
            review.append(record)
        elif calls > 0 and result_rate < 0.2:
            record["reason"] = f"交付率 {result_rate:.0%} < 20%（命中 {route_hits} 次，错误 {errors} 次）"
            quarantine.append(record)

    return {
        "days": days,
        "total_specs": len(all_specs),
        "enabled_specs": sum(1 for s in all_specs.values() if s.get("enabled", True)),
        "explicit_specs": sum(1 for s in all_specs.values() if s.get("explicit_only")),
        "used_engines": len(usage),
        "demote": demote,
        "review": review,
        "quarantine": quarantine,
        "info": info,
    }


def render(report: dict, verbose: bool = False) -> str:
    """渲染审计报告（人读）。"""
    lines: list[str] = []
    lines.append(f"源健康度审计（过去 {report['days']} 天）")
    lines.append(
        f"  收录 {report['total_specs']} 个源，启用 {report['enabled_specs']} 个，"
        f"显式专用 {report['explicit_specs']} 个，日志出现 {report['used_engines']} 个"
    )
    lines.append("")

    if report["demote"]:
        lines.append(f"  建议停用（{len(report['demote'])} 个，0 命中）：")
        for r in report["demote"][:10]:
            lines.append(f"    - {r['engine']}: {r['reason']}")
        if len(report["demote"]) > 10:
            lines.append(f"    ... 另有 {len(report['demote']) - 10} 个")
        lines.append("")

    if report["review"]:
        lines.append(f"  建议降级为 explicit_only（{len(report['review'])} 个，低价值）：")
        for r in report["review"][:10]:
            lines.append(f"    - {r['engine']}: {r['reason']}")
        if len(report["review"]) > 10:
            lines.append(f"    ... 另有 {len(report['review']) - 10} 个")
        lines.append("")

    if report["quarantine"]:
        lines.append(f"  建议隔离（{len(report['quarantine'])} 个，低交付率）：")
        for r in report["quarantine"]:
            lines.append(f"    - {r['engine']}: {r['reason']}")
        lines.append("")

    if report["info"]:
        lines.append(f"  提示（{len(report['info'])} 个，需密钥未配置）：")
        for r in report["info"][:5]:
            lines.append(f"    - {r['engine']}: {r['reason']}")
        if len(report["info"]) > 5:
            lines.append(f"    ... 另有 {len(report['info']) - 5} 个")
        lines.append("")

    if not (report["demote"] or report["review"] or report["quarantine"]):
        lines.append("  ✔ 全部源健康，无去留建议")
        lines.append("")

    lines.append("  运行 `argo doctor --fix` 自动执行停用与隔离；降级需人工判断。")
    return "\n".join(lines)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="源健康度审计（基于真实使用日志给出去留建议）")
    parser.add_argument("--days", type=int, default=90,
                        help="审计窗口（天），默认 90")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--verbose", action="store_true", help="显示全部明细")
    args = parser.parse_args()

    report = audit(days=args.days, verbose=args.verbose)

    if args.json:
        # CLI stdout 必须紧凑 JSON（cli_io.dumps）——缩进 JSON 会被
        # test_context_budget 判为「CLI 输出污染管道」。
        try:
            import cli_io
            print(cli_io.dumps(report))
        except ImportError:
            print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    else:
        print(render(report, verbose=args.verbose))
    return 0


if __name__ == "__main__":
    sys.exit(main())

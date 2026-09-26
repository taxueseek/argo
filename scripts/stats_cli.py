#!/usr/bin/env python3
"""stats_cli.py — `argo stats`：使用日志与反馈状态的读出口。

数据源 = telemetry.py 的本地 JSONL 流（<状态目录>/telemetry/，总开关
ARGO_TELEMETRY=0 可关，目录 ARGO_TELEMETRY_DIR 可注入）：
  query    每次非缓存搜索一条总账（次数/引擎/命中数/时延/救援）
  recovery 救援链触发概览
  route    路由采样
  merge    融合去重概览

纪律：只读不写；本地数据不出本机（隐私判据见 references/usage.md「日志与反馈」）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import telemetry  # noqa: E402
from cli_io import dumps  # noqa: E402


def _query_summary(n: int) -> dict:
    rows = telemetry.tail("query", n)
    if not rows:
        return {"samples": 0}
    counts = [r.get("count", 0) for r in rows]
    lat = [r.get("elapsed_ms") for r in rows if isinstance(r.get("elapsed_ms"), (int, float))]
    engines: Counter = Counter()
    for r in rows:
        for e in (r.get("engines_used") or [])[:8]:
            engines[e] += 1
    return {
        "samples": len(rows),
        "hit_rate": round(sum(1 for c in counts if c) / len(rows), 3),
        "avg_elapsed_ms": round(sum(lat) / len(lat)) if lat else None,
        "recovered": sum(1 for r in rows if r.get("recovered")),
        "top_engines": engines.most_common(5),
        "recent": [
            {"query": r.get("query"), "count": r.get("count"),
             "elapsed_ms": r.get("elapsed_ms")}
            for r in rows[-5:]
        ],
    }


def _stream_summary(name: str, n: int) -> dict:
    rows = telemetry.tail(name, n)
    return {"samples": len(rows), "recent": rows[-3:]}


def build_report(n: int) -> dict:
    return {
        "data_dir": str(telemetry.stream_dir()),
        "privacy": "本地 JSONL，不出本机；ARGO_TELEMETRY=0 可整体关闭",
        "query": _query_summary(n),
        "recovery": _stream_summary("recovery", n),
        "route": _stream_summary("route", n),
        "merge": _stream_summary("merge", n),
        "hints": [
            "流量回放/审计：argo search --archive（完整候选归档）",
            "引擎健康：argo preflight --probe / --list-engines --detail",
            "关闭遥测：ARGO_TELEMETRY=0",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="argo stats", description="使用日志与反馈状态（本地遥测只读出口）")
    ap.add_argument("-n", type=int, default=50, help="回看最近 N 条（默认 50）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)
    report = build_report(max(1, min(args.n, 500)))
    out = dumps(report)
    print(out if args.json else out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

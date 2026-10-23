#!/usr/bin/env python3
"""stats_cli.py — `argo stats`：使用日志与反馈状态的读出口。

数据源 = usage_log.py 的本地 JSONL 日志流（<状态目录>/usage_log/，总开关
ARGO_USAGE_LOG=0 可关，目录 ARGO_USAGE_LOG_DIR 可注入）：
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

import usage_log  # noqa: E402
from cli_io import dumps  # noqa: E402
from search_attribution import aggregate_slow_engines  # noqa: E402


def _query_summary(n: int) -> dict:
    rows = usage_log.tail("query", n)
    if not rows:
        # 空日志（CI 全新 runner / 首次安装）也要给稳定的字段形状：夜间账本
        # 直接读 avg_elapsed_ms，此前这里只回 {"samples": 0}，工作流每次
        # 首次跑必炸 KeyError（issue #19）。指标用 None 而不是 0：0ms 会被
        # 画成一条假曲线，None 才表示「没有样本」；调用方按 samples 判断。
        return {
            "samples": 0,
            "result_rate": None,
            "avg_elapsed_ms": None,
            "recovered": 0,
            "top_engines": [],
            "slowest_engines": [],
            "recent": [],
        }
    counts = [r.get("count", 0) for r in rows]
    lat = [r.get("elapsed_ms") for r in rows if isinstance(r.get("elapsed_ms"), (int, float))]
    engines: Counter = Counter()
    # 引擎级耗时归因（2026-10-15）：query 流新增 engine_latency_top，
    # 这里按「慢源累计耗时」排序，把「长期是谁在拖」直接摆到 stats 上——
    # 此前只能离线重算日志（local_search 独跑占 369s 就是这么翻出来的）。
    # 聚合是纯函数，住 search_attribution（与写侧同一份口径真源）。
    for r in rows:
        for e in (r.get("engines_used") or [])[:8]:
            engines[e] += 1
    return {
        "samples": len(rows),
        # 2026-10-18 改名：原字段叫 hit_rate，实际算的是「有结果的查询比例」
        # （count>0），与 SearchCache.stats 的缓存命中率同名不同义——实测被
        # 读成「缓存全命中」（=1.0）。保留语义，换掉会误导的名字。
        "result_rate": round(sum(1 for c in counts if c) / len(rows), 3),
        "avg_elapsed_ms": round(sum(lat) / len(lat)) if lat else None,
        "recovered": sum(1 for r in rows if r.get("recovered")),
        "top_engines": engines.most_common(5),
        "slowest_engines": aggregate_slow_engines(rows),
        "recent": [
            {"query": r.get("query"), "count": r.get("count"),
             "elapsed_ms": r.get("elapsed_ms")}
            for r in rows[-5:]
        ],
    }


def _stream_summary(name: str, n: int) -> dict:
    rows = usage_log.tail(name, n)
    return {"samples": len(rows), "recent": rows[-3:]}


def build_report(n: int) -> dict:
    return {
        "data_dir": str(usage_log.stream_dir()),
        "privacy": "本地使用日志 JSONL，仅本机不外发；ARGO_USAGE_LOG=0 可整体关闭",
        "query": _query_summary(n),
        "recovery": _stream_summary("recovery", n),
        "route": _stream_summary("route", n),
        "merge": _stream_summary("merge", n),
        "hints": [
            "流量回放/审计：argo search --archive（完整候选归档）",
            "引擎健康：argo preflight --probe / --list-engines --detail",
            "关闭日志：ARGO_USAGE_LOG=0",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="argo stats", description="使用日志与反馈状态（本地使用日志只读出口）")
    ap.add_argument("-n", type=int, default=50, help="回看最近 N 条（默认 50）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)
    report = build_report(max(1, min(args.n, 500)))
    out = dumps(report)
    print(out if args.json else out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

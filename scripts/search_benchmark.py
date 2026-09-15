#!/usr/bin/env python3
"""search_benchmark.py — Argo 可比较的搜索性能基准。

原则：先建立基线，再优化。这个脚本只测两类不会依赖外网的指标：
  1. route_query 热路径延迟；
  2. execute_search 编排层在确定性模拟引擎下的串行/并行墙钟。

它不声称模拟网络质量，也不把一次机器上的绝对毫秒数当成产品 SLA。
用途是回答两个更窄、可复现的问题：
  - 路由是否值得优化；
  - 多引擎调度是否真的兑现并行收益。

运行：
  python3 scripts/search_benchmark.py
  python3 scripts/search_benchmark.py --runs 7 --engine-delay 0.15
  python3 scripts/search_benchmark.py --json

真实网络质量、召回率和证据质量应由独立 benchmark/eval 数据集衡量，
不要用本脚本替代质量评测。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples) * 1000.0, 3)


def benchmark_route(queries: list[str], runs: int) -> dict[str, Any]:
    """测量 route_query 热路径，不联网。"""
    from route import route_query

    samples: list[float] = []
    for _ in range(max(1, runs)):
        t0 = time.perf_counter()
        for query in queries:
            route_query(query, mode="auto")
        samples.append(time.perf_counter() - t0)
    per_query = statistics.median(samples) / len(queries) * 1000.0
    return {
        "queries": len(queries),
        "runs": len(samples),
        "batch_median_ms": _median_ms(samples),
        "per_query_median_ms": round(per_query, 3),
    }


def benchmark_dispatch(
    engine_names: list[str],
    runs: int,
    engine_delay: float,
) -> dict[str, Any]:
    """用确定性模拟引擎比较 execute_search 串行/并行墙钟。

    fake engine 每次返回一条与查询词面一致的结果。三引擎均需要完成时，
    串行路径理论上接近 N×delay，并行路径接近 max(delay)。实际结果仍以
    Argo 自身编排逻辑为准，因此可用于检测调度回归。
    """
    import search
    from cache import SearchCache

    original_engine_search = search.engine_search
    original_missing_env = search._missing_env_for
    original_get_engines = search.get_engines
    original_get_execution_config = search.get_execution_config
    original_get_cost_factor = search.get_cost_factor

    def fake_engine_search(
        query: str, engine: str, n: int = 5, timeout: float | None = None,
        depth: str = "fast", mode: str = "auto", **_: Any,
    ) -> list[dict[str, Any]]:
        del timeout, depth, mode
        time.sleep(engine_delay)
        return [{
            "title": f"{query} result from {engine}",
            "url": f"https://example.invalid/{engine}/{n}",
            "snippet": f"{query} evidence from {engine}",
            "source": engine,
        }]

    # benchmark 必须完全脱离真实引擎、密钥和持久化健康状态。
    search.engine_search = fake_engine_search
    search._missing_env_for = lambda _eng: []
    search.get_engines = lambda: {}
    search.get_execution_config = lambda: {"retry_count": 0, "per_engine_budget_s": 10.0}
    search.get_cost_factor = lambda _eng: 1.0

    decision_base = {
        "domain": "general",
        "engine": engine_names[0],
        "engines_combo": engine_names,
        "tfidf_scores": [],
        "reason": "benchmark",
    }

    def run(parallel: bool) -> list[float]:
        samples: list[float] = []
        for i in range(max(1, runs)):
            decision = {**decision_base, "parallel": parallel}
            cache = SearchCache(db_path=str(
                Path.cwd() / f".argo-benchmark-{parallel}-{i}.db"
            ))
            try:
                t0 = time.perf_counter()
                out = search.execute_search(
                    "benchmark search",
                    decision,
                    max_results=len(engine_names),
                    timeout=max(2, int(engine_delay * 10)),
                    depth="fast",
                    cache=cache,
                    skip_cache=True,
                    mode="auto",
                )
                elapsed = time.perf_counter() - t0
                if out.get("count") != len(engine_names):
                    raise RuntimeError(
                        f"benchmark expected {len(engine_names)} results, got {out.get('count')}"
                    )
                samples.append(elapsed)
            finally:
                close = getattr(cache, "close", None)
                if callable(close):
                    close()
                db = Path.cwd() / f".argo-benchmark-{parallel}-{i}.db"
                for suffix in ("", "-wal", "-shm"):
                    try:
                        (Path(str(db) + suffix)).unlink()
                    except FileNotFoundError:
                        pass
        return samples

    try:
        serial = run(False)
        parallel = run(True)
    finally:
        search.engine_search = original_engine_search
        search._missing_env_for = original_missing_env
        search.get_engines = original_get_engines
        search.get_execution_config = original_get_execution_config
        search.get_cost_factor = original_get_cost_factor

    serial_ms = _median_ms(serial)
    parallel_ms = _median_ms(parallel)
    speedup = round(serial_ms / parallel_ms, 3) if parallel_ms else None
    return {
        "engines": engine_names,
        "runs": max(1, runs),
        "engine_delay_ms": round(engine_delay * 1000.0, 3),
        "serial_median_ms": serial_ms,
        "parallel_median_ms": parallel_ms,
        "parallel_speedup": speedup,
    }


def run_benchmark(runs: int = 5, engine_delay: float = 0.1) -> dict[str, Any]:
    queries = [
        "Python async best practices",
        "2026 US CPI latest",
        "中国 2025 年 GDP 总量",
        "React Server Components production",
        "OpenAI MCP specification",
        "量化投资 因子模型",
    ]
    engines = ["benchmark_a", "benchmark_b", "benchmark_c"]
    return {
        "schema_version": 1,
        "benchmark": "argo-search-offline-dispatch",
        "runs": max(1, runs),
        "route": benchmark_route(queries, runs),
        "dispatch": benchmark_dispatch(engines, runs, engine_delay),
        "interpretation": {
            "route": "per_query_median_ms is the routing-only baseline; optimize only if it is material to end-to-end latency.",
            "dispatch": "parallel_speedup compares Argo orchestration under equal deterministic engine delay; it is a scheduling regression guard, not a network benchmark.",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Argo deterministic search performance benchmark")
    parser.add_argument("--runs", type=int, default=5, help="samples per benchmark (default: 5)")
    parser.add_argument("--engine-delay", type=float, default=0.1, help="fake engine delay in seconds (default: 0.1)")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.engine_delay <= 0:
        parser.error("--runs must be >= 1 and --engine-delay must be > 0")

    result = run_benchmark(args.runs, args.engine_delay)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        route = result["route"]
        dispatch = result["dispatch"]
        print("Argo comparable performance baseline")
        print(f"route: {route['per_query_median_ms']:.3f} ms/query median")
        print(
            "dispatch: "
            f"serial {dispatch['serial_median_ms']:.3f} ms → "
            f"parallel {dispatch['parallel_median_ms']:.3f} ms "
            f"({dispatch['parallel_speedup']:.2f}×)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

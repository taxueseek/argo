#!/usr/bin/env python3
"""
benchmark.py — 性能测试

对比 v2.0 各关键路径延迟：
  - TF-IDF 路由
  - 双层缓存（L1 / L2）
  - 引擎执行
  - RRF 融合
"""
from __future__ import annotations

import json
import sys
import time
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from tfidf_router import get_router, semantic_route
from cache import SearchCache
from route import route_query


def bench_tfidf(n: int = 100) -> dict:
    """测试 TF-IDF 路由延迟。"""
    router = get_router()
    queries = ["英伟达财报", "Python异步编程", "基金推荐", "latest AI research",
               "北京旅游攻略", "股票行情", "transformer paper", "美联储加息"]
    t0 = time.perf_counter()
    for _ in range(n):
        for q in queries:
            router.route(q, top_k=3)
    elapsed = (time.perf_counter() - t0) * 1000
    total = n * len(queries)
    return {
        "total_calls": total,
        "total_ms": round(elapsed, 2),
        "avg_ms": round(elapsed / total, 4),
        "qps": round(total / (elapsed / 1000), 0),
    }


def bench_cache(n: int = 100) -> dict:
    """测试双层缓存读写延迟。"""
    cache = SearchCache()
    # 预热
    for i in range(10):
        cache.set(f"query_{i}", "anysearch", 5,
                  {"results": [{"title": f"result_{i}", "url": f"http://x/{i}"}]},
                  domain="tech")

    # GET 测试
    t0 = time.perf_counter()
    hits = 0
    for _ in range(n):
        for i in range(10):
            hit = cache.get(f"query_{i}", "anysearch", 5, domain="tech")
            if hit:
                hits += 1
    elapsed = (time.perf_counter() - t0) * 1000
    total = n * 10
    return {
        "total_calls": total,
        "hits": hits,
        "hit_rate": round(hits / total, 3),
        "total_ms": round(elapsed, 2),
        "avg_ms": round(elapsed / total, 4),
    }


def bench_route(n: int = 50) -> dict:
    """测试端到端路由决策延迟。"""
    queries = ["英伟达最新财报", "Python 异步编程最佳实践", "2026年AI芯片行业竞争格局",
               "基金定投策略", "笔记本电脑推荐", "latest transformer paper"]
    t0 = time.perf_counter()
    for _ in range(n):
        for q in queries:
            route_query(q)
    elapsed = (time.perf_counter() - t0) * 1000
    total = n * len(queries)
    return {
        "total_calls": total,
        "total_ms": round(elapsed, 2),
        "avg_ms": round(elapsed / total, 4),
    }


def main():
    print("=" * 60)
    print("Unified Search v2.0 — 性能基准测试")
    print("=" * 60)

    print("\n[1] TF-IDF 语义路由")
    r1 = bench_tfidf()
    print(f"  调用次数: {r1['total_calls']}")
    print(f"  总耗时:   {r1['total_ms']}ms")
    print(f"  单次延迟: {r1['avg_ms']}ms")
    print(f"  吞吐:     {r1['qps']} qps")

    print("\n[2] 双层缓存（L1/L2 读取）")
    r2 = bench_cache()
    print(f"  调用次数: {r2['total_calls']}")
    print(f"  命中次数: {r2['hits']}")
    print(f"  命中率:   {r2['hit_rate']}")
    print(f"  总耗时:   {r2['total_ms']}ms")
    print(f"  单次延迟: {r2['avg_ms']}ms")

    print("\n[3] 端到端路由决策")
    r3 = bench_route()
    print(f"  调用次数: {r3['total_calls']}")
    print(f"  总耗时:   {r3['total_ms']}ms")
    print(f"  单次延迟: {r3['avg_ms']}ms")

    print("\n" + "=" * 60)
    # 目标验证
    print("目标验证:")
    print(f"  TF-IDF < 5ms:    {'✓' if r1['avg_ms'] < 5 else '✗'} ({r1['avg_ms']}ms)")
    print(f"  缓存命中 < 0.1ms: {'✓' if r2['avg_ms'] < 0.1 else '✗'} ({r2['avg_ms']}ms)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""search_benchmark.py — Argo 可比较的搜索性能基准（已适配有界并发新架构）。

原则：先建立基线，再优化。这个脚本只测两类不依赖外网的指标：
  1. route_query 热路径延迟；
  2. execute_search 调度层在确定性模拟引擎下的串行/并行墙钟。

它不声称模拟网络质量，也不把某台机器上的绝对毫秒数当成产品 SLA。
用途是回答两个更窄、可复现的问题：
  - 路由热路径是否值得优化；
  - 多引擎调度是否真的兑现并行收益，并作为并行度的回归防护。

与早期版本相比，为了在「有界并发 + early-stop + 近重复去重」的新调度下仍然
成立，本基准做了四处确定性处理（每一处都对应新架构的真实行为，不是为了让数字
好看）：

1. ``no_early_stop=True`` 且 ``depth="deep"``：新架构的串行路径默认「首个引擎
   有合格结果即停」，fast/auto 的并行也是 primary 先跑、再分岔补发的 wave 结构。
   若照搬默认行为，串行只跑 1 个引擎、并行跑多个，测出来的是「早停差异」而不是
   「串/并行差异」。deep 在设计上就是全量并行（见 search.py 的调度分支），配合
   no_early_stop 让串行、并行都跑完全部 N 个引擎，对比才公平。
2. 每个假引擎返回一篇主题词汇几乎零重叠的文档（见 ``_FAKE_DOCS``）：融合阶段有
   minhash 近重复去重（默认阈值 0.85），模板雷同的假结果会被当成同一篇删掉，
   导致结果条数少于引擎数、串并行不可比。
3. 每次运行用唯一查询词：熔断/负缓存是进程内单例，复用同一查询会让「上一次」的
   状态影响「下一次」，破坏样本独立性。
4. 缓存落在临时目录并在结束时整体删除：``skip_cache=True`` 已绕过读写，但仍需传
   入缓存对象；SearchCache 没有 close，统一用临时目录保底，保证跑完不留文件。

运行：
  python3 scripts/search_benchmark.py
  python3 scripts/search_benchmark.py --runs 7 --engine-delay 0.2
  python3 scripts/search_benchmark.py --json

真实网络质量、召回率和证据质量应由独立 eval 数据集衡量，不要用本脚本替代。
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from cli_io import dumps

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# 新架构调度里被基准替换的接缝。它们在 search.py 中是模块级名字（部分从
# engines/config 导入后再绑定），重新绑定 search.<name> 即可在 execute_search
# 内生效；结束后必须逐一还原，避免污染同进程的其它代码。
_PATCHED_NAMES = (
    "engine_search",
    "_missing_env_for",
    "get_engines",
    "get_execution_config",
    "get_cost_factor",
)

# 三篇文档分属完全不同的主题域，标题/摘要词面几乎零重叠，确保不会被 RRF 之后的
# minhash 近重复去重（阈值 0.85）合并；URL 用保留测试域 example.invalid，也不会
# 被 SERP/跳转 URL 过滤剔除。
_FAKE_DOCS: dict[str, tuple[str, str]] = {
    "benchmark_a": (
        "Kubernetes pod scheduling latency investigation",
        "Container orchestration: kubelet queueing, cgroup throttling, "
        "namespace eviction and node autoscaling traces.",
    ),
    "benchmark_b": (
        "Treasury bond yield curve inversion historical analysis",
        "Fixed income: duration, convexity, sovereign spreads and "
        "inverted-curve recession signals observed across decades.",
    ),
    "benchmark_c": (
        "Glacier mass balance polar field expedition notes",
        "Cryosphere fieldwork: accumulation stakes, calving fronts, "
        "permafrost cores and satellite altimetry measurement grids.",
    ),
}

_DEFAULT_QUERIES = [
    "Python async best practices",
    "2026 US CPI latest",
    "中国 2025 年 GDP 总量",
    "React Server Components production",
    "OpenAI MCP specification",
    "量化投资 因子模型",
]
_DEFAULT_ENGINES = ["benchmark_a", "benchmark_b", "benchmark_c"]


def _median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples) * 1000.0, 3)


def _all_samples_ms(samples: list[float]) -> list[float]:
    return [round(s * 1000.0, 3) for s in samples]


def benchmark_route(queries: list[str], runs: int) -> dict[str, Any]:
    """测量 route_query 热路径，不联网。先做一次预热，避免首次 import/加载
    成本混进热路径样本（与 PR 原版本相比新增，测量对象因此更纯粹）。"""
    from route import route_query

    # 预热：触发路由表/模型的惰性加载，但不计入任何样本。
    for query in queries:
        route_query(query, mode="auto")

    samples: list[float] = []
    for _ in range(max(1, runs)):
        t0 = time.perf_counter()
        for query in queries:
            route_query(query, mode="auto")
        samples.append(time.perf_counter() - t0)
    per_query = statistics.median(samples) / len(queries) * 1000.0
    per_query_min = min(samples) / len(queries) * 1000.0
    return {
        "queries": len(queries),
        "runs": len(samples),
        "batch_median_ms": _median_ms(samples),
        "per_query_median_ms": round(per_query, 3),
        "per_query_min_ms": round(per_query_min, 3),
    }


def benchmark_dispatch(
    engine_names: list[str],
    runs: int,
    engine_delay: float,
    tmp_root: Path | str | None = None,
) -> dict[str, Any]:
    """用确定性假引擎比较 execute_search 串行/并行墙钟。

    每个假引擎 sleep 固定 ``engine_delay`` 后返回一篇差异化文档。N 个引擎全部
    需要完成时，串行路径接近 N×delay（叠加共同的融合固定开销），deep 全量并行
    路径接近 1×delay（叠加同一份固定开销）。结果以 Argo 自身调度为准，可用于检测
    调度并行度回归。全程不访问网络、不需要 API key、不读写真实缓存。

    ``tmp_root``：缓存文件的落点。传了就在该目录下建临时目录（目录由调用方
    拥有，跑完即删）；不传则用系统临时目录。测试**必须**传它——否则「有没有
    残留」只能靠 glob 系统临时目录来判断，而那是全局共享空间，任何并发的
    创建/清理都会让断言随机变红（实测 3 次里红 1 次，且失败时并无真实残留）。
    """
    import search
    from cache import SearchCache

    original = {name: getattr(search, name) for name in _PATCHED_NAMES}

    def fake_engine_search(
        query: str, engine: str, n: int = 5, timeout: float | None = None,
        depth: str = "fast", mode: str = "auto", **_: Any,
    ) -> list[dict[str, Any]]:
        del query, n, timeout, depth, mode
        time.sleep(engine_delay)
        title, snippet = _FAKE_DOCS.get(
            engine, (f"distinct document {engine}", f"unique corpus {engine}")
        )
        return [{
            "title": title,
            "url": f"https://example.invalid/{engine}/article",
            "snippet": snippet,
            "source": engine,
        }]

    # 基准必须完全脱离真实引擎、密钥与持久化健康状态：
    # - 假引擎顶替唯一网络出口 engine_search；
    # - 不缺环境变量、引擎规格表给空（重试次数由下面的 execution 配置锁为 0）；
    # - 成本系数恒为 1，避免任何配置文件差异影响调度。
    search.engine_search = fake_engine_search
    search._missing_env_for = lambda _eng: []
    search.get_engines = lambda: {}
    search.get_execution_config = lambda: {
        "retry_count": 0, "per_engine_budget_s": 10.0,
    }
    search.get_cost_factor = lambda _eng: 1.0

    # 外层等待预算要明显大于「串行跑完全部引擎」的理论时长，避免基准自己被超时截断。
    outer_timeout = max(5, int(engine_delay * (len(engine_names) + 2)) + 2)
    tmp_dir = tempfile.mkdtemp(
        prefix="argo-benchmark-",
        dir=str(tmp_root) if tmp_root is not None else None,
    )

    def run_once(parallel: bool, idx: int) -> float:
        # 唯一查询：隔离进程内熔断/负缓存单例，保证每个样本相互独立。
        unique_query = f"benchmark search {uuid.uuid4().hex}"
        cache = SearchCache(db_path=str(
            Path(tmp_dir) / f"bench-{int(parallel)}-{idx}-{uuid.uuid4().hex[:6]}.db"
        ))
        decision = {
            "domain": "general",
            "engine": engine_names[0],
            "engines_combo": list(engine_names),
            "tfidf_scores": [],
            "reason": "benchmark",
            "parallel": parallel,
            # 关键：串行也跑完全部引擎，不被「首个有结果即停」短路。
            "no_early_stop": True,
        }
        try:
            t0 = time.perf_counter()
            out = search.execute_search(
                unique_query,
                decision,
                max_results=len(engine_names),
                timeout=outer_timeout,
                # deep：并行走「全量一次性并发」分支而非 primary 先跑的 wave 结构。
                depth="deep",
                cache=cache,
                skip_cache=True,
                mode="auto",
            )
            elapsed = time.perf_counter() - t0
        finally:
            # SearchCache 无 close；连接随对象回收，文件由临时目录统一清理。
            del cache
        count = out.get("count")
        used = out.get("engines_used") or []
        if count != len(engine_names) or len(used) != len(engine_names):
            statuses = [o.get("status") for o in out.get("engine_outcomes", [])]
            raise RuntimeError(
                f"benchmark expected {len(engine_names)} results from all engines, "
                f"got count={count}, engines_used={used}, outcomes={statuses}. "
                "通常是假结果被近重复去重合并，或某引擎被准入/熔断拦截。"
            )
        return elapsed

    try:
        serial = [run_once(False, i) for i in range(max(1, runs))]
        parallel = [run_once(True, i) for i in range(max(1, runs))]
    finally:
        for name, value in original.items():
            setattr(search, name, value)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    serial_ms = _median_ms(serial)
    parallel_ms = _median_ms(parallel)
    speedup = round(serial_ms / parallel_ms, 3) if parallel_ms else None
    return {
        "engines": list(engine_names),
        "engine_count": len(engine_names),
        "runs": max(1, runs),
        "engine_delay_ms": round(engine_delay * 1000.0, 3),
        "serial_median_ms": serial_ms,
        "parallel_median_ms": parallel_ms,
        "serial_min_ms": round(min(serial) * 1000.0, 3),
        "parallel_min_ms": round(min(parallel) * 1000.0, 3),
        "parallel_speedup": speedup,
        "serial_samples_ms": _all_samples_ms(serial),
        "parallel_samples_ms": _all_samples_ms(parallel),
    }


def run_benchmark(runs: int = 5, engine_delay: float = 0.15) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "benchmark": "argo-search-offline-dispatch",
        "runs": max(1, runs),
        "route": benchmark_route(_DEFAULT_QUERIES, runs),
        "dispatch": benchmark_dispatch(_DEFAULT_ENGINES, runs, engine_delay),
        "interpretation": {
            "route": "per_query_median_ms 是纯路由基线；只有当它在端到端延迟中占比可观时才值得优化。",
            "dispatch": (
                "parallel_speedup 是在相同确定性引擎延迟下、串行跑满 N 个 vs deep 全量并行的墙钟比，"
                "是调度并行度的回归护栏，不是网络基准。融合/排序等固定 CPU 开销两种模式都要付，"
                "会稀释比值；调大 --engine-delay 可让调度差异占主导、更接近理想的 N 倍。"
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Argo deterministic search performance benchmark")
    parser.add_argument("--runs", type=int, default=5,
                        help="每个基准的采样次数（默认 5）")
    parser.add_argument("--engine-delay", type=float, default=0.15,
                        help="假引擎固定延迟秒数（默认 0.15）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--save-baseline", metavar="PATH",
                        help="结果（含环境 meta）写入 PATH，作为 --compare 的基线")
    parser.add_argument("--compare", metavar="PATH",
                        help="与基线对比：route/serial/parallel 中位数回归 >15%% 时退出码 1")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.engine_delay <= 0:
        parser.error("--runs 必须 >= 1，--engine-delay 必须 > 0")

    # 状态隔离必须早于任何状态模块 import。基准走真实 execute_search 的学习
    # 记录路径，不隔离就把 benchmark_a/b/c 写进生产 adaptive.db 与 quota.json
    # （实测生产库里 2113 行为基准残留）。迟一步就没救：route 在**模块级**
    # `from adaptive import get_learner`，而 adaptive.DB_PATH 是 import 期定下
    # 的常量，之后再改 ARGO_STATE_DIR 拉不回来（同 argo_paths 的模块级路径告警）。
    #
    # 放在 main() 而非模块级：测试只调 benchmark_dispatch/run_benchmark，
    # 不走本入口，且 conftest 已设 ARGO_STATE_DIR——模块级隔离会在 pytest
    # 收集期覆盖掉 conftest 的目录，制造跨用例串扰。
    import argo_paths
    state_dir = argo_paths.isolate_state_dir("argo-bench")
    try:
        result = run_benchmark(args.runs, args.engine_delay)
    finally:
        # 隔离目录是本次运行的一次性产物，跑完即删（与本脚本对 tmp_root 的
        # 「不留残留」约定一致）。
        shutil.rmtree(state_dir, ignore_errors=True)

    # 环境 meta：跨机器/跨时间数字漂移的归因依据（PR #14「可对比」的收口件）
    import platform
    result["env"] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # 与基线对比：三个 min（best-of）回归 >15% 记 REGRESSION，整体退出码 1。
    # 估计量选 min 而非 median（2026-09-30 实测）：CPU 微基准的噪声只增不减，
    # median-of-3 对单侧噪声敏感——套件负载下 median 实测波动 ~20%，单独就击穿
    # 15% 阈值造成假阳性回归告警；min 是 best-of 估计量（timeit 同款），同样
    # 数据实测波动 ~8%，余量充足。中位数仍随基线落盘供展示，不再用于判定。
    regressions: list[str] = []
    if args.compare:
        import json
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        if (base.get("dispatch") or {}).get("engine_delay_ms") != \
                result["dispatch"]["engine_delay_ms"]:
            print("[warn] engine_delay 与基线不同，dispatch 对比仅作参考")
        for section, key in (("route", "per_query_min_ms"),
                             ("dispatch", "serial_min_ms"),
                             ("dispatch", "parallel_min_ms")):
            b = (base.get(section) or {}).get(key)
            c = result[section][key]
            if not b:
                continue
            delta = (c - b) / b * 100.0
            hit = c > b * 1.15
            if hit:
                regressions.append(f"{section}.{key}")
            print(f"compare {section}.{key}: {b:.1f} → {c:.1f} ms "
                  f"({delta:+.1f}%){'  [REGRESSION >15%]' if hit else ''}")
        if regressions:
            print(f"性能回归: {', '.join(regressions)}")

    if args.save_baseline:
        out_path = Path(args.save_baseline)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(dumps(result) + "\n", encoding="utf-8")
        print(f"[baseline] 已写入 {out_path}")

    if args.json:
        print(dumps(result))
    else:
        route = result["route"]
        dispatch = result["dispatch"]
        print("Argo comparable performance baseline")
        print(f"route: {route['per_query_median_ms']:.3f} ms/query median "
              f"(min {route.get('per_query_min_ms', 0.0):.3f})")
        print(
            "dispatch: "
            f"serial {dispatch['serial_median_ms']:.3f} ms → "
            f"parallel {dispatch['parallel_median_ms']:.3f} ms "
            f"({dispatch['parallel_speedup']:.2f}×, "
            f"{dispatch['engine_count']} engines × "
            f"{dispatch['engine_delay_ms']:.0f} ms)"
        )
    return 1 if regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""search_benchmark 的确定性测试。

锁定四件事：
  1. route 基准输出形状正确、数值非负；
  2. dispatch 在新架构（有界并发 + early-stop + 近重复去重）下仍让全部 N 个
     引擎都产出结果，且串行墙钟明显累加、deep 全量并行显著更快；
  3. 整体结果可被机器读取（JSON round-trip）且字段契约稳定；
  4. 基准跑完不在**调用方指定的**临时根目录里留下缓存文件（传 tmp_root，
     不 glob 系统临时目录——那是全局共享空间，会让断言随机变红）。

全程离线：假引擎顶替了唯一网络出口，不需要 API key。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import search_benchmark  # noqa: E402


def test_route_benchmark_is_deterministic_shape():
    out = search_benchmark.benchmark_route(["Python async", "中国 GDP"], runs=2)
    assert out["queries"] == 2
    assert out["runs"] == 2
    assert out["per_query_median_ms"] >= 0
    assert out["batch_median_ms"] >= 0


def test_dispatch_parallel_is_faster_than_serial_and_covers_all_engines(tmp_path):
    """残留检查用**本用例自己的**目录，不 glob 系统临时目录。

    此前是 `set(Path(tempfile.gettempdir()).glob("argo-benchmark-*"))` 前后对比。
    那是全局共享空间：任何并发的创建/清理（另一次 pytest、手工跑基准、别的进程）
    都会让断言随机变红，而失败时并没有真实残留——实测 3 次里红 1 次、每次查残留
    都是空的。基准现在接受 tmp_root，把落点交给调用方，断言于是变成确定性的。
    """
    out = search_benchmark.benchmark_dispatch(
        ["benchmark_a", "benchmark_b", "benchmark_c"], runs=2, engine_delay=0.2,
        tmp_root=tmp_path,
    )

    # 三个引擎都被执行、各产出一条，样本数与 runs 一致。
    assert out["engine_count"] == 3
    assert len(out["serial_samples_ms"]) == 2
    assert len(out["parallel_samples_ms"]) == 2

    # 串行跑满 3 个引擎：墙钟至少应累加出接近 (N-1)×delay 的等待
    # （留 15% 余量，不把线程调度抖动算成失败）。
    expect_serial_floor_ms = (out["engine_count"] - 1) * out["engine_delay_ms"] * 0.85
    assert out["serial_median_ms"] >= expect_serial_floor_ms

    # 并行显著快于串行，且加速比明显大于 1（阈值 1.5，留出抖动余量）。
    assert out["parallel_median_ms"] < out["serial_median_ms"]
    assert out["parallel_speedup"] > 1.5

    # 跑完不得在自己的临时根目录里留下任何东西。
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == [], f"基准留下了临时文件：{leftovers}"


def test_json_benchmark_schema():
    out = search_benchmark.run_benchmark(runs=1, engine_delay=0.2)
    # round-trip 同时锁定机器可读契约。
    payload = json.loads(json.dumps(out, ensure_ascii=False))
    assert payload["schema_version"] == 1
    assert payload["benchmark"] == "argo-search-offline-dispatch"
    assert "route" in payload and "dispatch" in payload
    dispatch = payload["dispatch"]
    assert dispatch["engine_count"] == 3
    assert dispatch["parallel_speedup"] > 1.5
    assert "serial_median_ms" in dispatch and "parallel_median_ms" in dispatch


def test_save_baseline_and_compare_roundtrip(tmp_path):
    """基线闭环（2026-09-28，PR #14「可对比」收口）：save→compare 同参不误报。

    2026-09-30 补记：对比估计量已从 median 换成 min（best-of，见 compare
    处注释）——median-of-3 在套件负载下实测波动 ~20%，单独就能击穿 15%
    阈值造成假阳性（本测试曾在全量套件里红过，修复后隔离复跑仍有
    +9.6%~+16.9% 的漂移）；min 实测 ~8%。runs=3 保留：样本数影响所有段，
    min 的稳健性已足够。
    """
    base = tmp_path / "baseline.json"
    # --runs 5（2026-09-30）：min 估计量下样本越多越稳，5 次后套件满载
    # 实测假阳性率归零（3 次仍偶发 +16%）；耗时增 4s，可接受。
    rc1 = search_benchmark.main(["--runs", "5", "--engine-delay", "0.05",
                                 "--save-baseline", str(base)])
    assert rc1 == 0
    payload = json.loads(base.read_text(encoding="utf-8"))
    assert "env" in payload and "python" in payload["env"], \
        "基线必须带环境 meta（跨机器漂移归因用）"
    rc2 = search_benchmark.main(["--runs", "5", "--engine-delay", "0.05",
                                 "--compare", str(base)])
    assert rc2 == 0, "同机同参不得误报回归"


def test_compare_catches_real_regression(tmp_path):
    """存在性证明：10 倍延迟差必须被 >15% 阈值抓住（退出码 1）。"""
    base = tmp_path / "baseline.json"
    search_benchmark.main(["--runs", "3", "--engine-delay", "0.05",
                           "--save-baseline", str(base)])
    rc = search_benchmark.main(["--runs", "3", "--engine-delay", "0.5",
                                "--compare", str(base)])
    assert rc == 1, "真实回归必须被拦下，否则基准没有存在意义"

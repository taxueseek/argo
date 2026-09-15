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


def test_dispatch_parallel_is_faster_than_serial():
    out = search_benchmark.benchmark_dispatch(
        ["a", "b", "c"], runs=2, engine_delay=0.03
    )
    assert out["serial_median_ms"] >= 80
    assert out["parallel_median_ms"] < out["serial_median_ms"]
    assert out["parallel_speedup"] > 1.5


def test_json_benchmark_schema():
    out = search_benchmark.run_benchmark(runs=1, engine_delay=0.02)
    # round-trip also locks the documented machine-readable contract.
    payload = json.loads(json.dumps(out, ensure_ascii=False))
    assert payload["schema_version"] == 1
    assert payload["benchmark"] == "argo-search-offline-dispatch"
    assert "route" in payload and "dispatch" in payload
    assert payload["dispatch"]["parallel_speedup"] > 1.5

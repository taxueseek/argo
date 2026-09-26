#!/usr/bin/env python3
"""阶段耗时计时的测试（默认开，--no-timing 可关）。

## 为什么需要它

本仓此前的性能结论只能靠**外挂**计时得出：importtime 看冷启动、cProfile 看
CPU、临时包装模块级函数看阶段。「最大的瓶颈在哪」不是一个工具能自答的问题，
换个人、换台机器就得重做一遍——而实测过的事实是：缓存命中的搜索里，**约六成
墙钟花在进程固定开销上**（解释器启动 + import），它此前从不出现在任何输出里。

这里锁三件事：
  1. --no-timing 时不产生 `timing` 键；
  2. 开启后阶段齐全、占比自洽（按耗时降序、合计 ~100%）；
  3. 显式请求的字段不得被 `--fields agent` 剥掉——请求了却拿不到比不提供更糟。

运行：
  python3 -m pytest tests/test_search_timing.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import search as S  # noqa: E402


class TestStageTimingUnit:
    def test_empty_summary_is_safe(self):
        t = S.StageTiming()
        got = t.summary()
        assert got["stages"] == [] and got["stages_ms"] == 0

    def test_sorted_desc_with_pct(self):
        t = S.StageTiming()
        t.add("a", 10.0)
        t.add("b", 30.0)
        t.add("c", 60.0)
        got = t.summary()
        assert [r["stage"] for r in got["stages"]] == ["c", "b", "a"], \
            "必须按耗时降序——「最大的瓶颈在哪」就是第一行"
        assert got["stages_ms"] == 100
        assert sum(r["pct"] for r in got["stages"]) == pytest.approx(100.0, abs=0.5)

    def test_add_accumulates(self):
        t = S.StageTiming()
        t.add("x", 1.5)
        t.add("x", 2.5)
        assert t.summary()["stages"][0]["ms"] == 4.0


def _fake_engine(query, engine, n=5, timeout=None, depth=None, mode=None,
                 since=None, until=None, skip_cache=False):
    return [{"title": f"{engine} 结果", "url": f"https://{engine}.example.com/a",
             "snippet": "摘要内容 " * 10, "source": engine, "score": 0.5}]


def _run(timing):
    """跑一次真实调度（引擎被替换，不联网），返回结果。"""
    decision = {
        "domain": "general", "engine": "anysearch",
        "engines_combo": ["anysearch"], "tfidf_scores": [],
        "reason": "test", "parallel": False, "no_early_stop": True,
    }
    with patch.object(S, "engine_search", _fake_engine), \
            patch.object(S, "get_engines", lambda: {}), \
            patch.object(S, "_missing_env_for", lambda _e: []), \
            patch.object(S, "get_cost_factor", lambda _e: 1.0):
        return S.execute_search(
            "测试查询", decision, max_results=3, timeout=5, depth="balanced",
            cache=S.SearchCache(), skip_cache=True, mode="auto", timing=timing)


class TestInstrumentedPipeline:
    def test_not_measuring_means_no_timing_key(self):
        """不传计时器（等价于 --no-timing）就不该产生 timing 键。"""
        out = _run(None)
        assert "timing" not in out, "没在计时却输出了 timing 键"

    def test_on_collects_expected_stages(self, tmp_path):
        t = S.StageTiming()
        out = _run(t)
        assert "timing" in out
        stages = {r["stage"] for r in out["timing"]["stages"]}
        # 这些是每次搜索都必然经过的阶段
        for name in ("dispatch", "fusion", "dedupe", "rerank", "signals"):
            assert name in stages, f"缺阶段 {name}，实际 {sorted(stages)}"
        assert out["timing"]["stages_ms"] > 0

    def test_dispatch_block_reports_parallelism(self):
        t = S.StageTiming()
        out = _run(t)
        d = out["timing"]["dispatch"]
        assert d["engines_run"] >= 1
        assert d["engine_sum_ms"] >= 0 and d["wall_ms"] >= 0
        # 并发效率是「该加并发还是该摘慢源」的判据，必须给出
        assert "parallel_efficiency" in d

    def test_pct_is_relative_to_measured_stages(self):
        t = S.StageTiming()
        out = _run(t)
        total = sum(r["pct"] for r in out["timing"]["stages"])
        assert 95.0 <= total <= 105.0, \
            f"占比合计 {total}% 偏离 100——分母口径又不自洽了"


class TestAgentProfileKeepsRequestedTiming:
    """请求了却拿不到，比不提供更糟。"""

    def test_timing_survives_agent_stripping(self):
        payload = {"query": "q", "results": [], "timing": {"stages": []},
                   "engine_outcomes": [{"x": 1}], "tfidf_scores": []}
        out = S._strip_for_agent(payload)
        assert "timing" in out
        assert out["timing"] == {"stages": []}

    def test_usage_log_still_stripped(self):
        payload = {"query": "q", "results": [], "engine_outcomes": [{"x": 1}],
                   "tfidf_scores": [1, 2], "lang_pref": {"a": 1}}
        out = S._strip_for_agent(payload)
        for k in ("engine_outcomes", "tfidf_scores", "lang_pref"):
            assert k not in out, f"{k} 属本地使用日志标量，仍应被剥掉"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

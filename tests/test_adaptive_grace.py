#!/usr/bin/env python3
"""test_adaptive_grace — 对冲宽限窗的自适应收窄（2026-10-11，调度微手术）。

守三类契约：

1. **取数口径**（adaptive.latency_percentile）：7 天窗、只统计 success 且非空的
   「正常交付」，样本不足 / 快照过时 / 参数非法一律返回 None（调用方回落固定窗，
   绝不瞎猜）。
2. **只缩不放**（engine_dispatch._adaptive_grace）：快引擎的窗收到自己的 P75，
   慢引擎与无历史的引擎逐位不变；下界 0.3s 与 wave-1 既有 floor 同源；
   `ARGO_ADAPTIVE_GRACE=0` 整体关闭；学习库异常 fail-open 回固定窗。
3. **效果可见**（假引擎集成）：快主引擎（历史 P75=0.3s）异常慢到 0.55s 时，
   备选源在收窄后的窗内起跑（2 次调用）；关闭收窄时主引擎仍在固定 0.8s 窗内
   交付，只有 1 次调用。两个世界可区分，门有牙。

墙钟断言沿用 test_serial_hedge 的原则：假引擎把「谁慢、慢多久」写成字面量，
不碰真网络；阈值取两个世界之间且不贴边。
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import adaptive  # noqa: E402
import engine_dispatch  # noqa: E402
import search  # noqa: E402
from cache import SearchCache  # noqa: E402
from search import execute_search  # noqa: E402

QUERY = "自适应宽限窗回归查询"


def _uniq(tag: str) -> str:
    return f"t_adaptive_grace_{tag}_{os.getpid()}"


def _good(tag: str, n: int = 3) -> list:
    return [
        {"title": f"{QUERY} 数据 {tag} {i}",
         "snippet": f"关于{QUERY}的说明 {i}",
         "url": f"https://example.com/{tag}/{i}"}
        for i in range(n)
    ]


class TestLatencyPercentile(unittest.TestCase):
    """adaptive.latency_percentile 的取数口径。"""

    def setUp(self):
        self.learner = adaptive.get_learner()

    def _record(self, engine: str, latencies, *, success=True, empty=False):
        for lat in latencies:
            self.learner.record(engine, success, lat, empty=empty)

    def test_basic_percentile(self):
        eng = _uniq("basic")
        self._record(eng, [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000])
        self.assertEqual(self.learner.latency_percentile(eng, 0.75), 800.0)
        self.assertEqual(self.learner.latency_percentile(eng, 0.5), 500.0)

    def test_min_samples_guard(self):
        eng = _uniq("small")
        self._record(eng, [100, 200, 300])
        self.assertIsNone(self.learner.latency_percentile(eng, 0.75),
                          "样本不足 5 条必须返回 None，让调用方回落固定窗")

    def test_empty_rows_excluded(self):
        """空结果不进「正常交付」口径：秒回空结果不代表引擎快。"""
        eng = _uniq("emptymix")
        self._record(eng, [100, 100, 100, 100, 100, 100], empty=True)
        self._record(eng, [900, 900, 900, 900, 900, 900])
        self.assertEqual(self.learner.latency_percentile(eng, 0.75), 900.0)

    def test_failed_rows_excluded(self):
        eng = _uniq("failmix")
        self._record(eng, [50] * 8, success=False)
        self._record(eng, [700] * 6)
        self.assertEqual(self.learner.latency_percentile(eng, 0.75), 700.0)

    def test_stale_snapshot_returns_none(self):
        eng = _uniq("stale")
        self._record(eng, [400] * 6)
        cutoff = time.time() - (adaptive.STALE_AFTER_SECONDS + 60)
        with self.learner._lock:
            conn = self.learner._connect()
            conn.execute("UPDATE engine_perf SET created_at = ? WHERE engine = ?",
                         (cutoff, eng))
            conn.commit()
        self.assertIsNone(self.learner.latency_percentile(eng, 0.75),
                          "快照过时必须返回 None（引擎现状可能已变）")

    def test_bad_inputs_return_none(self):
        eng = _uniq("badin")
        self._record(eng, [400] * 6)
        self.assertIsNone(self.learner.latency_percentile(eng, 0.0))
        self.assertIsNone(self.learner.latency_percentile(eng, 1.0))
        self.assertIsNone(self.learner.latency_percentile(eng, "x"))
        self.assertIsNone(self.learner.latency_percentile(""))
        self.assertIsNone(self.learner.latency_percentile(_uniq("nosuch")))


class TestAdaptiveGrace(unittest.TestCase):
    """engine_dispatch._adaptive_grace 的只缩不放与 fail-open。"""

    def _grace(self, engine: str, base: float, p75_ms) -> float:
        fake = MagicMock()
        fake.latency_percentile.return_value = p75_ms
        with patch.dict(os.environ, {"ARGO_ADAPTIVE_GRACE": "1"}), \
                patch("adaptive.get_learner", return_value=fake):
            return engine_dispatch._adaptive_grace(engine, base)

    def test_fast_engine_shrinks_to_p75(self):
        self.assertEqual(self._grace("fast_eng", 0.8, 250.0), 0.3,
                         "P75 250ms 应收到下界 0.3s")

    def test_mid_engine_shrinks_between(self):
        self.assertEqual(self._grace("mid_eng", 0.8, 520.0), 0.52)

    def test_slow_engine_unchanged(self):
        self.assertEqual(self._grace("slow_eng", 0.8, 2600.0), 0.8,
                         "慢引擎（P75 > 窗）必须逐位不变，只缩不放")

    def test_no_history_unchanged(self):
        self.assertEqual(self._grace("cold_eng", 0.8, None), 0.8)

    def test_fail_open_on_learner_error(self):
        # 开关必须显式打开：本用例测的是「learner 抛错时 fail-open」，不设
        # ARGO_ADAPTIVE_GRACE=1 会在 env 闸门处直接返回 base，get_learner 根本
        # 不会被调到——fail-open 分支删掉用例照样绿（2026-10-12 实测无牙）。
        with patch.dict(os.environ, {"ARGO_ADAPTIVE_GRACE": "1"}), \
                patch("adaptive.get_learner", side_effect=RuntimeError("db locked")):
            self.assertEqual(engine_dispatch._adaptive_grace("any", 0.8), 0.8,
                             "学习库异常必须 fail-open 回固定窗")

    def test_env_kill_switch(self):
        fake = MagicMock()
        fake.latency_percentile.return_value = 250.0
        with patch.dict(os.environ, {"ARGO_ADAPTIVE_GRACE": "0"}), \
                patch("adaptive.get_learner", return_value=fake):
            self.assertEqual(engine_dispatch._adaptive_grace("fast_eng", 0.8), 0.8)
        fake.latency_percentile.assert_not_called()

    def test_zero_base_untouched(self):
        """显式 0 = 不节流（备选源与首引擎同时起跑），不得被抬到下界。"""
        self.assertEqual(self._grace("fast_eng", 0.0, 250.0), 0.0)

    def test_pb_hos_degraded_reliability_shrinks_grace(self):
        """PB-HOS 故障风险率收窄：即使 P75 未知或偏慢，低健康分（<0.45）主源提前触发对冲。"""
        fake = MagicMock()
        fake.latency_percentile.return_value = None
        fake.get_score.return_value = 0.20
        with patch.dict(os.environ, {"ARGO_ADAPTIVE_GRACE": "1"}), \
                patch("adaptive.get_learner", return_value=fake):
            g = engine_dispatch._adaptive_grace("shaky_primary", 0.8)
        self.assertEqual(g, 0.6)


class _AllowAllBreaker:
    def allow(self, *_a, **_k):
        return True, ""

    def get_negative(self, *_a, **_k):
        return None

    def status(self, *_a, **_k):
        return {"state": "closed"}

    def record_success(self, *_a, **_k):
        pass

    def record_failure(self, *_a, **_k):
        pass

    def record_note(self, *_a, **_k):
        pass

    def set_negative(self, *_a, **_k):
        pass

    def clear_negative(self, *_a, **_k):
        pass


class TestWave1AdaptiveHedge(unittest.TestCase):
    """集成：收窄真的让备选源提前起跑（两个世界可区分）。"""

    def _run(self, fake, engines, *, p75_ms, adaptive_on: bool):
        calls: list[str] = []

        def _spy(q, eng, **kw):
            calls.append(eng)
            return fake(q, eng)

        learner = MagicMock()
        learner.latency_percentile.return_value = p75_ms
        cache = SearchCache(db_path=":memory:")
        decision = {"engines_combo": list(engines), "engines": list(engines),
                    "parallel": True, "domain": "general",
                    "engine": engines[0], "early_stop_min_results": 1,
                    "no_early_stop": False}
        env = {"ARGO_ADAPTIVE_GRACE": "1" if adaptive_on else "0"}
        with patch.dict(os.environ, env), \
                patch("search.engine_search", side_effect=_spy), \
                patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()), \
                patch("quota.get_quota_manager", return_value=MagicMock()), \
                patch("adaptive.get_learner", return_value=learner):
            out = execute_search(
                QUERY, decision, max_results=5, timeout=10, depth="fast",
                cache=cache, skip_cache=True, mode="fast")
        return out, calls

    def test_fast_primary_hedges_earlier_when_history_is_fast(self):
        """历史 P75=0.3s 的主引擎 0.55s 还没回来 = 异常慢 → 备选源提前救场。

        对照（关闭收窄）：固定窗 0.8s 内主引擎 0.55s 已合格交付 → 只付 1 次。
        """
        def fake(_q, eng):
            if eng == "hist_fast_primary":
                time.sleep(0.55)
                return _good("hp")
            if eng == "rescue_backup":
                time.sleep(0.05)
                return _good("bk")
            return []

        out_on, calls_on = self._run(
            fake, ["hist_fast_primary", "rescue_backup"],
            p75_ms=300.0, adaptive_on=True)
        out_off, calls_off = self._run(
            fake, ["hist_fast_primary", "rescue_backup"],
            p75_ms=300.0, adaptive_on=False)

        self.assertIn("rescue_backup", calls_on,
                      f"收窄后备选源仍未提前起跑：{calls_on}")
        self.assertEqual(calls_off, ["hist_fast_primary"],
                         f"关闭收窄的基线臂应只付 1 次调用：{calls_off}")
        self.assertTrue(out_on["results"], "救场结果没有交付")

    def test_slow_primary_window_unchanged(self):
        """历史 P75=2.6s 的慢主引擎：窗不放大，0.55s 内交付 → 仍然只付 1 次。"""
        def fake(_q, eng):
            if eng == "hist_slow_primary":
                time.sleep(0.55)
                return _good("sp")
            if eng == "backup":
                time.sleep(0.05)
                return _good("bk")
            return []

        _out, calls = self._run(
            fake, ["hist_slow_primary", "backup"],
            p75_ms=2600.0, adaptive_on=True)
        self.assertEqual(calls, ["hist_slow_primary"],
                         f"慢引擎的窗被改动了：{calls}")


class TestSchemeARegressions(unittest.TestCase):
    """方案 A 核心契约回归测试（状态契约 + 调度提速 + POST 连接池复用）。"""

    def test_adaptive_grace_default_on(self):
        fake = MagicMock()
        fake.latency_percentile.return_value = 320.0
        with patch.dict(os.environ, {}, clear=False), \
                patch("adaptive.get_learner", return_value=fake):
            os.environ.pop("ARGO_ADAPTIVE_GRACE", None)
            self.assertEqual(engine_dispatch._adaptive_grace("fast_eng", 0.8), 0.32)

    def test_skipped_outcomes_not_recorded_as_zero_ms_failures(self):
        def fake(_q, eng, **_kw):
            return _good(eng)

        learner = MagicMock()
        learner.latency_percentile.return_value = None
        quota = MagicMock()
        quota.remote_exhausted_marks.return_value = {
            "exhausted_eng": {"until": time.time() + 3600, "reason": "quota"}
        }
        cache = SearchCache(db_path=":memory:")
        decision = {
            "engines_combo": ["exhausted_eng", "live_a", "live_b"],
            "engines": ["exhausted_eng", "live_a", "live_b"],
            "parallel": True,
            "domain": "general",
            "engine": "exhausted_eng",
            "early_stop_min_results": 3,
            "no_early_stop": False,
        }
        with patch("search.engine_search", side_effect=fake), \
                patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()), \
                patch("quota.get_quota_manager", return_value=quota), \
                patch("adaptive.get_learner", return_value=learner):
            out = execute_search(
                QUERY, decision, max_results=5, timeout=10, depth="fast",
                cache=cache, skip_cache=True, mode="auto")
        recorded_engines = [c.args[0] for c in learner.record.call_args_list]
        self.assertNotIn("exhausted_eng", recorded_engines,
                         f"skipped-quota-exhausted 不得记入 adaptive.db: {recorded_engines}")
        self.assertTrue(out["results"])

    def test_wave1_cumulative_sufficient_stops_before_wave2(self):
        """Wave-1 双引擎各回 2 条（单源 <3，跨源合计 4 >=3）时即停，不启 Wave-2。"""
        calls: list[str] = []

        def fake(_q, eng, **_kw):
            calls.append(eng)
            if eng in ("w1_a", "w1_b"):
                return _good(eng, n=2)
            return _good(eng, n=3)

        learner = MagicMock()
        learner.latency_percentile.return_value = 300.0
        quota = MagicMock()
        quota.remote_exhausted_marks.return_value = {}
        cache = SearchCache(db_path=":memory:")
        decision = {
            "engines_combo": ["w1_a", "w1_b", "w2_c"],
            "engines": ["w1_a", "w1_b", "w2_c"],
            "parallel": True,
            "domain": "general",
            "engine": "w1_a",
            "early_stop_min_results": 3,
            "no_early_stop": False,
        }
        with patch("search.engine_search", side_effect=fake), \
                patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()), \
                patch("quota.get_quota_manager", return_value=quota), \
                patch("adaptive.get_learner", return_value=learner):
            out = execute_search(
                QUERY, decision, max_results=5, timeout=10, depth="fast",
                cache=cache, skip_cache=True, mode="auto")
        self.assertNotIn("w2_c", calls, f"Wave-1 跨源累计达标后不应启动 Wave-2: {calls}")
        self.assertTrue(out["early_stopped"])

    def test_route_combo_filters_hard_down_engines(self):
        import route_combo
        quota = MagicMock()
        quota.is_hard_down.side_effect = lambda e: e == "octen"
        with patch("route_combo.get_quota_manager", return_value=quota), \
                patch("route_combo._breaker_blocked", return_value=False):
            filtered = route_combo.breaker_filter(
                ["anysearch", "octen", "local_bing"],
                {"anysearch", "octen", "local_bing"},
                skip_aux=False,
            )
            self.assertEqual(filtered, ["anysearch", "local_bing"])
            best, score, _ = route_combo.select_tfidf_best(
                "test query", {"primary_lang": "en"}, {},
                lambda _q, top_k=3: [("octen", 0.5, ""), ("anysearch", 0.4, "")],
            )
            self.assertEqual(best, "anysearch")
            self.assertEqual(score, 0.4)

    def test_post_http_engine_uses_http_client_post_by_default(self):
        import engines_base
        spec = {
            "engine_id": "test_post_eng",
            "method": "POST",
            "url": "https://example.com/api/search",
            "post_body": {"q": "{query}"},
            "format": "json",
            "output_map": {"items": "items", "item_title": "title", "item_url": "url"},
        }
        eng_fn = engines_base._build_http_engine(spec)
        fake_resp = {
            "status": 200,
            "text": '{"items": [{"title": "T1", "url": "https://example.com/1"}]}',
            "headers": {},
            "url": "https://example.com/api/search",
            "elapsed_ms": 12,
        }
        with patch.dict(os.environ, {"ARGO_ENGINE_HTTP_CLIENT": "1"}), \
                patch("http_client.HttpClient.post", return_value=fake_resp) as mock_post:
            res = eng_fn("hello", max_results=5)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["url"], "https://example.com/1")
            mock_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()


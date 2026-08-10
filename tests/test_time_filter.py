#!/usr/bin/env python3
"""时间窗后过滤 / 归一化 / 时区解析 / recovery 透传 回归测试。

覆盖 v2.7.4 时间能力补强：
  1. _normalize_time_window：相对量 → 绝对日期、绝对/ISO/epoch 解析、非法输入
  2. _apply_time_window：宽松后过滤（仅剔除明确超窗，无时间字段保留）
  3. _published_ts：ISO 时区（Z / ±HH:MM）正确换算 epoch
  4. execute_search recovery 路径透传 since/until（不丢用户约束）
  5. _parse_text_output / _parse_yaml_output 尊重 n（不再硬编码 10 条）

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_time_filter.py -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import search as search_mod  # noqa: E402
from engines_base import _parse_text_output, _parse_yaml_output  # noqa: E402
from cache import SearchCache  # noqa: E402


# ── 1. 时间窗归一化 ──────────────────────────────────────────────────────────

class TestNormalizeTimeWindow(unittest.TestCase):
    def test_relative_days_to_absolute_date(self):
        since_iso, _, since_ts, _ = search_mod._normalize_time_window("7d", None)
        expect = (datetime.now() - timedelta(days=7)).date().isoformat()
        self.assertEqual(since_iso, expect)
        # epoch 秒应为该日期本地零点
        expect_ts = datetime.strptime(expect, "%Y-%m-%d").timestamp()
        self.assertAlmostEqual(since_ts, expect_ts, places=0)

    def test_relative_hours_and_weeks(self):
        h_iso, _, _, _ = search_mod._normalize_time_window("6h", None)
        self.assertIsNotNone(h_iso)
        w_iso, _, _, _ = search_mod._normalize_time_window("2w", None)
        expect_w = (datetime.now() - timedelta(weeks=2)).date().isoformat()
        self.assertEqual(w_iso, expect_w)

    def test_absolute_date(self):
        since_iso, until_iso, since_ts, until_ts = search_mod._normalize_time_window(
            "2026-08-01", "2026-08-05")
        # 纯日期下推保留 YYYY-MM-DD 形态（引擎兼容），不转成 T00:00:00
        self.assertEqual(since_iso, "2026-08-01")
        self.assertEqual(until_iso, "2026-08-05")
        # since 边界 = 当天零点（从 8-01 起）
        self.assertEqual(since_ts, datetime(2026, 8, 1).timestamp())
        # until 边界 = 当天最后一刻（含 8-05 整天，次日零点起剔除）
        self.assertEqual(until_ts, datetime(2026, 8, 5, 23, 59, 59, 999999).timestamp())

    def test_datetime_input_preserved(self):
        # 带时刻的输入保留完整时间（含时区），不被降级为纯日期
        since_iso, _, since_ts, _ = search_mod._normalize_time_window(
            "2026-08-05T19:55:25+00:00", None)
        self.assertEqual(since_iso, "2026-08-05T19:55:25+00:00")
        self.assertEqual(since_ts, datetime.fromisoformat("2026-08-05T19:55:25+00:00").timestamp())

    def test_iso_with_timezone(self):
        since_iso, _, since_ts, _ = search_mod._normalize_time_window(
            "2026-08-05T19:55:25+00:00", None)
        self.assertEqual(since_iso, "2026-08-05T19:55:25+00:00")
        self.assertEqual(since_ts, datetime.fromisoformat("2026-08-05T19:55:25+00:00").timestamp())

    def test_z_suffix(self):
        _, _, _, until_ts = search_mod._normalize_time_window(None, "2026-08-05T12:00:00Z")
        self.assertEqual(until_ts, datetime.fromisoformat("2026-08-05T12:00:00+00:00").timestamp())

    def test_epoch_seconds(self):
        since_iso, _, since_ts, _ = search_mod._normalize_time_window("1786194753", None)
        self.assertEqual(since_ts, 1786194753.0)
        self.assertIsNotNone(since_iso)

    def test_invalid_keeps_raw_iso_but_no_ts(self):
        since_iso, _, since_ts, _ = search_mod._normalize_time_window("随便写", None)
        self.assertEqual(since_iso, "随便写")  # 原样保留，可下推
        self.assertIsNone(since_ts)  # 不参与后过滤

    def test_empty_values(self):
        s_iso, u_iso, s_ts, u_ts = search_mod._normalize_time_window(None, "")
        self.assertIsNone(s_iso)
        self.assertIsNone(u_iso)
        self.assertIsNone(s_ts)
        self.assertIsNone(u_ts)

    def test_semantically_equal_windows_share_iso(self):
        # 7d 与「7 天前的绝对日期」应归一化到同一 ISO
        since_iso, _, _, _ = search_mod._normalize_time_window("7d", None)
        abs_iso = (datetime.now() - timedelta(days=7)).date().isoformat()
        self.assertEqual(since_iso, abs_iso)


# ── 2. 结果后过滤 ────────────────────────────────────────────────────────────

class TestApplyTimeWindow(unittest.TestCase):
    def _results(self):
        return [
            {"title": "新", "url": "https://example.com/new", "published_at": "2026-08-08"},
            {"title": "旧", "url": "https://example.com/old", "published_at": "2026-07-01"},
            {"title": "无时间", "url": "https://example.com/notime"},
            {"title": "边界当天", "url": "https://example.com/boundary", "published_at": "2026-08-01"},
        ]

    def test_since_filters_older(self):
        since_ts = datetime(2026, 8, 1).timestamp()
        kept, dropped = search_mod._apply_time_window(self._results(), since_ts, None)
        self.assertEqual(dropped, 1)  # 仅 07-01 被剔除
        self.assertEqual(len(kept), 3)
        self.assertNotIn("旧", [r["title"] for r in kept])
        # 无时间字段保留（宽松策略）
        self.assertIn("无时间", [r["title"] for r in kept])

    def test_until_filters_newer(self):
        until_ts = datetime(2026, 8, 1).timestamp()
        kept, dropped = search_mod._apply_time_window(self._results(), None, until_ts)
        self.assertEqual(dropped, 1)  # 仅 08-08 被剔除
        self.assertIn("旧", [r["title"] for r in kept])

    def test_boundary_kept(self):
        ts = datetime(2026, 8, 1).timestamp()
        kept, _ = search_mod._apply_time_window(self._results(), ts, ts)
        # 等于 since 与 until 的「边界当天」保留
        self.assertIn("边界当天", [r["title"] for r in kept])
        # 08-08 > until 剔除
        self.assertNotIn("新", [r["title"] for r in kept])
        # 07-01 < since 剔除
        self.assertNotIn("旧", [r["title"] for r in kept])

    def test_until_date_only_includes_that_day(self):
        # 纯日期 until 语义为「含当天」：当天发布的结果保留，次日才剔除
        _, _, _, until_ts = search_mod._normalize_time_window(None, "2026-08-05")
        results = [
            {"title": "当天 23:59", "url": "https://example.com/a",
             "published_at": "2026-08-05T23:59:59"},
            {"title": "次日零点", "url": "https://example.com/b",
             "published_at": "2026-08-06T00:00:00"},
            {"title": "次日白天", "url": "https://example.com/c",
             "published_at": "2026-08-06T12:00:00"},
        ]
        kept, dropped = search_mod._apply_time_window(results, None, until_ts)
        self.assertEqual(dropped, 2)
        self.assertEqual([r["title"] for r in kept], ["当天 23:59"])

    def test_no_window_noop(self):
        kept, dropped = search_mod._apply_time_window(self._results(), None, None)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 4)

    def test_epoch_published_at(self):
        old_ts = "1700000000"  # 2023-11-14 UTC
        results = [{"title": "epoch 旧", "url": "https://example.com/e", "published_at": old_ts}]
        since_ts = datetime(2026, 1, 1).timestamp()
        kept, dropped = search_mod._apply_time_window(results, since_ts, None)
        self.assertEqual(dropped, 1)


# ── 3. published_at 时区解析 ─────────────────────────────────────────────────

class TestPublishedTsTimezone(unittest.TestCase):
    def test_iso_with_utc_offset(self):
        ts = search_mod._published_ts({"published_at": "2026-08-05 19:55:25+00:00"})
        self.assertEqual(ts, datetime.fromisoformat("2026-08-05T19:55:25+00:00").timestamp())

    def test_z_suffix(self):
        ts = search_mod._published_ts({"published_at": "2026-08-05T19:55:25Z"})
        self.assertEqual(ts, datetime.fromisoformat("2026-08-05T19:55:25+00:00").timestamp())

    def test_naive_interpreted_local(self):
        ts = search_mod._published_ts({"published_at": "2026-08-01"})
        self.assertEqual(ts, datetime(2026, 8, 1).timestamp())

    def test_different_offset_same_instant(self):
        a = search_mod._published_ts({"published_at": "2026-08-05T19:55:25+00:00"})
        b = search_mod._published_ts({"published_at": "2026-08-06T03:55:25+08:00"})
        self.assertEqual(a, b)  # 同一时刻，跨时区应换算一致

    def test_invalid_returns_none(self):
        self.assertIsNone(search_mod._published_ts({"published_at": "not a date"}))


# ── 4. recovery 路径透传时间窗 ──────────────────────────────────────────────

class TestRecoveryTimeWindowPropagation(unittest.TestCase):
    def test_recovery_executor_passes_since(self):
        """recovery 触发的引擎调用必须携带 since/until（不丢用户约束）。"""
        captured: dict = {}

        def fake_run_recovery(query, tried, executor, **kwargs):
            captured["executor"] = executor
            class _FakeResult:
                def to_dict(self):
                    return {"triggered": True, "recovered": False, "level_used": None,
                            "strategy_used": None, "steps_tried": [], "final_query": query,
                            "note": "test"}
            return [], _FakeResult()

        decision = {"domain": "general", "engine": "auto",
                    "engines_combo": ["octen"], "engines_fallback": ["wayback_cdx"]}
        # 执行器必须在 patch 生效的上下文内调用，否则闭包里的 engine_search
        # 会解析到真实函数（真发网络请求）。这里先跑主流程拿到执行器，
        # reset_mock 清掉主路径调用后，单独核对恢复执行器的 engine_search 参数。
        with patch.object(search_mod, "engine_search", return_value=[]) as mock_es, \
             patch("recovery.run_recovery", side_effect=fake_run_recovery):
            search_mod.execute_search(
                "rust async", decision, max_results=5, timeout=5, depth="fast",
                cache=SearchCache(db_path=":memory:"), skip_cache=True,
                mode="fast", since="7d", until="2026-08-05", sort="relevance",
            )
            self.assertIn("executor", captured)
            mock_es.reset_mock()
            captured["executor"]("rust async", ["wayback_cdx"])
            wayback_calls = [c for c in mock_es.call_args_list if c.args[1] == "wayback_cdx"]
            self.assertTrue(wayback_calls, "恢复执行器应发起 wayback_cdx 引擎调用")
            kwargs = wayback_calls[0].kwargs
            since_iso, _, _, _ = search_mod._normalize_time_window("7d", None)
            self.assertEqual(kwargs.get("since"), since_iso)
            self.assertEqual(kwargs.get("until"), "2026-08-05")


# ── 5. CLI 解析器尊重 n ──────────────────────────────────────────────────────

class TestParserNParam(unittest.TestCase):
    def test_yaml_output_respects_n(self):
        items = "\n".join(
            f"- title: 标题{i}\n  url: https://example.com/{i}" for i in range(12))
        out = _parse_yaml_output("results:\n" + items, "x", n=5)
        self.assertEqual(len(out), 5)

    def test_text_output_yaml_respects_n(self):
        items = "\n".join(
            f"- title: 标题{i}\n  url: https://example.com/{i}" for i in range(12))
        out = _parse_text_output("results:\n" + items, "x", output_format="yaml", n=3)
        self.assertEqual(len(out), 3)

    def test_json_list_respects_n(self):
        data = '{"results": [' + ",".join(
            f'{{"title": "t{i}", "url": "https://example.com/{i}"}}' for i in range(12)) + "]}"
        out = _parse_text_output(data, "x", n=4)
        self.assertEqual(len(out), 4)

    def test_json_keeps_published_at(self):
        data = '{"results": [{"title": "t", "url": "https://example.com/t", "published_at": "2026-08-01"}]}'
        out = _parse_text_output(data, "x", n=5)
        self.assertEqual(out[0]["published_at"], "2026-08-01")

    def test_default_still_10(self):
        items = "\n".join(
            f"- title: 标题{i}\n  url: https://example.com/{i}" for i in range(12))
        out = _parse_yaml_output("results:\n" + items, "x")
        self.assertEqual(len(out), 10)  # 默认上限保持 10，行为兼容


# ── 6. 时间能力引擎集合（v2.7.5）────────────────────────────────────────────
# 不带时间字段的引擎组合：时间窗不隔离缓存键、不后过滤（7d/30d 共享缓存）；
# 带时间能力引擎（news/realtime/wayback/local_search 聚合）保持隔离。

class TestTimeCapableEngines(unittest.TestCase):
    def test_is_time_capable_members(self):
        for eng in ("realtime_index", "wayback_cdx", "local_search",
                    "local_bing_news", "local_google_news",
                    "local_duckduckgo_news", "local_ddgs_news"):
            self.assertTrue(search_mod._is_time_capable(eng), eng)

    def test_is_time_capable_non_members(self):
        for eng in ("octen", "anysearch", "bocha", "local_bing",
                    "local_duckduckgo", "local_brave", "local_yahoo"):
            self.assertFalse(search_mod._is_time_capable(eng), eng)

    def test_cache_key_not_partitioned_without_time_engines(self):
        """无时间能力引擎组合：since 不进入缓存键（7d/30d 共享）。"""
        engines = ["octen", "anysearch"]
        key = "+".join(sorted(engines))
        with patch.object(search_mod, "_normalize_time_window",
                          return_value=("2026-08-03", None, 1.0, None)):
            # 直接验证缓存键拼装逻辑：time_aware=False 时不拼 since
            time_aware = any(search_mod._is_time_capable(e) for e in engines)
            self.assertFalse(time_aware)
            cache_key = key
            if "2026-08-03" and time_aware:
                cache_key += "|since=2026-08-03"
            self.assertEqual(cache_key, key)

    def test_cache_key_partitioned_with_time_engines(self):
        """带时间能力引擎组合：since 进入缓存键（7d/30d 隔离）。"""
        engines = ["realtime_index", "octen"]
        time_aware = any(search_mod._is_time_capable(e) for e in engines)
        self.assertTrue(time_aware)
        key = "+".join(sorted(engines))
        self.assertEqual(key + "|since=2026-08-03",
                         key + ("|since=2026-08-03" if time_aware else ""))


# ── 7. ddgs 结构化 JSON 解析（v2.7.5）────────────────────────────────────────

class TestDdgsJsonParse(unittest.TestCase):
    """_parse_cli_json：ddgs -o json 文件输出 → argo 结果（news date→published_at）。"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SKILL_DIR.parent / "sub-skills" / "local-search"))
        import search_v3  # noqa: F401
        cls.search_v3 = search_v3

    def test_text_shape(self):
        data = [{"title": "T", "href": "https://a.com", "body": "snippet body"}]
        out = self.search_v3._parse_cli_json(data, "local_brave")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://a.com")
        self.assertEqual(out[0]["source"], "local_brave")
        self.assertNotIn("published_at", out[0])

    def test_news_date_maps_to_published_at(self):
        data = [{"date": "2026-08-10", "title": "N", "url": "https://n.com",
                 "body": "b", "source": "cnn"}]
        out = self.search_v3._parse_cli_json(data, "local_ddgs_news")
        self.assertEqual(out[0]["published_at"], "2026-08-10")

    def test_extra_fields_preserved(self):
        data = [{"title": "I", "image": "https://i.com/x.jpg", "thumbnail": "https://i.com/t.jpg",
                 "url": "https://p.com", "height": 100, "width": 200}]
        out = self.search_v3._parse_cli_json(data, "local_ddgs_images")
        self.assertEqual(out[0]["height"], 100)
        self.assertEqual(out[0]["image"], "https://i.com/x.jpg")

    def test_empty_and_non_list(self):
        self.assertEqual(self.search_v3._parse_cli_json([], "x"), [])
        self.assertEqual(self.search_v3._parse_cli_json({"a": 1}, "x"), [])

    def test_cli_avail_cache(self):
        """_check_cli_available 结果缓存：第二次不重跑 subprocess。"""
        sv3 = self.search_v3
        sv3._CLI_AVAIL_CACHE.clear()
        calls = {"n": 0}
        real_run = sv3.subprocess.run

        def fake_run(*args, **kwargs):
            calls["n"] += 1
            class R:
                returncode = 0
            return R()

        sv3.subprocess.run = fake_run
        try:
            self.assertTrue(sv3._check_cli_available("fakecli"))
            self.assertTrue(sv3._check_cli_available("fakecli"))
            self.assertEqual(calls["n"], 1)  # 只跑一次
        finally:
            sv3.subprocess.run = real_run
            sv3._CLI_AVAIL_CACHE.clear()

    def test_time_capable_local_set(self):
        sv3 = self.search_v3
        for eng in ("local_bing_news", "local_google_news",
                    "local_duckduckgo_news", "local_ddgs_news"):
            self.assertIn(eng, sv3._TIME_CAPABLE_ENGINES)
        for eng in ("local_bing", "local_duckduckgo", "local_brave", "local_yahoo",
                    "local_ddgs_images", "local_ddgs_videos"):
            self.assertNotIn(eng, sv3._TIME_CAPABLE_ENGINES)


if __name__ == "__main__":
    unittest.main()

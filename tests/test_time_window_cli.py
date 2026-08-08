#!/usr/bin/env python3
"""CLI 桥接引擎回归测试 — YAML 解析 / filter_args 时间窗 / 裸命令路径校验 / 缓存隔离。

覆盖：
  1. _parse_yaml_output：结构化 YAML 解析、字段映射、published_at 保留（离线）
  2. cli builder filter_args：since/until 条件拼参（mock _run，离线）
  3. config._validate_engine_paths：PATH 裸命令不再误禁（离线）
  4. SearchCache per-engine 缓存：时间窗并入 key，不同窗口不串缓存（离线）

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_time_window_cli.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from engines_base import (  # noqa: E402
    _build_cli_engine,
    _parse_yaml_output,
)
from cache import SearchCache  # noqa: E402
import config as config_mod  # noqa: E402


# ── 1. YAML 解析 ──────────────────────────────────────────────────────────────

class TestParseYamlOutput(unittest.TestCase):
    def test_dict_results_with_published_at(self):
        text = """
query: test
results:
  - title: 文章甲
    url: https://example.com/a
    snippet: 摘要甲
    published_at: 2026-08-05 19:55:25+00:00
  - title: 文章乙
    link: https://example.com/b
    description: 摘要乙
"""
        out = _parse_yaml_output(text, "realtime_index")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "文章甲")
        self.assertEqual(out[0]["url"], "https://example.com/a")
        self.assertEqual(out[0]["snippet"], "摘要甲")
        self.assertEqual(out[0]["published_at"], "2026-08-05 19:55:25+00:00")
        self.assertEqual(out[0]["source"], "realtime_index")
        # 别名映射
        self.assertEqual(out[1]["url"], "https://example.com/b")
        self.assertEqual(out[1]["snippet"], "摘要乙")

    def test_top_level_list(self):
        text = """
- name: 标题一
  url: https://example.com/1
- name: 标题二
  url: https://example.com/2
"""
        out = _parse_yaml_output(text, "realtime_index")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "标题一")

    def test_items_key(self):
        text = "items:\n- title: t\n  url: https://example.com/t\n"
        out = _parse_yaml_output(text, "realtime_index")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "t")

    def test_no_title_no_url_skipped(self):
        text = "results:\n- snippet: 只有摘要\n- title: ok\n  url: https://example.com/ok\n"
        out = _parse_yaml_output(text, "realtime_index")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "ok")

    def test_truncation_and_cap(self):
        items = "\n".join(
            f"- title: {'x' * 300}\n  url: https://example.com/{i}\n  snippet: {'y' * 400}" for i in range(12)
        )
        out = _parse_yaml_output("results:\n" + items, "realtime_index")
        self.assertEqual(len(out), 10)
        self.assertEqual(len(out[0]["title"]), 200)
        self.assertEqual(len(out[0]["snippet"]), 300)

    def test_invalid_yaml_returns_empty(self):
        self.assertEqual(_parse_yaml_output("not: [valid", "realtime_index"), [])

    def test_empty_and_blank(self):
        self.assertEqual(_parse_yaml_output("", "realtime_index"), [])


# ── 2. cli builder filter_args ────────────────────────────────────────────────

class TestCliFilterArgs(unittest.TestCase):
    SPEC = {
        "_name": "realtime_index",
        "cmd": ["keenable"],
        "search_args": ["search", "{query}"],
        "output_format": "yaml",
        "filter_args": {
            "since": ["--published-after", "{since}"],
            "until": ["--published-before", "{until}"],
        },
    }
    YAML = "results:\n- title: t\n  url: https://example.com/t\n"

    def _build(self):
        return _build_cli_engine(dict(self.SPEC))

    def test_since_until_appended(self):
        fn = self._build()
        with patch("engines_base._run", return_value=self.YAML) as mock_run:
            fn("rust async", 5, 8, mode="fast", since="2026-08-01", until="2026-08-05")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:3], ["keenable", "search", "rust async"])
        self.assertEqual(cmd[3:], ["--published-after", "2026-08-01", "--published-before", "2026-08-05"])

    def test_no_window_no_filter_args(self):
        fn = self._build()
        with patch("engines_base._run", return_value=self.YAML) as mock_run:
            fn("rust async", 5, 8, mode="fast")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["keenable", "search", "rust async"])

    def test_only_since(self):
        fn = self._build()
        with patch("engines_base._run", return_value=self.YAML) as mock_run:
            fn("rust async", 5, 8, mode="fast", since="7d")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["keenable", "search", "rust async", "--published-after", "7d"])

    def test_empty_since_not_appended(self):
        fn = self._build()
        with patch("engines_base._run", return_value=self.YAML) as mock_run:
            fn("rust async", 5, 8, mode="fast", since="", until="")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["keenable", "search", "rust async"])


# ── 3. 裸命令路径校验 ─────────────────────────────────────────────────────────

class TestValidateEnginePaths(unittest.TestCase):
    def _specs(self):
        return {
            "realtime_index": {
                "enabled": True, "type": "cli", "cmd": ["keenable"],
            },
            "missing_path": {
                "enabled": True, "type": "cli",
                "cmd": ["python3", "/no/such/script.py"],
            },
            "http_engine": {"enabled": True, "type": "http", "url": "https://example.com"},
        }

    def test_bare_command_in_path_stays_enabled(self):
        cfg = {"engines": self._specs()}
        with patch.object(config_mod.shutil, "which", return_value="/opt/homebrew/bin/keenable"):
            config_mod._validate_engine_paths(cfg)
        self.assertTrue(cfg["engines"]["realtime_index"]["enabled"])

    def test_bare_command_not_in_path_disabled(self):
        cfg = {"engines": self._specs()}
        with patch.object(config_mod.shutil, "which", return_value=None):
            config_mod._validate_engine_paths(cfg)
        self.assertFalse(cfg["engines"]["realtime_index"]["enabled"])

    def test_missing_absolute_path_disabled(self):
        cfg = {"engines": self._specs()}
        with patch.object(config_mod.shutil, "which", return_value=None):
            config_mod._validate_engine_paths(cfg)
        self.assertFalse(cfg["engines"]["missing_path"]["enabled"])

    def test_existing_absolute_path_stays_enabled(self):
        cfg = {"engines": self._specs()}
        cfg["engines"]["missing_path"]["cmd"] = [sys.executable, __file__]
        with patch.object(config_mod.shutil, "which", return_value=None):
            config_mod._validate_engine_paths(cfg)
        self.assertTrue(cfg["engines"]["missing_path"]["enabled"])

    def test_http_engine_untouched(self):
        cfg = {"engines": self._specs()}
        with patch.object(config_mod.shutil, "which", return_value=None):
            config_mod._validate_engine_paths(cfg)
        self.assertTrue(cfg["engines"]["http_engine"]["enabled"])


# ── 4. per-engine 缓存时间窗隔离 ─────────────────────────────────────────────

class TestCacheTimeWindowIsolation(unittest.TestCase):
    def setUp(self):
        self.cache = SearchCache(db_path=":memory:")

    def test_key_differs_by_window(self):
        base = SearchCache._key("rust async", "realtime_index", 5, kind="engine")
        with_since = SearchCache._key("rust async", "realtime_index", 5, kind="engine", since="7d")
        with_until = SearchCache._key("rust async", "realtime_index", 5, kind="engine", until="2026-08-05")
        with_both = SearchCache._key("rust async", "realtime_index", 5, kind="engine",
                                     since="2026-08-01", until="2026-08-05")
        self.assertNotEqual(base, with_since)
        self.assertNotEqual(base, with_until)
        self.assertNotEqual(with_since, with_both)
        # 同参幂等
        again = SearchCache._key("rust async", "realtime_index", 5, kind="engine", since="7d")
        self.assertEqual(with_since, again)

    def test_get_engine_window_isolated(self):
        results = [{"title": "t", "url": "https://example.com/t", "source": "realtime_index"}]
        self.cache.set_engine("rust async", "realtime_index", 5, results, since="7d")
        # 同窗口命中
        hit = self.cache.get_engine("rust async", "realtime_index", 5, since="7d")
        self.assertEqual(len(hit), 1)
        # 无时间窗不串
        self.assertIsNone(self.cache.get_engine("rust async", "realtime_index", 5))
        # 不同窗口不串
        self.assertIsNone(self.cache.get_engine("rust async", "realtime_index", 5,
                                                since="2026-08-01", until="2026-08-05"))


if __name__ == "__main__":
    unittest.main()

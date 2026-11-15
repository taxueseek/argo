#!/usr/bin/env python3
"""FxTwitter 接入测试 — 声明式引擎注册 + 解析器阈值 + live 连通（可 skip）。

`sources` 已删（2026-11-15）：旧 `social_engines/twitter_engine.py` 的搜索通道
三处全失效、零生产调用者，`twitter` 引擎自 2026-10-21 改走 `scripts/x_search.py`
（见 engines/specs/twitter.yaml 头注）。保留本文件守两件事：
  1. fxtwitter / twitter 两个声明式引擎仍注册可用；
  2. 搜索侧 `min_digits=10` 阈值仍在世——`argo search "20"` 的 20 是页码/数量，
     不该被当成推文 ID（解析器唯一实现在 fxtwitter_api.extract_status_id）。

运行：
  python3 -m pytest tests/test_fxtwitter.py -v
  python3 tests/test_fxtwitter.py
  # 含真实 API：
  ARGO_LIVE=1 python3 tests/test_fxtwitter.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from config import load_config, get_engines  # noqa: E402
from fxtwitter_api import extract_status_id  # noqa: E402

LIVE = os.environ.get("ARGO_LIVE", "").strip() in {"1", "true", "yes"}


class TestFxTwitterRegistration(unittest.TestCase):
    def test_fxtwitter_in_config(self):
        load_config(force=True)
        engines = get_engines()
        self.assertIn("fxtwitter", engines, "fxtwitter 未注册（检查 engines/specs/fxtwitter.yaml）")
        spec = engines["fxtwitter"]
        self.assertTrue(spec.get("enabled", True))
        self.assertIn("api.fxtwitter.com", spec.get("url", ""))

    def test_twitter_enabled(self):
        load_config(force=True)
        engines = get_engines()
        self.assertIn("twitter", engines)
        self.assertTrue(engines["twitter"].get("enabled", True))

    def test_twitter_engine_uses_x_search_not_legacy_script(self):
        """twitter 引擎不得再指向已删的 social_engines/twitter_engine.py。"""
        load_config(force=True)
        spec = get_engines()["twitter"]
        cmd = " ".join(str(c) for c in (spec.get("cmd") or []))
        self.assertIn("x_search.py", cmd)
        self.assertNotIn("twitter_engine", cmd)


class TestSearchSideStatusIdThreshold(unittest.TestCase):
    """搜索侧阈值 min_digits=10：短数字是页码/数量，不是推文 ID。"""

    def test_extract_status_id_from_url(self):
        self.assertEqual(
            extract_status_id("https://x.com/jack/status/20", min_digits=10),
            "20",
        )
        self.assertEqual(
            extract_status_id(
                "https://twitter.com/jack/status/1548602399862013953", min_digits=10),
            "1548602399862013953",
        )

    def test_short_digits_rejected_on_search_side(self):
        # `argo search "20"` 的 20 不该被当成推文 ID
        self.assertIsNone(extract_status_id("20", min_digits=10))
        self.assertIsNone(extract_status_id("OpenAI research", min_digits=10))

    def test_explicit_id_still_accepted_by_packager_side(self):
        # `argo tweet "20"` 是用户显式给的 ID，min_digits=1 必须认
        self.assertEqual(extract_status_id("20"), "20")


class TestFxTwitterRichFieldMapping(unittest.TestCase):
    """上游富字段必须落进结果（2026-11-15 补映射后锁死）。

    此前 spec 只映射 text/url，导致结果里没有 published_at、source 是引擎名
    而非作者、互动量全部丢失——时间线排序与「谁发的」都拿不到。
    """

    def _rows(self, payload):
        import engines_base
        spec = {
            "_name": "fxtwitter",
            "type": "http",
            "method": "GET",
            "url": "https://api.fxtwitter.com/2/search",
            "query_param": "q",
            "preserve_source": True,
            "output_map": {
                "items": "results",
                "item_title": "text",
                "item_url": "url",
                "item_summary": "text",
                "item_published_at": "created_timestamp",
                "item_upstream_authority": "likes",
                "item_upstream_score": "views",
                "item_source": "author.screen_name",
            },
        }
        body = json.dumps(payload)

        def fake_get(url, headers, timeout, engine="?", tls_profiles=None):
            return body

        with patch.object(engines_base, "_http_get_raw", side_effect=fake_get):
            return engines_base._build_http_engine(spec)("x", n=5, timeout=10)

    def test_author_beats_engine_name_as_source(self):
        rows = self._rows({"results": [{
            "text": "hello", "url": "https://x.com/alice/status/1",
            "created_timestamp": 1794736508, "likes": 7, "views": 42,
            "author": {"screen_name": "alice"},
        }]})
        self.assertEqual(rows[0]["source"], "alice",
                         "preserve_source 未生效，作者名被纠正回引擎名")

    def test_published_at_and_interactions_present(self):
        rows = self._rows({"results": [{
            "text": "hello", "url": "https://x.com/alice/status/1",
            "created_timestamp": 1794736508, "likes": 7, "views": 42,
            "author": {"screen_name": "alice"},
        }]})
        self.assertEqual(rows[0]["published_at"], "2026-11-15")
        self.assertEqual(rows[0]["upstream_authority"], "7")
        self.assertEqual(rows[0]["upstream_score"], "42")

    def test_zero_interactions_not_dropped(self):
        """点赞为 0 是真实值，不得因 falsy 判据被丢掉（否则零互动帖永远无信号）。"""
        rows = self._rows({"results": [{
            "text": "hello", "url": "https://x.com/bob/status/2",
            "created_timestamp": 1794736508, "likes": 0, "views": 0,
            "author": {"screen_name": "bob"},
        }]})
        self.assertEqual(rows[0]["upstream_authority"], "0")
        self.assertEqual(rows[0]["upstream_score"], "0")


@unittest.skipUnless(LIVE, "set ARGO_LIVE=1 for live FxTwitter calls")
class TestFxTwitterLive(unittest.TestCase):
    def test_live_engine_search_twitter(self):
        from engines import search as engine_search

        results = engine_search("OpenAI", "twitter", n=3, timeout=15)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)

    def test_live_engine_search_fxtwitter(self):
        from engines import search as engine_search

        results = engine_search("OpenAI", "fxtwitter", n=3, timeout=15)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        self.assertTrue(results[0].get("title") or results[0].get("url"))


def main():
    # 默认跑离线；ARGO_LIVE=1 含 live
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    # 额外：标准化 validate（若脚本存在）
    validate = ROOT / "scripts" / "engine_validate.py"
    if validate.exists() and LIVE:
        import subprocess

        for eng in ("fxtwitter", "twitter"):
            print(f"\n--- engine_validate {eng} ---")
            subprocess.run(
                [sys.executable, str(validate), "--engine", eng, "--stage", "health"],
                cwd=str(ROOT),
            )
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()

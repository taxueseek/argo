#!/usr/bin/env python3
"""FxTwitter 接入测试 — 离线解析 + 配置注册 + live 连通（可 skip）。

运行：
  cd ~/.workbuddy/skills/argo
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
sys.path.insert(0, str(SCRIPT_DIR / "social_engines"))

from social_engines import twitter_engine as tw  # noqa: E402
from config import load_config, get_engines  # noqa: E402

LIVE = os.environ.get("ARGO_LIVE", "").strip() in {"1", "true", "yes"}


SAMPLE_SEARCH = {
    "code": 200,
    "results": [
        {
            "type": "status",
            "url": "https://x.com/jack/status/20",
            "id": "20",
            "text": "just setting up my twttr",
            "author": {
                "screen_name": "jack",
                "name": "jack",
            },
            "likes": 100,
            "reposts": 50,
            "replies": 10,
            "views": 1000,
            "created_at": "Tue Mar 21 20:50:14 +0000 2006",
            "lang": "en",
        },
        {
            "type": "status",
            "url": "https://x.com/demo/status/21",
            "id": "21",
            "text": "second tweet about Python asyncio",
            "author": {"screen_name": "demo", "name": "Demo"},
            "likes": 3,
            "reposts": 1,
            "replies": 0,
        },
    ],
    "cursor": {"bottom": "x"},
}


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


class TestFxTwitterParsers(unittest.TestCase):
    def test_status_to_result_schema(self):
        item = SAMPLE_SEARCH["results"][0]
        r = tw._status_to_result(item, rank=0)
        self.assertIsNotNone(r)
        assert r is not None
        for key in ("title", "url", "snippet", "source", "score", "social_meta"):
            self.assertIn(key, r)
        self.assertEqual(r["source"], "twitter")
        self.assertTrue(r["url"].startswith("http"))
        self.assertIn("just setting up", r["snippet"])
        meta = r["social_meta"]
        self.assertEqual(meta["platform"], "twitter")
        self.assertEqual(meta["author"], "jack")
        self.assertEqual(meta["likes"], 100)
        self.assertEqual(meta["provider"], "fxtwitter")

    def test_extract_status_id_from_url(self):
        self.assertEqual(
            tw._extract_status_id("https://x.com/jack/status/20"),
            "20",
        )
        self.assertEqual(
            tw._extract_status_id("https://twitter.com/jack/status/1548602399862013953"),
            "1548602399862013953",
        )
        self.assertEqual(tw._extract_status_id("20"), None)  # too short (<10)
        self.assertEqual(tw._extract_status_id("1548602399862013953"), "1548602399862013953")
        self.assertIsNone(tw._extract_status_id("OpenAI research"))

    def test_search_fxtwitter_parses_mock(self):
        body = json.dumps(SAMPLE_SEARCH).encode("utf-8")

        def fake_get(url, headers=None, timeout=10, max_retries=2):
            return body, 200

        with patch.object(tw, "_http_get_with_retry", side_effect=fake_get):
            results = tw.search_fxtwitter("twttr", n=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["social_meta"]["author"], "jack")
        self.assertEqual(results[0]["source"], "twitter")
        self.assertTrue(results[0]["title"])

    def test_search_routes_to_status_for_url(self):
        status_payload = {
            "code": 200,
            "status": SAMPLE_SEARCH["results"][0],
        }
        body = json.dumps(status_payload).encode("utf-8")

        def fake_get(url, headers=None, timeout=10, max_retries=2):
            self.assertIn("/status/", url)
            return body, 200

        with patch.object(tw, "_http_get_with_retry", side_effect=fake_get):
            results = tw.search_fxtwitter("https://x.com/jack/status/20", n=3)
        self.assertEqual(len(results), 1)
        self.assertIn("twttr", results[0]["snippet"])


@unittest.skipUnless(LIVE, "set ARGO_LIVE=1 for live FxTwitter calls")
class TestFxTwitterLive(unittest.TestCase):
    def test_live_search(self):
        results = tw.search_fxtwitter("OpenAI", n=3, timeout=12)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0, "live search 空结果")
        r = results[0]
        self.assertTrue(r.get("title") or r.get("url"))
        self.assertEqual(r.get("source"), "twitter")
        self.assertEqual((r.get("social_meta") or {}).get("provider"), "fxtwitter")

    def test_live_status(self):
        results = tw.fetch_status("20", timeout=12)
        self.assertEqual(len(results), 1)
        self.assertIn("twttr", results[0]["snippet"].lower())

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

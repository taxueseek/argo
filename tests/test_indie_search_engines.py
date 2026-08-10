#!/usr/bin/env python3
"""独立索引引擎测试 — Marginalia / Wiby。

覆盖：
  1. 解析（离线 mock，必过）：字段映射、大写键、坏数据容错
  2. 注册（离线）：config.yaml 声明 + builder 注册一致性
  3. 真实调用（网络依赖，可 skip）

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_indie_search_engines.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from engines_builders_data import (  # noqa: E402
    _build_marginalia_engine,
    _build_wiby_engine,
)


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestMarginaliaParse(unittest.TestCase):
    def setUp(self):
        self.engine = _build_marginalia_engine({"timeout": 8})

    def test_basic_shape(self):
        payload = {
            "query": "python",
            "results": [
                {"title": "T1", "url": "https://a.example/1", "description": "desc one",
                 "quality": "3.0", "format": "html"},
                {"title": "T2", "url": "https://b.example/2", "description": "desc two"},
            ],
        }
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(payload).encode())):
            res = self.engine("python", 3)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["title"], "T1")
        self.assertEqual(res[0]["url"], "https://a.example/1")
        self.assertEqual(res[0]["snippet"], "desc one")
        self.assertEqual(res[0]["source"], "marginalia")

    def test_n_respects_limit(self):
        payload = {"results": [{"title": f"T{i}", "url": f"https://x/{i}"} for i in range(5)]}
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(payload).encode())):
            res = self.engine("q", 2)
        self.assertEqual(len(res), 2)

    def test_empty_and_missing_fields(self):
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps({"results": []}).encode())):
            self.assertEqual(self.engine("q", 3), [])
        payload = {"results": [{"url": "https://only-url"}]}
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(payload).encode())):
            res = self.engine("q", 3)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "")

    def test_network_error_returns_empty(self):
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertEqual(self.engine("q", 3), [])


class TestWibyParse(unittest.TestCase):
    def setUp(self):
        self.engine = _build_wiby_engine({"timeout": 8})

    def test_uppercase_keys(self):
        payload = [
            {"URL": "https://a.example/1", "Title": "A Page", "Snippet": "snip", "Description": "desc"},
            {"URL": "https://b.example/2", "Title": "B Page", "Snippet": "snip2"},
        ]
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(payload).encode())):
            res = self.engine("q", 3)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["title"], "A Page")
        self.assertEqual(res[0]["url"], "https://a.example/1")
        self.assertEqual(res[0]["snippet"], "snip")

    def test_mixed_and_bad_items(self):
        payload = [
            {"URL": "https://c.example/3", "Title": "C"},
            "not a dict",
            {"URL": "https://d.example/4", "Title": "D"},
        ]
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(payload).encode())):
            res = self.engine("q", 5)
        self.assertEqual(len(res), 2)  # 非 dict 元素跳过

    def test_non_list_response(self):
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps({"a": 1}).encode())):
            self.assertEqual(self.engine("q", 3), [])

    def test_network_error_returns_empty(self):
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertEqual(self.engine("q", 3), [])


class TestIndieRegistry(unittest.TestCase):
    def test_registered_in_builders(self):
        from engines_builders import _build_marginalia_engine as bm, _build_wiby_engine as bw  # noqa: F401
        from engines import get_registry
        reg = get_registry()
        self.assertIn("marginalia", reg)
        self.assertIn("wiby", reg)

    def test_config_declared(self):
        from config import load_config
        cfg = load_config()
        for name in ("marginalia", "wiby"):
            spec = cfg.get("engines", {}).get(name)
            self.assertIsNotNone(spec, f"{name} 未在 config.yaml 声明")
            self.assertTrue(spec.get("enabled"))
            self.assertEqual(spec.get("cost_tier"), "free")


if __name__ == "__main__":
    unittest.main()

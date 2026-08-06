#!/usr/bin/env python3
"""回归测试 — health_probe 只探测 local_* 子引擎。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


class TestHealthProbeScope(unittest.TestCase):
    def test_probe_all_engines_only_local(self):
        import health_probe

        probed: list[str] = []

        def fake_probe_http(url: str, timeout: float = 1.5):
            probed.append(url)
            return True, 1.0, ""

        fake_config = {
            "engines": {
                "local_bing": {"type": "http", "url": "https://www.bing.com/"},
                "local_baidu": {"type": "http", "url": "https://www.baidu.com/"},
                "wikipedia": {"type": "http", "url": "https://www.wikipedia.org/"},
                "arxiv": {"type": "http", "url": "https://export.arxiv.org/"},
                "github": {"type": "http", "url": "https://api.github.com/"},
            }
        }

        with patch("health_probe._probe_http", side_effect=fake_probe_http), \
             patch("health_probe.load_config", return_value=fake_config), \
             patch("health_probe.get_engines",
                   side_effect=lambda cfg: cfg["engines"]):
            results = health_probe.probe_all_engines()

        # 只探测 local_*：2 个 URL，非 local 引擎不触发 HEAD
        self.assertEqual(len(probed), 2)
        self.assertTrue(all("bing" in u or "baidu" in u for u in probed))
        self.assertEqual(set(results.keys()), {"local_bing", "local_baidu"})


if __name__ == "__main__":
    unittest.main()

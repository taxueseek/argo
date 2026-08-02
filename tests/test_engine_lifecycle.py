#!/usr/bin/env python3
"""引擎生命周期：env / admission / external YAML / validate（离线为主）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import engine_env  # noqa: E402
import engine_admission  # noqa: E402
import config as config_mod  # noqa: E402


class TestEngineEnv(unittest.TestCase):
    def test_argo_prefix_preferred(self):
        with patch.dict(os.environ, {
            "ARGO_TAVILY_API_KEY": "argo-key",
            "TAVILY_API_KEY": "legacy-key",
        }, clear=False):
            self.assertEqual(engine_env.get_env(engine_env.KNOWN_ENV_ALIASES["tavily"]), "argo-key")

    def test_legacy_fallback(self):
        env = {k: v for k, v in os.environ.items() if "TAVILY" not in k}
        env["TAVILY_API_KEY"] = "legacy-only"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(engine_env.get_env(engine_env.KNOWN_ENV_ALIASES["tavily"]), "legacy-only")

    def test_expand_placeholders(self):
        with patch.dict(os.environ, {"ARGO_BOCHA_API_KEY": "b1"}, clear=False):
            out = engine_env.expand_placeholders("Bearer {BOCHA_API_KEY}")
            self.assertEqual(out, "Bearer b1")

    def test_missing_env_tavily(self):
        env = {k: v for k, v in os.environ.items() if "TAVILY" not in k}
        with patch.dict(os.environ, env, clear=True):
            miss = engine_env.missing_env_for("tavily", {"type": "http"})
            self.assertTrue(miss)
            self.assertFalse(engine_env.env_ready("tavily", {"type": "http"}))

    def test_enable_disable_lists(self):
        with patch.dict(os.environ, {
            "ARGO_ENABLE_ENGINES": "a,b",
            "ARGO_DISABLE_ENGINES": "b",
        }, clear=False):
            self.assertTrue(engine_env.is_engine_allowed_by_env("a"))
            self.assertFalse(engine_env.is_engine_allowed_by_env("b"))
            self.assertFalse(engine_env.is_engine_allowed_by_env("c"))


class TestAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self._old = engine_admission.DEFAULT_ADMISSION_DIR
        engine_admission.DEFAULT_ADMISSION_DIR = self.dir

    def tearDown(self):
        engine_admission.DEFAULT_ADMISSION_DIR = self._old

    def test_record_and_block(self):
        rec = engine_admission.record_validation(
            "demo_engine",
            stages_passed=["health"],
            quality_score=0.9,
            avg_latency_ms=120,
            blocked=False,
            admit=True,
            health={"ok": True, "status": "pass"},
        )
        self.assertFalse(rec["blocked"])
        self.assertTrue(rec.get("admitted_at"))
        self.assertTrue(engine_admission.is_admitted("demo_engine"))
        engine_admission.set_blocked("demo_engine", True, reason="test")
        self.assertTrue(engine_admission.is_blocked("demo_engine"))
        filtered = engine_admission.filter_routable(["demo_engine", "other"])
        self.assertEqual(filtered, ["other"])


class TestExternalSpecMerge(unittest.TestCase):
    def test_merge_external_yaml(self):
        specs = ROOT / "engines" / "specs"
        specs.mkdir(parents=True, exist_ok=True)
        path = specs / "lifecycle_probe_engine.yaml"
        path.write_text(
            "\n".join([
                "engine_id: lifecycle_probe_engine",
                "type: http",
                "enabled: true",
                "cost_tier: free",
                "method: GET",
                "url: https://example.com/search",
                "query_param: q",
                "timeout: 5",
            ]),
            encoding="utf-8",
        )
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        cfg = config_mod.load_config(force=True)
        self.assertIn("lifecycle_probe_engine", cfg.get("engines", {}))
        spec = cfg["engines"]["lifecycle_probe_engine"]
        self.assertEqual(spec.get("type"), "http")
        self.assertTrue(spec.get("_external_spec"))


class TestValidateSchemaHelper(unittest.TestCase):
    def test_schema_ok(self):
        from engine_validate import _schema_ok
        ok, msg, rate = _schema_ok([
            {"title": "A", "url": "http://x", "source": "t"},
        ])
        self.assertTrue(ok)
        self.assertGreaterEqual(rate, 0.5)
        ok2, _, _ = _schema_ok([])
        self.assertFalse(ok2)


class TestListStatus(unittest.TestCase):
    def test_list_detail_runs(self):
        from engine_status import list_engines_detail, engine_detail
        rows = list_engines_detail()
        self.assertTrue(len(rows) > 10)
        # hackernews 应为 free 且通常 env_ready
        hn = engine_detail("hackernews")
        self.assertEqual(hn["engine_id"], "hackernews")
        self.assertTrue(hn["env_ready"])


if __name__ == "__main__":
    unittest.main()

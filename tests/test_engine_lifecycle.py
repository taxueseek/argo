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
        # 同步屏蔽密钥文件兜底（本机 ~/.config/argo/env 真有 tavily key）
        with patch.dict(os.environ, env, clear=True), \
             patch("engine_env._envfile_path",
                   lambda: Path("/nonexistent/argo/env")):
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


def _no_firecrawl_env() -> dict:
    return {k: v for k, v in os.environ.items() if "FIRECRAWL" not in k}


class TestFirecrawlKeyless(unittest.TestCase):
    """firecrawl 可选密钥：缺 key 不阻断路由（keyless 免费层）。"""

    _NONEXIST_ENVFILE = lambda: Path("/nonexistent/argo/env")  # noqa: E731

    def test_firecrawl_optional_keyless_ready(self):
        with patch.dict(os.environ, _no_firecrawl_env(), clear=True), \
             patch("engine_env._envfile_path", self._NONEXIST_ENVFILE):
            self.assertTrue(engine_env.env_ready("firecrawl", {}))
            self.assertEqual(engine_env.required_env_for("firecrawl", {}), [])
            self.assertEqual(engine_env.missing_env_for("firecrawl", {}), [])


class TestPostHeaderKeyless(unittest.TestCase):
    """POST 型 HTTP 引擎缺 key 时不发送认证残留头（与 GET 路径对齐）。

    回归背景：firecrawl 为 POST，此前 POST 分支无 _header_meaningful 过滤，
    keyless 时会把 'Bearer {FIRECRAWL_API_KEY}' 原样发出导致 401。
    """

    _NONEXIST_ENVFILE = lambda: Path("/nonexistent/argo/env")  # noqa: E731

    @staticmethod
    def _post_spec() -> dict:
        return {
            "engine_id": "fc_test",
            "_name": "fc_test",
            "type": "http",
            "method": "POST",
            "url": "https://api.example.test/v2/search",
            "timeout": 5,
            "headers": {
                "Authorization": "Bearer {FIRECRAWL_API_KEY}",
                "Content-Type": "application/json",
            },
            "body": {"query": "{query}", "limit": "{n}"},
            "output_map": {
                "items": "data.web",
                "item_title": "title",
                "item_url": "url",
                "item_summary": "description",
            },
        }

    @staticmethod
    def _run_capture(env_extra: dict) -> tuple[list, dict]:
        """跑一次 mock urlopen 的 POST 引擎，返回 (results, 捕获的请求头/体)。"""
        from engines_base import _build_http_engine

        captured: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"data": {"web": [
                    {"title": "t1", "url": "https://x/1", "description": "d1"},
                ]}}).encode("utf-8")

        def _fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        with patch.dict(os.environ, env_extra, clear=True), \
             patch("engine_env._envfile_path", TestPostHeaderKeyless._NONEXIST_ENVFILE), \
             patch("urllib.request.urlopen", _fake_urlopen):
            eng = _build_http_engine(TestPostHeaderKeyless._post_spec())
            results = eng("climate", n=3)
        return results, captured

    def test_keyless_auth_header_not_sent(self):
        results, captured = self._run_capture(_no_firecrawl_env())
        self.assertEqual(len(results), 1)
        header_keys = {k.lower() for k in captured["headers"]}
        self.assertNotIn("authorization", header_keys)
        self.assertIn("content-type", header_keys)

    def test_with_key_auth_header_sent(self):
        env = _no_firecrawl_env()
        env["ARGO_FIRECRAWL_API_KEY"] = "fc-live"
        results, captured = self._run_capture(env)
        self.assertEqual(len(results), 1)
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers.get("authorization"), "Bearer fc-live")


if __name__ == "__main__":
    unittest.main()

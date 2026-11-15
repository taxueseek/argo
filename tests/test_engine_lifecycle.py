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
        # 同步屏蔽密钥文件保底（本机 ~/.config/argo/env 真有 tavily key）
        with patch.dict(os.environ, env, clear=True), \
             patch("engine_env._envfile_paths",
                   lambda: [Path("/nonexistent/argo/env")]):
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
        # 清配置缓存**必须在删除文件之后**：addCleanup 是 LIFO，若把它单独
        # 注册在 unlink 之后，它会先跑（文件还在），缓存里照样留着这个探针引擎。
        # 合并成一次 cleanup、顺序写死，避免再踩。不清的后果不是本测试失败，而是
        # 污染后续测试：test_engine_families 断言「每个引擎都显式声明 family」，
        # 撞上残留的 lifecycle_probe_engine 就红（实测
        # `pytest tests/test_engine_lifecycle.py tests/test_engine_families.py`
        # 稳定 1 failed；字母序把 families 排在前时才侥幸通过）。
        def _purge():
            path.unlink(missing_ok=True)
            config_mod.load_config(force=True)
        self.addCleanup(_purge)
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


class TestRelevanceGate(unittest.TestCase):
    """相关性判据（2026-09-16 加）。

    背景：Seltz 的检索层被接错语料，中文查询返回时政与播客条目，但字段
    100% 完整、延迟正常，在只看 schema 的旧判据下拿 quality_score=1.0 通过
    准入。这组测试锁住「结构合法但答非所问要能被挡下」。
    """

    def test_garbage_results_fail(self):
        from engine_validate import _relevance_check
        spec = {"coverage": ["general"]}
        garbage = [
            {"title": "Chinese Military Winning Weapon of Cognitive Operations",
             "snippet": "military informatization"},
            {"title": "Luxembourg Securitization Attracts Investors",
             "snippet": "securitization vehicles"},
        ]
        r = _relevance_check(spec, garbage, "RRF 融合算法")
        self.assertTrue(r["applicable"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["hit_rate"], 0.0)

    def test_relevant_results_pass(self):
        from engine_validate import _relevance_check
        spec = {"coverage": ["general"]}
        good = [{"title": "RRF 融合算法原理",
                 "snippet": "Reciprocal Rank Fusion 倒数排序融合"}]
        r = _relevance_check(spec, good, "RRF 融合算法")
        self.assertTrue(r["ok"])
        self.assertEqual(r["hit_rate"], 1.0)

    def test_partial_hit_is_not_a_failure(self):
        """判负门槛是零交集，不是多数命中。

        mdn 的 canary 是单词 "Python"，工作正常却只有 0.2 命中率
        （Python 章节页标题未必含 python 字样）。用多数规则会误杀。
        """
        from engine_validate import _relevance_check
        spec = {"coverage": ["general"]}
        mixed = [
            {"title": "Object", "snippet": "The Object type represents"},
            {"title": "Array", "snippet": "Array methods and iterators"},
            {"title": "Python", "snippet": "Python is a scripting language"},
        ]
        r = _relevance_check(spec, mixed, "Python")
        self.assertTrue(r["ok"], "部分命中不得判负")
        self.assertLess(r["hit_rate"], 0.5)

    def test_declared_off_engine_is_skipped(self):
        """热榜/快讯型源按设计忽略查询词，不得用相关性评判。"""
        from engine_validate import _relevance_applicable
        ok, why = _relevance_applicable(
            {"coverage": ["general"], "relevance_check": False}, "热点")
        self.assertFalse(ok)
        self.assertEqual(why, "declared_off")

    def test_latin_query_applies_even_without_english_tag(self):
        """coverage 声明的是内容域时不得跳过英文查询。

        mdn 声明 docs/web/javascript、github 声明 code、arxiv 声明
        academic——都不含 english，但都能处理英文查询。若要求必须声明
        english 才判定，判据对这些源形同虚设。
        """
        from engine_validate import _relevance_applicable
        for cov in (["docs", "web"], ["code"], ["academic"], ["wiki", "factual"]):
            ok, why = _relevance_applicable({"coverage": cov}, "Python asyncio")
            self.assertTrue(ok, f"coverage={cov} 不应跳过英文查询（{why}）")

    def test_cjk_query_on_english_only_engine_is_skipped(self):
        """中文查询配纯英文源：词面零交集不代表不相关，判据无意义。"""
        from engine_validate import _relevance_applicable
        ok, why = _relevance_applicable({"coverage": ["english", "news"]}, "美联储 降息")
        self.assertFalse(ok)
        self.assertIn("cross_lang", why)

    def test_empty_results_not_charged_to_relevance(self):
        """空结果归 schema 记账，相关性不得抢走归因。"""
        from engine_validate import _relevance_check
        r = _relevance_check({"coverage": ["general"]}, [], "Python")
        self.assertFalse(r["applicable"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["relevance_msg"], "not_applicable: no_results")

    def test_relevance_failure_caps_quality_score(self):
        """相关性多数不通过时质量分封顶，不让答非所问的引擎继续拿高分。"""
        import engine_validate as ev
        qs = [{"id": f"q{i}", "query": "python asyncio"} for i in range(5)]

        def fake_health(engine_id, *, query=None, n=3, timeout=10.0):
            return {"ok": True, "status": "pass", "count": 3, "latency_ms": 10.0,
                    "schema_ok": True, "schema_msg": "字段完整率 100%",
                    "field_complete_rate": 1.0, "sample_title": "x",
                    "relevance_ok": False, "relevance_applicable": True,
                    "relevance_hit_rate": 0.0, "relevance_msg": "零交集"}

        orig_health, orig_spec, orig_env = ev.run_health, ev._get_spec, ev.env_ready
        self.addCleanup(lambda: setattr(ev, "run_health", orig_health))
        self.addCleanup(lambda: setattr(ev, "_get_spec", orig_spec))
        self.addCleanup(lambda: setattr(ev, "env_ready", orig_env))
        ev.run_health = fake_health
        ev._get_spec = lambda engine_id: {"coverage": ["general"]}
        ev.env_ready = lambda engine_id, spec: True

        res = ev.run_quality("fake_engine", queries=qs)
        self.assertFalse(res["ok"], "相关性多数不通过时 quality 应判负")
        self.assertTrue(res["relevance_capped"])
        self.assertLessEqual(res["quality_score"], 0.3)


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

    _NONEXIST_ENVFILE = lambda: [Path("/nonexistent/argo/env")]  # noqa: E731
    # 注意：这里 patch 的是 _envfile_paths（候选列表）——只屏蔽首选路径时代码
    # 仍会去读历史/平台惯例候选，本机真配了 key 就会漏隔离。

    def test_firecrawl_optional_keyless_ready(self):
        with patch.dict(os.environ, _no_firecrawl_env(), clear=True), \
             patch("engine_env._envfile_paths", self._NONEXIST_ENVFILE):
            self.assertTrue(engine_env.env_ready("firecrawl", {}))
            self.assertEqual(engine_env.required_env_for("firecrawl", {}), [])
            self.assertEqual(engine_env.missing_env_for("firecrawl", {}), [])


class TestPostHeaderKeyless(unittest.TestCase):
    """POST 型 HTTP 引擎缺 key 时不发送认证残留头（与 GET 路径保持一致）。

    回归背景：firecrawl 为 POST，此前 POST 分支无 _header_meaningful 过滤，
    keyless 时会把 'Bearer {FIRECRAWL_API_KEY}' 原样发出导致 401。
    """

    _NONEXIST_ENVFILE = lambda: [Path("/nonexistent/argo/env")]  # noqa: E731
    # 注意：这里 patch 的是 _envfile_paths（候选列表）——只屏蔽首选路径时代码
    # 仍会去读历史/平台惯例候选，本机真配了 key 就会漏隔离。

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
            # 67edf08 起占位符白名单 fail-closed：可选 key 也须声明才展开
            # （allowed_env=授权但非必需，未配置时仍由 _header_meaningful 过滤）
            "allowed_env": ["FIRECRAWL_API_KEY"],
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
             patch("engine_env._envfile_paths", TestPostHeaderKeyless._NONEXIST_ENVFILE), \
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

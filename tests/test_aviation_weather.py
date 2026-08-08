#!/usr/bin/env python3
"""tests/test_aviation_weather.py — 航空气象 METAR 引擎原生集成

吸收外部航空气象查询技能的能力，以 argo 声明式 http 引擎（aviation_weather）
原生接入，不引用任何外部技能名称：
  - engines/specs/aviation_weather.yaml：免 Key FAA 官方公开 API
  - 按 ICAO 机场代码查询实时 METAR（逗号分隔可多机场）
  - 输出含机场名（preserve_source 保留）与 reportTime 时间戳
  - config.yaml aviation_weather 域路由接入（优先于 weather_query）

覆盖：spec 注册、family/dedupe 存活、域路由接线、字段提取、
来源保留语义、URL 构造、全链路 registry 调用。
全程 mock 网络层，离线必过（ARGO_LIVE=1 时追加真实 API 冒烟）。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = SKILL_DIR / "scripts"
for p in (str(SCRIPT_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)

from engines_base import _build_http_engine, _ensure_engine_source  # noqa: E402
from engine_families import family_of, dedupe_by_family  # noqa: E402
from config import load_config, get_engines  # noqa: E402

LIVE = os.environ.get("ARGO_LIVE", "").strip() in {"1", "true", "yes"}

# ── 真实 API 响应结构样例（取自在线抓包，字段名逐一对齐） ──────────────────

SAMPLE_ITEMS = [
    {
        "icaoId": "KLAX",
        "receiptTime": "2026-08-08T14:56:39.143Z",
        "obsTime": 1786200780,
        "reportTime": "2026-08-08T15:00:00.000Z",
        "temp": 21.7,
        "dewp": 18.9,
        "wdir": 330,
        "wspd": 4,
        "visib": "10+",
        "altim": 1015.3,
        "rawOb": "METAR KLAX 081453Z 33004KT 10SM FEW008 SCT011 22/19 A2998 RMK AO2 SLP149 T02170189 51008 $",
        "lat": 33.9382,
        "lon": -118.3866,
        "elev": 30,
        "name": "Los Angeles Intl, CA, US",
        "cover": "SCT",
        "fltCat": "VFR",
    },
    {
        "icaoId": "KSMO",
        "receiptTime": "2026-08-08T14:54:10.596Z",
        "obsTime": 1786200660,
        "reportTime": "2026-08-08T15:00:00.000Z",
        "temp": 21.1,
        "dewp": 18.3,
        "wdir": 0,
        "wspd": 0,
        "visib": "10+",
        "altim": 1015.3,
        "rawOb": "METAR KSMO 081451Z 00000KT 10SM FEW012 SCT035 21/18 A2998 RMK AO2 SLP151 T02110183",
        "lat": 34.0158,
        "lon": -118.4513,
        "elev": 53,
        "name": "Santa Monica Muni, CA, US",
        "cover": "FEW",
        "fltCat": "VFR",
    },
]


class _FakeResp:
    """上下文管理器风格的 urlopen 响应桩。"""

    def __init__(self, body: bytes, url: str = "") -> None:
        self._body = body
        self.url = url

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _run_http(query: str, n: int = 5, body: bytes | None = None,
              spec_overrides: dict | None = None) -> tuple[list[dict], str]:
    """用 mock urlopen 跑一次 http 引擎调用，返回 (results, 实际请求 URL)。"""
    spec = dict(get_engines()["aviation_weather"])
    spec["_name"] = "aviation_weather"
    if spec_overrides:
        spec.update(spec_overrides)
    engine = _build_http_engine(spec)
    body = body if body is not None else json.dumps(SAMPLE_ITEMS).encode("utf-8")
    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResp(body)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        results = engine(query, n=n)
    return results, captured.get("url", "")


class TestRegistration(unittest.TestCase):
    """spec 注册 + 字段完整性"""

    def test_registered_and_enabled(self) -> None:
        load_config(force=True)
        engines = get_engines()
        self.assertIn("aviation_weather", engines, "aviation_weather 未注册（检查 engines/specs/aviation_weather.yaml）")
        self.assertTrue(engines["aviation_weather"].get("enabled", True))
        self.assertIn("aviationweather.gov", engines["aviation_weather"].get("url", ""))

    def test_spec_key_fields(self) -> None:
        engines = get_engines()
        spec = engines["aviation_weather"]
        self.assertEqual(spec.get("type"), "http")
        self.assertEqual(spec.get("family"), "misc_vertical")
        self.assertEqual(spec.get("query_param"), "ids")
        self.assertEqual(spec.get("format"), "json")
        self.assertTrue(spec.get("canary_query"))
        self.assertTrue(spec.get("preserve_source"), "preserve_source 应为 true 保留机场名标注")
        om = spec.get("output_map", {})
        self.assertEqual(om.get("items"), ".")
        self.assertEqual(om.get("item_title"), "icaoId")
        self.assertEqual(om.get("item_summary"), "rawOb")
        self.assertEqual(om.get("item_source"), "name")
        self.assertEqual(om.get("item_published_at"), "reportTime")

    def test_spec_file_exists(self) -> None:
        spec_file = SKILL_DIR / "engines" / "specs" / "aviation_weather.yaml"
        self.assertTrue(spec_file.is_file(), "缺少 engines/specs/aviation_weather.yaml")

    def test_family_mapping(self) -> None:
        spec = get_engines()["aviation_weather"]
        self.assertEqual(family_of("aviation_weather", spec), "misc_vertical")


class TestRouting(unittest.TestCase):
    """域路由接线 + dedupe 存活"""

    def test_aviation_domain_has_engine_and_precedes_weather_query(self) -> None:
        import yaml
        cfg = yaml.safe_load(open(SKILL_DIR / "config.yaml", encoding="utf-8"))
        domains = cfg["domains"]
        names = [d["name"] for d in domains]
        self.assertIn("aviation_weather", names)
        self.assertIn("weather_query", names)
        av = next(d for d in domains if d["name"] == "aviation_weather")
        self.assertIn("aviation_weather", av["engines_combo"],
                      "aviation_weather 域 combo 应包含 aviation_weather")
        # 航空气象意图优先路由：aviation_weather 域须排在 weather_query 之前
        self.assertLess(names.index("aviation_weather"), names.index("weather_query"))

    def test_domains_match_aviation_intent(self) -> None:
        """航空气象类查询应命中 aviation_weather 域。"""
        from route import match_domains
        import yaml
        cfg = yaml.safe_load(open(SKILL_DIR / "config.yaml", encoding="utf-8"))
        for q in ("KLAX metar", "查一下浦东机场天气", "aviation weather KSMO", "TAF KLAX"):
            hits = match_domains(q, cfg["domains"], max_n=3)
            names = {d["name"] for d in hits}
            self.assertIn("aviation_weather", names, f"查询未命中 aviation_weather 域: {q}")

    def test_weather_intent_does_not_steal_aviation(self) -> None:
        """普通天气查询不应命中 aviation_weather 域（不抢占 weather_query）。"""
        from route import match_domains
        import yaml
        cfg = yaml.safe_load(open(SKILL_DIR / "config.yaml", encoding="utf-8"))
        for q in ("北京明天天气怎么样", "上海气温"):
            hits = match_domains(q, cfg["domains"], max_n=3)
            names = {d["name"] for d in hits}
            self.assertNotIn("aviation_weather", names, f"普通天气查询误命中 aviation_weather: {q}")

    def test_dedupe_keeps_engine_with_qweather_same_family(self) -> None:
        """qweather/aviation_weather 同属 misc_vertical 时互不挤压。"""
        specs = get_engines()
        combo = ["qweather", "byted", "aviation_weather"]
        deduped = dedupe_by_family(
            combo, max_per_family=2, spec_lookup=specs,
            limit_families=frozenset({"web_general"}),
        )
        self.assertIn("aviation_weather", deduped)


class TestParsing(unittest.TestCase):
    """字段提取 + 时间戳 + 来源保留"""

    def test_extracts_fields(self) -> None:
        results, url = _run_http("KLAX,KSMO", n=5)
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertEqual(first["title"], "KLAX")
        self.assertEqual(first["url"], "https://aviationweather.gov/metar?ids=KLAX")
        self.assertIn("METAR KLAX", first["snippet"])

    def test_published_at_preserved(self) -> None:
        results, _ = _run_http("KLAX", n=5)
        self.assertEqual(results[0]["published_at"], "2026-08-08T15:00:00.000Z")
        self.assertEqual(results[1]["published_at"], "2026-08-08T15:00:00.000Z")

    def test_source_preserved_via_preserve_source(self) -> None:
        """preserve_source=true 时保留机场名，而非改写为引擎名。"""
        results, _ = _run_http("KLAX", n=5)
        self.assertEqual(results[0]["source"], "Los Angeles Intl, CA, US")
        self.assertEqual(results[1]["source"], "Santa Monica Muni, CA, US")

    def test_without_preserve_source_corrects_to_engine_name(self) -> None:
        """去掉 preserve_source 时回落默认语义：source 统一为引擎名。"""
        results, _ = _run_http("KLAX", n=5,
                               spec_overrides={"preserve_source": False})
        self.assertEqual(results[0]["source"], "aviation_weather")

    def test_url_construction(self) -> None:
        """URL 应带 ids=ICAO、format=json、hours=2；不得残留占位符。"""
        results, url = _run_http("KLAX,KSMO", n=5)
        self.assertIn("ids=KLAX%2CKSMO", url)
        self.assertIn("format=json", url)
        self.assertIn("hours=2", url)
        self.assertNotIn("{", url, "URL 不得含未解析占位符残留")

    def test_engine_search_integration_via_registry(self) -> None:
        """走真实 registry 构建路径（engines.search），验证端到端接线。"""
        from engines import search as engine_search

        body = json.dumps(SAMPLE_ITEMS).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            return _FakeResp(body)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            results = engine_search("KLAX", "aviation_weather", n=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["_engine"], "aviation_weather")
        self.assertIn("published_at", results[0])


@unittest.skipUnless(LIVE, "set ARGO_LIVE=1 for live aviation_weather calls")
class TestLive(unittest.TestCase):
    def test_live_search(self) -> None:
        from engines import search as engine_search

        results = engine_search("KLAX", "aviation_weather", n=3, timeout=12)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0, "live 空结果")
        r = results[0]
        self.assertTrue(r.get("title"))
        self.assertIn("METAR", r.get("snippet", ""))
        self.assertIn("published_at", r)


if __name__ == "__main__":
    unittest.main()

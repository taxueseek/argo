#!/usr/bin/env python3
"""tests/test_wx.py — 免 Key 天气查询引擎原生集成

吸收外部天气查询技能的能力（wttr.in / Open-Meteo 双免费源），以 argo
声明式 cli 引擎（weather）+ 原生脚本 scripts/wx.py 接入，不引用任何
外部技能名称：
  - engines/specs/weather.yaml：cli spec，调 scripts/wx.py
  - 输出结构化 YAML：当前天气 + 未来预报，含 title/url/snippet/published_at
  - config.yaml weather_query 域 combo 追加 weather（qweather 需 Key，本引擎补免 Key 通道）

覆盖：spec 注册、family/dedupe 存活、域路由接线、wx.py 解析逻辑
（wttr.in 主路径 / Open-Meteo 兜底 / 查询词清洗）、cli 引擎全链路。
全程 mock 网络层，离线必过（ARGO_LIVE=1 时追加真实调用冒烟）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = SKILL_DIR / "scripts"
for p in (str(SCRIPT_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)

import wx  # noqa: E402
from engines_base import _build_cli_engine  # noqa: E402
from engine_families import family_of  # noqa: E402
from config import load_config, get_engines  # noqa: E402

LIVE = os.environ.get("ARGO_LIVE", "").strip() in {"1", "true", "yes"}

# ── wttr.in 真实响应结构样例（取自在线抓包，字段名逐一对齐） ────────────────

WTTR_SAMPLE = {
    "current_condition": [
        {
            "temp_C": "28", "FeelsLikeC": "32", "humidity": "85",
            "windspeedKmph": "31", "winddir16Point": "NE", "cloudcover": "78",
            "visibility": "9", "observation_time": "02:17 PM",
            "weatherDesc": [{"value": "Patchy rain nearby"}],
        }
    ],
    "nearest_area": [
        {"areaName": [{"value": "Yangpu"}], "country": [{"value": "China"}], "region": ""}
    ],
    "weather": [
        {"date": "2026-08-08", "mintempC": "27", "maxtempC": "32",
         "hourly": [{"time": "1100", "tempC": "32", "humidity": "64",
                     "windspeedKmph": "29",
                     "weatherDesc": [{"value": "Patchy rain nearby"}]}]},
        {"date": "2026-08-09", "mintempC": "26", "maxtempC": "27",
         "hourly": [{"time": "1100", "tempC": "26", "humidity": "91",
                     "windspeedKmph": "36",
                     "weatherDesc": [{"value": "Light rain"}]}]},
    ],
}

OM_SAMPLE = {
    "current_weather": {
        "temperature": 26.3, "windspeed": 13.4, "winddirection": 230,
        "weathercode": 3, "time": "2026-08-08T14:20",
    },
    "daily": {
        "time": ["2026-08-08", "2026-08-09"],
        "temperature_2m_max": [30.1, 29.0],
        "temperature_2m_min": [24.2, 23.5],
        "precipitation_probability_max": [40, 70],
    },
}


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _patch_wttr(body: bytes = json.dumps(WTTR_SAMPLE).encode("utf-8")):
    """mock urlopen 返回 wttr.in 响应。"""
    return patch("urllib.request.urlopen", return_value=_FakeResp(body))


class TestRegistration(unittest.TestCase):
    """spec 注册 + 字段完整性"""

    def test_registered_and_enabled(self) -> None:
        load_config(force=True)
        engines = get_engines()
        self.assertIn("weather", engines, "weather 未注册（检查 engines/specs/weather.yaml）")
        self.assertTrue(engines["weather"].get("enabled", True))

    def test_spec_key_fields(self) -> None:
        engines = get_engines()
        spec = engines["weather"]
        self.assertEqual(spec.get("type"), "cli")
        self.assertEqual(spec.get("family"), "misc_vertical")
        self.assertEqual(spec.get("output_format"), "yaml")
        self.assertTrue(spec.get("canary_query"))
        self.assertEqual(spec.get("cmd"), ["python3", "scripts/wx.py"])

    def test_spec_and_script_exist(self) -> None:
        self.assertTrue((SKILL_DIR / "engines" / "specs" / "weather.yaml").is_file())
        self.assertTrue((SKILL_DIR / "scripts" / "wx.py").is_file())

    def test_family_mapping(self) -> None:
        spec = get_engines()["weather"]
        self.assertEqual(family_of("weather", spec), "misc_vertical")


class TestRouting(unittest.TestCase):
    """域路由接线"""

    def test_weather_query_combo_has_engine(self) -> None:
        import yaml
        cfg = yaml.safe_load(open(SKILL_DIR / "config.yaml", encoding="utf-8"))
        domains = {d["name"]: d for d in cfg["domains"]}
        self.assertIn("weather_query", domains)
        self.assertIn("weather", domains["weather_query"]["engines_combo"],
                      "weather_query 域 combo 应包含 weather")


class TestWxPy(unittest.TestCase):
    """wx.py 脚本逻辑（mock 网络层）"""

    def test_parse_location_cleans_weather_words(self) -> None:
        self.assertEqual(wx._parse_location(["上海天气"]), "上海")
        self.assertEqual(wx._parse_location(["北京今天天气怎么样"]), "北京")
        self.assertEqual(wx._parse_location(["New York weather"]), "New York")
        self.assertEqual(wx._parse_location(["31.23,121.47"]), "31.23,121.47")
        self.assertEqual(wx._parse_location(["31.23", "121.47"]), "31.23,121.47")
        self.assertEqual(wx._parse_location([""]), "Beijing")

    def test_wttr_parses_current_and_forecast(self) -> None:
        with _patch_wttr():
            rows = wx._wttr("Shanghai")
        self.assertGreaterEqual(len(rows), 3, "应输出当前 1 条 + 预报 2 条")
        cur = rows[0]
        self.assertIn("Shanghai 当前", cur["title"], "标题应使用查询词而非 nearest_area 区域名")
        self.assertIn("局部阵雨", cur["title"], "英文 desc 应中文化")
        self.assertIn("体感 32", cur["snippet"])
        self.assertIn("湿度 85", cur["snippet"])
        self.assertTrue(cur["url"].startswith("https://wttr.in/"))
        self.assertTrue(cur["published_at"])
        self.assertIn("2026-08-08", rows[1]["title"])

    def test_open_meteo_fallback(self) -> None:
        with patch("urllib.request.urlopen", return_value=_FakeResp(json.dumps(OM_SAMPLE).encode("utf-8"))):
            rows = wx._open_meteo("31.23,121.47")
        self.assertGreaterEqual(len(rows), 2)
        self.assertIn("阴", rows[0]["title"], "WMO code 3 应映射为阴")
        self.assertIn("26.3", rows[0]["title"])
        self.assertIn("降雨概率 40%", rows[1]["title"], "预报行应含降水概率")

    def test_wttr_failure_falls_back_to_open_meteo(self) -> None:
        """wttr.in 主路径异常时，main() 应整体回退 Open-Meteo。"""
        import io

        with patch.object(wx, "_wttr", side_effect=OSError("boom")), \
             patch.object(wx, "_open_meteo",
                          return_value=[{"title": "31.23,121.47 当前 26°C 阴",
                                         "url": "https://open-meteo.com/",
                                         "snippet": "风速 13km/h",
                                         "published_at": "2026-08-08"}]):
            buf = io.StringIO()
            with patch("sys.stdout", buf), patch.object(sys, "argv", ["wx.py", "31.23,121.47"]):
                rc = wx.main()
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("open-meteo.com", out)
        self.assertIn("当前 26°C 阴", out)


class TestGeocodeAqiMerge(unittest.TestCase):
    """v2.8 新增能力：地理编码 / AQI / 英中映射 / 双源融合（mock 网络层）。"""

    GEO_SAMPLE = {
        "results": [{"name": "上海", "latitude": 31.22222,
                     "longitude": 121.45806, "country": "中国"}],
    }

    def test_geocode_chinese_city(self) -> None:
        with patch("urllib.request.urlopen",
                   return_value=_FakeResp(json.dumps(self.GEO_SAMPLE).encode("utf-8"))):
            r = wx._geocode("上海")
        self.assertEqual(r["lat"], 31.22222)
        self.assertEqual(r["name"], "上海")
        self.assertEqual(r["country"], "中国")

    def test_geocode_no_result_returns_none(self) -> None:
        with patch("urllib.request.urlopen",
                   return_value=_FakeResp(json.dumps({"results": []}).encode("utf-8"))):
            self.assertIsNone(wx._geocode("不存在的城市xyz"))

    def test_zh_mapping(self) -> None:
        self.assertEqual(wx._zh("Patchy rain nearby"), "局部阵雨")
        self.assertEqual(wx._zh("Light Rain"), "小雨", "大小写不敏感")
        self.assertEqual(wx._zh("Sunny"), "晴")
        self.assertEqual(wx._zh("Unknown desc"), "Unknown desc", "未命中保留原文")

    def test_open_meteo_with_geocoding_label(self) -> None:
        """地名输入：geocode 后走 Open-Meteo，标题用城市名而非坐标。"""
        def fake_urlopen(req, timeout=8):
            if "geocoding-api" in req.full_url:
                body = json.dumps(self.GEO_SAMPLE).encode("utf-8")
            else:
                body = json.dumps(OM_SAMPLE).encode("utf-8")
            return _FakeResp(body)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rows = wx._open_meteo("上海")
        self.assertGreaterEqual(len(rows), 1)
        self.assertIn("上海", rows[0]["title"])
        self.assertNotIn("31.22", rows[0]["title"], "标题不应暴露坐标")

    def test_aqi_snippet(self) -> None:
        """AQI 字段拼入当前行 snippet（含等级）。"""
        aqi = {"current": {"pm10": 15.6, "pm2_5": 13.5}}

        def fake_urlopen(req, timeout=8):
            if "air-quality" in req.full_url:
                body = json.dumps(aqi).encode("utf-8")
            else:
                body = json.dumps(OM_SAMPLE).encode("utf-8")
            return _FakeResp(body)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rows = wx._open_meteo("31.23,121.47")
        self.assertIn("PM2.5 13.5", rows[0]["snippet"])
        self.assertIn("优", rows[0]["snippet"], "13.5µg/m³ 应评优")

    def test_aqi_level(self) -> None:
        self.assertEqual(wx._aqi_level(20), "优")
        self.assertEqual(wx._aqi_level(50), "良")
        self.assertEqual(wx._aqi_level(100), "轻度污染")
        self.assertEqual(wx._aqi_level(200), "重度污染")
        self.assertEqual(wx._aqi_level(None), "")

    def test_merge_wttr_current_om_forecast(self) -> None:
        """融合：当前取 wttr（信息全），同日预报取 OM（含降水概率），去重。"""
        w = [
            {"title": "上海 当前 26°C 小雨", "url": "https://wttr.in/",
             "snippet": "体感 29°C", "published_at": "2026-08-08"},
            {"title": "上海 2026-08-08 24°C~28°C", "url": "https://wttr.in/",
             "snippet": "小雨", "published_at": "2026-08-08"},
        ]
        o = [
            {"title": "上海 当前 26.3°C 小雨", "url": "https://open-meteo.com/",
             "snippet": "风速 13km/h", "published_at": "2026-08-08"},
            {"title": "上海 2026-08-08 24.2°C~30.1°C · 降雨概率 40%",
             "url": "https://open-meteo.com/", "snippet": "Open-Meteo 预报",
             "published_at": "2026-08-08"},
            {"title": "上海 2026-08-09 23.5°C~29°C · 降雨概率 70%",
             "url": "https://open-meteo.com/", "snippet": "Open-Meteo 预报",
             "published_at": "2026-08-09"},
        ]
        rows = wx._merge(w, o)
        self.assertEqual(len(rows), 3, "同日预报应去重")
        self.assertIn("体感", rows[0]["snippet"], "当前天气 wttr 优先")
        self.assertIn("降雨概率", rows[1]["title"], "预报 OM 优先（含降水概率）")
        self.assertEqual(rows[1]["published_at"], "2026-08-08")
        self.assertEqual(rows[2]["published_at"], "2026-08-09")


class TestCliEngine(unittest.TestCase):
    """cli spec 引擎构建 + 全链路"""

    def test_build_cli_engine_calls_script_and_parses_yaml(self) -> None:
        spec = dict(get_engines()["weather"])
        spec["_name"] = "weather"
        engine = _build_cli_engine(spec)
        yaml_out = """- title: Shanghai 当前 28°C Patchy rain nearby
  url: https://wttr.in/Shanghai
  snippet: 体感 32°C · 湿度 85%
  published_at: '2026-08-08'
- title: Shanghai 2026-08-08 27°C~32°C
  url: https://wttr.in/Shanghai
  snippet: Patchy rain nearby
  published_at: '2026-08-08'
"""
        captured: dict[str, list] = {}

        class _R:
            returncode = 0
            stdout = yaml_out
            stderr = ""

        def fake_run(cmd, capture_output, text, timeout):
            captured["cmd"] = cmd
            return _R()

        with patch.object(subprocess, "run", side_effect=fake_run):
            results = engine("上海天气", n=3)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Shanghai 当前 28°C Patchy rain nearby")
        self.assertIn("scripts/wx.py", captured["cmd"])
        self.assertIn("上海天气", captured["cmd"])

    def test_engine_search_integration_via_registry(self) -> None:
        from engines import search as engine_search

        yaml_out = ("- title: Beijing 当前 30°C Sunny\n"
                    "  url: https://wttr.in/Beijing\n"
                    "  snippet: 体感 32°C\n"
                    "  published_at: '2026-08-08'\n")

        class _R:
            returncode = 0
            stdout = yaml_out
            stderr = ""

        with patch.object(subprocess, "run", return_value=_R()):
            results = engine_search("北京天气", "weather", n=3)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["_engine"], "weather")
        self.assertIn("published_at", results[0])


@unittest.skipUnless(LIVE, "set ARGO_LIVE=1 for live weather calls")
class TestLive(unittest.TestCase):
    def test_live_script(self) -> None:
        rows = wx._wttr("Shanghai")
        self.assertGreater(len(rows), 0, "live 空结果")
        self.assertIn("title", rows[0])
        self.assertIn("url", rows[0])

    def test_live_engine_search(self) -> None:
        from engines import search as engine_search

        results = engine_search("上海天气", "weather", n=3, timeout=15)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0, "live 空结果")
        self.assertTrue(results[0].get("title"))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""tests/test_train.py — 免 Key 火车余票查询引擎原生集成

吸收外部列车查询技能的能力（12306 官方接口：init 取会话 Cookie + queryG 查余票，
免 Key），以 argo 声明式 cli 引擎（train）+ 原生脚本 scripts/train.py 接入，
不引用任何外部技能名称：
  - engines/specs/train.yaml：cli spec，调 scripts/train.py
  - 输出结构化 YAML：车次/出发到达时间/历时/余票，含 title/url/snippet/published_at
  - config.yaml modal_card 域 combo 追加 train（火车票语义原先走通用引擎）

覆盖：spec 注册、域路由接线、查询词解析（起止站/日期/车次类型）、站点表解析与
解析函数（站名→站点码）、真实管道行字段映射、两步接口 mock（cookie + queryG）、
cli 引擎全链路。全程 mock 网络层，离线必过（ARGO_LIVE=1 时追加真实调用冒烟）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse as up
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = SKILL_DIR / "scripts"
for p in (str(SCRIPT_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)

import train  # noqa: E402
from engines_base import _build_cli_engine  # noqa: E402
from engine_families import family_of  # noqa: E402
from config import load_config, get_engines  # noqa: E402

LIVE = os.environ.get("ARGO_LIVE", "").strip() in {"1", "true", "yes"}

# ── 真实数据样例 ─────────────────────────────────────────────────────────────

# 12306 queryG 真实返回行（G531 北京南→上海虹桥，2026-08-10，unquote 后的 58 字段管道文本）
PIPE_ROW = (
    "3op4o6Svkj6xWwXDAqG7aQze+EsIp5d53N8p9JWNRWWOFy5kfGTjXNyTCalfgBwPyXAt5+0HCurE\n"
    "oRiaRhKnN6Z64HvpUHBAAa4ss5+qLLlEN2HOksTlPzYPx+7D663Yo3VAMElnA8DDc/hHgjLHJPOU\n"
    "3L3n6gLH9ZXJUDuVGDzo/FUsUL5I9gffEzAWfhSshXEz57RvBj4jSH9i3vrqUDrbIeMCRv4IL8+x\n"
    "zj8hWLn310Adm7XA5Tjs7pbpYpMbruQOVloYXDADtVDXtVIhuBazTMjaE7nYvECIEm48/KSEqKZ9\n"
    "E1ivd2uvMshEDsPKFC40khd4WkJPm1E4hNPeximc7Dhsw7KdDCL44HRQrDU="
    "|预订|240000G53106|G531|VNP|AOH|VNP|AOH|06:08|12:04|05:56|Y|OvDjaTa+UAxNqwR8uE1gE+bL8H76IkCO2Dz3BzuubbzWUqTagimZh6WZp78="
    "|20260810|3|P2|01|13|1|0|||||||有||||有|有|9||90M0O0W0|9MOO|0|0||9231500009M103300021O062600021O062603030"
    "|0|||||1|5#1#Q02#S#z#0#z#z|O062600021||CHN,CHN|||N#N#||90084M0082O0079W0079|202607271245|Y|"
)

# station_name.js 真实格式片段（北京南/上海虹桥/上海）
STATION_JS = (
    "'@bjb|北京北|VAP|beijingbei|bjb|0|0357|北京|||"
    "@bjn|北京南|VNP|beijingnan|bjn|3|0357|北京|||"
    "@shh|上海|SHH|shanghai|shh|1|0357|上海|||"
    "@aoh|上海虹桥|AOH|shanghaihongqiao|aoh|1|0357|上海|||'"
)


class _FakeResp:
    """mock 响应：可携带 Set-Cookie 头，与 urllib 响应接口兼容。"""

    def __init__(self, body: bytes, headers: dict | None = None, url: str = "") -> None:
        self._body = body
        self._headers = headers or {}
        self.url = url

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body

    @property
    def headers(self):
        class _H:
            def __init__(self, data):
                self._data = data

            def get_all(self, key: str, default=None):
                return self._data.get(key, default or [])

        return _H(self._headers)


def fake_urlopen(req, timeout: float = 8):
    """按 URL 分发的两步接口 mock。"""
    url = req.full_url
    if "leftTicket/init" in url:
        return _FakeResp(
            b"<html></html>",
            headers={"Set-Cookie": [
                "JSESSIONID=509D56350FCC84DF53C36D2157DF9A71; Path=/otn; HttpOnly",
                "SF_cookie_2=40844164; path=/",
            ]},
            url=url,
        )
    if "leftTicket/queryG" in url:
        body = json.dumps({"data": {"result": [up.quote(PIPE_ROW)]}}).encode("utf-8")
        return _FakeResp(body, url=url)
    if "station_name.js" in url:
        return _FakeResp(STATION_JS.encode("utf-8"), url=url)
    raise AssertionError(f"unexpected url: {url}")


class TestRegistration(unittest.TestCase):
    """spec 注册 + 字段完整性"""

    def test_registered_and_enabled(self) -> None:
        load_config(force=True)
        engines = get_engines()
        self.assertIn("train", engines, "train 未注册（检查 engines/specs/train.yaml）")
        self.assertTrue(engines["train"].get("enabled", True))

    def test_spec_key_fields(self) -> None:
        engines = get_engines()
        spec = engines["train"]
        self.assertEqual(spec.get("type"), "cli")
        self.assertEqual(spec.get("family"), "misc_vertical")
        self.assertEqual(spec.get("output_format"), "yaml")
        self.assertTrue(spec.get("canary_query"))
        self.assertEqual(spec.get("cmd"), ["python3", "scripts/train.py"])

    def test_spec_and_script_exist(self) -> None:
        self.assertTrue((SKILL_DIR / "engines" / "specs" / "train.yaml").is_file())
        self.assertTrue((SKILL_DIR / "scripts" / "train.py").is_file())

    def test_family_mapping(self) -> None:
        spec = get_engines()["train"]
        self.assertEqual(family_of("train", spec), "misc_vertical")


class TestRouting(unittest.TestCase):
    """域路由接线"""

    def test_modal_card_combo_has_engine(self) -> None:
        import yaml
        cfg = yaml.safe_load(open(SKILL_DIR / "config.yaml", encoding="utf-8"))
        domains = {d["name"]: d for d in cfg["domains"]}
        self.assertIn("modal_card", domains)
        self.assertIn("train", domains["modal_card"]["engines_combo"],
                      "modal_card 域 combo 应包含 train")


class TestParseQuery(unittest.TestCase):
    """查询词解析（起止站/日期/车次类型）"""

    def test_separator_forms(self) -> None:
        self.assertEqual(train._parse_query("上海到北京")["from"], "上海")
        self.assertEqual(train._parse_query("上海到北京")["to"], "北京")
        self.assertEqual(train._parse_query("北京→上海")["from"], "北京")
        self.assertEqual(train._parse_query("北京 上海")["to"], "上海")
        self.assertEqual(train._parse_query("从北京到上海")["from"], "北京")

    def test_date_words(self) -> None:
        q = train._parse_query("北京→上海 明天")
        self.assertEqual(q["date"], "2026-08-09")  # 今天 2026-08-08
        q = train._parse_query("北京→上海 后天")
        self.assertEqual(q["date"], "2026-08-10")
        q = train._parse_query("北京到上海 今天")
        self.assertEqual(q["date"], "2026-08-08")

    def test_explicit_date(self) -> None:
        q = train._parse_query("2026-08-10 北京 上海")
        self.assertEqual(q["date"], "2026-08-10")
        q = train._parse_query("北京到上海 8月10日")
        self.assertEqual(q["date"], "2026-08-10")

    def test_train_type(self) -> None:
        self.assertEqual(train._parse_query("上海到北京 高铁")["type"], "G")
        self.assertEqual(train._parse_query("上海到北京 动车")["type"], "D")
        self.assertEqual(train._parse_query("上海到北京 特快")["type"], "T")

    def test_default_date_is_tomorrow(self) -> None:
        q = train._parse_query("上海到北京")
        self.assertEqual(q["date"], "2026-08-09")  # 默认查明天（当天接口常无数据）

    def test_unparseable_returns_none(self) -> None:
        self.assertIsNone(train._parse_query(""))
        self.assertIsNone(train._parse_query("上海"))
        self.assertIsNone(train._parse_query("随便什么乱七八糟"))


class TestStations(unittest.TestCase):
    """站点表解析与站名→站点码"""

    def setUp(self) -> None:
        self.data = train._parse_station_data(STATION_JS)

    def test_parse_station_data(self) -> None:
        self.assertIn("VNP", self.data["STATIONS"])
        self.assertEqual(self.data["STATIONS"]["VNP"]["station_name"], "北京南")
        self.assertIn("北京南", self.data["NAME_STATIONS"])

    def test_resolve_exact_station(self) -> None:
        r = train._resolve_station(self.data, "北京南")
        self.assertEqual(r["station_code"], "VNP")

    def test_resolve_city_main_station(self) -> None:
        r = train._resolve_station(self.data, "上海")
        self.assertEqual(r["station_code"], "SHH")

    def test_resolve_city_first_station(self) -> None:
        r = train._resolve_station(self.data, "上海虹桥")
        self.assertEqual(r["station_code"], "AOH")

    def test_resolve_suffix_stripped(self) -> None:
        r = train._resolve_station(self.data, "北京南站")
        self.assertEqual(r["station_code"], "VNP")

    def test_resolve_unknown_returns_none(self) -> None:
        self.assertIsNone(train._resolve_station(self.data, "不存在的地方"))


class TestTicketParsing(unittest.TestCase):
    """真实管道行字段映射 + 结果行构建"""

    def setUp(self) -> None:
        self.data = train._parse_station_data(STATION_JS)
        self.fields = PIPE_ROW.split("|")
        self.ticket = train._parse_ticket(self.fields, self.data)

    def test_field_mapping(self) -> None:
        self.assertEqual(self.ticket["trainCode"], "G531")
        self.assertEqual(self.ticket["fromStation"], "北京南")
        self.assertEqual(self.ticket["toStation"], "上海虹桥")
        self.assertEqual(self.ticket["departTime"], "06:08")
        self.assertEqual(self.ticket["arriveTime"], "12:04")
        self.assertEqual(self.ticket["duration"], "05:56")
        self.assertEqual(self.ticket["canBuy"], "Y")
        self.assertEqual(self.ticket["date"], "20260810")

    def test_seat_fields(self) -> None:
        self.assertEqual(self.ticket["swz"], "9")   # 商务/特等 9 张
        self.assertEqual(self.ticket["zy"], "有")   # 一等座
        self.assertEqual(self.ticket["ze"], "有")   # 二等座
        self.assertEqual(self.ticket["wz"], "有")   # 无座
        self.assertEqual(self.ticket["rw"], "")     # 软卧（G 字头无）

    def test_build_rows(self) -> None:
        rows = train._build_rows([self.ticket], 5)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["title"], "G531 北京南 06:08→上海虹桥 12:04")
        self.assertIn("历时 5h56m", row["snippet"])
        self.assertIn("商务/特等 9", row["snippet"])
        self.assertIn("一等 有", row["snippet"])
        self.assertIn("二等 有", row["snippet"])
        self.assertIn("可购", row["snippet"])
        self.assertEqual(row["published_at"], "2026-08-10")
        self.assertTrue(row["url"].startswith("https://kyfw.12306.cn/"))


class TestNetwork(unittest.TestCase):
    """两步接口（mock 网络层）"""

    def test_get_cookie(self) -> None:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            cookie = train._get_cookie()
        self.assertIn("JSESSIONID=509D56350FCC84DF53C36D2157DF9A71", cookie)
        self.assertIn("SF_cookie_2=40844164", cookie)

    def test_query_api_decodes_pipe_row(self) -> None:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rows = train._query_api("VNP", "AOH", "2026-08-10", "JSESSIONID=x")
        self.assertEqual(len(rows), 1)
        fields = rows[0]
        self.assertEqual(fields[train._F["trainCode"]], "G531")
        self.assertEqual(fields[train._F["departTime"]], "06:08")

    def test_load_stations_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            payload = {"ts": int(__import__("time").time()), "data": train._parse_station_data(STATION_JS)}
            (cache_dir / "stations.json").write_text(json.dumps(payload), "utf-8")
            with patch("urllib.request.urlopen", side_effect=AssertionError("不应抓网络")):
                data = train._load_stations(cache_dir=cache_dir)
        self.assertIn("VNP", data["STATIONS"])

    def test_load_stations_cache_miss_fetches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                data = train._load_stations(cache_dir=cache_dir)
            self.assertIn("VNP", data["STATIONS"])
            self.assertTrue((cache_dir / "stations.json").is_file(), "应回写缓存")

    def test_main_prints_yaml(self) -> None:
        import io
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.object(sys, "argv", ["train.py", "北京南到上海虹桥"]):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = train.main()
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("G531 北京南 06:08→上海虹桥 12:04", out)
        self.assertIn("published_at: '2026-08-10'", out)


class TestCliEngine(unittest.TestCase):
    """cli spec 引擎构建 + 全链路"""

    def test_build_cli_engine_calls_script_and_parses_yaml(self) -> None:
        spec = dict(get_engines()["train"])
        spec["_name"] = "train"
        engine = _build_cli_engine(spec)
        yaml_out = """- title: G531 北京南 06:08→上海虹桥 12:04
  url: https://kyfw.12306.cn/otn/leftTicket/init
  snippet: 历时 5h56m · 二等 有 · 可购
  published_at: '2026-08-10'
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
            results = engine("北京到上海", n=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "G531 北京南 06:08→上海虹桥 12:04")
        self.assertIn("scripts/train.py", captured["cmd"])
        self.assertIn("北京到上海", captured["cmd"])

    def test_engine_search_integration_via_registry(self) -> None:
        from engines import search as engine_search

        yaml_out = ("- title: G2 北京南 07:00→上海虹桥 11:36\n"
                    "  url: https://kyfw.12306.cn/otn/leftTicket/init\n"
                    "  snippet: 历时 4h36m · 二等 有 · 可购\n"
                    "  published_at: '2026-08-10'\n")

        class _R:
            returncode = 0
            stdout = yaml_out
            stderr = ""

        with patch.object(subprocess, "run", return_value=_R()):
            results = engine_search("北京到上海", "train", n=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["_engine"], "train")
        self.assertIn("published_at", results[0])


@unittest.skipUnless(LIVE, "set ARGO_LIVE=1 for live 12306 calls")
class TestLive(unittest.TestCase):
    def test_live_script(self) -> None:
        data = train._load_stations(force=True)
        frm = train._resolve_station(data, "北京")
        to = train._resolve_station(data, "上海")
        self.assertIsNotNone(frm, "北京应可解析")
        self.assertIsNotNone(to, "上海应可解析")
        cookie = train._get_cookie()
        rows = train._query_api(frm["station_code"], to["station_code"], "2026-08-10", cookie)
        self.assertGreater(len(rows), 0, "live 空结果")
        self.assertEqual(rows[0][train._F["trainCode"]], "G531")

    def test_live_engine_search(self) -> None:
        from engines import search as engine_search

        results = engine_search("北京到上海", "train", n=5, timeout=25)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0, "live 空结果")
        self.assertTrue(results[0].get("title"))
        self.assertIn("→", results[0]["title"])


if __name__ == "__main__":
    unittest.main()

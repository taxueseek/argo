#!/usr/bin/env python3
"""tests/test_train.py — 免 Key 火车余票/中转/经停查询引擎原生集成

吸收外部列车查询技能的能力（12306 官方接口：init 取会话 Cookie + queryG 查余票，
lcquery 查中转，queryTrainInfo 查经停，免 Key），以 argo 声明式 cli 引擎（train）
+ 原生脚本 scripts/train.py 接入，不引用任何外部技能名称：
  - engines/specs/train.yaml：cli spec，调 scripts/train.py
  - 输出结构化 YAML：车次/出发到达时间/历时/余票，含 title/url/snippet/published_at
  - 行 URL 带查询身份参数：表格行不共享裸 URL，防 URL 去重折叠（fred 约定）
  - config.yaml modal_card 域 combo 追加 train（火车票语义原先走通用引擎）

覆盖：spec 注册、域路由接线、查询模式路由（余票/中转/经停）、查询词解析（起止站/
日期/车次类型/换乘站）、站点表解析与解析函数（站名→站点码）、真实管道行字段映射、
行身份 URL（归一化 + minhash 数据行豁免契约）、两步/三路接口 mock（cookie +
queryG / lcquery 翻页 / train search + queryTrainInfo）、cli 引擎全链路。全程 mock
网络层，离线必过（ARGO_LIVE=1 时追加真实调用冒烟）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse as up
from datetime import datetime, timedelta, timezone
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


def _cn_today(days: int = 0) -> str:
    """与 scripts/train.py `_today_cn` 同基准：UTC+8 当日日期字符串。"""
    base = datetime.now(timezone(timedelta(hours=8)))
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")

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

# station_name.js 真实格式片段（北京北/北京南/上海/上海虹桥/南京南）
STATION_JS = (
    "'@bjb|北京北|VAP|beijingbei|bjb|0|0357|北京|||"
    "@bjn|北京南|VNP|beijingnan|bjn|3|0357|北京|||"
    "@shh|上海|SHH|shanghai|shh|1|0357|上海|||"
    "@aoh|上海虹桥|AOH|shanghaihongqiao|aoh|1|0357|上海|||"
    "@njh|南京南|NKH|nanjingnan|njh|1|0357|江苏|||'"
)

# lcQuery/init 页面里的查询路径变量（真实形态：var lc_search_url = '/lcquery/queryG';）
LC_INIT_HTML = b"<html><script>var lc_search_url = '/lcquery/queryG';</script></html>"

# 中转方案（lcquery/queryG data.middleList 条目，字段按真实响应结构）
TRANSFER_PLAN = {
    "all_lishi": "13:30", "start_time": "08:00", "arrive_time": "21:30",
    "train_date": "2026-08-10",
    "from_station_name": "北京南", "middle_station_name": "南京南",
    "end_station_name": "上海虹桥",
    "from_station_code": "VNP", "middle_station_code": "NKH", "end_station_code": "AOH",
    "first_train_no": "24000000G1070", "second_train_no": "24000000G8307",
    "train_count": 2, "same_station": "0", "same_train": "N", "wait_time": "25",
    "fullList": [
        {"train_no": "24000000G1070", "station_train_code": "G107",
         "start_time": "08:00", "arrive_time": "12:11", "lishi": "04:11",
         "from_station_name": "北京南", "to_station_name": "南京南",
         "ze_num": "有", "zy_num": "9"},
        {"train_no": "24000000G8307", "station_train_code": "G8307",
         "start_time": "12:36", "arrive_time": "21:30", "lishi": "08:54",
         "from_station_name": "南京南", "to_station_name": "上海虹桥",
         "ze_num": "有"},
    ],
}

# 经停站（queryTrainInfo/query data.data 条目，字段按真实响应结构；
# 空位实测 "--" 与 "----" 两种形态都出现过）
ROUTE_STATIONS = [
    {"station_name": "北京南", "arrive_time": "----", "start_time": "11:10",
     "stopover_time": "----", "station_no": "01"},
    {"station_name": "南京南", "arrive_time": "14:15", "start_time": "14:18",
     "stopover_time": "3", "station_no": "12"},
    {"station_name": "上海虹桥", "arrive_time": "17:23", "start_time": "--",
     "stopover_time": "--", "station_no": "31"},
]


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
    """按 URL 分发的三路接口 mock（余票/中转/经停共用）。"""
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
    if "lcQuery/init" in url:
        return _FakeResp(LC_INIT_HTML, url=url)
    if "lcquery/queryG" in url:
        # 翻页 mock：result_index=0 → 1 条方案 + can_query Y；翻页 → 空批 + N
        idx = up.parse_qs(up.urlparse(url).query).get("result_index", ["0"])[0]
        data = ({"flag": True, "result_index": 10, "can_query": "Y",
                 "middleList": [TRANSFER_PLAN]} if idx == "0"
                else {"flag": True, "result_index": 20, "can_query": "N",
                      "middleList": []})
        return _FakeResp(json.dumps({"data": data}).encode("utf-8"), url=url)
    if "train/search" in url:
        body = {"data": [{"station_train_code": "G2", "train_no": "24000000G20I"}]}
        return _FakeResp(json.dumps(body).encode("utf-8"), url=url)
    if "queryTrainInfo/query" in url:
        body = {"data": {"data": ROUTE_STATIONS}}
        return _FakeResp(json.dumps(body).encode("utf-8"), url=url)
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
        # cmd 末项解析为绝对路径（外置 spec 的相对路径在合并后统一解析）：
        # 引擎以子进程方式执行且不设 cwd，相对路径在非仓库根目录下会失败。
        cmd = spec.get("cmd") or []
        self.assertEqual(cmd[0], "python3")
        self.assertTrue(cmd[-1].endswith("scripts/train.py"), cmd)

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
        self.assertEqual(q["date"], _cn_today(1))
        q = train._parse_query("北京→上海 后天")
        self.assertEqual(q["date"], _cn_today(2))
        q = train._parse_query("北京到上海 今天")
        self.assertEqual(q["date"], _cn_today(0))

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
        self.assertEqual(q["date"], _cn_today(1))  # 默认查明天（当天接口常无数据）

    def test_unparseable_returns_none(self) -> None:
        self.assertIsNone(train._parse_query(""))
        self.assertIsNone(train._parse_query("上海"))
        self.assertIsNone(train._parse_query("随便什么乱七八糟"))

    def test_station_names_are_not_char_set_stripped(self) -> None:
        """站名首字不得被当填充字符剥掉。

        2026-09-21 实测（修复前）：`strip(" 站车票张次列高动直特快速字头，。！？")`
        按字符集剥站名，把「张家口」剥成「家口」、「包头」剥成「包」、
        「高碑店」剥成「碑店」、「次渠」剥成「渠」——这些是真实站名，
        剥完解析不到站点码，整条余票查询静默失败。
        """
        for query, frm, to in [
            ("张家口 到 北京 高铁", "张家口", "北京"),
            ("包头 到 北京", "包头", "北京"),
            ("高碑店 到 北京", "高碑店", "北京"),
            ("次渠 到 亦庄", "次渠", "亦庄"),
        ]:
            q = train._parse_query(query)
            self.assertEqual((q["from"], q["to"]), (frm, to), query)

    def test_ticket_quantity_is_not_a_station(self) -> None:
        """「1张」是购票数量，不能占掉起站位。"""
        q = train._parse_query("1张 北京 到 上海 车票")
        self.assertEqual((q["from"], q["to"]), ("北京", "上海"))
        q = train._parse_query("1张票 北京 到 上海")
        self.assertEqual((q["from"], q["to"]), ("北京", "上海"))

    def test_ticket_quantity_guard_requires_token_boundary(self) -> None:
        """摘「N张」必须按**词尾**判，不能按「后面不接哪几个字」判。

        2026-09-21 实测：`\\d{1,2}\\s*张(?![站口字])` 这种负向列举漏掉了
        「买1 张家口 到 北京」——张字被吃掉，终站变成「家口」。
        """
        q = train._parse_query("买1 张家口 到 北京")
        self.assertEqual(q["to"], "张家口")
        q = train._parse_query("2张 张家口 到 北京")
        self.assertEqual((q["from"], q["to"]), ("张家口", "北京"))


class TestModeRouting(unittest.TestCase):
    """查询模式路由（余票/中转/经停）"""

    def test_route_by_code_and_word(self) -> None:
        for q in ("G2 经停", "G531途经哪些站", "K123 后天 停靠站", "G2 时刻"):
            self.assertEqual(train._parse_mode(q), "route", q)

    def test_route_by_bare_code(self) -> None:
        """裸车次号（含「次/时刻表」后缀）单独出现时问的只能是这趟车。"""
        for q in ("G2", "G2次", "G2 时刻表"):
            self.assertEqual(train._parse_mode(q), "route", q)

    def test_transfer_words(self) -> None:
        self.assertEqual(train._parse_mode("北京到拉萨 中转"), "transfer")
        self.assertEqual(train._parse_mode("北京 上海 换乘"), "transfer")

    def test_default_tickets(self) -> None:
        for q in ("上海到北京", "北京→上海 后天 G", "北京到上海 高铁"):
            self.assertEqual(train._parse_mode(q), "tickets", q)

    def test_cjk_boundary_needs_lookaround(self) -> None:
        """车次号紧贴汉字也必须命中：Python 的 \\b 对 CJK 无效（\\w 含汉字）。"""
        m = train._TRAIN_CODE_RE.search("查G2经停")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "G2")


class TestParseMiddle(unittest.TestCase):
    """换乘站提取（中转X / 在X中转 / 经X，须过站点表认证）"""

    def setUp(self) -> None:
        self.data = train._parse_station_data(STATION_JS)

    def test_after_trigger(self) -> None:
        mid, rest = train._parse_middle("北京南到上海虹桥 中转南京南", self.data)
        self.assertEqual(mid["station_code"], "NKH")
        self.assertNotIn("南京南", rest)

    def test_capture_stops_at_destination(self) -> None:
        """「中转南京南到上海」须整段截出南京南——捕获非贪婪，边界字符
        （到/至/空白/标点/结尾）收尾，不许跨字扫描提前满足。"""
        mid, rest = train._parse_middle("北京 中转南京南到上海", self.data)
        self.assertEqual(mid["station_name"], "南京南")
        self.assertIn("上海", rest)

    def test_capture_boundary_at_to_word(self) -> None:
        mid, _ = train._parse_middle("北京 中转上海到拉萨", self.data)
        self.assertEqual(mid["station_name"], "上海")

    def test_middle_before_zai(self) -> None:
        mid, rest = train._parse_middle("在南京南中转", self.data)
        self.assertEqual(mid["station_code"], "NKH")

    def test_jing_trigger(self) -> None:
        mid, _ = train._parse_middle("北京到拉萨 经南京南", self.data)
        self.assertEqual(mid["station_code"], "NKH")

    def test_no_middle(self) -> None:
        mid, rest = train._parse_middle("北京南到上海虹桥 中转", self.data)
        self.assertIsNone(mid)
        self.assertIn("中转", rest)  # 未捕获则原文不动，交后续剥离

    def test_unresolvable_capture_ignored(self) -> None:
        """解析不出站码的捕获一律弃用，宁可丢中转站也不错当换乘站。"""
        mid, rest = train._parse_middle("北京到上海 中转不存在的地方", self.data)
        self.assertIsNone(mid)
        self.assertIn("中转", rest)


class TestRowIdentityUrl(unittest.TestCase):
    """行身份 URL：同查询不同车次不得共享裸 URL（防 URL 去重 10→1 折叠）"""

    def setUp(self) -> None:
        self.data = train._parse_station_data(STATION_JS)
        self.ticket = train._parse_ticket(PIPE_ROW.split("|"), self.data)
        self.route = {"date": "2026-08-10", "from_code": "VNP", "to_code": "AOH"}

    def test_distinct_trains_distinct_urls(self) -> None:
        t2 = dict(self.ticket, trainCode="G103")
        rows = train._build_rows([self.ticket, t2], 5, self.route)
        self.assertNotEqual(rows[0]["url"], rows[1]["url"])

    def test_identity_survives_canonicalization(self) -> None:
        """管线契约钉死：url_canon 归一化必须保留行身份参数，且
        search_rank 的 _distinct_data_rows（minhash 数据行豁免）认得它。"""
        from url_canon import canonical_url
        from search_rank import _distinct_data_rows
        t2 = dict(self.ticket, trainCode="G103")
        rows = train._build_rows([self.ticket, t2], 5, self.route)
        ka, kb = canonical_url(rows[0]["url"]), canonical_url(rows[1]["url"])
        self.assertNotEqual(ka, kb, "归一 URL 丢掉了行身份参数")
        self.assertTrue(_distinct_data_rows(ka, kb), "数据行豁免未认出行身份")

    def test_no_route_keeps_legacy_bare_url(self) -> None:
        rows = train._build_rows([self.ticket], 5)
        self.assertEqual(rows[0]["url"], "https://kyfw.12306.cn/otn/leftTicket/init")


class TestRouteMode(unittest.TestCase):
    """经停查询（车次号搜 train_no → queryTrainInfo 查经停）"""

    def test_search_train_no(self) -> None:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            no = train._search_train_no("G2", "2026-08-10", "JSESSIONID=x")
        self.assertEqual(no, "24000000G20I")

    def test_route_api(self) -> None:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            stations = train._route_api("24000000G20I", "2026-08-10", "JSESSIONID=x")
        self.assertEqual([s["station_name"] for s in stations],
                         ["北京南", "南京南", "上海虹桥"])

    def test_build_route_row(self) -> None:
        row = train._build_route_row("G2", ROUTE_STATIONS, "2026-08-10", "24000000G20I")
        self.assertIn("共3站", row["title"])
        self.assertIn("北京南 11:10开", row["snippet"])
        self.assertIn("南京南 14:15到/14:18开(停3分)", row["snippet"])
        self.assertIn("上海虹桥 17:23到", row["snippet"])
        self.assertIn("train_no=24000000G20I", row["url"])
        self.assertEqual(row["published_at"], "2026-08-10")

    def test_main_route_mode(self) -> None:
        import io
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.object(sys, "argv", ["train.py", "G2 经停"]):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = train.main()
        self.assertEqual(rc, 0)
        self.assertIn("G2 经停站", buf.getvalue())


class TestTransferMode(unittest.TestCase):
    """中转查询（lc_search_url 运行时解析 → lcquery 翻页取方案）"""

    def test_get_lcquery_path(self) -> None:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(train._get_lcquery_path(), "/lcquery/queryG")

    def test_get_lcquery_path_fails_loud(self) -> None:
        """路径解析失败必须报错，不许静默回落旧路径伪装成「无中转方案」。"""
        with patch("urllib.request.urlopen",
                   side_effect=lambda req, timeout=8: _FakeResp(b"<html></html>",
                                                                url=req.full_url)):
            with self.assertRaises(RuntimeError):
                train._get_lcquery_path()

    def test_transfer_api_pagination(self) -> None:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            plans, path, err = train._transfer_api("VNP", "AOH", "", "2026-08-10",
                                                   "JSESSIONID=x", limit=5)
        self.assertIsNone(err)
        self.assertEqual(path, "/lcquery/queryG")
        self.assertEqual(len(plans), 1)  # 第二页空批收尾
        self.assertEqual(plans[0]["first_train_no"], "24000000G1070")

    def test_build_transfer_rows(self) -> None:
        ctx = {"date": "2026-08-10", "path": "/lcquery/queryG",
               "from_code": "VNP", "to_code": "AOH", "middle_code": "NKH"}
        rows = train._build_transfer_rows([TRANSFER_PLAN], 5, ctx)
        row = rows[0]
        self.assertEqual(row["title"],
                         "G107+G8307 北京南→南京南→上海虹桥 08:00→21:30")
        self.assertIn("总历时 13h30m", row["snippet"])
        self.assertIn("换乘 南京南 等25分", row["snippet"])
        self.assertIn("G107 08:00→南京南 12:11", row["snippet"])
        self.assertIn("末段 二等 有", row["snippet"])
        self.assertIn("first_train_no=24000000G1070", row["url"])
        self.assertEqual(row["published_at"], "2026-08-10")

    def test_main_transfer_mode(self) -> None:
        import io
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.object(sys, "argv", ["train.py", "北京南到上海虹桥 中转"]):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = train.main()
        self.assertEqual(rc, 0)
        self.assertIn("G107+G8307", buf.getvalue())


class TestStationCacheLocation(unittest.TestCase):
    """站点表缓存不得落在源码树内。

    2026-09-19 实测事故：默认缓存目录是 `scripts/data/`（源码树内），而
    tests/conftest.py 只隔离了 ARGO_STATE_DIR——一次全量 pytest 把夹具里的
    4 个站写进 scripts/data/stations.json，此后 7 天（TTL）真实查询只认这
    4 个站，其余站名全部解析失败且无任何报错。缓存改走
    argo_paths.state_path("train") 后，测试会话自动隔离。
    """

    def test_default_cache_dir_is_outside_source_tree(self) -> None:
        cache_dir = train._cache_dir()
        source_tree = SKILL_DIR.resolve()
        self.assertNotEqual(cache_dir, source_tree)
        self.assertNotIn(source_tree, cache_dir.resolve().parents,
                         f"站点缓存又落回源码树：{cache_dir}")

    def test_default_cache_dir_follows_state_dir(self) -> None:
        # conftest 把 ARGO_STATE_DIR 指到临时目录，缓存必须跟着走。
        state_dir = os.environ.get("ARGO_STATE_DIR", "")
        if not state_dir:
            self.skipTest("本会话未设置 ARGO_STATE_DIR")
        self.assertTrue(
            str(train._cache_dir()).startswith(state_dir),
            f"站点缓存未跟随 ARGO_STATE_DIR：{train._cache_dir()}")

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

        def fake_run(cmd, capture_output, text, timeout, encoding=None,
                     errors=None):
            captured["cmd"] = cmd
            return _R()

        with patch.object(subprocess, "run", side_effect=fake_run):
            results = engine("北京到上海", n=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "G531 北京南 06:08→上海虹桥 12:04")
        self.assertTrue(any(str(c).endswith("scripts/train.py") for c in captured["cmd"]), captured["cmd"])
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

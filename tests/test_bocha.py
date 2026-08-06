#!/usr/bin/env python3
"""博查引擎测试 — web-search 解析修复 + freshness 动态化 + ai-search 模态卡。

覆盖（全部离线，mock urlopen，无需真实 API）：
  1. bocha 专用解析：data.webPages.value 嵌套路径正确提取 name/url/summary/siteName/date
  2. freshness 动态化：周级词 → oneWeek、日级词 → oneDay、普通词 → noLimit
  3. bocha_ai 模态卡：webpage message → 标准结果；模态卡 message → card 结果；
     image message 跳过；空内容 message 跳过
  4. 无 key 时显式返回错误项（不静默、不抛异常）

运行：
  python3 -m pytest tests/test_bocha.py -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from engines_builders_cn import (  # noqa: E402
    _bocha_freshness,
    _build_bocha_ai_engine,
    _build_bocha_engine,
)


class _FakeResp:
    """HTTP 响应替身（context manager 协议，供 urlopen 调用方使用）。"""

    def __init__(self, payload: Any):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


_WEB_PAGE = {
    "id": "https://api.bochaai.com/v1/#WebPages.0",
    "name": "北京天气预报-中国天气网",
    "url": "https://weather.com.cn/beijing/",
    "summary": "晴转多云，18-25 度，北风 3 级",
    "datePublished": "2026-08-05",
    "siteName": "中国天气网",
}


def _fake_urlopen(payload: dict[str, Any]):
    def _fn(req, timeout=8):
        return _FakeResp(payload)
    return _fn


class TestBochaWebEngine(unittest.TestCase):
    def setUp(self):
        self._saved_keys = {
            k: os.environ.get(k) for k in ("BOCHA_API_KEY", "ARGO_BOCHA_API_KEY")
        }
        os.environ["BOCHA_API_KEY"] = "sk-test"
        self.builder = _build_bocha_engine({"timeout": 8})

    def tearDown(self):
        for k, v in self._saved_keys.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_web_pages_nested_path(self):
        """修复点：data.webPages.value 嵌套路径必须被正确解析。"""
        payload = {"data": {"webPages": {"value": [_WEB_PAGE]}}}
        with patch("urllib.request.urlopen", _fake_urlopen(payload)):
            results = self.builder("北京天气", 3)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["title"], "北京天气预报-中国天气网")
        self.assertEqual(r["url"], "https://weather.com.cn/beijing/")
        self.assertEqual(r["snippet"], "晴转多云，18-25 度，北风 3 级")
        self.assertEqual(r["site_name"], "中国天气网")
        self.assertEqual(r["date"], "2026-08-05")
        self.assertEqual(r["source"], "bocha")

    def test_missing_key_returns_error_item(self):
        os.environ.pop("BOCHA_API_KEY", None)
        results = self.builder("测试", 3)
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertEqual(results[0]["source"], "bocha")

    def test_web_search_payload_uses_dynamic_freshness(self):
        """时效敏感查询应使用 oneDay 而非静态 oneYear。"""
        seen = {}

        def _capture(req, timeout=8):
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp({"data": {"webPages": {"value": []}}})

        with patch("urllib.request.urlopen", _capture):
            self.builder("今日热点新闻", 3)
        self.assertEqual(seen["body"]["freshness"], "oneDay")
        self.assertTrue(seen["body"]["summary"])


class TestBochaAiEngine(unittest.TestCase):
    def setUp(self):
        self._saved_keys = {
            k: os.environ.get(k) for k in ("BOCHA_API_KEY", "ARGO_BOCHA_API_KEY")
        }
        os.environ["BOCHA_API_KEY"] = "sk-test"
        self.builder = _build_bocha_ai_engine({"timeout": 12})

    def tearDown(self):
        for k, v in self._saved_keys.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self, messages):
        payload = {"messages": messages}
        with patch("urllib.request.urlopen", _fake_urlopen(payload)):
            return self.builder("北京天气", 5)

    def test_webpage_message_to_standard_result(self):
        results = self._run([
            {"content_type": "webpage", "content": json.dumps({"value": [_WEB_PAGE]})},
        ])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "北京天气预报-中国天气网")
        self.assertEqual(results[0]["source"], "bocha_ai")
        self.assertNotIn("card_type", results[0])

    def test_modal_card_message_to_card_result(self):
        results = self._run([
            {"content_type": "weather", "content": json.dumps(
                {"city": "北京", "temp": "24", "condition": "多云", "humidity": "55%"})},
        ])
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["card_type"], "weather")
        self.assertIn("city: 北京", r["snippet"])
        self.assertEqual(r["card_data"]["temp"], "24")
        self.assertIn("天气", r["title"])
        self.assertEqual(r["score"], 1.0)

    def test_image_and_empty_messages_skipped(self):
        results = self._run([
            {"content_type": "image", "content": json.dumps({"url": "http://x"})},
            {"content_type": "unknown_type", "content": "{}"},
        ])
        self.assertEqual(results, [])

    def test_mixed_messages_order_preserved(self):
        results = self._run([
            {"content_type": "webpage", "content": json.dumps({"value": [_WEB_PAGE]})},
            {"content_type": "stock", "content": json.dumps({"name": "贵州茅台", "price": "1480.00"})},
        ])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "北京天气预报-中国天气网")
        self.assertEqual(results[1]["card_type"], "stock")

    def test_missing_key_returns_error_item(self):
        os.environ.pop("BOCHA_API_KEY", None)
        results = self.builder("测试", 3)
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertEqual(results[0]["source"], "bocha_ai")


class TestFreshness(unittest.TestCase):
    def test_day_level_sensitive_to_one_day(self):
        for q in ("今日热点新闻", "北京天气 实时", "最新进展", "盘中快讯"):
            self.assertEqual(_bocha_freshness(q), "oneDay", q)

    def test_week_level_to_one_week(self):
        for q in ("本周新能源政策", "本月经济数据", "最近一周的赛事", "past week news"):
            self.assertEqual(_bocha_freshness(q), "oneWeek", q)

    def test_evergreen_to_no_limit(self):
        for q in ("Python 教程", "React 源码分析", "长城在哪", ""):
            self.assertEqual(_bocha_freshness(q), "noLimit", q)


class TestBochaHttp403Safe(unittest.TestCase):
    """403 / 网络错误应由 safe_search 吞掉，返回空列表（不抛、不污染）。"""

    def setUp(self):
        self._saved_keys = {
            k: os.environ.get(k) for k in ("BOCHA_API_KEY", "ARGO_BOCHA_API_KEY")
        }
        os.environ["BOCHA_API_KEY"] = "sk-test"

    def tearDown(self):
        for k, v in self._saved_keys.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_web_403_returns_empty(self):
        import urllib.error

        def _boom(*_a, **_k):
            raise urllib.error.HTTPError(
                "https://api.bochaai.com/v1/web-search", 403, "Forbidden", {}, None
            )

        eng = _build_bocha_engine({"timeout": 3})
        with patch("urllib.request.urlopen", _boom):
            results = eng("今日金价", 3)
        self.assertEqual(results, [])

    def test_ai_403_returns_empty(self):
        import urllib.error

        def _boom(*_a, **_k):
            raise urllib.error.HTTPError(
                "https://api.bochaai.com/v1/ai-search", 403, "Forbidden", {}, None
            )

        eng = _build_bocha_ai_engine({"timeout": 3})
        with patch("urllib.request.urlopen", _boom):
            results = eng("今日金价", 3)
        self.assertEqual(results, [])


class TestModalCardRouting(unittest.TestCase):
    """modal_card 域：命中结构化语义；不误抢 weather/stock/macro/medical。"""

    def test_modal_card_hits(self):
        from route import route_query

        for q in (
            "今天金价多少一克",
            "今日油价",
            "明天杭州到上海高铁",
            "本周星座运势",
            "挂号 三甲医院",
        ):
            d = route_query(q)
            self.assertEqual(d.get("domain"), "modal_card", q)
            engines = d.get("engines") or d.get("engines_combo") or []
            self.assertIn("bocha_ai", engines, q)
            # 纯结构化路径：应含 bocha 兜底，不混 local_bing / openstreetmap
            self.assertIn("bocha", engines, q)
            self.assertNotIn("local_bing", engines, q)
            self.assertNotIn("local_openstreetmap", engines, q)

    def test_modal_card_survives_fast_mode(self):
        """cost_tier=low 的 bocha 在 mode=fast 下不得被 0.85 阈值裁掉。"""
        from route import route_query

        d = route_query("今天金价多少一克", mode="fast")
        self.assertEqual(d.get("domain"), "modal_card")
        engines = d.get("engines") or d.get("engines_combo") or []
        self.assertIn("bocha_ai", engines)
        self.assertIn("bocha", engines)
        self.assertNotIn("anysearch", engines)
        self.assertNotIn("duckduckgo", engines)

    def test_modal_card_keeps_declared_without_api_key(self):
        """缺 BOCHA_API_KEY 时仍保留声明引擎，不静默改走 anysearch。"""
        from route import route_query

        saved = {
            k: os.environ.get(k) for k in ("BOCHA_API_KEY", "ARGO_BOCHA_API_KEY")
        }
        try:
            for k in saved:
                os.environ.pop(k, None)
            d = route_query("今日油价")
            self.assertEqual(d.get("domain"), "modal_card")
            engines = d.get("engines") or d.get("engines_combo") or []
            self.assertIn("bocha_ai", engines)
            self.assertIn("bocha", engines)
            self.assertNotIn("anysearch", engines)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_does_not_steal_other_domains(self):
        from route import route_query

        cases = {
            "北京天气": "weather_query",
            "今日A股收盘": "stock_query",
            "美元兑人民币": "macro_data",
            "咳嗽症状": "medical",
        }
        for q, expect in cases.items():
            d = route_query(q)
            self.assertEqual(d.get("domain"), expect, q)


class TestParseGenericNested(unittest.TestCase):
    """通用解析器应吃到 data.webPages.value 双层嵌套（解析根因修复）。"""

    def test_bocha_shaped_payload(self):
        from engines_base import _parse_generic

        payload = {
            "data": {
                "webPages": {
                    "value": [{
                        "name": "北京天气预报",
                        "url": "https://weather.example/bj",
                        "summary": "晴 18-25",
                    }]
                }
            }
        }
        results = _parse_generic(payload, "bocha")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "北京天气预报")
        self.assertEqual(results[0]["url"], "https://weather.example/bj")
        self.assertIn("晴", results[0]["snippet"])
        self.assertEqual(results[0]["source"], "bocha")

    def test_empty_on_garbage(self):
        from engines_base import _parse_generic

        self.assertEqual(_parse_generic({}, "x"), [])
        self.assertEqual(_parse_generic({"data": {"meta": 1}}, "x"), [])


if __name__ == "__main__":
    unittest.main()

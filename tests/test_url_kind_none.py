#!/usr/bin/env python3
"""tests/test_url_kind_none.py — 声明式 spec 的 url_kind: none（issue #29 口径 B）

无 url 槽的题录/语义片段条目此前被 SERP 闸当壳页整批吞（serp_guard 空url判壳 +
search_pipeline 整条丢弃）。口径 B：spec 显式声明 url_kind: none 后——
  - 行上被 _apply_url_kind 戳上 url_kind，三条产出路径全覆盖（声明式 output_map、
    自定义 JSON parser、CLI 桥）
  - serp_guard.is_unusable_row 只豁免「空 url 判壳页」，SERP 与内容农场判据照走
全程离线（CLI 案例走真子进程但只跑内置 json.dump）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = SKILL_DIR / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import serp_guard as sg  # noqa: E402
import engines_base as eb  # noqa: E402


class TestIsUnusableRow(unittest.TestCase):
    def test_url_kind_none_kept_without_url(self):
        self.assertFalse(sg.is_unusable_row({"url_kind": "none", "title": "纯题录"}))

    def test_url_kind_none_with_serp_url_still_dropped(self):
        self.assertTrue(sg.is_unusable_row(
            {"url_kind": "none", "url": "https://www.bing.com/search?q=a"}))

    def test_url_kind_none_farm_rules_still_apply(self):
        with patch.object(sg, "is_content_farm", return_value=True):
            self.assertTrue(sg.is_unusable_row({"url_kind": "none", "title": "x"}))

    def test_plain_empty_url_still_dropped(self):
        self.assertTrue(sg.is_unusable_row({"title": "没声明就没豁免"}))


class TestApplyUrlKind(unittest.TestCase):
    def test_stamps_dict_rows_only(self):
        rows = [{"title": "a"}, {"title": "b", "url": "https://x"}, "junk"]
        out = eb._apply_url_kind(rows, {"url_kind": "none"})
        self.assertEqual(out[0]["url_kind"], "none")
        self.assertEqual(out[1]["url_kind"], "none")
        self.assertEqual(out[2], "junk")

    def test_no_kind_no_field(self):
        out = eb._apply_url_kind([{"title": "a"}], {})
        self.assertNotIn("url_kind", out[0])


class TestDeclarativeHttpPath(unittest.TestCase):
    SPEC = {
        "url": "https://api.example.com/search?q={query}",
        "output_map": {"items": "results", "item_title": "title",
                       "item_url": "url", "item_summary": "snippet"},
        "url_kind": "none",
    }

    def test_items_without_url_kept_and_stamped(self):
        raw = json.dumps({"results": [
            {"title": "题录一"},
            {"title": "题录二", "url": "https://real/1"},
        ]})
        rows = eb._parse_http_payload(raw, "json", "myeng", 5,
                                      self.SPEC["output_map"], self.SPEC)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.get("url_kind") == "none" for r in rows))
        self.assertEqual(rows[0].get("url", ""), "")

    def test_without_url_kind_untouched(self):
        spec = {k: v for k, v in self.SPEC.items() if k != "url_kind"}
        raw = json.dumps({"results": [{"title": "题录一"}]})
        rows = eb._parse_http_payload(raw, "json", "myeng", 5,
                                      spec["output_map"], spec)
        self.assertNotIn("url_kind", rows[0])


class TestCustomParserPath(unittest.TestCase):
    def test_stamped(self):
        def _parse(data):
            return [{"title": t} for t in data.get("hits", [])]

        with patch.dict(eb._CUSTOM_JSON_PARSERS, {"mycust": _parse}):
            raw = json.dumps({"hits": [{"title": "a"}]})
            rows = eb._parse_http_payload(raw, "json", "mycust", 5, {},
                                          {"url_kind": "none"})
        self.assertEqual(rows[0].get("url_kind"), "none")


class TestCliPath(unittest.TestCase):
    def test_cli_items_without_url_kept(self):
        code = ("import sys,json;"
                "json.dump([{'title':'题录甲'},{'title':'题录乙','url':'https://real/2'}],sys.stdout)")
        spec = {"_name": "fake_cli", "cmd": ["python3", "-c", code],
                "search_args": [], "timeout": 10, "url_kind": "none"}
        eng = eb._build_cli_engine(spec)
        rows = eng("q", 5)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.get("url_kind") == "none" for r in rows))


class TestNcpssdSpec(unittest.TestCase):
    """首个真实受益 spec：无 PDF 直链的条目只有题录，不该被吞。"""

    def test_spec_declares_url_kind_none(self):
        import yaml
        spec = yaml.safe_load(
            (SKILL_DIR / "engines" / "specs" / "ncpssd.yaml").read_text(encoding="utf-8"))
        self.assertEqual(spec.get("url_kind"), "none")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""tests/test_x_search.py — X 关键词搜索（ego-browser 通道）离线单测。

scripts/x_search.py 的离线单测：
  - _build_js：query/base/marker/n/maxScroll 注入、走 data-testid 抽取
  - _extract_json：标记行 JSON 提取（夹杂 notice 行）、坏 JSON 返回 None
  - _handle_from_url / _rows：统一 schema、去非法 URL、互动数映射
  - main() 全链路（mock subprocess）：成功出 JSON、真·空结果 rc=0、
    ego 缺失 rc=1、子进程失败 rc=1、未渲染 rc=1、空查询 rc=1

全程 mock ego-browser 子进程，离线必过（真实通道慢且依赖登录态）。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = SKILL_DIR / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import x_search as xs  # noqa: E402

MARKER = xs.JSON_MARKER


def _stdout(items, ok=True) -> str:
    return ("[ego-browser:notice] 提示行（应被忽略）\n"
            + MARKER + json.dumps({"ok": ok, "items": items}, ensure_ascii=False))


def _run_main(argv, *, stdout="", rc=0, which="ego-browser"):
    m = Mock()
    m.returncode = rc
    m.stdout = stdout
    m.stderr = "" if rc == 0 else "boom"
    buf = io.StringIO()
    with patch.object(xs.shutil, "which", return_value=which), \
         patch.object(xs.subprocess, "run", return_value=m), \
         contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = xs.main(argv)
    return code, buf.getvalue()


class TestBuildJs(unittest.TestCase):
    def test_injects_query_and_marker(self):
        js = xs._build_js("from:OpenAI -is:retweet", 15, "live")
        self.assertIn("from:OpenAI -is:retweet", js)
        self.assertIn(MARKER, js)
        self.assertIn(xs.X_SEARCH_BASE, js)
        self.assertIn('"n": 15', js)
        self.assertIn('"sort": "live"', js)

    def test_extraction_uses_testid_not_class(self):
        js = xs._build_js("q", 5, "top")
        self.assertIn('data-testid="cellInnerDiv"', js)
        self.assertIn('data-testid="tweetText"', js)


class TestExtractJson(unittest.TestCase):
    def test_skips_notice_lines(self):
        self.assertEqual(xs._extract_json(_stdout([])), {"ok": True, "items": []})

    def test_absent_and_broken(self):
        self.assertIsNone(xs._extract_json("nothing"))
        self.assertIsNone(xs._extract_json(MARKER + "{broken"))


class TestHandleAndRows(unittest.TestCase):
    def test_handle_from_url(self):
        self.assertEqual(
            xs._handle_from_url("https://x.com/OpenAI/status/123"), "OpenAI")
        self.assertEqual(xs._handle_from_url("garbage"), "")

    def test_rows_schema(self):
        items = [{"text": "hello world", "href": "https://x.com/a/status/9",
                  "published_at": "2026-10-15T00:00:00.000Z",
                  "likes": 3, "reposts": 2, "replies": 1}]
        rows = xs._rows(items, 5)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["url"], "https://x.com/a/status/9")
        self.assertEqual(r["source"], "x_search")
        self.assertEqual(r["social_meta"]["author"], "a")
        self.assertEqual(r["social_meta"]["likes"], 3)

    def test_rows_drops_non_http(self):
        self.assertEqual(xs._rows([{"href": "javascript:alert(1)"}], 5), [])


class TestMain(unittest.TestCase):
    ITEMS = [{"text": "t", "href": "https://x.com/u/status/1",
              "published_at": "", "likes": 0, "reposts": 0, "replies": 0}]

    def test_success_json(self):
        code, out = _run_main(["OpenAI", "--json"], stdout=_stdout(self.ITEMS))
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(rows[0]["url"], "https://x.com/u/status/1")

    def test_success_human(self):
        code, out = _run_main(["OpenAI"], stdout=_stdout(self.ITEMS))
        self.assertEqual(code, 0)
        self.assertIn("https://x.com/u/status/1", out)

    def test_genuine_empty_is_ok(self):
        """真·空结果（页面渲染完成、无命中）应 rc=0，不是失败。"""
        code, out = _run_main(["zzz", "--json"], stdout=MARKER + json.dumps({"ok": True, "empty": True, "items": []}))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_unrendered_is_failure(self):
        code, _ = _run_main(["OpenAI"], stdout=_stdout([], ok=False))
        self.assertEqual(code, 1)

    def test_ego_missing(self):
        code, _ = _run_main(["OpenAI"], which=None)
        self.assertEqual(code, 1)

    def test_subprocess_failure(self):
        code, _ = _run_main(["OpenAI"], stdout="", rc=3)
        self.assertEqual(code, 1)

    def test_empty_query(self):
        code, _ = _run_main(["   "])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

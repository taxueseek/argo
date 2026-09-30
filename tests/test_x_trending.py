#!/usr/bin/env python3
"""tests/test_x_trending.py — X (Twitter) Trending 热榜引擎（ego-browser 通道）

scripts/x_trending.py（cli 引擎 x_trending）离线单测：
  - _build_js：URL/等待秒数/条数上限注入、无状态文件痕迹
  - _extract_json：标记行 JSON 提取（夹杂 notice 行）
  - main() 全链路（mock subprocess）：成功出 YAML（排名/话题/分类/链接）、
    空结果 rc=1、ego 缺失 rc=1、子进程失败 rc=1
  - 解析语义：X 的「·」分隔行不得混入分类或话题

全程 mock ego-browser 子进程，离线必过。
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

import x_trending as xt  # noqa: E402

MARKER = xt.JSON_MARKER


def _ego_stdout(items: list, ok: bool = True) -> str:
    return (
        "[ego-browser:notice] 提示行（应被忽略）\n"
        + MARKER + json.dumps({"ok": ok, "items": items}, ensure_ascii=False)
    )


def _rows_from_stdout(stdout: str) -> list:
    """跑 main() 并取回它打到 stdout 的 YAML（用 capture 模拟）。"""
    import io
    import contextlib
    from unittest.mock import Mock

    def fake_run(cmd, **kw):
        m = Mock()
        m.returncode = 0
        m.stdout = stdout
        m.stderr = ""
        return m

    buf = io.StringIO()
    with patch.object(xt.shutil, "which", return_value="ego-browser"), \
         patch.object(xt.subprocess, "run", side_effect=fake_run), \
         contextlib.redirect_stdout(buf):
        code = xt.main(["--n", "10"])
    import yaml
    return code, (yaml.safe_load(buf.getvalue()) or [])


class TestBuildJs(unittest.TestCase):
    def test_injects_url_wait_and_n(self):
        js = xt._build_js(7)
        self.assertIn(xt.X_TRENDING_URL, js)
        self.assertIn(f'"n": 7', js)
        self.assertIn(str(xt.JS_RESULT_WAIT_S), js)

    def test_no_state_file_touch(self):
        """零状态面：脚本不得出现状态文件/锁的读写（2026-09-30 设计取舍）。"""
        src = (SCRIPT_DIR / "x_trending.py").read_text(encoding="utf-8")
        for banned in ("makedirs", "state_path", "flock", 'open('):
            self.assertNotIn(banned, src, f"不应出现 {banned}")


class TestExtractJson(unittest.TestCase):
    def test_skips_notice_lines(self):
        self.assertEqual(xt._extract_json(_ego_stdout([])), {"ok": True, "items": []})

    def test_absent_and_broken(self):
        self.assertIsNone(xt._extract_json("nothing here"))
        self.assertIsNone(xt._extract_json(MARKER + "{broken"))


class TestMain(unittest.TestCase):
    ITEMS = [
        {"rank": "1", "category": "娱乐 趋势", "topic": "#Qさま",
         "href": "https://x.com/search?q=%23Q%E3%81%95%E3%81%BE"},
        {"rank": "2", "category": "全球趋势", "topic": "supercell", "href": ""},
    ]

    def test_success_rows(self):
        code, rows = _rows_from_stdout(_ego_stdout(self.ITEMS))
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "1. #Qさま")
        self.assertEqual(rows[0]["snippet"], "X Trending | 娱乐 趋势")
        self.assertTrue(rows[0]["url"].startswith("https://x.com/search?q="))
        # href 缺失时按话题拼搜索链接（分类不入链接）
        self.assertEqual(rows[1]["url"], "https://x.com/search?q=supercell")

    def test_empty_is_failure(self):
        code, rows = _rows_from_stdout(_ego_stdout([], ok=False))
        self.assertEqual(code, 1)
        self.assertEqual(rows, [])

    def test_ego_missing(self):
        with patch.object(xt.shutil, "which", return_value=None):
            self.assertEqual(xt.main(["--n", "5"]), 1)

    def test_subprocess_failure(self):
        from unittest.mock import Mock
        m = Mock()
        m.returncode = 3
        m.stdout = ""
        m.stderr = "boom"
        with patch.object(xt.shutil, "which", return_value="ego-browser"), \
             patch.object(xt.subprocess, "run", return_value=m):
            self.assertEqual(xt.main(["--n", "5"]), 1)


class TestFocus(unittest.TestCase):
    """位置参数 query 的语义：泛词看全榜，关键词聚焦（无命中回落全榜）。"""

    ROWS = [
        {"title": "1. #AI艺术", "snippet": "X Trending | 科技 趋势"},
        {"title": "2. supercell", "snippet": "X Trending | 全球趋势"},
    ]

    def test_generic_words_pass_through(self):
        for q in ("", "热榜", "热搜", "trending", "HOT"):
            self.assertEqual(xt._focus(self.ROWS, q), self.ROWS, q)

    def test_keyword_filters(self):
        out = xt._focus(self.ROWS, "AI")
        self.assertEqual(len(out), 1)
        self.assertIn("AI", out[0]["title"])

    def test_no_hit_falls_back_to_full_board(self):
        self.assertEqual(xt._focus(self.ROWS, "量子计算"), self.ROWS)


class TestParseSemantics(unittest.TestCase):
    """X 的行结构含「·」分隔行——不得混入分类或话题（live 实测踩过）。"""

    def test_separator_dot_not_in_category(self):
        js = xt._build_js(5)
        self.assertIn('!== "·"', js)          # 分隔行被显式过滤
        self.assertIn("lines[lines.length - 1]", js)   # 话题取末行
        self.assertIn("lines[lines.length - 2]", js)   # 分类取话题前一行


if __name__ == "__main__":
    unittest.main()

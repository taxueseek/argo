#!/usr/bin/env python3
"""tests/test_sogou_weixin_browser.py — 搜狗微信·浏览器兜底通道

scripts/sogou_weixin_browser.py（cli 引擎 sogou_weixin_browser）离线单测：
  - _build_js：查询词编码/引号换行逃逸、URL 注入、空间句柄透传
  - _assemble_link：url+= 拼接 → mp.weixin 直链回落 → 中间链降级（同 HTTP 车道判据）
  - _norm_published_at / _extract_json / _safe_state_path
  - main() 全链路（mock subprocess）：成功 rc=0 + 空间句柄持久化、反爬 rc=1、
    ego 缺失 rc=1、最小间隔补睡/不睡
  - cli 引擎全链路：_build_cli_engine 按 spec 调脚本，YAML 经 _parse_text_output
    进结果管道（account 折进 snippet 头部因解析器不保留额外字段）

全程 mock ego-browser 子进程与 time.sleep，离线必过。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = SKILL_DIR / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import sogou_weixin_browser as swb  # noqa: E402

MARKER = swb.JSON_MARKER


def _ego_stdout(items: list, space_id: int = 42, blocked: bool = False) -> str:
    return (
        "[ego-browser:notice] 某轮提示行（应被忽略）\n"
        + MARKER
        + json.dumps({"ok": not blocked, "space_id": space_id,
                      "blocked": blocked, "got": not blocked, "items": items},
                     ensure_ascii=False)
    )


class TestBuildJs(unittest.TestCase):
    def test_query_encoded(self):
        js = swb._build_js('引号"与换行\n及\'单引号', 5, 2, 5, None)
        self.assertIn("https://weixin.sogou.com/weixin?type=2&query=", js)
        goto_arg = js.split("goto(")[1].split(")")[0]
        self.assertNotIn('"', goto_arg[1:-1])  # 引号只出现在 JSON 字面量定界符上
        self.assertIn("%E5%BC%95%E5%8F%B7", js)  # 「引」已 percent-encode

    def test_space_id_passthrough(self):
        self.assertIn('"spaceId": 7', swb._build_js("测试", 5, 2, 5, 7))

    def test_no_space_creates_fresh(self):
        self.assertIn('"spaceId": null', swb._build_js("测试", 5, 2, 5, None))

    def test_marker_and_limits_injected(self):
        js = swb._build_js("测试", 3, 1, 2, None)
        self.assertIn(swb.JSON_MARKER, js)
        self.assertIn('"resolveLimit": 2', js)
        self.assertIn("type=1", js)


class TestAssembleLink(unittest.TestCase):
    ORIGINAL = "https://weixin.sogou.com/link?url=abc"

    def test_frag_join_wins(self):
        real, ok = swb._assemble_link(
            ["https://mp.weixin.qq.com/s?src=11", "&sig=xx"], "", self.ORIGINAL)
        self.assertTrue(ok)
        self.assertEqual(real, "https://mp.weixin.qq.com/s?src=11&sig=xx")

    def test_fallback_url(self):
        real, ok = swb._assemble_link([], "https://mp.weixin.qq.com/s?x=1", self.ORIGINAL)
        self.assertTrue(ok)
        self.assertEqual(real, "https://mp.weixin.qq.com/s?x=1")

    def test_degrade_keeps_intermediate(self):
        real, ok = swb._assemble_link([], "", self.ORIGINAL)
        self.assertFalse(ok)
        self.assertEqual(real, self.ORIGINAL)


class TestNormAndExtract(unittest.TestCase):
    def test_published_at(self):
        iso = swb._norm_published_at("1790740013")
        self.assertIn("T", iso)  # ISO 且带本时区偏移
        self.assertEqual(swb._norm_published_at(""), "")
        self.assertEqual(swb._norm_published_at("abc"), "")

    def test_extract_json_skips_noise(self):
        out = "notice line\nanother\n" + MARKER + '{"ok": true}'
        self.assertEqual(swb._extract_json(out), {"ok": True})
        self.assertIsNone(swb._extract_json("no marker here"))
        self.assertIsNone(swb._extract_json(MARKER + "{broken"))

    def test_extract_json_stderr_fallback(self):
        # ego-browser 的 console.log 实际走 stderr（2026-09-30 实测），两流都要找
        self.assertEqual(swb._extract_json(""), None)


class TestSafeStatePath(unittest.TestCase):
    def test_rejects_traversal(self):
        with self.assertRaises(ValueError):
            swb._safe_state_path("~/.config/argo/../evil.json")

    def test_rejects_outside_allowlist(self):
        with self.assertRaises(ValueError):
            swb._safe_state_path("/Users/evil/other.json")

    def test_accepts_default_and_tmp(self):
        self.assertTrue(swb._safe_state_path(swb.STATE_PATH))
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(swb._safe_state_path(str(Path(td) / "s.json")))


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.tmp.name) / "sogou_state.json")
        patcher = patch.object(swb, "STATE_PATH", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_main(self, argv, ego_stdout, which="ego-browser", rc=0):
        """mock ego 子进程与 sleep（记录补睡秒数），返回 (rc, run_mock, sleeps)。"""
        sleeps: list = []

        def fake_run(cmd, **kw):
            m = unittest.mock.Mock()
            m.returncode = rc
            m.stdout = ego_stdout
            m.stderr = ""
            return m

        with patch.object(swb.shutil, "which", return_value=which), \
             patch.object(swb.subprocess, "run", side_effect=fake_run) as run_mock, \
             patch.object(swb.time, "sleep", side_effect=sleeps.append):
            code = swb.main(argv)
        return code, run_mock, sleeps

    def test_success_persists_space_handle(self):
        code, run_mock, _ = self._run_main(
            ["测试", "--n", "5"], _ego_stdout([{"title": "t", "url": "u"}]))
        self.assertEqual(code, 0)
        state = json.loads(Path(self.state).read_text())
        self.assertEqual(state["space_id"], 42)
        self.assertGreater(state["last_ts"], 0)
        cmd = run_mock.call_args[0][0]
        self.assertIn("ego-browser", cmd)
        self.assertIn("nodejs", cmd)
        self.assertIn(swb.JSON_MARKER, cmd[-1])

    def test_blocked_returns_1(self):
        code, _, _ = self._run_main(["测试"], _ego_stdout([], blocked=True))
        self.assertEqual(code, 1)

    def test_ego_missing_returns_1(self):
        code, _, _ = self._run_main(["测试"], "", which=None)
        self.assertEqual(code, 1)

    def test_subprocess_failure_returns_1(self):
        code, _, _ = self._run_main(["测试"], "", rc=3)
        self.assertEqual(code, 1)

    def test_min_interval_sleeps_then_not(self):
        code, _, _ = self._run_main(["测试"], _ego_stdout([]))
        self.assertEqual(code, 0)
        # 紧接第二次调用：应触发补睡
        code2, _, sleeps2 = self._run_main(["测试"], _ego_stdout([]))
        self.assertEqual(code2, 0)
        self.assertTrue(len(sleeps2) >= 1 and 0 < sleeps2[0] <= swb.MIN_INTERVAL_CAP)
        # 人为把 last_ts 拨旧：不再补睡
        st = json.loads(Path(self.state).read_text())
        st["last_ts"] = time.time() - 3600
        Path(self.state).write_text(json.dumps(st))
        code3, _, sleeps3 = self._run_main(["测试"], _ego_stdout([]))
        self.assertEqual(code3, 0)
        self.assertEqual(sleeps3, [])


class TestCliEngineChain(unittest.TestCase):
    """spec → _build_cli_engine → 脚本 stdout → _parse_text_output 全链路。"""

    SPEC = {
        "_name": "sogou_weixin_browser",
        "cmd": ["python3", "scripts/sogou_weixin_browser.py"],
        "search_args": ["{query}", "--n", "{n}"],
        "output_format": "yaml",
        "timeout": 75,
    }

    def test_yaml_output_enters_pipeline(self):
        from engines_base import _build_cli_engine

        yaml_out = (
            "- title: 我把浏览器搜索引擎换成了 DeepSeek\n"
            "  url: https://mp.weixin.qq.com/s?src=11&sig=x\n"
            "  snippet: 公众号「PandaAI」 一行链接开启深度思考\n"
            "  published_at: 2026-09-30T12:26:53+08:00\n"
            "  url_resolved: true\n"
            "- title: 只剩中间链的条目\n"
            "  url: https://weixin.sogou.com/link?url=abc\n"
            "  url_resolved: false\n"
        )
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            m = unittest.mock.Mock()
            m.returncode = 0
            m.stdout = yaml_out
            m.stderr = ""
            return m

        engine = _build_cli_engine(self.SPEC)
        with patch("subprocess.run", side_effect=fake_run):
            rows = engine("我把浏览器搜索引擎换成了 DeepSeek", n=5)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "我把浏览器搜索引擎换成了 DeepSeek")
        self.assertTrue(rows[0]["url"].startswith("https://mp.weixin.qq.com/"))
        self.assertEqual(rows[0]["source"], "sogou_weixin_browser")
        # argo 的 yaml 加载器把 ISO 时间戳解析成 datetime 对象，管道里 str()
        # 后是空格分隔形态——CLI yaml 引擎的固有行为，断言按实际契约锁死
        self.assertEqual(rows[0]["published_at"], "2026-09-30 12:26:53+08:00")
        self.assertTrue(any("sogou_weixin_browser.py" in c for c in captured["cmd"]))
        self.assertIn("我把浏览器搜索引擎换成了 DeepSeek", captured["cmd"])


if __name__ == "__main__":
    unittest.main()

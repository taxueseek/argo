#!/usr/bin/env python3
"""社交引擎 CLI 静默失效回归（2026-11-03 issue #36 / PR #37）。

守两件事：

1. `xiaohongshu`：`search()` 必须给 `xhs` 传 `--json`。不传时 xhs-cli 只打印
   人类可读文本，`_parse_xhs_output` 的 json.loads 失败 → 静默返回空列表，
   与「查了没有」同形，是最难查的一类故障。
2. `twitter`：任一通道的**错误占位**不得阻断后续兜底通道。
   - `search_fxtwitter` 在异常时返回 `[{"error": ...}]`（非空），旧代码
     `if results: return results` 会把它当有效结果直接返回；
   - `tw` CLI 返回 `{"error": "No tweets found for ..."}` 时，旧解析器
     把它当成一条 tweet 产出（无 text 无 url 的空壳）。

运行：
  python3 -m pytest tests/test_social_cli_fallback.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "social_engines"))

from social_engines import twitter_engine as tw  # noqa: E402
from social_engines import xiaohongshu_engine as xhs  # noqa: E402


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestXhsJsonFlag(unittest.TestCase):
    """xhs-cli 的 --json 必须出现在实际下发的命令行里。"""

    def test_search_passes_json_flag(self):
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc(0, json.dumps({"items": []}))

        with patch.object(xhs.subprocess, "run", side_effect=fake_run):
            xhs.search("露营 攻略", n=3)

        self.assertIn("--json", captured["cmd"],
                      "xhs search 未传 --json：CLI 会返回纯文本，解析必然失败")
        self.assertEqual(captured["cmd"][:2], ["xhs", "search"])

    def test_not_authenticated_surfaces_error(self):
        """未登录要带可归因 error，而不是静默返回空。"""
        out = xhs.search.__name__  # 触发模块已导入
        self.assertEqual(out, "search")
        with patch.object(xhs.subprocess, "run",
                          return_value=_FakeProc(1, "NoCookieError: not logged in")):
            res = xhs.search("测试")
        self.assertEqual(len(res), 1)
        self.assertIn("not_authenticated", res[0]["error"])

    def test_plain_text_output_yields_no_results(self):
        """不带 --json 的纯文本输出解析不出东西——这正是要传 flag 的原因。"""
        with patch.object(xhs.subprocess, "run",
                          return_value=_FakeProc(0, "### 1. 标题\n- **URL**: https://x\n")):
            self.assertEqual(xhs.search("测试"), [])


class TestTwitterCliFallback(unittest.TestCase):
    """twitter 兜底链：任一通道的错误占位都不得阻断后续通道。"""

    def test_fxtwitter_error_placeholder_does_not_block_cli(self):
        """FxTwitter 返 error 占位时应继续走 CLI，而不是把它当结果返回。"""
        cli_envelope = json.dumps({
            "ok": True,
            "data": [{
                "id": "1900000000000000001",
                "text": "from twitter-cli",
                "author": {"screenName": "insider", "name": "Insider"},
                "metrics": {"likes": 42, "retweets": 7, "replies": 3, "views": 900},
                "createdAtISO": "2026-10-30T17:00:00+00:00",
                "url": "https://x.com/insider/status/1900000000000000001",
            }],
        })
        with patch.object(tw, "search_fxtwitter",
                          return_value=[{"error": "fxtwitter timeout", "source": "twitter"}]), \
             patch.object(tw.subprocess, "run", return_value=_FakeProc(0, cli_envelope)) as m:
            res = tw.search("OpenAI", n=3)

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["url"], "https://x.com/insider/status/1900000000000000001")
        self.assertEqual(res[0]["social_meta"]["author"], "insider")
        self.assertEqual(res[0]["social_meta"]["likes"], 42)
        self.assertEqual(res[0]["published_at"], "2026-10-30T17:00:00+00:00")
        # 先试 twitter CLI，再退 tw
        self.assertEqual(m.call_args[0][0][:3], ["twitter", "search", "OpenAI"])
        self.assertIn("--json", m.call_args[0][0])

    def test_error_dict_from_cli_is_not_a_tweet(self):
        """tw 返回错误字典时必须被跳过，且继续尝试下一个通道。"""
        with patch.object(tw, "search_fxtwitter", return_value=[]), \
             patch.object(tw.subprocess, "run",
                          return_value=_FakeProc(
                              0, json.dumps({"error": "No tweets found for OpenAI"}))), \
             patch.object(tw, "search_nitter", return_value=[{"title": "nitter ok"}]) as m_nitter:
            res = tw.search("OpenAI", n=3)

        self.assertEqual(res, [{"title": "nitter ok"}])
        self.assertTrue(m_nitter.called, "错误字典不应被当成结果，应继续兜底")

    def test_all_channels_down_returns_fx_error(self):
        """全通道不可用时把 FxTwitter 的 error 占位带回去（失败可见契约）。"""
        with patch.object(tw, "search_fxtwitter",
                          return_value=[{"error": "boom", "source": "twitter"}]), \
             patch.object(tw.subprocess, "run", side_effect=FileNotFoundError()), \
             patch.object(tw, "search_nitter", return_value=[]):
            res = tw.search("OpenAI", n=3)
        self.assertEqual(res, [{"error": "boom", "source": "twitter"}])


class TestParseTwJsonFormats(unittest.TestCase):
    """_parse_tw_json 必须认三种输出形态，并跳过错误字典。"""

    def test_jsonl(self):
        raw = "\n".join([
            json.dumps({"id": "1", "text": "first", "user": {"screen_name": "a"}}),
            json.dumps({"id": "2", "text": "second", "user": {"screen_name": "b"}}),
        ])
        res = tw._parse_tw_json(raw, 5)
        self.assertEqual([r["title"] for r in res], ["first", "second"])
        self.assertEqual(res[0]["social_meta"]["author"], "a")

    def test_array(self):
        raw = json.dumps([{"id": "1", "text": "solo"}])
        res = tw._parse_tw_json(raw, 5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "solo")

    def test_envelope(self):
        raw = json.dumps({"ok": True, "data": [
            {"id": "1", "text": "env", "author": {"screenName": "z"},
             "metrics": {"likes": 5}}]})
        res = tw._parse_tw_json(raw, 5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["social_meta"]["likes"], 5)

    def test_single_object(self):
        """单条 tweet 对象也要认——PR #37 的原实现会在这里返回空。"""
        raw = json.dumps({"id": "1", "text": "single", "user": {"screen_name": "s"}})
        res = tw._parse_tw_json(raw, 5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "single")

    def test_error_envelope_and_dict_skipped(self):
        for raw in (
            json.dumps({"ok": False, "data": []}),
            json.dumps({"ok": 0, "data": []}),
            json.dumps({"error": "No tweets found for x"}),
        ):
            self.assertEqual(tw._parse_tw_json(raw, 5), [], f"应跳过：{raw}")

    def test_empty_and_garbage(self):
        self.assertEqual(tw._parse_tw_json("", 5), [])
        self.assertEqual(tw._parse_tw_json("   \n ", 5), [])
        self.assertEqual(tw._parse_tw_json("not json at all", 5), [])

    def test_respects_n(self):
        raw = json.dumps([{"id": str(i), "text": f"t{i}"} for i in range(10)])
        self.assertEqual(len(tw._parse_tw_json(raw, 3)), 3)


if __name__ == "__main__":
    unittest.main()

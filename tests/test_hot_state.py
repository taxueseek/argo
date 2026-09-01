#!/usr/bin/env python3
"""热生效基建回归（2026-08-30）。

覆盖四条机制：
  1. hot_state.fingerprint / HotFile：mtime+size 签名语义
  2. should_reload：基线 → 变更 → 消费；ARGO_NO_AUTORELOAD 护栏
  3. engine_env 密钥文件热读：~/.config/argo/env 兜底、os.environ 优先、
     改文件无需重启
  4. quota 跨进程热读：另一进程写状态文件，本进程无需重启即可见
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import hot_state  # noqa: E402
from hot_state import HotFile, fingerprint  # noqa: E402
import engine_env  # noqa: E402
import quota as quota_mod  # noqa: E402


class TestFingerprint(unittest.TestCase):

    def test_changes_on_mtime_and_is_stable(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.py"
            p.write_text("x=1\n")
            fp1 = fingerprint([p])
            os.utime(p, (time.time() + 2, time.time() + 2))
            fp2 = fingerprint([p])
            self.assertNotEqual(fp1, fp2, "mtime 变化必须改变指纹")
            self.assertEqual(fp2, fingerprint([p]), "同状态指纹必须稳定")

    def test_missing_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            ghost = Path(td) / "ghost.py"
            self.assertEqual(fingerprint([ghost]), fingerprint([ghost]))


class TestHotFile(unittest.TestCase):

    def test_baseline_change_heal_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "f.txt"
            p.write_text("a")
            hot = HotFile(p)
            self.assertFalse(hot.changed(), "首次调用是建基线")
            os.utime(p, (time.time() + 2, time.time() + 2))
            self.assertTrue(hot.changed())
            self.assertFalse(hot.changed(), "不重复报告同一签名")
            p.unlink()
            self.assertTrue(hot.changed(), "删除也是变更")
            self.assertFalse(hot.changed())


class TestShouldReload(unittest.TestCase):

    def setUp(self):
        hot_state._last_fingerprint = None
        hot_state._last_check = 0.0

    def tearDown(self):
        hot_state._last_fingerprint = None
        hot_state._last_check = 0.0

    def test_baseline_then_change_then_consumed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.py"
            p.write_text("1\n")
            with patch.object(hot_state, "code_paths", lambda: [p]):
                self.assertFalse(hot_state.should_reload(min_interval=0), "基线")
                os.utime(p, (time.time() + 2, time.time() + 2))
                self.assertTrue(hot_state.should_reload(min_interval=0), "变更")
                self.assertFalse(hot_state.should_reload(min_interval=0), "已消费")

    def test_no_autoreload_guard(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.py"
            p.write_text("1\n")
            with patch.object(hot_state, "code_paths", lambda: [p]), \
                 patch.dict(os.environ, {"ARGO_NO_AUTORELOAD": "1"}):
                hot_state.should_reload(min_interval=0)  # 建基线
                os.utime(p, (time.time() + 2, time.time() + 2))
                self.assertFalse(hot_state.should_reload(min_interval=0),
                                 "护栏开关必须生效")


class TestEnvFileHotRead(unittest.TestCase):
    """get_env 的 ~/.config/argo/env 兜底与热轮换。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        # 双重隔离：HOME 重定向（env 文件路径）+ 清掉宿主 shell 的同名密钥。
        # 只 patch HOME 不够——get_env 的 os.environ 优先级高于 env 文件，
        # 宿主已 export ZHIHU_ACCESS_SECRET 等密钥时断言必然落空。
        env_keys = [k for k in os.environ
                    if k.startswith("ARGO_")
                    or k in ("ZHIHU_ACCESS_SECRET", "BOCHA_API_KEY",
                             "EXA_API_KEY", "OCTEN_API_KEY", "TAVILY_API_KEY",
                             "TINYFISH_API_KEY", "ANYSEARCH_API_KEY",
                             "WEB_SEARCH_API_KEY", "WEREAD_API_KEY")]
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self._saved = {k: os.environ.pop(k) for k in env_keys}
        self._home = patch.dict(os.environ, {"HOME": str(self.tmp)})
        self._home.start()
        engine_env._envfile_sig = ()
        engine_env._envfile_cache = {}

    def tearDown(self):
        self._home.stop()
        os.environ.update(self._saved)
        self._env.stop()
        engine_env._envfile_sig = ()
        engine_env._envfile_cache = {}
        self._td.cleanup()

    def _write(self, content: str):
        cfg = self.tmp / ".config" / "argo"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "env").write_text(content)
        os.utime(cfg / "env", (time.time() + 1, time.time() + 1))

    def test_file_fallback_and_parsing(self):
        self._write('export ZHIHU_ACCESS_SECRET="sec-abc"\n'
                    "BOCHA_API_KEY=bo-xyz\n"
                    "# 注释行\n"
                    "这行没有等号\n")
        self.assertEqual(engine_env.get_env("ZHIHU_ACCESS_SECRET"), "sec-abc")
        self.assertEqual(engine_env.get_env("BOCHA_API_KEY"), "bo-xyz")
        self.assertEqual(engine_env.get_env(["ARGO_EXA_API_KEY", "EXA_API_KEY"], "dft"), "dft")

    def test_os_environ_takes_precedence(self):
        self._write("export ZHIHU_ACCESS_SECRET='from-file'\n")
        with patch.dict(os.environ, {"ZHIHU_ACCESS_SECRET": "from-env"}):
            self.assertEqual(engine_env.get_env("ZHIHU_ACCESS_SECRET"), "from-env")

    def test_hot_rotation_without_restart(self):
        self._write("export ZHIHU_ACCESS_SECRET='old'\n")
        self.assertEqual(engine_env.get_env("ZHIHU_ACCESS_SECRET"), "old")
        self._write("export ZHIHU_ACCESS_SECRET='new'\n")  # 改文件即生效
        self.assertEqual(engine_env.get_env("ZHIHU_ACCESS_SECRET"), "new")

    def test_missing_file_is_clean(self):
        self.assertEqual(engine_env.get_env("ZHIHU_ACCESS_SECRET", "dft"), "dft")


    def test_zhihu_style_code_message_envelope(self):
        """知乎顶层 Code/Message 封套：Code=20001 鉴权失败必须暴露为错误。"""
        from engines_base import _parse_http_payload
        raw = json.dumps({"Code": 20001, "Message": "Authorization failed",
                          "Data": None})
        out = _parse_http_payload(raw, "", "zhihu", 3,
                                  {"items": "Data.Items", "item_title": "Title"}, {})
        self.assertEqual(len(out), 1)
        self.assertIn("20001", out[0]["error"])
        # Code=0 成功封套不得误判
        ok_raw = json.dumps({"Code": 0, "Message": "success",
                             "Data": {"Items": [{"Title": "t", "Url": "https://x"}]}})
        out2 = _parse_http_payload(ok_raw, "", "zhihu", 3,
                                   {"items": "Data.Items", "item_title": "Title"}, {})
        self.assertEqual(len(out2), 1)
        self.assertNotIn("error", out2[0])


class TestQuotaCrossProcessHotRead(unittest.TestCase):
    """另一进程写 quota.json，本进程无需重启即看到（配额自愈全局化）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.prof = Path(self._td.name) / "profiles.json"
        self.state = Path(self._td.name) / "quota.json"
        self.prof.write_text(json.dumps({
            "byted": {"limit": 5, "period": "day", "qps": 5, "cost_tier": "free"},
        }))
        self.state.write_text("{}")
        self._patches = [
            patch.object(quota_mod, "QUOTA_PROFILES_PATH", self.prof),
            patch.object(quota_mod, "QUOTA_STATE_PATH", self.state),
        ]
        for p in self._patches:
            p.start()
        self.mgr = quota_mod.QuotaManager()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._td.cleanup()

    def test_remote_mark_visible_without_restart(self):
        self.assertFalse(self.mgr.is_remote_exhausted("byted"))
        # 「另一个进程」直接写状态文件（真实场景：CLI 标记，server 侧可见）
        st = json.loads(self.state.read_text())
        st["byted"] = {"remote_exhausted": {
            "until": time.time() + 3600, "reason": "10406", "marked_at": time.time()}}
        self.state.write_text(json.dumps(st))
        self.assertTrue(self.mgr.is_remote_exhausted("byted"),
                        "跨进程标记必须热可见")

    def test_profiles_change_reflected(self):
        self.assertEqual(self.mgr.get_remaining_ratio("byted"), 1.0)
        self.prof.write_text(json.dumps({
            "byted": {"limit": 10, "period": "day", "qps": 5, "cost_tier": "free"},
        }))
        os.utime(self.prof, (time.time() + 1, time.time() + 1))
        self.mgr.record("byted", success=True)  # used=1
        self.assertEqual(self.mgr.get_remaining_ratio("byted"), 0.9,
                         "profile 热读后按新 limit 计算")


if __name__ == "__main__":
    unittest.main()

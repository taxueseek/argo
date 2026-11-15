#!/usr/bin/env python3
"""社交引擎 CLI 静默失效回归（2026-11-03 issue #36 / PR #37）。

守一件事：`xiaohongshu` 的 `search()` 必须给 `xhs` 传 `--json`。不传时
xhs-cli 只打印人类可读文本，`_parse_xhs_output` 的 json.loads 失败 → 静默
返回空列表，与「查了没有」同形，是最难查的一类故障。

原第 2 组（twitter 兜底链：错误占位不阻断后续通道、`_parse_tw_json` 三形态）
随 `social_engines/twitter_engine.py` 一并删除（2026-11-15）：该文件零生产
调用者，`twitter` 引擎自 2026-10-21 改走 `scripts/x_search.py`，兜底链与
CLI 解析器已无宿主。twitter 声明式引擎的注册与 live 连通改由
tests/test_fxtwitter.py 守。

运行：
  python3 -m pytest tests/test_social_cli_fallback.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "social_engines"))

from social_engines import xiaohongshu_engine as xhs  # noqa: E402


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestXhsJsonFlag(unittest.TestCase):
    """xhs-cli 的 --json 必须出现在实际下发的命令行里。"""

    def test_search_passes_json_flag(self):
        with patch.object(xhs.subprocess, "run",
                          return_value=_FakeProc(0, '{"results": []}')) as m:
            xhs.search("测试", n=3)
        argv = m.call_args[0][0]
        self.assertIn("--json", argv)

    def test_not_authenticated_surfaces_error(self):
        with patch.object(xhs.subprocess, "run",
                          return_value=_FakeProc(1, "", "未登录")):
            res = xhs.search("测试", n=3)
        self.assertEqual(res, [])

    def test_plain_text_output_yields_no_results(self):
        """CLI 未接 --json 时输出人类可读文本：必须解析失败返回空，不崩。"""
        with patch.object(xhs.subprocess, "run",
                          return_value=_FakeProc(0, "搜索到的笔记：\n1. xxx")):
            self.assertEqual(xhs.search("测试"), [])


if __name__ == "__main__":
    unittest.main()

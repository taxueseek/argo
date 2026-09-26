#!/usr/bin/env python3
"""批次十引擎测试 — zhihu_global v2。

覆盖（全部离线，打桩打在读取处——模块属性 b10.http_open / b10.get_env）：
  1. zhihu_global v2：带 site:/since 过滤时候选池取满（Count=20）再客户端截断、
     无过滤 Count=min(n,20)、时钟类错误给行动提示、HTTPError 暴露为 error item

（toutiao 全文搜索与 local_quark 夸克于 2026-09-26 移除：前者 builder 未注册
 进类型表从未运行过、后者上游连接层不可达，见 commit 记录；购物族 ddg_site
 六源同期移除。）

运行：
  python3 -m pytest tests/test_batch10_engines.py -v
"""

from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import engines_builders_batch10 as b10  # noqa: E402


def _fake_http_open(payload):
    """http_open 替身：payload 为 Exception 时抛出，否则返回固定响应体。"""
    import json as _json

    class _Resp:
        def read(self):
            if isinstance(payload, bytes):
                return payload
            if isinstance(payload, str):
                return payload.encode("utf-8")
            return _json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fn(req, timeout=None, engine="", **kw):
        if isinstance(payload, Exception):
            raise payload
        return _Resp()
    return _fn


class TestZhihuGlobalV2(unittest.TestCase):

    def _engine(self):
        return b10._build_zhihu_global_engine({"_name": "zhihu_global"})

    def test_no_secret_is_honest_empty(self):
        with patch.object(b10, "get_env", lambda names: None):
            self.assertEqual(self._engine()("AI", n=5), [])

    def _capture_open(self, payload):
        seen = {}

        def _fn(req, timeout=None, engine="", **kw):
            seen["url"] = getattr(req, "full_url", "")
            seen["engine"] = engine
            class _R:
                def read(self):
                    return __import__("json").dumps(payload).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return _R()
        return _fn, seen

    def test_filtered_fills_pool_then_truncates(self):
        items = [{"Title": f"条目{i}", "Url": f"https://zhuanlan.zhihu.com/p/{i}"}
                 for i in range(20)]
        fn, seen = self._capture_open({"Code": 0, "Data": {"Items": items}})
        with patch.object(b10, "get_env", lambda names: "secret"), \
             patch.object(b10, "http_open", fn):
            out = self._engine()("site:zhuanlan.zhihu.com argo", n=5)
        self.assertIn("Count=20", seen["url"])       # 候选池取满
        self.assertIn("Filter=host", seen["url"])    # 站点限定下发
        self.assertEqual(len(out), 5)                # 客户端截断

    def test_unfiltered_count_is_n(self):
        fn, seen = self._capture_open({"Code": 0, "Data": {"Items": []}})
        with patch.object(b10, "get_env", lambda names: "secret"), \
             patch.object(b10, "http_open", fn):
            self._engine()("argo", n=5)
        self.assertIn("Count=5", seen["url"])
        self.assertNotIn("Filter=", seen["url"])

    def test_clock_error_gets_actionable_hint(self):
        fn, _ = self._capture_open({"Code": 30001, "Message": "timestamp expired"})
        with patch.object(b10, "get_env", lambda names: "secret"), \
             patch.object(b10, "http_open", fn):
            out = self._engine()("argo", n=5)
        self.assertIn("error", out[0])
        self.assertIn("时钟", out[0]["error"])

    def test_http_error_exposed(self):
        err = urllib.error.HTTPError("https://developer.zhihu.com/x", 401,
                                     "Unauthorized", None, None)
        with patch.object(b10, "get_env", lambda names: "secret"), \
             patch.object(b10, "http_open", _fake_http_open(err)):
            out = self._engine()("argo", n=5)
        self.assertIn("HTTP 401", out[0]["error"])


if __name__ == "__main__":
    unittest.main()

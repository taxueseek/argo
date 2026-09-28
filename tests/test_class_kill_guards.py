#!/usr/bin/env python3
"""类杀守卫：一次修复消灭一类的回归锚点（2026-09-28）。

本文件的用例守的不是单个 bug，而是一类形状：

1. search_rank：语言调整失败不得把「未调整值」写进 _weight_cache——
   缓存键含 lang，固化 30s 会让该语言持续拿降级权重。
   与已修的「退化结果不写缓存」「写入守卫拒绝不取消检索」同源：
   吞异常 + 缓存 = 固化降级值。

范围说明（同批审查、代码即修复、不另设测试的项）：
  - robots_guard._cache / fetch_v3._identity_mem 加界：照抄同文件/已提交
    的既有 cap 模式（_parser_cache），非新逻辑；
  - link_source 子进程 timeout：defer——cmd /c junction 形态被安全门禁
    标记为需先重构参数化，详见审查报告。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_class_kill_guards.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import search_rank as sr  # noqa: E402

_SRC = "exa/tavily"  # 合并来源形态：静态权重取最高、可靠性取最低


class TestWeightCacheNotPoisonedOnAdjustFailure(unittest.TestCase):
    """lang_capability 瞬断时：值可用，但缓存必须保持干净。"""

    def setUp(self):
        sr._weight_cache.clear()

    def tearDown(self):
        sr._weight_cache.clear()

    def test_adjust_failure_not_cached_and_retried(self):
        calls = {"n": 0}

        def _boom(*_a, **_k):
            calls["n"] += 1
            raise RuntimeError("标定服务瞬断")

        with mock.patch("lang_capability.score_adjust", _boom):
            out1 = sr._engine_weight(_SRC, "fr")
            self.assertIsNone(
                sr._weight_cache.get((_SRC, "fr")),
                "调整失败的值不得写入缓存（30s 固化降级权重）")
            out2 = sr._engine_weight(_SRC, "fr")
        self.assertEqual(calls["n"], 2,
                         "未缓存 ⇒ 每次调用都重试调整（瞬断自愈的前提）")
        self.assertTrue(out1 > 0 and out2 > 0)

    def test_success_path_still_cached(self):
        """对照：调整成功必须照常进缓存（守卫不得误伤正常路径）。"""
        with mock.patch("lang_capability.score_adjust", return_value=1.2):
            sr._engine_weight(_SRC, "fr")
        self.assertIsNotNone(sr._weight_cache.get((_SRC, "fr")))

    def test_no_lang_path_unaffected(self):
        """无 lang 时与旧行为一致：照常缓存。"""
        sr._engine_weight(_SRC, None)
        self.assertIsNotNone(sr._weight_cache.get((_SRC, "")))


if __name__ == "__main__":
    unittest.main()

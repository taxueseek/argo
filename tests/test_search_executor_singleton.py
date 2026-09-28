#!/usr/bin/env python3
"""include-local 本地搜索执行器的单例回归（2026-09-28）。

此前每次 --include-local 调用都新建 ThreadPoolExecutor 且从不 shutdown：
CLI 一次性进程无感，常驻 MCP server 每跑一次就泄漏一个非 daemon 线程
（线性累积，永不回收）。修法是模块级单例——本测试守「重复获取同一实例 +
重复提交不涨线程」两条不变量。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import search  # noqa: E402


class TestLocalSeekExecutorSingleton(unittest.TestCase):
    def test_same_executor_reused(self):
        self.assertIs(search._get_local_seek_executor(),
                      search._get_local_seek_executor())

    def test_repeat_submit_does_not_grow_threads(self):
        before = threading.active_count()
        futures = [search._get_local_seek_executor().submit(time.sleep, 0.01)
                   for _ in range(5)]
        for f in futures:
            f.result(timeout=5)
        # max_workers=1 的单例池：5 次提交也只多 1 个 worker 线程
        self.assertLessEqual(threading.active_count(), before + 1,
                             "重复提交不得新增线程（泄漏回归信号）")


if __name__ == "__main__":
    unittest.main()

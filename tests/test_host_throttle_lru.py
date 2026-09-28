#!/usr/bin/env python3
"""host_throttle LRU 淘汰的活跃桶保护回归（2026-09-28）。

淘汰只允许踢**零活跃**的桶：踢掉正被 lease() 持有的桶后，同 key 会建新桶，
旧桶租户与新桶并行——该主机的并发上限与最小间隔瞬间双份（限速击穿）。
全部桶都活跃时宁可让桶数短暂越过上限（桶键空间受配置约束，有界），
也不打破约束。

运行：
  cd ~/.agents/skills/argo
  python3 -m pytest tests/test_host_throttle_lru.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import http_client as hc  # noqa: E402

# 打桩：任意 URL 都给同一组限流参数、host 即分组——让桶键按 URL 区分，
# 专测淘汰策略本身（归一逻辑归 test_host_throttle 管）。
_PATCHES = (patch.object(hc, "_MAX_BUCKETS", 2),
            patch.object(hc, "_limits_for", lambda url, engine=None: (2, 0)),
            patch.object(hc, "host_group_for", lambda url: url))


class TestLruProtectsActiveBuckets(unittest.TestCase):
    def setUp(self):
        hc._BUCKETS.clear()
        hc._bucket_last_used.clear()
        for p in _PATCHES:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in _PATCHES])

    def tearDown(self):
        hc._BUCKETS.clear()
        hc._bucket_last_used.clear()

    def _key(self, host: str) -> str:
        return next(k for k in hc._BUCKETS if host in k)

    def test_active_bucket_survives_churn(self):
        with hc.host_throttle("https://a.example.com/x"):
            with hc.host_throttle("https://b.example.com/x"):
                pass  # 桶数到上限
            a_key, a_obj = self._key("a.example"), hc._BUCKETS[self._key("a.example")]
            with hc.host_throttle("https://c.example.com/x"):  # 触发淘汰
                pass
            self.assertIn(a_key, hc._BUCKETS,
                          "正被 lease() 持有的桶不得被 LRU 踢掉")
            self.assertIs(hc._BUCKETS[a_key], a_obj, "同 key 不得换桶（换桶=限速击穿）")

    def test_idle_bucket_is_evicted_first(self):
        with hc.host_throttle("https://a.example.com/x"):
            pass
        a_key = self._key("a.example")
        with hc.host_throttle("https://b.example.com/x"):
            pass
        b_key = self._key("b.example")
        with hc.host_throttle("https://c.example.com/x"):  # A、B 都空闲：踢最久未用的 A
            self.assertNotIn(a_key, hc._BUCKETS)
            self.assertIn(b_key, hc._BUCKETS)

    def test_over_limit_without_idle_is_tolerated(self):
        with patch.object(hc, "_MAX_BUCKETS", 1):
            with hc.host_throttle("https://a.example.com/x"):
                # A 活跃、桶数已达上限：新建 B 无可踢——宁越上限不破约束
                with hc.host_throttle("https://b.example.com/x"):
                    self.assertEqual(len(hc._BUCKETS), 2)


if __name__ == "__main__":
    unittest.main()

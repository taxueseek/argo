#!/usr/bin/env python3
"""single_flight 合并器 + 免费引擎 n 桶化。"""

from __future__ import annotations

import sys
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from single_flight import SingleFlightCoalescer  # noqa: E402


class TestCoalescer(unittest.TestCase):
    def test_concurrent_same_key_runs_once(self):
        c = SingleFlightCoalescer()
        calls = []

        def fn():
            calls.append(time.time())
            time.sleep(0.15)  # 保证并发重叠
            return "result"

        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(c.run("k", fn, wait_timeout=5.0))
            )
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(results), {"result"})
        self.assertEqual(c.leaders, 1)
        self.assertEqual(c.coalesced, 4)

    def test_different_keys_run_separately(self):
        c = SingleFlightCoalescer()
        runs = []

        def fn_a():
            return "a"

        def fn_b():
            return "b"

        self.assertEqual(c.run("ka", fn_a), "a")
        self.assertEqual(c.run("kb", fn_b), "b")
        self.assertEqual(len(runs), 0)
        self.assertEqual(c.leaders, 2)

    def test_leader_failure_follower_falls_back(self):
        c = SingleFlightCoalescer()
        state = {"fail": True, "calls": 0}

        def fn():
            state["calls"] += 1
            if state["fail"]:
                raise RuntimeError("boom")
            return "ok"

        with self.assertRaises(RuntimeError):
            c.run("k", fn)  # leader 失败
        state["fail"] = False
        self.assertEqual(c.run("k", fn), "ok")  # 下一调用者是新 leader
        self.assertEqual(state["calls"], 2)

    def test_disabled_by_env(self):
        import importlib
        import os
        os.environ["ARGO_TOOL_CALL_COALESCE"] = "0"
        try:
            from single_flight import SingleFlightCoalescer as C2
            c = C2()
            calls = []

            def fn():
                calls.append(1)
                return "x"

            c.run("k", fn)
            c.run("k", fn)
            self.assertEqual(len(calls), 2)  # 关闭时每次都独立执行
        finally:
            os.environ["ARGO_TOOL_CALL_COALESCE"] = "1"
            importlib.reload(sys.modules["single_flight"])


class TestBucketN(unittest.TestCase):
    def test_free_engine_buckets_up(self):
        from engines import bucket_n
        # anysearch：免费引擎（cost_factor=1.0）
        self.assertEqual(bucket_n("anysearch", 5), 10)
        self.assertEqual(bucket_n("anysearch", 12), 20)
        self.assertEqual(bucket_n("anysearch", 55), 100)
        self.assertEqual(bucket_n("anysearch", 101), 100)  # 上限截断

    def test_paid_engine_keeps_exact(self):
        from engines import bucket_n
        # tavily / exa：付费引擎保持精确 n（按结果计费不得放大）
        self.assertEqual(bucket_n("tavily", 5), 5)
        self.assertEqual(bucket_n("tavily", 12), 12)


class TestEnginesCoalesce(unittest.TestCase):
    def test_concurrent_same_call_single_execution(self):
        from engines import search as engine_search

        calls = []

        def fake_fn(query, n, timeout, depth="fast", mode="fast", **kw):
            calls.append((query, n))
            time.sleep(0.1)
            return [{"title": f"t{i}", "url": f"https://x/{i}", "snippet": "s"}
                    for i in range(n)]

        import engines as E
        with unittest.mock.patch.object(E, "get_registry",
                                        return_value={"fake": fake_fn}):
            results = []
            threads = [
                threading.Thread(
                    target=lambda: results.append(
                        engine_search("q", "fake", n=5)))
                for _ in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 10)  # free 引擎桶化到 10
        self.assertEqual(len(results[0]), 10)

    def test_paid_engine_not_bucketed_in_call(self):
        from engines import search as engine_search

        calls = []

        def fake_fn(query, n, timeout, depth="fast", mode="fast", **kw):
            calls.append(n)
            return [{"title": "t", "url": "https://x/1", "snippet": "s"}]

        import engines as E
        with unittest.mock.patch.object(
                E, "get_registry", return_value={"tavily": fake_fn}):
            engine_search("q", "tavily", n=5)
        self.assertEqual(calls, [5])


if __name__ == "__main__":
    unittest.main()

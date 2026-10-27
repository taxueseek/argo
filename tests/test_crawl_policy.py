#!/usr/bin/env python3
"""crawl_policy 节奏测试（虚拟时钟，不真的等待）。

锁定三件事：
  1. 同主机请求按 interval 串行；不同主机互不影响
  2. 429 后惩罚加倍，下一次请求确实多等；成功后惩罚减半
  3. Retry-After 与 status 从 fetch_v3 结果里的兜底读取
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from crawl_policy import HostGate, retry_after_seconds, status_of


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _gate(clock, **kwargs):
    kwargs.setdefault("delay_resolver", lambda _url: None)
    return HostGate(clock=clock, sleep=clock.sleep, **kwargs)


class TestInterval(unittest.TestCase):
    def test_same_host_waits_interval(self):
        clock = FakeClock()
        gate = _gate(clock, interval=1.0, per_host_concurrency=4)
        with gate.slot("https://ex.com/a"):
            pass
        start = clock.now
        with gate.slot("https://ex.com/b"):
            pass
        self.assertGreaterEqual(clock.now - start, 1.0)

    def test_different_hosts_do_not_wait(self):
        clock = FakeClock()
        gate = _gate(clock, interval=1.0)
        with gate.slot("https://a.com/x"):
            pass
        start = clock.now
        with gate.slot("https://b.com/x"):
            pass
        self.assertEqual(clock.now, start)

    def test_crawl_delay_wins_when_larger(self):
        clock = FakeClock()
        gate = _gate(clock, interval=0.2, delay_resolver=lambda _u: 3.0)
        with gate.slot("https://ex.com/a"):
            pass
        start = clock.now
        with gate.slot("https://ex.com/b"):
            pass
        self.assertGreaterEqual(clock.now - start, 3.0)


class TestBackoff(unittest.TestCase):
    def test_429_doubles_penalty_then_success_halves(self):
        clock = FakeClock()
        gate = _gate(clock, interval=0.0)
        with gate.slot("https://ex.com/a") as lease:
            lease.note(429)
        start = clock.now
        with gate.slot("https://ex.com/b") as lease:
            self.assertGreaterEqual(clock.now - start, 0.25)
            lease.note(200)
        start2 = clock.now
        with gate.slot("https://ex.com/c") as lease:
            waited = clock.now - start2
            lease.note(200)
        self.assertLess(waited, 0.25)

    def test_retry_after_extends_penalty(self):
        clock = FakeClock()
        gate = _gate(clock, interval=0.0)
        with gate.slot("https://ex.com/a") as lease:
            lease.note(429, retry_after=2.0)
        start = clock.now
        with gate.slot("https://ex.com/b"):
            pass
        self.assertGreaterEqual(clock.now - start, 2.0)


class TestResultHelpers(unittest.TestCase):
    def test_status_from_field(self):
        self.assertEqual(status_of({"status": 429}), 429)

    def test_status_from_error_text(self):
        self.assertEqual(status_of({"error": "HTTP 429 Too Many Requests"}), 429)

    def test_retry_after_from_dict(self):
        self.assertEqual(retry_after_seconds({"retry_after": 3}), 3.0)

    def test_retry_after_from_headers(self):
        self.assertEqual(retry_after_seconds({"headers": {"Retry-After": "5"}}), 5.0)

    def test_retry_after_missing(self):
        self.assertIsNone(retry_after_seconds({}))


if __name__ == "__main__":
    unittest.main()

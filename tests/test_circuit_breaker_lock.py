#!/usr/bin/env python3
"""test_circuit_breaker_lock — 熔断状态的跨进程增量不丢（2026-09-27）。

守的是 `CircuitBreaker` 状态文件的「读-改-写」跨进程安全性。

背景：`record_failure` 的 `failures` / `opens` 是 `DISABLE_AFTER_OPENS`
自动禁用的唯一依据。此前整段变更只受进程内 `threading.RLock` 保护，而
CLI / 常驻 MCP server / 评测脚本三者并行时各自持有构造期读到的旧快照、
各自 +1、后写者覆盖前写者——实测 4 进程 × 20 次 record_failure 丢 48%
（6 进程 35%、8 进程 44%）。后果不是「计数不准」而是**安全机制失效**：
真坏掉的引擎攒不够 opens，于是持续被派发，故障源永远下线不了。

与 `quota.py` 的 `test_quota_concurrency` 同形：锁是 argo_paths.file_lock
提供的现成原语，这里锁的是「它有没有真的被用上、且用在了整段变更外侧」。

红/绿由本文件自己保证：`_count_lost` 在未修复代码上必然 > 0
（4 进程 × 25 次，丢 40%+），在修复后恒为 0。
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

ENG = "race_probe_engine"
PROCS = 4
PER_PROC = 25


def _worker(state_path: str, _n: int) -> None:
    """子进程入口：每进程连打 PER_PROC 次失败，每次自行落盘。

    刻意不在结尾调 `_save(force=True)`——那是另一条无锁的全量覆盖路径，
    会把本测试测的并发窗口搅浑（测的是 record_failure 自身的锁覆盖）。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import circuit_breaker as cb

    breaker = cb.CircuitBreaker(state_path=state_path)
    for _ in range(PER_PROC):
        breaker.record_failure(ENG, kind="error")


def _count_lost(state_path: str) -> tuple[int, int]:
    """返回 (实际记录数, 期望数)。"""
    data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    got = int((data.get("engines", {}).get(ENG, {}) or {}).get("failures", 0))
    return got, PROCS * PER_PROC


class TestCircuitBreakerCrossProcess(unittest.TestCase):
    """跨进程并发下 failures 增量不得丢失。"""

    def test_no_lost_updates_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "circuit_breaker.json")
            # 预置一个已存在的文件，让每个子进程构造期都读得到同一个起点
            Path(state_path).write_text(
                json.dumps({"engines": {}}), encoding="utf-8")

            ctx = mp.get_context("spawn")
            procs = [ctx.Process(target=_worker, args=(state_path, PER_PROC))
                     for _ in range(PROCS)]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=120)
                self.assertEqual(p.exitcode, 0,
                                 f"worker exited {p.exitcode}")

            got, expected = _count_lost(state_path)
            self.assertEqual(
                got, expected,
                f"跨进程丢失 {expected - got}/{expected} 次 record_failure "
                f"增量——熔断计数不再可信，自动禁用会失效。"
                f"（record_failure 的变更必须整段跑在 "
                f"argo_paths.file_lock 内）")

    def test_failure_still_actually_increments(self) -> None:
        """正向断言：锁不能把计数变没。空锁/跳过 mutator 都会挂在这里。"""
        from circuit_breaker import CircuitBreaker

        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "circuit_breaker.json")
            b = CircuitBreaker(state_path=state_path)
            for _ in range(3):
                b.record_failure(ENG, kind="error")
            got, _ = _count_lost(state_path)
            self.assertEqual(got, 3)

    def test_empty_kind_needs_two_calls(self) -> None:
        """语义守卫：empty 权重低，两次 empty 才算一次 failure。"""
        from circuit_breaker import CircuitBreaker

        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "circuit_breaker.json")
            b = CircuitBreaker(state_path=state_path)
            b.record_failure(ENG, kind="empty")
            self.assertEqual(_count_lost(state_path)[0], 0,
                             "第一次 empty 不该计入 failures")
            b.record_failure(ENG, kind="empty")
            self.assertEqual(_count_lost(state_path)[0], 1,
                             "第二次 empty 应计一次 failure")

    def test_record_success_resets_counters(self) -> None:
        """record_success 走同一锁路径：成功后计数归零。"""
        from circuit_breaker import CircuitBreaker

        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "circuit_breaker.json")
            b = CircuitBreaker(state_path=state_path)
            for _ in range(3):
                b.record_failure(ENG, kind="error")
            b.record_success(ENG)
            self.assertEqual(_count_lost(state_path)[0], 0,
                             "成功后 failures 应归零")

    def test_unwritable_path_does_not_raise(self) -> None:
        """fail-open：锁/写盘层出问题绝不能把搜索主路径带崩。"""
        from circuit_breaker import CircuitBreaker

        with tempfile.TemporaryDirectory() as tmp:
            b = CircuitBreaker(state_path=os.path.join(tmp, "no", "such",
                                                       "dir", "cb.json"))
            b.record_failure(ENG, kind="error")  # 不应抛
            b.record_success(ENG)                  # 不应抛
            self.assertEqual(b._engines[ENG]["state"], "closed")


if __name__ == "__main__":
    unittest.main()

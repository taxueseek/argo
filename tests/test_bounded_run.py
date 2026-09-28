#!/usr/bin/env python3
"""bounded_run 的行为保证：总时限硬性生效、异常隔离、提前收尾、慢任务不被等待。"""

from __future__ import annotations

import ast
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

from bounded_run import TaskError, run_bounded


class TestRunBounded(unittest.TestCase):
    def test_all_fast_returns_everything(self):
        finished, unfinished = run_bounded([1, 2, 3], lambda x: x * 10, 5.0, max_workers=2)
        self.assertEqual(unfinished, [])
        self.assertEqual(sorted(v for _, v in finished), [10, 20, 30])

    def test_hard_time_bound_not_blocked_by_slow_task(self):
        # 一个慢任务睡 5 秒，但总时限 0.3 秒：函数必须在约 0.3 秒就返回，
        # 且慢任务计入 unfinished（不被 join）。
        def worker(x):
            if x == "slow":
                time.sleep(5.0)
            return x

        t0 = time.monotonic()
        finished, unfinished = run_bounded(["fast", "slow"], worker, 0.3, max_workers=2)
        dt = time.monotonic() - t0
        self.assertLess(dt, 1.0, f"被慢任务拖住了：实际耗时 {dt:.2f}s")
        self.assertEqual(unfinished, ["slow"])
        self.assertEqual([v for _, v in finished], ["fast"])

    def test_exception_isolated_as_taskerror(self):
        def worker(x):
            if x == 2:
                raise ValueError("boom")
            return x

        finished, unfinished = run_bounded([1, 2, 3], worker, 2.0, max_workers=3)
        self.assertEqual(unfinished, [])
        good = {i: v for i, v in finished if not isinstance(v, TaskError)}
        bad = [v for _, v in finished if isinstance(v, TaskError)]
        self.assertEqual(good, {1: 1, 3: 3})
        self.assertEqual(len(bad), 1)
        self.assertIsInstance(bad[0].exc, ValueError)

    def test_enough_stops_early(self):
        def worker(x):
            time.sleep(0.05)
            return x

        # 拿到任意 2 个就收尾：排队中的其余任务不再启动
        finished, unfinished = run_bounded(
            list(range(8)), worker, 5.0, max_workers=2,
            enough=lambda done: len(done) >= 2)
        self.assertEqual(len(finished), 2)
        # 提前收尾后不应把 8 个全跑完
        self.assertLess(len(finished) + len(unfinished), 8)

    def test_empty_items(self):
        finished, unfinished = run_bounded([], lambda x: x, 1.0)
        self.assertEqual(finished, [])
        self.assertEqual(unfinished, [])

    def test_never_started_items_count_as_unfinished(self):
        # 任务数超过并发上限、且都慢到等不到：没排上起跑的任务也必须进 unfinished，
        # 不能被悄悄丢掉（finished 与 unfinished 合起来要等于全部输入）。
        def slow(x):
            time.sleep(5.0)
            return x
        items = list(range(7))
        finished, unfinished = run_bounded(items, slow, 0.2, max_workers=2)
        self.assertEqual(finished, [])
        self.assertEqual(sorted(unfinished), items)

    def test_max_workers_caps_concurrency(self):
        active = {"now": 0, "peak": 0}
        import threading
        lock = threading.Lock()

        def worker(x):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.05)
            with lock:
                active["now"] -= 1
            return x

        run_bounded(list(range(10)), worker, 5.0, max_workers=3)
        self.assertLessEqual(active["peak"], 3)


def _call_name(node: ast.AST) -> str:
    """取调用表达式的末级名字，如 concurrent.futures.as_completed → as_completed。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _has_timeout_arg(call: ast.Call) -> bool:
    if any(kw.arg == "timeout" for kw in call.keywords):
        return True
    return len(call.args) >= 2  # as_completed(futures, timeout) 位置第二参


class TestNoAutoJoiningTimeout(unittest.TestCase):
    """静态检查：不要在 `with ThreadPoolExecutor(...)` 里用带超时的 as_completed。

    因为 with 退出会自动 join 全部线程，把超时架空（慢任务照样拖住返回）。需要
    「最多等 N 秒」时一律用 bounded_run.run_bounded。有意等全部完成的场景
    （as_completed 不传 timeout）不在此列。
    """

    def test_no_timed_as_completed_inside_with_pool(self):
        offenders = []
        for path in SCRIPTS.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for with_node in ast.walk(tree):
                if not isinstance(with_node, ast.With):
                    continue
                is_pool_with = any(
                    _call_name(item.context_expr) == "ThreadPoolExecutor"
                    for item in with_node.items
                )
                if not is_pool_with:
                    continue
                for sub in ast.walk(with_node):
                    if (isinstance(sub, ast.Call)
                            and _call_name(sub.func) == "as_completed"
                            and _has_timeout_arg(sub)):
                        offenders.append(f"{path.name}:{sub.lineno}")
        self.assertEqual(
            offenders, [],
            "with ThreadPoolExecutor 内的 as_completed 超时会被退出时的 join 架空，"
            "请改用 bounded_run.run_bounded：" + ", ".join(offenders))


class TestRunBoundedStagger(unittest.TestCase):
    """stagger_s 的对冲语义：首任务先跑，宽限窗内交付只付一次调用。

    锁住三条不变式：
      1. 首任务慢 → 宽限窗后才补发（对冲），不是立即全发、也不是死等；
      2. 首任务快且合格 → 备用任务根本不启动（成本回退）；
      3. 首任务快但空手 → 立即补发（没有可对冲的对象，等满窗纯属浪费）。
    """

    def test_slow_primary_gets_hedged_after_grace(self):
        calls: list[str] = []

        def worker(x):
            calls.append(x)
            if x == "slow":
                time.sleep(0.6)
                return []
            time.sleep(0.05)
            return [f"hit-{x}"]

        t0 = time.monotonic()
        finished, unfinished = run_bounded(
            ["slow", "fast"], worker, 3.0, max_workers=3,
            enough=lambda fin: any(isinstance(v, list) and v for _, v in fin),
            stagger_s=0.2)
        dt = time.monotonic() - t0
        # 对冲后 ≈ grace + 快任务时长（0.25s），而非串行和（0.65s）
        self.assertLess(dt, 0.5, f"对冲未生效：实际耗时 {dt:.2f}s")
        self.assertGreaterEqual(dt, 0.2, "备用任务不应早于宽限窗启动")
        self.assertEqual(sorted(calls), ["fast", "slow"])
        hits = [v for _, v in finished if isinstance(v, list) and v]
        self.assertEqual(hits, [["hit-fast"]])

    def test_fast_primary_costs_single_call(self):
        calls: list[str] = []

        def worker(x):
            calls.append(x)
            time.sleep(0.05)
            return [f"hit-{x}"]

        t0 = time.monotonic()
        finished, _ = run_bounded(
            ["a", "b"], worker, 3.0, max_workers=3,
            enough=lambda fin: any(isinstance(v, list) and v for _, v in fin),
            stagger_s=0.3)
        dt = time.monotonic() - t0
        self.assertEqual(calls, ["a"], "grace 内交付不应补发备用任务")
        self.assertLess(dt, 0.25)

    def test_empty_primary_falls_through_immediately(self):
        calls: list[str] = []

        def worker(x):
            calls.append(x)
            time.sleep(0.05)
            return [] if x == "a" else [f"hit-{x}"]

        t0 = time.monotonic()
        finished, _ = run_bounded(
            ["a", "b"], worker, 3.0, max_workers=3,
            enough=lambda fin: any(isinstance(v, list) and v for _, v in fin),
            stagger_s=0.3)
        dt = time.monotonic() - t0
        # 首任务 0.05s 空手返回：备用任务应立即接上，不等满 0.3s 宽限窗
        self.assertEqual(sorted(calls), ["a", "b"])
        self.assertLess(dt, 0.2, f"空手首任务后仍等了宽限窗：{dt:.2f}s")
        hits = [v for _, v in finished if isinstance(v, list) and v]
        self.assertEqual(hits, [["hit-b"]])

    def test_stagger_zero_keeps_legacy_immediate_parallel(self):
        # stagger_s=0（默认）：行为与改造前完全一致——立即填满并发位
        started: list[float] = []
        t0 = time.monotonic()

        def worker(x):
            started.append(time.monotonic() - t0)
            time.sleep(0.1)
            return x

        run_bounded(["a", "b", "c"], worker, 3.0, max_workers=3)
        self.assertEqual(len(started), 3)
        for s in started:
            self.assertLess(s, 0.05, "stagger=0 时应立即并发启动")


if __name__ == "__main__":
    unittest.main()

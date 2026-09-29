#!/usr/bin/env python3
"""串行路径的对冲：死源不再独占整段预算（2026-09-17）。

## 这条门锁的是什么

`parallel=False` 的垂直域（行情/宏观/天气/赛事/地理）此前是**严格串行**：
前一个引擎必须耗满自己的超时上限（收紧后 5-8s）才轮到下一个。24 条代表性
查询实测：dispatch 超出「首个可用引擎完成时刻」的部分 p50 39ms、p90 2436ms、
合计 22.4s——尾部全在这条路径上，而 wave-1/wave-2 早就有对冲。

修法是**错开起步**：首个引擎跑过 `serial_stagger_s` 还没回来才补发备选源。
happy path（首个引擎很快交付）仍然只付一次调用——这是成本语义，也是下面
第一条用例锁的东西。

## 为什么用假引擎而不是真网络

墙钟断言只有在「耗时完全由我们控制」时才不抖。真网络的门在慢机器/沙箱上
会假红，假红一次就会有人把门删掉。这里用 patch 掉的 engine_search，把
「谁慢、慢多久、给不给结果」写成字面量，断言才有牙。

## 消融

`test_ablation_disabled_stagger_restores_serial` 把宽限窗设成大于引擎超时
上限的值，确认**退回严格串行**（墙钟回到死源的完整耗时）。没有这条对照，
上面「墙钟 < 1.5s」可能只是碰巧成立——例如对冲压根没生效，而 backup 恰好
也很快。注意起步间隔是**单调**的：0 表示不节流（备选源与首引擎同时起跑），
不是「关闭对冲」；关闭要把值调大。
"""

from __future__ import annotations

import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import search
from cache import SearchCache
from search import StageTiming, execute_search

QUERY = "中国的通胀率是多少"


def _good(tag: str, n: int = 3) -> list:
    return [
        {"title": f"{QUERY} 数据 {tag} {i}",
         "snippet": f"关于{QUERY}的说明 {i}",
         "url": f"https://example.com/{tag}/{i}"}
        for i in range(n)
    ]


class _AllowAllBreaker:
    def allow(self, *_a, **_k):
        return True, ""

    def get_negative(self, *_a, **_k):
        return None

    def status(self, *_a, **_k):
        return {"state": "closed"}

    def record_success(self, *_a, **_k):
        pass

    def record_failure(self, *_a, **_k):
        pass

    def record_note(self, *_a, **_k):
        pass

    def set_negative(self, *_a, **_k):
        pass

    def clear_negative(self, *_a, **_k):
        pass


class SerialHedgeBase(unittest.TestCase):
    def _run(self, fake, engines, *, stagger=None, mode: str = "fast",
             parallel: bool = False, timeout: int = 10):
        calls: list[str] = []

        def _spy(q, eng, **kw):
            calls.append(eng)
            return fake(q, eng)

        cache = SearchCache(db_path=":memory:")
        decision = {"engines_combo": list(engines), "engines": list(engines),
                    "parallel": parallel, "domain": "macro_data",
                    "engine": engines[0], "early_stop_min_results": 1,
                    "no_early_stop": False}
        t0 = time.perf_counter()
        with ExitStack() as stack:
            for p in (
                patch("search.engine_search", side_effect=_spy),
                patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
                patch("quota.get_quota_manager", return_value=MagicMock()),
                *([patch.object(search, "_PRIMARY_GRACE_S", stagger)]
                  if stagger is not None else []),
            ):
                stack.enter_context(p)
            out = execute_search(
                QUERY, decision, max_results=5, timeout=timeout, depth="fast",
                cache=cache, skip_cache=True, mode=mode)
        return out, calls, time.perf_counter() - t0


class TestSerialHedgeKillsDeadPrimary(SerialHedgeBase):
    def test_dead_primary_does_not_cost_its_full_timeout(self):
        """首引擎睡到超时上限、零结果：备选源必须在它超时前就起跑。

        改造前：墙钟 ≈ dead 的 5s（备选源要等它跑完才轮到）。
        改造后：墙钟 ≈ stagger + backup 的 0.05s。
        """
        def fake(_q, eng):
            if eng == "dead_primary":
                time.sleep(5.0)
                return []
            if eng == "fast_backup":
                time.sleep(0.05)
                return _good("bk")
            return []

        out, calls, wall = self._run(fake, ["dead_primary", "fast_backup"])
        self.assertIn("fast_backup", calls, f"备选源根本没被起跑：{calls}")
        self.assertLess(wall, 1.5,
                        f"死源仍独占预算，墙钟 {wall:.2f}s（改造前约 5s）")
        self.assertTrue(out["results"], "备选源的结果没有交付")

    def test_happy_path_still_pays_exactly_one_call(self):
        """成本语义不回退：首引擎在起步间隔内交付合格结果 → 只付一次调用。"""
        def fake(_q, eng):
            if eng == "quick_primary":
                time.sleep(0.05)
                return _good("qp")
            if eng == "backup":
                time.sleep(0.02)
                return _good("bk")
            return []

        out, calls, wall = self._run(fake, ["quick_primary", "backup"])
        self.assertEqual(calls, ["quick_primary"],
                         f"合格首引擎不应触发备选源：{calls}")
        self.assertLess(wall, 0.5, f"happy path 变慢了：{wall:.2f}s")
        self.assertTrue(out["results"])

    def test_ablation_disabled_stagger_restores_serial(self):
        """消融：宽限窗调大到超过引擎超时上限 → 退回严格串行。

        没有这条对照，上面「墙钟 < 1.5s」可能只是碰巧——比如对冲压根没生效，
        而 backup 恰好也很快。这条确认「有对冲」与「无对冲」是两个可区分的
        世界，门有牙。
        """
        def fake(_q, eng):
            if eng == "dead_primary":
                time.sleep(1.2)
                return []
            if eng == "fast_backup":
                time.sleep(0.05)
                return _good("bk")
            return []

        _out, calls, wall_on = self._run(
            fake, ["dead_primary", "fast_backup"], stagger=0.8)
        _out2, _calls2, wall_off = self._run(
            fake, ["dead_primary", "fast_backup"], stagger=99.0)
        # 实测（本机）：有对冲 ≈ 0.8(stagger) + 0.05(backup) ≈ 0.9s；
        # 无对冲 ≈ 1.2(死源全程) + 0.05 ≈ 1.25s。阈值取两者之间且不贴边。
        self.assertLess(wall_on, 1.05,
                        f"有对冲时不应等满死源：{wall_on:.2f}s")
        self.assertGreater(wall_off, 1.15,
                           f"消融档应退回严格串行：{wall_off:.2f}s")
        self.assertLess(wall_on + 0.1, wall_off,
                        f"对冲没有可测量的收益：{wall_on:.2f}s vs {wall_off:.2f}s")

    def test_hedge_never_starts_more_than_three(self):
        """并发位仍是 3：对冲只提前起步，不放大并发。"""
        peak = {"n": 0}

        def fake(_q, eng):
            peak["n"] = max(peak["n"], len(active))
            time.sleep(0.6)
            return []

        active: set[str] = set()

        def _spy(q, eng, **kw):
            active.add(eng)
            try:
                return fake(q, eng)
            finally:
                active.discard(eng)

        cache = SearchCache(db_path=":memory:")
        engines = [f"e{i}" for i in range(6)]
        decision = {"engines_combo": engines, "engines": engines,
                    "parallel": False, "domain": "macro_data",
                    "engine": engines[0], "early_stop_min_results": 1,
                    "no_early_stop": False}
        with ExitStack() as stack:
            for p in (patch("search.engine_search", side_effect=_spy),
                      patch("circuit_breaker.get_breaker",
                            return_value=_AllowAllBreaker()),
                      patch("quota.get_quota_manager", return_value=MagicMock())):
                stack.enter_context(p)
            execute_search(QUERY, decision, max_results=5, timeout=10,
                           depth="fast", cache=cache, skip_cache=True,
                           mode="fast")
        self.assertLessEqual(peak["n"], 3, f"并发位被突破：{peak['n']}")


class TestWastedBookkeeping(unittest.TestCase):
    """wasted_ms = 被编排主动弃置的引擎已经花掉的墙钟。

    口径换过两次，两次都是因为「好看但不指向动作」被否：
      ① 「所有非 ok 引擎的 latency 之和」——不是时间量，并行时必然大于墙钟
         （实测「世界杯 2026 主办国」wasted 2847ms > dispatch 2313ms）。上一轮
         的优化盘点正是据此写下「调度不用动」。
      ② 「wall - useful」——有界并发下是恒等式：触发早停的引擎自己就是最后
         完成的那个，返回当刻 wall == useful，于是**恒为 0**（实测每轮皆 0）。
         空转的仪表比没有仪表更糟：它让人以为调度无浪费可调。

    现在这个口径既算得清（数据本来就存在于 _settle_pending 的 lat_ms），
    也指向具体动作：数值大 = hedge 排得太靠前 / tail_grace 给得太宽 /
    并发上限太小，任一条都能直接改。
    """

    def _dispatch(self, engines, parallel=True, depth="fast", mode="fast",
                  grace=0.2):
        def _spy(q, eng, **kw):
            if eng.startswith("slow_winner"):
                # 必须**跨过对冲的起步间隔**才会补发 hedge，否则获胜引擎在
                # hedge 还没起跑时就返回——那是「早停、未弃置」，不是弃置。
                time.sleep(grace + 0.2)
                return [{"title": QUERY + " x", "snippet": "s",
                         "url": "https://a.com/1"}]
            time.sleep(3.0)   # hedge 已起跑，获胜时仍在跑 → 会被弃置
            return []

        cache = SearchCache(db_path=":memory:")
        decision = {"engines_combo": list(engines), "engines": list(engines),
                    "parallel": parallel, "domain": "macro_data",
                    "engine": engines[0], "early_stop_min_results": 1,
                    "no_early_stop": False}
        with ExitStack() as stack:
            for p in (patch("search.engine_search", side_effect=_spy),
                      patch("circuit_breaker.get_breaker",
                            return_value=_AllowAllBreaker()),
                      patch("quota.get_quota_manager", return_value=MagicMock()),
                      # 显式钉住对冲起步间隔：本类断言的是「弃置发生时 wasted>0」，
                      # 若吃环境里的真实 grace 值，前序用例改过它就会让本用例
                      # 静默退化成「早停但没弃置」——一次靠运气通过的测试等于没测。
                      patch.object(search, "_PRIMARY_GRACE_S", grace)):
                stack.enter_context(p)
            out = execute_search(QUERY, decision, max_results=5, timeout=10,
                                 depth=depth, cache=cache, skip_cache=True,
                                 mode=mode, timing=StageTiming())
        return out["timing"]["dispatch"]

    def test_wasted_counts_abandoned_engines(self):
        """核心断言：早停真的弃置了陪跑引擎时，wasted 必须大于 0。

        旧口径下这一轮恒为 0——所以它在未修代码上失败，红灯有意义。
        """
        d = self._dispatch(["slow_winner", "hedge_b"])
        for key in ("wall_ms", "useful_ms", "wasted_ms"):
            self.assertIn(key, d, f"缺字段 {key}")
        self.assertTrue(d.get("early_stopped"), f"本该早停：{d}")
        self.assertGreater(d["wasted_ms"], 0,
                           f"弃置了引擎却报 wasted=0——仪表失明：{d}")

    def test_wasted_zero_when_nothing_abandoned(self):
        """无弃置时必须是 0——证明上一条不是「恒 > 0」的空门。"""
        d = self._dispatch(["slow_winner"], mode="deep")
        self.assertEqual(d["wasted_ms"], 0, f"单引擎无弃置却报浪费：{d}")

    def test_wasted_is_nonnegative_and_bounded(self):
        """wasted 仍必须是有限非负值（不再是墙钟差，但也不能乱跳）。"""
        d = self._dispatch(["slow_winner", "hedge_b"])
        self.assertGreaterEqual(d["wasted_ms"], 0)
        self.assertLess(d["wasted_ms"], 60_000, f"wasted 离谱：{d}")
        self.assertLessEqual(d["useful_ms"], d["wall_ms"],
                             "useful 不得超过墙钟")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""配额耗尽引擎路由隔离的回归门（2026-10-04，方案 A）。

## 背景

配额状态机（quota.mark_remote_exhausted）会把「远端明示配额耗尽」
的引擎标记到周期边界自愈。route_combo 的 F7 在 combo 路由层排除
hard-down 引擎，但**主路由（TF-IDF）/ 恢复链 / 显式 engine= 都不经
那里**——耗尽引擎仍被路由为主引擎，每次白烧一次引擎往返并触发恢复
链。实测 bocha 403「not enough money」：单轮 ~0.9s 引擎调用 +
~1.7s 恢复 = ~2.6s 纯浪费，占冷搜索基线总时长 63%。

## 这一批锁的是什么

run_dispatch 的 per-engine 闸口（engine_dispatch）现在把配额状态机
已标记的引擎 early-return 为 `skipped-quota-exhausted`，与熔断/缺密
码同形。四条门：

| 缺陷 | 症状 | 本文件的门 |
|------|------|-----------|
| 耗尽引擎仍被调用 | engine_search 对 bocha 发起真实请求 | `TestExhaustedEngineNotCalled` |
| skip 被当失败记账 | 熔断累计 opens → auto-disable 错封引擎 | `TestSkipIsNotAFailure` |
| skip 写负缓存 | 同一查询 30s 内直接跳过该引擎 | `TestSkipWritesNoNegativeCache` |
| skip 重标记配额 | 以 now 重置 until、滑动延长冷却窗口 | `TestSkipDoesNotRemarkQuota` |

## 写门的纪律

每条门都要能在改造前的代码上失败：改造前 bocha 会进 engine_search
（第一条即红），且 outcome 落成 error/no-results 分支（后三条的断言
回调不被以预期方式调用）。打桩 QuotaManager.remote_exhausted_marks
而非落盘 quota.json——锁的是「闸口读快照并据此跳过」这一行为，
与配额状态机的落盘/自愈解耦。
"""

from __future__ import annotations

import importlib
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import engine_dispatch as ED  # noqa: E402

QUERY = "测试查询"
EXHAUSTED_ENGINE = "bocha"
HEALTHY_ENGINE = "anysearch"


class _FakeBreaker:
    """记录失败/负缓存调用，供断言「skip 不计失败、不负缓存」。"""

    def __init__(self):
        self.failures = []
        self.negatives = []

    def allow(self, eng):
        return True, ""

    def get_negative(self, query, eng):
        return None

    def record_success(self, eng):
        pass

    def record_failure(self, eng, kind="error", attribution=None):
        self.failures.append((eng, kind))

    def record_note(self, eng, attr):
        pass

    def set_negative(self, query, eng, status=None):
        self.negatives.append((eng, status))

    def clear_negative(self, query, eng):
        pass

    def status(self, eng):
        return {}


class _FakeQuotaBatch:
    def __init__(self):
        self.records = []

    def add(self, eng, contributed):
        self.records.append((eng, contributed))


def _run(monkeypatched_marks, engines):
    """打桩配额快照后跑一次 run_dispatch，返回可断言的事实集。

    两个坑，都在这里收口：
    1. **模块对象会被替换**：test_argo_paths / test_envsync 等用例
       在运行期 `sys.modules.pop("quota")` 后重新 import，sys.modules
       里的 quota 模块对象被换掉。本文件顶层的 `import quota as Q`
       绑定的是**收集期**的对象，patch 打在旧对象上、run_dispatch
       运行时读新对象——全量套件里 patch 静默失效（隔离运行却绿）。
       所以这里在**运行期**经 sys.modules 解析当前对象再 patch。
    2. patch `quota.get_quota_manager` 工厂（而非 QuotaManager 类
       方法）：run_dispatch 的惰性 `from quota import get_quota_manager`
       每次调用都重读模块属性，工厂级打桩不受单例/其他用例遗留状态
       影响。
    """
    quota_mod = sys.modules.get("quota") or importlib.import_module("quota")
    fake_mgr = SimpleNamespace(
        remote_exhausted_marks=lambda: dict(monkeypatched_marks))
    with patch.object(quota_mod, "get_quota_manager", return_value=fake_mgr):
        return _run_inner(engines)


def _run_inner(engines):
    # 配额状态机：只有 EXHAUSTED_ENGINE 被标记远端耗尽
    searched = []

    def fake_engine_search(retrieval_query, eng, *a, **k):
        searched.append(eng)
        return [{"title": "t", "url": "https://x/1",
                 "snippet": "s", "source": eng}]

    breaker = _FakeBreaker()
    quota_batch = _FakeQuotaBatch()
    remarked = []

    result = ED.run_dispatch(
        query=QUERY, retrieval_query=QUERY, engines=list(engines),
        decision={"domain": "general_search"}, parallel=False,
        domain="general_search", mode="auto", depth="fast",
        max_results=3, timeout=10, net_timeout=5.0, skip_cache=True,
        cache=None, breaker=breaker,
        since_iso=None, until_iso=None, t0=time.time(),
        engine_search=fake_engine_search,
        get_engines_fn=lambda: {},
        get_execution_config_fn=lambda: {},
        missing_env_for=lambda eng: [],
        classify_outcome=ED.classify_engine_outcome,
        quota_batch=quota_batch,
        note_quota_exhausted=lambda eng, detail="": remarked.append(eng),
        per_engine_budget_s=10.0, fast_budget_s=10.0, auto_budget_s=10.0,
        primary_grace_s=5.0,
    )
    return {
        "searched": searched,
        "breaker": breaker,
        "quota_batch": quota_batch,
        "remarked": remarked,
        "outcomes": {o["engine"]: o for o in result.engine_outcomes},
        "raw": result.raw_results,
    }


class QuotaRouteFixTests(unittest.TestCase):
    marks = {EXHAUSTED_ENGINE: {
        "reason": "HTTP 403 not enough money",
        "until": time.time() + 86400}}

    def test_exhausted_engine_not_called(self):
        """耗尽引擎不进 engine_search，健康引擎照常跑。"""
        f = _run(self.marks, [EXHAUSTED_ENGINE, HEALTHY_ENGINE])
        self.assertNotIn(EXHAUSTED_ENGINE, f["searched"],
                         "配额耗尽引擎不应发起真实请求")
        self.assertIn(HEALTHY_ENGINE, f["searched"],
                     "健康引擎必须照常被调用")

    def test_skip_outcome_status_and_detail(self):
        """跳过产出 skipped-quota-exhausted 状态与自愈时间 detail。"""
        f = _run(self.marks, [EXHAUSTED_ENGINE, HEALTHY_ENGINE])
        outcome = f["outcomes"].get(EXHAUSTED_ENGINE)
        self.assertIsNotNone(outcome, "耗尽引擎必须产出 outcome")
        self.assertEqual(outcome["status"], "skipped-quota-exhausted")
        self.assertIn("配额耗尽", outcome.get("detail", ""))

    def test_skip_is_not_a_failure(self):
        """skip 不计熔断失败（配额不是引擎健康问题，不该 auto-disable）。"""
        f = _run(self.marks, [EXHAUSTED_ENGINE])
        self.assertNotIn(
            (EXHAUSTED_ENGINE, "error"), f["breaker"].failures,
            "skip 不得以 kind=error 计入熔断失败")

    def test_skip_writes_no_negative_cache(self):
        """skip 不写负缓存（换环境/周期边界即好，缓存它没有信息量）。"""
        f = _run(self.marks, [EXHAUSTED_ENGINE])
        self.assertNotIn(
            (EXHAUSTED_ENGINE, "skipped-quota-exhausted"),
            f["breaker"].negatives,
            "skip 不得写负缓存")

    def test_skip_does_not_remark_quota(self):
        """skip 不重标记配额（否则滑动延长冷却窗口、破坏周期边界自愈）。"""
        f = _run(self.marks, [EXHAUSTED_ENGINE])
        self.assertNotIn(
            EXHAUSTED_ENGINE, f["remarked"],
            "skip 不得重调 note_quota_exhausted（标记只由真 403 现场写）")

    def test_healthy_engine_still_contributes(self):
        """对照面：健康引擎照常贡献结果、计成功。"""
        f = _run(self.marks, [HEALTHY_ENGINE])
        self.assertIn(HEALTHY_ENGINE, f["raw"])
        self.assertEqual(f["outcomes"][HEALTHY_ENGINE]["status"], "ok")

    def test_no_marks_falls_through_to_call(self):
        """无耗尽标记时引擎照常调用（闸口 fail-open，不误伤）。"""
        f = _run({}, [EXHAUSTED_ENGINE])
        self.assertIn(EXHAUSTED_ENGINE, f["searched"],
                      "无标记时引擎必须被调用")


if __name__ == "__main__":
    unittest.main()

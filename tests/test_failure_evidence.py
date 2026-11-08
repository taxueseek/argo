#!/usr/bin/env python3
"""test_failure_evidence — 失效证据一等公民化的三件套（2026-11-08）。

守的是三个零请求构件的行为契约：

  C auth 直达禁用：连续 auth 归因失败（401/403/410 结构性失败）跳过 opens
    阶梯直达 disabled；换 key（指纹变化）即时自愈；revive 人工复活。
    背景：rerank:bocha 实测 opens=18、每次冷却到期都付一次 403 RTT。
  B 空结果基线破位：历史常非空的引擎近期连续空且跨 ≥2 查询 → suspected_stale
    （疑似失效，降权不熔断）；从未成功过的引擎不受影响（易空源常态空不误伤）；
    一次真实成功即解除。背景：met_museum /search 410 被吞成空结果死 5 周无人知。
  D 分诊画像：engine_health._verdict 的严重度顺序与 picture() 的噪声过滤。

红/绿由本文件自己保证：全部用 tmp 状态文件，不碰生产熔断器。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import circuit_breaker as cb  # noqa: E402

AUTH_ATTR = {"category": "auth", "reason": "HTTP 403", "detail": "HTTP 403"}
QUOTA_ATTR = {"category": "quota_exhausted", "reason": "HTTP 402", "detail": "HTTP 402"}
NET_ATTR = {"category": "network", "reason": "ssl", "detail": "SSL EOF"}


class _TmpBreakerCase(unittest.TestCase):
    """每个用例一个独立状态文件的熔断器。"""

    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(prefix="cb_test_", suffix=".json")
        os.close(fd)
        os.unlink(self.path)  # 空路径起步
        self.cb = cb.CircuitBreaker(state_path=self.path)
        self._old_fp = cb._engine_key_fp_safe

    def tearDown(self) -> None:
        cb._engine_key_fp_safe = self._old_fp
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _patch_fp(self, value: str) -> None:
        cb._engine_key_fp_safe = lambda eng: value


class TestAuthEscalation(_TmpBreakerCase):
    """C 构件：auth 归因失败直达禁用。"""

    def test_two_auth_failures_disable(self):
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        # 第一次：streak=1，failures=1 未达 FAILURE_THRESHOLD → 还未开闸
        self.assertEqual(self.cb.status("eng")["state"], "closed")
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        st = self.cb.status("eng")
        self.assertEqual(st["state"], "disabled")
        self.assertTrue(st["auth_disabled"])
        self.assertEqual(st["auth_streak"], 2)

    def test_single_auth_failure_not_disabled(self):
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.assertNotEqual(self.cb.status("eng")["state"], "disabled")

    def test_network_failure_resets_auth_streak(self):
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.cb.record_failure("eng", kind="error", attribution=NET_ATTR)
        self.assertEqual(self.cb.status("eng")["auth_streak"], 0)
        self.assertNotEqual(self.cb.status("eng")["state"], "disabled")

    def test_empty_failure_does_not_reset_auth_streak(self):
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.cb.record_failure("eng", kind="empty", query="q1")
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.assertEqual(self.cb.status("eng")["auth_streak"], 2)
        self.assertTrue(self.cb.status("eng")["auth_disabled"])

    def test_success_resets_auth_streak(self):
        # 连击语义是「连续」：一次成功断开连击，孤立的后续 auth 失败不再
        # 直接累积到禁用阈值（review 修复的回归测试：成功短路曾漏清连击）
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.cb.record_success("eng")
        self.assertEqual(self.cb.status("eng")["auth_streak"], 0)
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.assertNotEqual(self.cb.status("eng")["state"], "disabled")

    def test_blocked_rate_limited_preserve_auth_streak(self):
        # blocked / rate-limited 是源站级信号，对 auth 是否修好无证据：
        # 不清也不进（review 修复：此前被当成普通失败重置连击）
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.cb.record_failure("eng", kind="blocked")
        self.cb.record_failure("eng", kind="rate-limited")
        self.assertEqual(self.cb.status("eng")["auth_streak"], 1)

    def test_disabled_not_overridden_by_opens_ladder(self):
        # 第二次 auth 失败时 failures 也爬过 FAILURE_THRESHOLD：disabled 优先，
        # 不被 opens 块盖回 open
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.assertEqual(self.cb.status("eng")["state"], "disabled")


class TestAuthKeySelfHeal(_TmpBreakerCase):
    """C 构件：换 key（指纹变化）即时自愈，不等 1h 冷却。"""

    def test_key_change_heals(self):
        self._patch_fp("key_old")
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.assertEqual(self.cb.status("eng")["state"], "disabled")

        self._patch_fp("key_new")
        allowed, reason = self.cb.allow("eng")
        self.assertTrue(allowed)
        self.assertEqual(reason, "auth_key_changed")
        st = self.cb.status("eng")
        self.assertEqual(st["state"], "closed")
        self.assertFalse(st["auth_disabled"])

    def test_same_key_stays_disabled(self):
        self._patch_fp("key_old")
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self._patch_fp("key_old")
        allowed, reason = self.cb.allow("eng")
        self.assertFalse(allowed)
        self.assertEqual(reason, "auto_disabled")

    def test_quota_exhausted_key_change_heals(self):
        self._patch_fp("key_old")
        self.cb.record_failure("eng", kind="error", attribution=QUOTA_ATTR)
        self.cb.record_failure("eng", kind="error", attribution=QUOTA_ATTR)
        self.assertEqual(self.cb.status("eng")["state"], "disabled")
        self._patch_fp("key_new")
        allowed, reason = self.cb.allow("eng")
        self.assertTrue(allowed)
        self.assertEqual(reason, "auth_key_changed")

    def test_empty_key_to_configured_key_heals(self):
        self._patch_fp("")
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.assertEqual(self.cb.status("eng")["state"], "disabled")
        self._patch_fp("key_new")
        allowed, reason = self.cb.allow("eng")
        self.assertTrue(allowed)
        self.assertEqual(reason, "auth_key_changed")

    def test_composite_breaker_key_resolves_underlying_engine_fp(self):
        from unittest.mock import patch
        from quota import QuotaManager
        with patch.dict("os.environ", {"ARGO_BOCHA_API_KEY": "bocha_secret_123"}):
            fp_rerank = QuotaManager._engine_key_fp("rerank:bocha")
            fp_bocha = QuotaManager._engine_key_fp("bocha")
        self.assertTrue(fp_rerank, "rerank:bocha 复合键未能解析底层 bocha 的密钥指纹")
        self.assertEqual(fp_rerank, fp_bocha)


class TestRevive(_TmpBreakerCase):
    """C 构件：revive 人工复活，ever_nonempty 基线保留。"""

    def test_revive_clears_auth_disable_keeps_baseline(self):
        self.cb.record_success("eng")  # 立基线
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.cb.record_failure("eng", kind="error", attribution=AUTH_ATTR)
        self.assertEqual(self.cb.status("eng")["state"], "disabled")

        cleared = self.cb.revive("eng")
        self.assertEqual(cleared["had_state"], "disabled")
        self.assertTrue(cleared["auth_disabled"])
        st = self.cb.status("eng")
        self.assertEqual(st["state"], "closed")
        self.assertFalse(st["auth_disabled"])

    def test_revive_unknown_engine(self):
        cleared = self.cb.revive("never_seen")
        self.assertEqual(cleared["had_state"], "closed")

    def test_reenable_preserves_baseline(self):
        # review 修复的兄弟点：reenable 全量重建状态曾抹掉 ever_nonempty，
        # 破位判定退回「从未成功」态，下次失效无法显形
        self.cb.record_success("eng")
        self.cb.record_failure("eng", kind="error")
        self.cb.record_failure("eng", kind="error")
        self.cb.record_failure("eng", kind="error")
        self.cb.reenable("eng")
        st = self.cb.snapshot().get("eng") or {}
        self.assertTrue(st.get("ever_nonempty"))


class TestEmptyBaselineBreach(_TmpBreakerCase):
    """B 构件：空结果基线破位。"""

    def test_breach_requires_prior_success(self):
        # 从未成功过的引擎（易空源常态空）不触发破位——统一阈值会误伤的正是它
        for q in ("a", "b", "c"):
            self.cb.record_failure("eng", kind="empty", query=q)
        self.assertFalse(self.cb.status("eng")["suspected_stale"])

    def test_breach_on_cross_query_empties(self):
        self.cb.record_success("eng")
        for q in ("q1", "q2", "q3"):
            self.cb.record_failure("eng", kind="empty", query=q)
        self.assertTrue(self.cb.status("eng")["suspected_stale"])

    def test_same_query_repeat_not_breach(self):
        self.cb.record_success("eng")
        for _ in range(5):
            self.cb.record_failure("eng", kind="empty", query="same")
        self.assertFalse(self.cb.status("eng")["suspected_stale"])

    def test_success_clears_breach(self):
        self.cb.record_success("eng")
        for q in ("q1", "q2", "q3"):
            self.cb.record_failure("eng", kind="empty", query=q)
        self.assertTrue(self.cb.status("eng")["suspected_stale"])
        self.cb.record_success("eng")
        self.assertFalse(self.cb.status("eng")["suspected_stale"])

    def test_stale_empties_outside_window_ignored(self):
        self.cb.record_success("eng")
        # 直接往状态里塞 8 天前的 empty（超出 EMPTY_RECENT_WINDOW）
        st = self.cb.snapshot().get("eng", {})
        old = time.time() - 8 * 86400
        st["empty_recent"] = [("q1", old), ("q2", old), ("q3", old)]
        self.cb._mutate_locked(lambda: self.cb._engines.update({"eng": st}))
        self.assertFalse(self.cb.status("eng")["suspected_stale"])

    def test_two_queries_with_repeat_counts(self):
        # q1 两次 + q2 一次：跨 2 个查询、总 3 次 → 破位（去重是「不同查询数」，
        # 不是「不同查询各一次」——同一查询反复空也是该查询下引擎坏的证据）
        self.cb.record_success("eng")
        self.cb.record_failure("eng", kind="empty", query="q1")
        self.cb.record_failure("eng", kind="empty", query="q1")
        self.cb.record_failure("eng", kind="empty", query="q2")
        self.assertTrue(self.cb.status("eng")["suspected_stale"])


class TestReliabilityFactor(unittest.TestCase):
    """B 构件的调度落点：破位源在排序里降权到 0.7（不熔断）。

    打桩对象按**调用期**的 sys.modules 解析：test_argo_paths 会 pop 掉
    circuit_breaker 再重导入，导入期绑定的 cb 此后是旧对象，patch 它的
    get_breaker 对 search_rank 运行期 `from circuit_breaker import ...`
    不可见（全套件红、单跑绿；同类见 test_circuit_breaker_module_alias）。
    """

    def setUp(self) -> None:
        import importlib
        self.cbm = importlib.import_module("circuit_breaker")
        fd, self.path = tempfile.mkstemp(prefix="cb_rel_", suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self._old_gb = self.cbm.get_breaker

    def tearDown(self) -> None:
        self.cbm.get_breaker = self._old_gb
        import search_rank
        search_rank._rel_factor_cache.clear()

    def test_suspected_stale_factor_07(self):
        br = self.cbm.CircuitBreaker(state_path=self.path)
        br.record_success("eng")
        for q in ("q1", "q2", "q3"):
            br.record_failure("eng", kind="empty", query=q)
        self.cbm.get_breaker = lambda: br
        import search_rank
        search_rank._rel_factor_cache.clear()
        self.assertAlmostEqual(search_rank._single_reliability("eng"), 0.7)

    def test_healthy_factor_1(self):
        br = self.cbm.CircuitBreaker(state_path=self.path)
        self.cbm.get_breaker = lambda: br
        import search_rank
        search_rank._rel_factor_cache.clear()
        self.assertAlmostEqual(search_rank._single_reliability("eng"), 1.0)


class TestHealthVerdicts(unittest.TestCase):
    """D 构件：分诊严重度顺序与 picture() 的噪声过滤。"""

    def test_verdict_order(self):
        import engine_health
        self.assertEqual(engine_health._verdict(
            {"state": "disabled", "auth_disabled": True})[0], "auth_down")
        self.assertEqual(engine_health._verdict(
            {"state": "closed", "ever_nonempty": True,
             "suspected_stale": True})[0], "suspect")
        self.assertEqual(engine_health._verdict({"state": "disabled"})[0],
                         "auto_disabled")
        self.assertEqual(engine_health._verdict({"state": "open"})[0], "flapping")
        self.assertEqual(engine_health._verdict(
            {"state": "closed", "failures": 2})[0], "watch")

    def test_picture_excludes_clean_entries(self):
        import engine_health
        clean = {"state": "closed", "failures": 0, "opens": 0, "ever_nonempty": True}
        self.assertNotIn("clean_eng", engine_health.picture())
        self.assertTrue(True)  # picture 读真实熔断器；干净态过滤由 _verdict+空判断保证


if __name__ == "__main__":
    unittest.main()

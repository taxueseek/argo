#!/usr/bin/env python3
"""回归测试 — P0 备用源弹性（2026-08-07）：

P0-1 fallback 语义修复：combo 非空时 fallback 候选并入
P0-2 16 个单引擎域补齐候选（config.yaml 配置校验）
P0-3 组合期熔断剔除：确定不可用源不沉底保留，直接剔除让候选顶位
P0-4 login_hint：登录态意图标注（五路协同种子）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

# P0-2：16 个原单引擎域 → 补齐后的候选映射
_FALLBACK_MAP = {
    "wechat_search":       ("wechat_sogou", "anysearch"),
    "hackernews_search":   ("hackernews",   "anysearch"),
    "stackoverflow_search":("stackoverflow", "anysearch"),
    "v2ex_search":         ("v2ex",         "anysearch"),
    "ths_hot_search":      ("ths_hot",      "anysearch"),
    "cls_telegraph_search":("cls_telegraph", "jin10"),
    "em_news_search":      ("em_global_news", "cls_telegraph"),
    "chem_search":         ("pubchem",      "openalex"),
    "species_search":      ("gbif",         "wikipedia"),
    "rfc_search":          ("rfc_editor",   "anysearch"),
    "zhihu_hot_list":      ("zhihu_hot",    "zhihu"),
    "crypto_search":       ("coingecko",    "anysearch"),
    "game_search":         ("steam",        "anysearch"),
    "prediction_market":   ("polymarket",   "anysearch"),
    "dictionary_search":   ("free_dictionary", "wikipedia"),
    "image_search":        ("openverse",    "anysearch"),
}


class _FakeQuota:
    """默认配额可用；测试可按引擎覆盖。"""
    def __init__(self, available: set[str] | None = None):
        self._available = available

    def is_available(self, engine: str, mode: str = "auto") -> bool:
        if self._available is None:
            return True
        return engine in self._available


class _FakeBreaker:
    """默认 closed；测试可按引擎覆盖 state/cooldown。"""
    def __init__(self, states: dict | None = None):
        self._states = states or {}

    def status(self, engine: str) -> dict:
        return self._states.get(engine, {"state": "closed", "cooldown_remain": 0})


class TestFallbackMerge(unittest.TestCase):
    """P0-1：fallback 在 combo 非空时必须并入（旧逻辑 combo 非空即忽略 fallback）。"""

    def test_combo_nonempty_merges_real_fallback(self):
        from route import _get_engines_combo
        domain = {
            "name": "cls_telegraph_search",
            "primary": "cls_telegraph",
            "fallback": "jin10",
            "engines_combo": ["cls_telegraph"],
            "parallel": False,
        }
        with patch("route.get_quota_manager", return_value=_FakeQuota()):
            combo = _get_engines_combo(domain, {"cls_telegraph", "jin10"}, mode="auto")
        self.assertEqual(combo, ["cls_telegraph", "jin10"])

    def test_fallback_already_in_combo_not_duplicated(self):
        from route import _get_engines_combo
        domain = {
            "name": "fund_query",
            "primary": "eastmoney",
            "fallback": "anysearch",
            "engines_combo": ["eastmoney", "anysearch"],
        }
        with patch("route.get_quota_manager", return_value=_FakeQuota()):
            combo = _get_engines_combo(domain, {"eastmoney", "anysearch"}, mode="auto")
        self.assertEqual(combo, ["eastmoney", "anysearch"])

    def test_fallback_not_enabled_is_skipped(self):
        from route import _get_engines_combo
        domain = {
            "name": "rfc_search",
            "primary": "rfc_editor",
            "fallback": "anysearch",
            "engines_combo": ["rfc_editor"],
        }
        # anysearch 不在 enabled → fallback 不得混入
        with patch("route.get_quota_manager", return_value=_FakeQuota()):
            combo = _get_engines_combo(domain, {"rfc_editor"}, mode="auto")
        self.assertEqual(combo, ["rfc_editor"])

    def test_fallback_equals_primary_not_merged(self):
        from route import _get_engines_combo
        domain = {
            "name": "legacy",
            "primary": "x",
            "fallback": "x",  # 旧配置 fallback==primary，无备用语义
            "engines_combo": ["x"],
        }
        with patch("route.get_quota_manager", return_value=_FakeQuota()):
            combo = _get_engines_combo(domain, {"x", "y"}, mode="auto")
        self.assertEqual(combo, ["x"])


class TestBreakerRemoval(unittest.TestCase):
    """P0-3：确定不可用（disabled / open+cooldown>0）引擎直接剔除，
    不沉底保留；half-open（cooldown 已过）保留探测资格。"""

    def test_open_with_cooldown_removed(self):
        from route import _get_engines_combo
        domain = {
            "name": "test",
            "primary": "wikidata",
            "engines_combo": ["wikidata", "baidu_baike"],
        }
        states = {
            "wikidata": {"state": "open", "cooldown_remain": 30},
            "baidu_baike": {"state": "closed", "cooldown_remain": 0},
        }
        with patch("route.get_quota_manager", return_value=_FakeQuota()), \
             patch("circuit_breaker.get_breaker", return_value=_FakeBreaker(states)):
            combo = _get_engines_combo(domain, {"wikidata", "baidu_baike"}, mode="auto")
        # 故障源剔除，候选顶位
        self.assertEqual(combo, ["baidu_baike"])
        self.assertNotIn("wikidata", combo)

    def test_disabled_removed(self):
        from route import _get_engines_combo
        domain = {
            "name": "test",
            "primary": "a",
            "engines_combo": ["a", "b"],
        }
        states = {"a": {"state": "disabled", "cooldown_remain": 0}}
        with patch("route.get_quota_manager", return_value=_FakeQuota()), \
             patch("circuit_breaker.get_breaker", return_value=_FakeBreaker(states)):
            combo = _get_engines_combo(domain, {"a", "b"}, mode="auto")
        self.assertEqual(combo, ["b"])

    def test_half_open_kept_for_probe(self):
        """open 但 cooldown 已过 → half-open 探测资格，保留不剔除。"""
        from route import _get_engines_combo
        domain = {
            "name": "test",
            "primary": "a",
            "engines_combo": ["a", "b"],
        }
        states = {"a": {"state": "open", "cooldown_remain": 0}}
        with patch("route.get_quota_manager", return_value=_FakeQuota()), \
             patch("circuit_breaker.get_breaker", return_value=_FakeBreaker(states)):
            combo = _get_engines_combo(domain, {"a", "b"}, mode="auto")
        self.assertIn("a", combo)
        self.assertIn("b", combo)

    def test_all_unusable_yields_empty(self):
        """域内全部不可用 → 返回空集，交由 route_query 尾部兜底。"""
        from route import _get_engines_combo
        domain = {
            "name": "test",
            "primary": "a",
            "engines_combo": ["a"],
        }
        states = {"a": {"state": "open", "cooldown_remain": 60}}
        with patch("route.get_quota_manager", return_value=_FakeQuota()), \
             patch("circuit_breaker.get_breaker", return_value=_FakeBreaker(states)):
            combo = _get_engines_combo(domain, {"a"}, mode="auto")
        self.assertEqual(combo, [])

    def test_all_healthy_order_untouched(self):
        """全可用 → 顺序与集合不变（缓存键稳定，零速度倒退）。"""
        from route import _get_engines_combo
        domain = {
            "name": "test",
            "primary": "anysearch",
            "engines_combo": ["anysearch", "wikipedia", "arxiv"],
        }
        # 隔离自适应学习干扰（分数可能触发过滤）；三引擎分属
        # web_general/knowledge/academic 族，能力族去重不收缩
        with patch("route.get_quota_manager", return_value=_FakeQuota()), \
             patch("route._adaptive_learner", None):
            combo = _get_engines_combo(domain, {"anysearch", "wikipedia", "arxiv"}, mode="auto")
        self.assertEqual(combo, ["anysearch", "wikipedia", "arxiv"])


class TestLoginHint(unittest.TestCase):
    """P0-4：登录态意图标注。"""

    def test_strong_signal_any_domain(self):
        from route import _detect_login_intent
        for q in ("我的关注列表", "我的基金持仓", "私密收藏夹", "会员专享内容"):
            r = _detect_login_intent(q, "general_search")
            self.assertTrue(r["needs_login"], q)

    def test_weak_signal_sensitive_domain_only(self):
        from route import _detect_login_intent
        # 登录敏感域 + 弱信号 → 命中
        r = _detect_login_intent("我的账号信息", "zhihu_content")
        self.assertTrue(r["needs_login"])
        # 普通域 + 弱信号 → 不误报（「如何注册账号」是公开查询）
        r = _detect_login_intent("如何注册账号", "general_search")
        self.assertFalse(r["needs_login"])

    def test_plain_query_no_hint(self):
        from route import _detect_login_intent
        r = _detect_login_intent("北京 AI 公司融资", "tech_deep")
        self.assertFalse(r["needs_login"])

    def test_english_strong(self):
        from route import _detect_login_intent
        self.assertTrue(_detect_login_intent("my saved articles", "zhihu_content")["needs_login"])
        self.assertFalse(_detect_login_intent("how to sign in github", "general_search")["needs_login"])


class TestConfigCandidates(unittest.TestCase):
    """P0-2：16 个原单引擎域已补齐 engines_combo 双成员 + fallback。"""

    def test_all_16_domains_have_real_candidates(self):
        import yaml
        cfg = yaml.safe_load((SKILL_DIR / "config.yaml").read_text())
        doms = {d["name"]: d for d in cfg["domains"]}
        for name, (primary, fb) in _FALLBACK_MAP.items():
            d = doms.get(name)
            self.assertIsNotNone(d, f"域缺失: {name}")
            self.assertEqual((d.get("engines_combo") or [])[:2], [primary, fb],
                             f"{name}: combo 应为 [{primary}, {fb}]")
            self.assertEqual(d.get("fallback"), fb, f"{name}: fallback 应为 {fb}")


if __name__ == "__main__":
    unittest.main()

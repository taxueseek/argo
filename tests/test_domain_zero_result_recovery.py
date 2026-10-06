#!/usr/bin/env python3
"""域路由零结果恢复回归门（2026-09-14）。

验收发现的既有缺口：垂直域 combo 被预算截断 + 全域零结果时，恢复链被
复杂度门压到 L2（L3 换引擎被禁）——域命中查询无解（实测「中国 2025 年
GDP 总量」macro_data 零结果收场，而通用引擎本可答好）。修复三件：
  ① route：中国宏观词 → nbs_stats（国家统计局）前置（镜像 worldbank
    非美国前置模式）；
  ② route：engines_fallback 候选改为「域声明未试成员优先」，域最清楚
    自己的保底次序；
  ③ execute：域命中零结果时恢复链放行 L3（域候选是路由的定向保底声明，
    不再被复杂度门压掉）。

运行：
  python3 -m pytest tests/test_domain_zero_result_recovery.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _nbs_stats_enabled() -> bool:
    """nbs_stats 上游停用时（WAF + V2 端点 404，2026-09-30）跳过其前置断言；
    重开后自动恢复覆盖，不删测试。"""
    try:
        import yaml
        cfg = yaml.safe_load(
            (SCRIPT_DIR.parent / "config.yaml").read_text(encoding="utf-8"))
        return bool((cfg.get("engines") or {}).get("nbs_stats", {}).get("enabled", True))
    except Exception:
        return True


NBS_STATS_ENABLED = _nbs_stats_enabled()

from route import route_query  # noqa: E402
from search import execute_search  # noqa: E402
from cache import SearchCache  # noqa: E402


_MACRO_GOOD = [
    {"title": "国家统计局：2025年国内生产总值（GDP）初步核算结果",
     "url": "https://www.stats.gov.cn/sj/zxfb/202601/t20260117_demo.html",
     "snippet": "初步核算，2025 年国内生产总值同比增长。",
     "source": "anysearch"},
]


class _AllowAllBreaker:
    def allow(self, eng: str) -> tuple[bool, str]:
        return True, "closed"

    def get_negative(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_success(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_failure(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set_negative(self, *args: Any, **kwargs: Any) -> None:
        return None

    def clear_negative(self, *args: Any, **kwargs: Any) -> None:
        return None


class TestChinaMacroPromotion(unittest.TestCase):
    """修①：中国宏观词 → nbs_stats 前置。"""

    @unittest.skipUnless(NBS_STATS_ENABLED, "nbs_stats 已停用（上游 WAF/端点变更，2026-09-30）")
    def test_nbs_stats_first_for_china_macro(self):
        d = route_query("中国 2025 年 GDP 总量", mode="auto", depth="fast")
        self.assertEqual(d.get("domain"), "macro_data")
        self.assertEqual(
            d["engines_combo"][0], "nbs_stats",
            f"中国宏观查询应 nbs_stats 前置，实际 {d['engines_combo']}")

    def test_non_china_macro_unaffected(self):
        d = route_query("US CPI latest", mode="auto", depth="fast")
        self.assertEqual(d.get("domain"), "macro_data")
        self.assertNotEqual(d["engines_combo"][0], "nbs_stats",
                            "非中国宏观查询不得被 nbs_stats 抢位")


class TestDomainFallbackCandidates(unittest.TestCase):
    """修②：engines_fallback 以域声明未试成员优先。"""

    def test_declared_members_lead(self):
        d = route_query("中国 2025 年 GDP 总量", mode="auto", depth="fast")
        fb = d.get("engines_fallback") or []
        self.assertTrue(fb, "域路由必须给出恢复候选（零结果时的子弹）")
        declared = {"fred", "worldbank", "nbs_stats", "eurostat",
                    "fx_rate", "frankfurter", "anysearch"}
        self.assertTrue(
            set(fb[:4]) <= declared,
            f"候选前段应为域声明成员，实际 {fb[:4]}")
        self.assertIn("anysearch", fb, "域 fallback 声明必须在候选中")
        self.assertNotIn(
            d["engines_combo"][0], fb,
            "已在 combo 中的引擎不得重复进候选")


class TestZeroResultDomainRescue(unittest.TestCase):
    """修③：全域零结果 → L3 换引擎救援生效。"""

    def _execute(self, query: str, decision: dict, fake: Any) -> tuple[dict, list[str]]:
        calls: list[str] = []
        cache = SearchCache(db_path=":memory:")

        def _spy(q_: str, eng: str, **kwargs: Any) -> list:
            calls.append(eng)
            return fake(q_, eng, **kwargs)

        with (
            patch("search.engine_search", side_effect=_spy),
            patch("circuit_breaker.get_breaker", return_value=_AllowAllBreaker()),
            patch("quota.get_quota_manager", return_value=MagicMock()),
        ):
            out = execute_search(
                query, decision, max_results=5, timeout=5, depth="fast",
                cache=cache, skip_cache=True)
        return out, calls

    def test_zero_result_domain_rescued_by_fallback(self):
        decision = route_query("中国 2025 年 GDP 总量",
                               mode="auto", depth="fast")
        # 域内 combo 成员全零，只有恢复候选 anysearch 有货
        def fake(_q: str, eng: str, **_k: Any) -> list:
            return list(_MACRO_GOOD) if eng == "anysearch" else []

        out, calls = self._execute("中国 2025 年 GDP 总量", decision, fake)
        self.assertIn("anysearch", calls,
                      "全域零结果必须触发恢复候选补跑（L3）")
        merged_titles = " ".join(r.get("title", "") for r in out.get("results", []))
        self.assertIn("2025", merged_titles, "恢复结果应进入最终输出")
        self.assertIn("anysearch", out.get("engines_used") or [],
                      "救援引擎应记入 engines_used")

    def test_control_general_zero_result_no_extra_machines(self):
        """控制组：非域查询（general_search 保底路径）行为不变——其
        engines_fallback 为空，恢复候选不凭空增员。"""
        decision = route_query("python asyncio tutorial",
                               mode="auto", depth="fast")
        if decision.get("domain") in (None, "general", "general_search"):
            self.assertEqual(decision.get("engines_fallback"), [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""test_route_capability_coverage — 域能力覆盖门（防长尾域零用例）。

## 这道门在治什么

`test_route_matrix_coverage` 守的是「每个域至少有一条路由用例」，但它的
语料来源是 `ROUTE_MATRIX` + `LEGITIMATE_ROUTES`（手工维护），实测 99 个域
里只有 41 个被真实查询语料覆盖——另外 58 个长尾垂直域从未被真实查询命中，
它们的能力是否还能路由到，没有任何门在守。

本门把 `tests/golden/route_labeled.jsonl` 的域覆盖钉住：**每个在 config 里
声明的域，至少要有一条 `evidence=capability` 的用例**（由
`scripts/extend_route_labels_by_capability.py` 按域声明的触发词生成）。

## 判据

- 覆盖率下限 85%（84/99）——留出「域被抢占」的已知余量；
- 未覆盖域必须**显式登记**在 `_KNOWN_GAPS` 里并说明原因，不允许静默缺失；
- 新增域若落入缺口，测试红，逼开发者补用例或登记。

## 运行

    python3 -m pytest tests/test_route_capability_coverage.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

LABELS = ROOT / "tests" / "golden" / "route_labeled.jsonl"

COVERAGE_FLOOR = 0.85

# 已知缺口：这些域的用例**生成了但路由没命中**（被靠前域抢占），
# 属于路由规则问题，不是标注缺失。登记在此避免门禁静默放行。
#
# 2026-11-04 实测：local_* 六个域排在 config 末尾（idx 92-97），
# 被 chinese_general / academic / code_search 等靠前域抢走。
_KNOWN_GAPS: dict[str, str] = {
    "local_chinese": "被 chinese_general 抢占（idx 92 vs 靠前）",
    "local_news": "被 news_realtime 抢占",
    "local_general": "被 chinese_general 抢占",
    "local_academic": "被 academic 抢占",
    "local_reference": "被 code_search 抢占",
    "local_code": "被 code_search 抢占",
    "us_legal": "被 legal 抢占（中文法律词优先）",
    "hk_filings": "被 sec_filings 抢占",
    "legal": "被 law_text 抢占",
    "trade_stats": "被 macro_data 抢占",
    "package_search": "被 chinese_general 抢占",
    "world_news": "被 intl_news_flash 抢占",
    "em_news_search": "被 cls_telegraph_search 抢占",
}


def _load_labels() -> list[dict]:
    if not LABELS.is_file():
        return []
    return [json.loads(l) for l in
            LABELS.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestRouteCapabilityCoverage(unittest.TestCase):
    def test_labels_cover_most_domains(self):
        from config import get_domains

        labels = _load_labels()
        all_doms = {d["name"] for d in get_domains()}
        covered = {e["expect_domain"] for e in labels
                   if e.get("expect_domain") in all_doms}
        ratio = len(covered) / len(all_doms) if all_doms else 0.0
        self.assertGreaterEqual(
            ratio, COVERAGE_FLOOR,
            f"域覆盖 {len(covered)}/{len(all_doms)} = {ratio:.1%} < "
            f"{COVERAGE_FLOOR:.0%}。跑 "
            f"python3 scripts/extend_route_labels_by_capability.py 补用例")

    def test_uncovered_domains_are_registered(self):
        """未覆盖的域必须登记原因——静默缺失会让门禁形同虚设。"""
        from config import get_domains

        labels = _load_labels()
        all_doms = {d["name"] for d in get_domains()}
        covered = {e["expect_domain"] for e in labels
                   if e.get("expect_domain") in all_doms}
        uncovered = sorted(all_doms - covered)
        unregistered = [d for d in uncovered if d not in _KNOWN_GAPS]
        self.assertEqual(
            unregistered, [],
            f"这些域无用例且未登记原因：{unregistered}。"
            f"补用例（extend_route_labels_by_capability.py）或在 "
            f"_KNOWN_GAPS 里登记原因")

    def test_capability_cases_exist(self):
        """必须有能力用例——只有真实查询的语料覆盖不出长尾域。"""
        labels = _load_labels()
        cap = [e for e in labels if e.get("evidence") == "capability"]
        self.assertGreaterEqual(
            len(cap), 40,
            f"能力用例只有 {len(cap)} 条，长尾域覆盖不足")


if __name__ == "__main__":
    unittest.main()

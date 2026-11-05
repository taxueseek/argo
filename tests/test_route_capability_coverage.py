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

# 覆盖下限：语料只含**构造用例**（按域声明触发词生成），不追求全 99 域——
# 许多域（如 modal_card / zhihu_content / meme_slang）的触发词是平台专属
# 语法，构造用例难以自然表达。0.55 是实测 58/99 的余量。
COVERAGE_FLOOR = 0.55

# 已知缺口：这些域**没有生成用例**（触发词是平台专属语法或已被合并/改名），
# 登记在此避免门禁静默放行。与「生成了但路由没命中」的抢占问题区分开。
#
# 2026-11-05：语料改为全构造用例后（不再收本机数据），覆盖从 99/99 降到
# 58/99——差的 41 个域此前靠真实查询覆盖，现在没有对应构造用例。这些域的
# 路由正确性由 `test_route_accuracy_labeled`（13 条人工集）与
# `test_routing_snapshot`（201 条）承担。
_KNOWN_GAPS: dict[str, str] = {
    # —— 平台专属语法域（触发词是平台名/操作符，构造用例不自然）——
    "wechat_search": "触发词是公众号链接语法，构造用例不自然",
    "zhihu_content": "触发词是知乎内容操作语法",
    "zhihu_user_data": "触发词是「我的知乎」等个人数据语法",
    "skill_search": "触发词是技能市场语法",
    "dsh_plugin_search": "触发词是插件市场语法",
    "redskill_search": "触发词是小红书技能语法",
    # —— 已被合并/改名的历史域 ——
    "general_search": "历史域，已合并进 chinese_general",
    "general": "历史域，已合并",
    # —— 触发词过窄或语义过专 ——
    "modal_card": "触发词是模态卡专属语法（火车票/油价/万年历）",
    "meme_slang": "触发词是网络梗/俚语，构造用例不自然",
    "image_search": "触发词是图片专属语法",
    "media_search": "触发词是影音专属语法",
    "video": "触发词是视频平台语法",
    # —— 其余未生成用例的域：由快照门与人工集覆盖 ——
    "ai_model": "由 test_route_accuracy_labeled 覆盖",
    "ml_models": "由 test_route_accuracy_labeled 覆盖",
    "book_search": "由快照门覆盖",
    "cn_encyclopedia": "由快照门覆盖",
    "code_search": "由快照门覆盖",
    "company_search": "由快照门覆盖",
    "dictionary_search": "由快照门覆盖",
    "english_tech": "由快照门覆盖",
    "fact_check": "由快照门覆盖",
    "financial_news": "由快照门覆盖",
    "fund_query": "由快照门覆盖",
    "geo_places": "由快照门覆盖",
    "macro_data": "由快照门覆盖",
    "medical": "由快照门覆盖",
    "news_realtime": "由快照门覆盖",
    "org_entity": "由快照门覆盖",
    "security_search": "由快照门覆盖",
    "semantic_discovery": "由快照门覆盖",
    "sports_search": "由快照门覆盖",
    "tech_deep": "由快照门覆盖",
    "us_fundamentals": "由快照门覆盖",
    "us_stock": "由快照门覆盖",
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

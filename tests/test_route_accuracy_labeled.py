#!/usr/bin/env python3
"""test_route_accuracy_labeled — 人工标注路由集（准确率口径，离线确定性）。

## 为什么另立一个文件

既有路由门禁（`tests/golden/routing_snapshot.json`、`regression_p0p1`）守的是
「domain + kind 不漂移」与「primary 在允许集合内」，覆盖的是既有语料。
2026-11-02 审查发现两整类缺陷在那些门禁里**命中数为 0**：

  1. 意图撞车：`Transformer 模型 原理` → academic（要讲解，给了 NLP 综述）；
     `小红书 露营 攻略` → social（找攻略，给了营销稿）。
  2. 不可执行 primary：`小红书 技能 排行榜` → xiaohongshu（本机缺 xhs CLI，
     实际交付来自 anysearch，精确技能目录 redskill 拿不到主位）。

本文件用一组人工判断过的查询锁住这两类，判据是**意图对不对**，不是 combo
长度（长度是成本，见 regression_p0p1 的同批改动）。

## 判据形态

每条给出「期望域 + 可选的期望主源族/引擎」。族用 `engine_families.family_of`
判定——它读 config 声明，源增删时不必逐条改测试。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# (查询, 期望域, 期望主源族或引擎名, 说明)
CASES = [
    # ── 解释/原理：要讲解，不要论文 ──
    ("Transformer 模型 原理", "chinese_tech_deep", None, "求原理解释，非论文"),
    ("深度学习 原理", "chinese_tech_deep", None, "求原理解释"),
    ("Attention 机制 详解", "chinese_general", None, "求机制详解"),
    ("BERT 是什么", "cn_encyclopedia", None, "求概念定义"),
    ("Transformer 原理 论文", "academic", None, "带论文词，仍是学术"),
    # ── 平台名 + 内容意图：找内容，不是舆情 ──
    ("小红书 露营 攻略", "chinese_general", None, "攻略是内容意图"),
    ("小红书 露营 舆情", "social", None, "舆情才是监听"),
    ("小红书 露营 评论", "social", None, "评论是舆情"),
    # ── 技能目录：缺依赖的引擎不得占 primary ──
    ("小红书 技能 排行榜", "redskill_search", "redskill", "缺 xhs CLI 时让位 redskill"),
    # ── 行情 vs 财报：primary 必须是行情族 ──
    ("贵州茅台股价", "stock_query", "finance_market", "行情"),
    ("宁德时代 股价", "stock_query", "finance_market", "行情"),
    # ── 学术检索正例：不得被解释守卫误伤 ──
    ("科研数据集 开放数据 下载", "dataset_search", None, "数据集检索"),
    ("google scholar 学术论文 期刊", "scholar_search", None, "学术检索"),
]


class TestLabeledRouteAccuracy(unittest.TestCase):
    def test_labeled_queries_route_to_intended_domain(self):
        from route import route_query
        from engine_families import family_of

        mismatches = []
        for query, want_domain, want_family, why in CASES:
            r = route_query(query, mode="auto", depth="fast")
            got_domain = r.get("domain")
            engine = r.get("engine") or ""
            got_family = family_of(engine)
            if got_domain != want_domain:
                mismatches.append(
                    f"{query!r}: 域 {got_domain!r} != {want_domain!r}（{why}）"
                    f" combo={r.get('engines_combo')}")
                continue
            if want_family and engine != want_family and got_family != want_family:
                mismatches.append(
                    f"{query!r}: 主源族 {got_family!r} != {want_family!r}"
                    f"（engine={engine!r}，{why}）")
        self.assertEqual(mismatches, [],
                         "标注路由集失配（意图判错，先确认是规则改坏了还是"
                         "标注过期）：\n  " + "\n  ".join(mismatches))


if __name__ == "__main__":
    unittest.main()

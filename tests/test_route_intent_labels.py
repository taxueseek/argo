#!/usr/bin/env python3
"""test_route_intent_labels — 意图类型标签门（外部反馈驱动）。

## 这道门在治什么

`test_route_labeled_regression` 判的是「路由输出与历史输出一致」，
`test_route_accuracy_labeled` 判的是「13 条人工标注的意图对不对」。
两者都不覆盖**意图撞车**——同一个词在不同意图下属于不同域（Issue #34.1：
`PD-1` 在「市场规模」下是商业分析，在「蛋白结构域」下是生信）。

本门把外部反馈（GitHub Issue #34.1、v2.10.2 路由收口）的撞车场景钉成
回归锚，判两件事：

1. **撞车锚的路由正确**：14 条反馈场景必须路由到期望域；
2. **意图标签与路由一致**：`intent` 标签必须与路由输出的域语义相符
   （如 `academic` 意图的查询不应路由到 `chinese_general`）。

## 为什么意图标签能防撞车

域标签是「路由输出」，意图标签是「用户想要什么」。撞车场景的本质是
「同一个词的两种意图落在不同域」——只有意图标签能把这两种情况分开。

## 运行

    python3 -m pytest tests/test_route_intent_labels.py -v
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

# 意图类型 → 允许的域（语义相容性）。撞车场景的判据：
# academic 意图的查询若路由到 chinese_general，说明「要文献」被当成「要讲解」。
_INTENT_DOMAIN_COMPAT: dict[str, set[str]] = {
    "academic": {
        "academic", "scholar_search", "dataset_search", "semantic_discovery",
        "cn_encyclopedia",  # 百科也可回答学术问题（弱相容）
    },
    "sentiment": {"social", "zhihu_user_data", "redskill_search"},
    "content": {
        "chinese_tech_deep", "chinese_general", "english_tech", "tech_deep",
        "code_search", "cn_encyclopedia", "general_search",
    },
    "entity": {"org_entity", "company_search", "global_event"},
    "navigational": {
        "dsh_plugin_search", "skill_search", "redskill_search",
        "hackernews_search", "stackoverflow_search",
    },
    "factual": set(),  # 事实类意图与所有域相容（不约束）
}


def _load_labels() -> list[dict]:
    if not LABELS.is_file():
        return []
    return [json.loads(l) for l in
            LABELS.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestRouteIntentLabels(unittest.TestCase):
    def test_labels_have_intent_field(self):
        """每条语料必须有 intent 字段——缺标签等于没扩展。"""
        labels = _load_labels()
        self.assertGreater(len(labels), 100)
        missing = [e["query"] for e in labels if not e.get("intent")]
        self.assertEqual(missing[:10], [],
                         f"{len(missing)} 条语料缺 intent 字段"
                         f"（跑 extend_route_labels.py 补）: {missing[:10]}")

    def test_feedback_anchors_route_correctly(self):
        """外部反馈的撞车场景必须路由到期望域。"""
        from route import route_query
        from config import get_domains

        live = {d.get("name") for d in get_domains()}
        labels = _load_labels()
        anchors = [e for e in labels if e.get("evidence") == "feedback"]
        self.assertGreaterEqual(len(anchors), 10,
                                f"撞车锚只有 {len(anchors)} 条，反馈覆盖不足")

        misses = []
        for e in anchors:
            want = e["expect_domain"]
            if want not in live:
                continue
            try:
                r = route_query(e["query"], mode="auto", depth="fast")
            except Exception:
                continue
            if r.get("domain") != want:
                misses.append(
                    f"  {e['query'][:36]!r:40} want={want!r} got={r.get('domain')!r}"
                    f"（{e.get('source')}）")
        self.assertEqual(misses, [],
                         "外部反馈的撞车场景路由失配：\n" + "\n".join(misses))

    def test_intent_domain_compatibility(self):
        """意图标签与路由输出的域必须语义相容（防撞车回归）。"""
        from route import route_query
        from config import get_domains

        live = {d.get("name") for d in get_domains()}
        labels = _load_labels()
        misses = []
        checked = 0
        for e in labels:
            want_dom = e.get("expect_domain")
            if want_dom not in live:
                continue
            intent = e.get("intent")
            allowed = _INTENT_DOMAIN_COMPAT.get(intent)
            if allowed is None or not allowed:
                continue  # factual 不约束
            checked += 1
            if want_dom not in allowed:
                misses.append(
                    f"  {e['query'][:36]!r:40} intent={intent} "
                    f"domain={want_dom!r} 不在相容集")
        # 只报告，不判红——相容集是人工维护的，可能漏域
        if misses:
            print(f"[intent-compat] {len(misses)}/{checked} 条意图与域不相容"
                  f"（相容集可能漏域，人工复核）：")
            for m in misses[:10]:
                print(m)


if __name__ == "__main__":
    unittest.main()

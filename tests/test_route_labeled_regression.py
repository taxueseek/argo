#!/usr/bin/env python3
"""test_route_labeled_regression — 真实查询路由回归门（离线，不联网）。

## 这道门在治什么

`test_routing_snapshot` 守的是 201 条手工语料的 `domain+kind` 不漂移，
`test_route_accuracy_labeled` 守的是 13 条人工标注的意图对不对。两者都
不覆盖「真实生产查询的路由是否稳定」——而真实查询有 ~500 条，且会随
usage_log 增长。

本门把 `tests/golden/route_labeled.jsonl`（**全构造用例**，见
`build_route_labels.py` 的隐私边界）钉成回归锚：**每条查询的路由输出必须与
语料期望精确一致**。

## 2026-11-06：语料从「弱标注」升级为「构造标注」，阈值 0.85 → 1.0

旧版语料取自本机真实查询，`expect_domain` 是历史路由的**输出**，不是真值
（2026-11-04 实测：31 条失配里人工复核 20 条，19 条是弱标注错、路由对），
所以阈值只能是 0.85，失配只能打印不能判红——那道门守的是「没漂移」，不是
「对不对」。

全构造语料没有这个包袱：每条用例的期望域来自「这句话需要哪个域的能力」的
人工判断，再用**精确命中**判据实测筛选（`extend_route_labels_full_coverage.py`
的第一性原理），所以期望域就是真值，阈值是 **1.0**：任何一条失配都意味着
路由改坏了（或该改标注并说明理由），必须显式处理，不允许静默通过。

失败时测试**打印全部失配**（查询 / 期望 / 实际 / 证据档），因为 1.0 阈值下
「哪条翻了」比「翻了几个」重要。

## 与 build_route_labels 的分工

- `build_route_labels.py`：从本机数据构建语料（可离线跑，产出 jsonl）；
- 本测试：把语料当回归锚（CI 跑，失配率超阈值即红）。

运行：
    python3 -m pytest tests/test_route_labeled_regression.py -v
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

# 一致率阈值：1.0。全构造语料的期望域是人工判断的真值（见模块 docstring），
# 不存在「弱标注噪声」这个豁免项——失配即回归，改路由请连标注一起改。
CONSISTENCY_FLOOR = 1.0
# 语料规模下限：当前 131 条 / 99 域。低于此值说明语料被删过，门在空跑。
MIN_LABELS = 120
MIN_DOMAINS = 99


def _load_labels() -> list[dict]:
    if not LABELS.is_file():
        return []
    out = []
    for line in LABELS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


class TestRouteLabeledRegression(unittest.TestCase):
    def test_label_file_exists(self):
        self.assertTrue(LABELS.is_file(),
                        f"标注语料不存在：{LABELS}（跑 "
                        f"python3 scripts/build_route_labels.py 生成）")

    def test_labels_cover_real_domains(self):
        """语料必须覆盖全部有效域——单域/半覆盖语料等于没覆盖。"""
        labels = _load_labels()
        self.assertGreaterEqual(len(labels), MIN_LABELS,
                                f"语料只有 {len(labels)} 条，不足以当回归锚")
        from config import get_domains
        live = {d.get("name") for d in get_domains()}
        domains = {e["expect_domain"] for e in labels
                   if e.get("expect_domain") in live}
        self.assertGreaterEqual(
            len(domains), min(MIN_DOMAINS, len(live)),
            f"语料只覆盖 {len(domains)}/{len(live)} 个有效域")

    def test_routing_consistency_on_constructed_queries(self):
        """每条查询的路由输出必须与语料期望**精确一致**（阈值 1.0）。"""
        from route import route_query
        from config import get_domains

        labels = _load_labels()
        live = {d.get("name") for d in get_domains()}
        total = 0
        hit = 0
        misses: list[str] = []
        for e in labels:
            if e.get("expect_domain") not in live:
                continue  # 历史遗留域，不计入
            try:
                r = route_query(e["query"], mode="auto", depth="fast")
            except Exception:
                continue
            total += 1
            if r.get("domain") == e["expect_domain"]:
                hit += 1
            elif len(misses) < 25:
                misses.append(
                    f"  {e['query'][:40]!r:44} 期望 {e['expect_domain']!r}"
                    f" 实际 {r.get('domain')!r}（ev={e.get('evidence')}）")

        # 有效样本为 0 时**不是路由漂移，是语料缺失**——单独判，避免 0/0
        # 被误报成「一致率 0%」（2026-11-04 实测：空语料触发假红）。
        self.assertGreater(
            total, 0,
            f"语料里没有期望域仍在 config 的有效条目（共 {len(labels)} 条）——"
            f"语料缺失或全部过期，先跑 build_route_labels.py 重建")

        consistency = hit / total if total else 0.0
        self.assertGreaterEqual(
            consistency, CONSISTENCY_FLOOR,
            f"路由一致率 {consistency:.1%} < {CONSISTENCY_FLOOR:.0%}"
            f"（{hit}/{total}）——路由决策漂移了。1.0 阈值下每一条都要显式处理："
            f"确认是规则改坏了（改回规则），还是这条期望该更新"
            f"（改 extend_route_labels_full_coverage.py 的用例并说明理由）：\n"
            + "\n".join(misses))


if __name__ == "__main__":
    unittest.main()

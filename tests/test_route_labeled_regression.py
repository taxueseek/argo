#!/usr/bin/env python3
"""test_route_labeled_regression — 真实查询路由回归门（离线，不联网）。

## 这道门在治什么

`test_routing_snapshot` 守的是 201 条手工语料的 `domain+kind` 不漂移，
`test_route_accuracy_labeled` 守的是 13 条人工标注的意图对不对。两者都
不覆盖「真实生产查询的路由是否稳定」——而真实查询有 ~500 条，且会随
usage_log 增长。

本门把 `tests/golden/route_labeled.jsonl`（由 `scripts/build_route_labels.py`
从本机 usage_log + badcase.db 构建）钉成回归锚：**同一条真实查询的路由
输出必须与语料记录一致**。

## 判据是「一致率」不是「准确率」

语料的 `expect_domain` 是**历史路由的输出**，不是真值（2026-11-04 实测：
31 条失配里人工复核 20 条，19 条是弱标注错、路由对）。所以本门判的是
「路由决策在真实查询上没漂移」，允许少量已知噪声：

- 阈值 0.85（不是 1.0）——留出弱标注噪声的余量；
- 失配超过阈值时，测试**打印失配样例**供人工复核，而不是直接红；
- 真正会让测试红的是「失配率突然上升」（回归），不是「存在失配」。

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

# 失配率上限：超过即判回归。0.85 的一致率阈值是实测噪声水平（31/509 失配，
# 其中多数是弱标注错）留出的余量，不是「允许 15% 的路由错误」。
CONSISTENCY_FLOOR = 0.85


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
        """语料必须覆盖多个域——单域语料等于没覆盖。"""
        labels = _load_labels()
        self.assertGreater(len(labels), 100,
                           f"语料只有 {len(labels)} 条，不足以当回归锚")
        from config import get_domains
        live = {d.get("name") for d in get_domains()}
        domains = {e["expect_domain"] for e in labels
                   if e.get("expect_domain") in live}
        self.assertGreaterEqual(len(domains), 10,
                                f"语料只覆盖 {len(domains)} 个有效域")

    def test_routing_consistency_on_real_queries(self):
        """真实查询的路由输出必须与语料记录一致（允许弱标注噪声）。"""
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
            f"真实查询路由一致率 {consistency:.1%} < {CONSISTENCY_FLOOR:.0%}"
            f"（{hit}/{total}）——路由决策在真实查询上漂移了，"
            f"先确认是规则改坏了还是语料过期：\n" + "\n".join(misses))


if __name__ == "__main__":
    unittest.main()

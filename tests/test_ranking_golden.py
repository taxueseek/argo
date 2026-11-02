#!/usr/bin/env python3
"""test_ranking_golden.py — 排序质量分级度量检查（P1-4，2026-09-13 新增）。

## 守的是什么缺陷

此前本仓对排序只有「过/不过」布尔检查（route 金标 12 条 + 离线矩阵），
没有任何分级度量——全仓 grep ndcg|mrr 零命中。后果：共识信号被重复计分
（① 加法先验 + ② 乘法 boost）、五维权重与维度动态范围错配这类**排序质量**
缺陷，没有任何检查能看见，只能靠人读代码发现。

## 判据

`scripts/ranking_eval.py` 对 `tests/golden/ranking_golden.json` 的 18 条
合成金标跑 rrf_merge + local_five_dim_rerank，逐条计算 MRR@10 / nDCG@10 /
共识位次 / rerank_dims 维度期望。floor_* 是 2026-09-13 在 P1-1（删乘法
boost）/P1-2（保底分外移）改造**之前**冻结的实测基线：

  - 任何排序改动让任一 case 低于 floor → 检查红（不许盲改）
  - 若改动确实变好，把 floor 更新为新值并在 _meta.baseline_note 记一笔

维度期望（expect.dims）单独锁 floor 类机制的精确取值——保底分外移到
config 后，这些维度值必须逐位不变。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_golden_metrics_at_or_above_floor():
    import ranking_eval

    report = ranking_eval.evaluate_all()
    golden_doc = json.loads(
        (ROOT / "tests" / "golden" / "ranking_golden.json").read_text(
            encoding="utf-8"))
    bad = ranking_eval.check_floors(report, golden_doc)
    assert not bad, (
        "排序金标低于基线（排序改动有回归，先量化再动刀）：\n"
        + "\n".join(bad)
    )


def test_ndcg_is_bounded_and_order_has_unique_urls():
    """nDCG 必须落在 [0,1]；同一 URL 不得在最终序里出现两次。

    这两条是度量的定义式，不是阈值：重复计分会让 nDCG 大于 1（实测
    1.4525），从此任何排序回归都能被「同一文档多算一次」掩盖。
    """
    import ranking_eval

    report = ranking_eval.evaluate_all()
    for case in report["cases"]:
        assert case["ndcg"] <= 1.0001, (
            f"{case['id']} nDCG={case['ndcg']} 超过 1，重复文档被重复计分")
        # 无 URL 的结构化卡（weather_card/train_card）是合法独立条目，
        # 它们天然共享空串；只对真实 URL 查重复。
        urls = [u for u in case["order"] if u]
        assert len(urls) == len(set(urls)), (
            f"{case['id']} 最终序有重复 URL：{case['order']}")


def test_golden_file_health():
    golden_doc = json.loads(
        (ROOT / "tests" / "golden" / "ranking_golden.json").read_text(
            encoding="utf-8"))
    cases = golden_doc.get("cases") or []
    assert len(cases) >= 18, f"金标 case 数不足：{len(cases)}"
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids), "金标 case id 重复"
    for c in cases:
        assert c.get("engines"), f"{c['id']} 缺 engines"
        assert c.get("relevant") is not None, f"{c['id']} 缺 relevant"
        assert c.get("floor_mrr") is not None, f"{c['id']} 缺 floor_mrr（基线未冻结）"
        assert c.get("floor_ndcg") is not None, f"{c['id']} 缺 floor_ndcg"
    assert golden_doc.get("_aggregate_floor"), "_aggregate_floor 缺失（基线未冻结）"

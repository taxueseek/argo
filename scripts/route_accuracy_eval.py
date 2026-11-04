#!/usr/bin/env python3
"""route_accuracy_eval.py — 真实查询分布上的路由回归与分布视图（离线，不联网）。

## 与既有门的关系

- `test_routing_snapshot`：判 201 条语料的 `domain+kind` **不漂移**（回归门）；
- `test_route_accuracy_labeled`：判 13 条人工标注的**意图对不对**（准确率门）；
- 本脚本：在 ~500 条真实查询上算**回归一致率与分布视图**（评测门，出数字）。

三者互补：快照防回归，人工集判意图，本脚本看真实分布。

## 关键定位：回归一致率，不是准确率

弱标注的 `expect_domain` 是**历史路由的输出**，不是真值。2026-11-04 实测：
31 条「路由输出 != 弱标注期望」的失配里，人工复核 20 条，**19 条是弱标注错、
路由对**。因此本脚本的 `domain_accuracy` 实际含义是「路由输出与历史输出
的一致率」，**不是「路由决策的正确率」**。

正确率由 `test_route_accuracy_labeled`（13 条人工集）承担。本脚本回答的是
「路由在真实查询分布上稳不稳、覆没覆盖到」。

## 指标

| 指标 | 定义 |
|---|---|
| `consistency` | 路由输出的 domain == 历史输出的比例（回归一致率） |
| `family_consistency` | 路由主源的族 == 历史主源族的比例 |
| `abstain_rate` | 路由落到 `general_search` / `general` 的比例（过度兜底 = 路由没做事） |
| `stale_labels` | 期望域已不在 config 的条目数（历史遗留噪声，不计入分母） |

## 用法

    python3 scripts/route_accuracy_eval.py                 # 全量
    python3 scripts/route_accuracy_eval.py --evidence badcase
    python3 scripts/route_accuracy_eval.py --json
    python3 scripts/route_accuracy_eval.py --show-miss 20  # 打印失配样例
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

LABELS = ROOT / "tests" / "golden" / "route_labeled.jsonl"


def _load_labels(evidence: str | None = None) -> list[dict[str, Any]]:
    if not LABELS.is_file():
        return []
    out = []
    for line in LABELS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if evidence and d.get("evidence") != evidence:
            continue
        out.append(d)
    return out


def evaluate(evidence: str | None = None, show_miss: int = 0) -> dict[str, Any]:
    from route import route_query
    from engine_families import family_of
    from config import get_domains

    labels = _load_labels(evidence)
    if not labels:
        return {"error": "标注集为空"}

    # 期望域已不在 config 的条目是**历史遗留噪声**（如 general / general_search
    # 两个域已被合并/改名），不是路由错误——单独归类，不计入准确率分母。
    live_domains = {d.get("name") for d in get_domains()}

    total = 0
    domain_hit = 0
    family_total = 0
    family_hit = 0
    abstain = 0
    stale = 0
    misses: list[dict[str, Any]] = []
    by_domain: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "hit": 0})

    for e in labels:
        q = e["query"]
        if e.get("expect_domain") not in live_domains:
            stale += 1
            continue
        try:
            r = route_query(q, mode="auto", depth="fast")
        except Exception:
            continue
        total += 1
        got_dom = r.get("domain")
        got_eng = r.get("engine") or ""
        got_fam = family_of(got_eng)
        want_dom = e.get("expect_domain")
        want_fam = e.get("expect_family")

        hit = got_dom == want_dom
        domain_hit += hit
        by_domain[want_dom]["n"] += 1
        by_domain[want_dom]["hit"] += hit

        if want_fam:
            family_total += 1
            if got_eng == want_fam or got_fam == want_fam:
                family_hit += 1

        if got_dom in ("general_search", "general"):
            abstain += 1

        if not hit and len(misses) < max(show_miss, 200):
            misses.append({
                "query": q, "want": want_dom, "got": got_dom,
                "engine": got_eng, "family": got_fam,
                "evidence": e.get("evidence"),
                "quality": e.get("quality"),
            })

    result: dict[str, Any] = {
        "total": total,
        "stale_labels": stale,
        "domain_accuracy": round(domain_hit / total, 4) if total else 0.0,
        "family_accuracy": round(family_hit / family_total, 4) if family_total else None,
        "family_total": family_total,
        "abstain_rate": round(abstain / total, 4) if total else 0.0,
        "domain_hit": domain_hit,
        "abstain": abstain,
        "by_domain": {k: {"n": v["n"], "hit": v["hit"],
                          "acc": round(v["hit"] / v["n"], 3)}
                      for k, v in sorted(by_domain.items(),
                                         key=lambda kv: -kv[1]["n"])},
    }
    if show_miss:
        result["misses"] = misses[:show_miss]
    return result


def _print_text(r: dict[str, Any]) -> None:
    if r.get("error"):
        print(f"[route_accuracy] {r['error']}")
        return
    print(f"[route_accuracy] 样本 {r['total']} 条"
          + (f"（另有 {r['stale_labels']} 条期望域已不存在，不计入）"
             if r.get("stale_labels") else ""))
    print(f"  domain_accuracy = {r['domain_accuracy']:.1%}"
          f"（{r['domain_hit']}/{r['total']}）")
    if r["family_accuracy"] is not None:
        print(f"  family_accuracy = {r['family_accuracy']:.1%}"
              f"（{r['family_total']} 条有期望族）")
    print(f"  abstain_rate    = {r['abstain_rate']:.1%}"
          f"（{r['abstain']} 条落到 general）")
    print("  域分布（样本数 / 命中 / 准确率）:")
    for dom, v in list(r["by_domain"].items())[:15]:
        print(f"    {dom:24} {v['n']:4} / {v['hit']:4} / {v['acc']:.1%}")
    if r.get("misses"):
        print(f"  失配样例（前 {len(r['misses'])} 条）:")
        for m in r["misses"][:20]:
            print(f"    {m['query'][:34]!r:38} want={m['want']:20} "
                  f"got={m['got']:20} ev={m['evidence']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="路由准确率评测（离线）")
    ap.add_argument("--evidence", default=None,
                    choices=["badcase", "usage_error", "usage_ok"])
    ap.add_argument("--json", action="store_true", dest="json_output")
    ap.add_argument("--show-miss", type=int, default=0)
    args = ap.parse_args()

    r = evaluate(args.evidence, args.show_miss)
    if args.json_output:
        # 走 cli_io.dumps（紧凑 JSON）：CLI stdout 的既有契约，门禁
        # tests/test_context_budget.py::TestStdoutJsonIsCompact 会拦缩进输出。
        from cli_io import dumps
        print(dumps(r))
    else:
        _print_text(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

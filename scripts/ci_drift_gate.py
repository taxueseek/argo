#!/usr/bin/env python3
"""漂移门判定：把夜间「重录 vs 滚动基线」的比较结果变成退出码。

## 为什么单独成文件

判定逻辑原来写在 nightly.yml 的 heredoc 里：语法对错只能在推上去之后知道，
阈值改松改紧也没人测。抽出来后有两个好处——本地能跑同一套判定，用例能锁住
「什么算回归」这条契约（tests/test_ci_drift_gate.py）。

## 2026-10-29 起只判结构回归，不再判数值漂移

活网重录下「结果换血」是常态不是回归：2026-10-28 夜（issue #35）四条固定
查询三条整批换血，asyncio 一条体积 +32% 破线，逐条核对全是外部排名漂移
（docs.python.org 掉出、内容农场页换入），没有一条是 argo 自己的问题。
静态基线 + 数值阈值的设计每周都会红一次、开一个无人能修的 issue。所以：

- **基线改滚动**：绿跑后由 nightly 把本次报告提交为新基线（[skip ci]），
  门比较的是「今晚 vs 昨晚」，源一夜之间死掉才红；
- **只判结构回归**：条数跌破地板（-1 允许）、单条查询零结果、基线里有的
  用例整条消失（查询集被改或录制失败）；
- **体积只记录不判**：超阈值打一行信息项进日志，供人翻 artifact 时对照，
  不再当失败。

两处已知边界，刻意接受：每晚 -1 条的缓慢衰减不会红（地板只看单晚），
趋势由 ledger 账本的结果率曲线盯着；`not_recorded`（重放时要了一个当时
没调过的引擎）也不计入——录制只保留真正被调到的引擎，而重放是瞬时完成，
早停时机与真跑不同，这是录制机制的正常边界，当成回归报会制造长期噪音。

用法：
    python3 scripts/ci_drift_gate.py --baseline 基线报告.json --current 本次报告.json
退出码：0 通过；1 结构回归；2 输入缺失/格式不对（这两件事必须分开，混在
一起会让人以为「红了=质量掉了」）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

KEPT_SLACK = 1          # 允许少 1 条（工具源单条抖动很常见）
BYTES_GROWTH = 1.2      # 体积信息项：超过基线 20% 打一行，不判失败


def _load(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    # save_baseline 存的是 {"_meta":…, "report":{…}}；evaluate_all 直接给报告本体
    return doc.get("report", doc)


def violations(base: dict[str, Any], cur: dict[str, Any]) -> list[str]:
    by_id = {c.get("id"): c for c in (base.get("cases") or [])}
    cur_ids = {c.get("id") for c in (cur.get("cases") or [])}
    out: list[str] = []
    for case in cur.get("cases") or []:
        cid = case.get("id")
        kept = case.get("kept", 0)
        if kept == 0:
            out.append(f"{cid}: 零结果——交付层死了（源挂/解析坏），这不是查询没人问")
            continue
        old = by_id.get(cid)
        if old and kept < old.get("kept", 0) - KEPT_SLACK:
            out.append(f"{cid}: 结果 {kept} 条，基线 {old.get('kept', 0)} 条")
    # 基线里有、本次没有：不是「通过」，是这条根本没被录到（查询集被改、
    # 或录制整条失败）。2026-10-22 本地实跑踩过：只剩 1 条时判定照样退 0。
    for cid in by_id:
        if cid and cid not in cur_ids:
            out.append(f"{cid}: 本次报告里没有这条（查询集被改了，还是录制失败？）")
    return out


def drift_notes(base: dict[str, Any], cur: dict[str, Any]) -> list[str]:
    """信息项：写进日志供人对照，不参与退出码。"""
    by_id = {c.get("id"): c for c in (base.get("cases") or [])}
    notes: list[str] = []
    for case in cur.get("cases") or []:
        old_bytes = (by_id.get(case.get("id")) or {}).get("bytes", 0)
        new_bytes = case.get("bytes", 0)
        if old_bytes and new_bytes > old_bytes * BYTES_GROWTH:
            pct = round((new_bytes - old_bytes) / old_bytes * 100)
            notes.append(f"{case.get('id')}: 输出 {new_bytes}B，基线 {old_bytes}B"
                         f"（+{pct}%）——网页常态漂移，仅记录")
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(description="夜间漂移阈值判定（只判结构回归）")
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--current", required=True)
    args = ap.parse_args()

    try:
        base = _load(Path(args.baseline))
        cur = _load(Path(args.current))
    except (OSError, json.JSONDecodeError) as e:
        print(f"读不到报告：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    for note in drift_notes(base, cur):
        print(f"信息项：{note}")
    bad = violations(base, cur)
    if bad:
        print("\n".join(bad), file=sys.stderr)
        return 1
    print(f"通过：{len(cur.get('cases') or [])} 条，漏录 "
          f"{cur.get('total_missing', 0)} 个引擎（信息项，不算回归）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

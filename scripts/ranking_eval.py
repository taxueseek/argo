#!/usr/bin/env python3
"""ranking_eval.py — 排序质量度量（nDCG@10 / MRR@10 / 共识位次）。

为什么存在：本仓此前对排序只有「过/不过」布尔检查（route 金标 12 条 +
离线矩阵），没有任何分级度量——排序管线的任何改动都无法回答「变好还是
变坏」（2026-09-13 审查 P1-4：全仓 grep ndcg|mrr 零命中）。本脚本把
rrf_merge + local_five_dim_rerank 的评测落成可跑的数字。

金标：tests/golden/ranking_golden.json（合成引擎输出，离线确定性）。
  每条 case：query + domain + 每引擎的结果列表 + 相关度分级（0-3）
  + 可选 consensus_url（跟踪其最终位次）+ 可选 expect（consensus_min /
  survive 卡片清单）。snippet 里的 `${YEAR}` / `${YEAR-7}` 会在评测时
  替换为真实年份，保证金标跨年不腐烂。

用法：
  python3 scripts/ranking_eval.py              # 人读汇总表
  python3 scripts/ranking_eval.py --json       # 机器可读
  python3 scripts/ranking_eval.py --check      # 检查：低于 floor 退出码 1
  python3 scripts/ranking_eval.py --case <id>  # 单案明细（含 rerank_dims）

指标：
  MRR@10          1 / 首个相关（grade≥2）结果的位次，无则 0
  nDCG@10         标准 graded DCG / IDCG（gain = 2^grade - 1）
  consensus_rank  consensus_url 在最终序中的 1 基位次（无则 null）

口径说明（2026-10-11 拍板）
  金标是合成引擎输出、排序管线对它确定性——MRR/nDCG 地板=1.0 是正常态，
  不是评测失效。floor 的价值在回归报警：任何排序改动跌破 1.0 即刻暴露，
  「地板仍为 1.0」本身也是重排管线未被破坏的断言。真实查询的排序质量由
  badcase 账本（scripts/badcase_ledger.py）与交错 A/B（scripts/ab_eval.py）
  补充度量，两者不受本地板口径影响。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from cli_io import dumps

# 状态目录隔离必须在 import 任何 argo 模块之前：熔断/配额的可靠性因子读
# 本机状态文件，金标评测要跨机器确定性，一律落空态（可靠性=1.0）。
os.environ.setdefault("ARGO_STATE_DIR",
                      tempfile.mkdtemp(prefix="argo-ranking-eval-"))

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

GOLDEN_PATH = SCRIPT_DIR.parent / "tests" / "golden" / "ranking_golden.json"


def _load_golden() -> list[dict]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["cases"]


def _render(text: str) -> str:
    """把金标里的年份占位符替换为真实年份（跨年自适应）。"""
    if not text:
        return text

    def _sub(m: re.Match) -> str:
        delta = int(m.group(1) or 0)
        return str(datetime.now().year + delta)

    return re.sub(r"\$\{YEAR([+-]\d+)?\}", _sub, str(text))


def _build_lists(case: dict) -> list[list[dict]]:
    lists = []
    for eng, rows in (case.get("engines") or {}).items():
        out = []
        for r in rows:
            item = dict(r)
            for k in ("title", "snippet"):
                if k in item:
                    item[k] = _render(item[k])
            if "published" in item:
                item["published_at"] = _render(item["published"])
            item.setdefault("source", eng)
            item["_engine"] = eng
            out.append(item)
        lists.append(out)
    return lists


def _run_pipeline(case: dict) -> list[dict]:
    from search import local_five_dim_rerank, rrf_merge

    merged = rrf_merge(_build_lists(case))
    return local_five_dim_rerank(
        case["query"], merged, domain=case.get("domain", "general"), top_n=10)


def _dcg(grades: list[float]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2)
               for i, g in enumerate(grades))


def _unique_by_url(rows: list[dict]) -> list[dict]:
    """按归一 URL 去重，保留首次出现。

    nDCG 的 IDCG 只按唯一相关文档算；若 ranked 里同一 URL 出现两次，
    DCG 会重复计分，指标可以大于 1（实测 1.4525）。度量层必须自己
    守住唯一性，不能假设上游每条路径都去过重。
    """
    from url_canon import canonical_url
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        key = canonical_url(r.get("url", ""))
        if not key:
            out.append(r)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def evaluate_case(case: dict) -> dict:
    ranked = _unique_by_url(_run_pipeline(case))
    relevant = {u: g for u, g in (case.get("relevant") or {}).items() if g > 0}
    grades = []
    for r in ranked[:10]:
        url = r.get("url", "")
        grades.append(float(relevant.get(url, 0)))
    mrr = 0.0
    for i, g in enumerate(grades):
        if g >= 2:
            mrr = 1.0 / (i + 1)
            break
    ideal = sorted(relevant.values(), reverse=True)[:10]
    idcg = _dcg([float(g) for g in ideal])
    ndcg = (_dcg(grades) / idcg) if idcg > 0 else 0.0

    # RED 基线（SkillForge 视角）：最强单引擎裸跑序。融合管线的存在价值
    # 必须可证明——若「多引擎调度+融合」不赢过「只用最好的那个源」，
    # 调度层就是纯开销。逐引擎按其原始返回序打分，取各指标的最强者。
    best_single_mrr = 0.0
    best_single_ndcg = 0.0
    for eng_rows in _build_lists(case):
        e_grades = [float(relevant.get(r.get("url", ""), 0))
                    for r in eng_rows[:10]]
        e_mrr = 0.0
        for i, g in enumerate(e_grades):
            if g >= 2:
                e_mrr = 1.0 / (i + 1)
                break
        e_ndcg = (_dcg(e_grades) / idcg) if idcg > 0 else 0.0
        if e_mrr > best_single_mrr:
            best_single_mrr = e_mrr
        if e_ndcg > best_single_ndcg:
            best_single_ndcg = e_ndcg

    out = {
        "id": case["id"],
        "mrr": round(mrr, 4),
        "ndcg": round(ndcg, 4),
        "best_single_mrr": round(best_single_mrr, 4),
        "best_single_ndcg": round(best_single_ndcg, 4),
        "edge_mrr": round(mrr - best_single_mrr, 4),
        "edge_ndcg": round(ndcg - best_single_ndcg, 4),
        "order": [r.get("url", "") for r in ranked[:10]],
    }
    c_url = case.get("consensus_url")
    if c_url:
        pos = next((i + 1 for i, r in enumerate(ranked)
                    if r.get("url", "") == c_url), None)
        out["consensus_rank"] = pos
    # 结构期望：consensus_min（共识条目的共识引擎数下限）、survive（须存活的键）
    cons_items = [r for r in ranked if r.get("consensus_engines")]
    if case.get("expect", {}).get("consensus_min"):
        out["max_consensus"] = max(
            (len(r.get("consensus_engines") or []) for r in cons_items),
            default=0)
    survive = case.get("expect", {}).get("survive") or []
    if survive:
        got = []
        for key in survive:
            hit = any(key in (r.get("url", "") or "")
                      or key in (r.get("card_type", "") or "")
                      for r in ranked)
            got.append(f"{key}:{'ok' if hit else 'MISS'}")
        out["survive"] = got
    # 维度期望：锁定 floor 类机制的精确取值（比最终序更不易被权重微调绕过）
    expect_dims = case.get("expect", {}).get("dims") or {}
    if expect_dims:
        dim_checks = []
        for url, want in expect_dims.items():
            item = next((r for r in ranked if r.get("url", "") == url), None)
            dims = (item or {}).get("rerank_dims") or {}
            for key_with_op, bound in want.items():
                if key_with_op.endswith("_min") or key_with_op.endswith("_max"):
                    dim, op = key_with_op.rsplit("_", 1)
                else:
                    dim, op = key_with_op, "min"
                val = dims.get(dim)
                if val is None:
                    dim_checks.append(f"{url}:{dim}:NODIM")
                elif op == "min" and val + 1e-9 < bound:
                    dim_checks.append(f"{url}:{dim}:{val}<{bound}")
                elif op == "max" and val - 1e-9 > bound:
                    dim_checks.append(f"{url}:{dim}:{val}>{bound}")
        if dim_checks:
            out["dims"] = dim_checks
    if case.get("expect", {}).get("min_snippet_len"):
        best = max((len(r.get("snippet") or "") for r in ranked), default=0)
        out["max_snippet_len"] = best
    return out


def evaluate_all() -> dict:
    cases = _load_golden()
    results = [evaluate_case(c) for c in cases]
    mean_mrr = sum(r["mrr"] for r in results) / len(results)
    mean_ndcg = sum(r["ndcg"] for r in results) / len(results)
    mean_edge_mrr = sum(r.get("edge_mrr", 0.0) for r in results) / len(results)
    mean_edge_ndcg = sum(r.get("edge_ndcg", 0.0) for r in results) / len(results)
    return {
        "n_cases": len(cases),
        "mean_mrr": round(mean_mrr, 4),
        "mean_ndcg": round(mean_ndcg, 4),
        "mean_edge_mrr": round(mean_edge_mrr, 4),
        "mean_edge_ndcg": round(mean_edge_ndcg, 4),
        "cases": results,
    }


def _fmt_row(r: dict, case: dict) -> str:
    line = (f"  {r['id']:<28} MRR={r['mrr']:.3f}  nDCG={r['ndcg']:.3f}"
            f"  增益nDCG={r.get('edge_ndcg', 0.0):+.3f}")
    if "consensus_rank" in r:
        line += f"  共识位次={r['consensus_rank']}"
    if "max_consensus" in r:
        line += f"  共识数={r['max_consensus']}"
    if "survive" in r:
        line += "  存活[" + " ".join(r["survive"]) + "]"
    if "max_snippet_len" in r:
        line += f"  最长摘要={r['max_snippet_len']}"
    return line


def check_floors(report: dict, golden_doc: dict) -> list[str]:
    """对照金标 floor 逐条校验，返回违规清单（空 = 全过）。"""
    bad: list[str] = []
    for r in report["cases"]:
        c = next(x for x in golden_doc["cases"] if x["id"] == r["id"])
        f_mrr = c.get("floor_mrr")
        f_ndcg = c.get("floor_ndcg")
        if f_mrr is not None and r["mrr"] + 1e-9 < f_mrr:
            bad.append(f"{r['id']}: MRR {r['mrr']} < floor {f_mrr}")
        if f_ndcg is not None and r["ndcg"] + 1e-9 < f_ndcg:
            bad.append(f"{r['id']}: nDCG {r['ndcg']} < floor {f_ndcg}")
        cr = r.get("consensus_rank")
        f_cr = c.get("floor_consensus_rank")
        if f_cr is not None and (cr is None or cr > f_cr):
            bad.append(f"{r['id']}: 共识位次 {cr} > floor {f_cr}")
        if "max_consensus" in r:
            f_c = c.get("expect", {}).get("consensus_min", 0)
            if r["max_consensus"] < f_c:
                bad.append(f"{r['id']}: 共识数 {r['max_consensus']} < {f_c}")
        for s in r.get("survive", []):
            if s.endswith("MISS"):
                bad.append(f"{r['id']}: 卡片/键未存活 {s}")
        for d in r.get("dims", []):
            bad.append(f"{r['id']}: 维度期望未满足 {d}")
        if "max_snippet_len" in r:
            f_len = c["expect"]["min_snippet_len"]
            if r["max_snippet_len"] < f_len:
                bad.append(f"{r['id']}: 最长摘要 {r['max_snippet_len']} < {f_len}")
    agg = golden_doc.get("_aggregate_floor") or {}
    if agg:
        if report["mean_mrr"] + 1e-9 < agg.get("mean_mrr", 0):
            bad.append(f"mean MRR {report['mean_mrr']} < {agg['mean_mrr']}")
        if report["mean_ndcg"] + 1e-9 < agg.get("mean_ndcg", 0):
            bad.append(f"mean nDCG {report['mean_ndcg']} < {agg['mean_ndcg']}")
        # RED/GREEN 消融地板：融合管线相对最强单引擎的聚合增益不得转负——
        # 排序/融合层的任何改动若把增益改没了，绝对地板可能仍达标（合成
        # 数据上限高），只有这条能抓到「调度层退化为摆设」的回归。
        f_edge = agg.get("fused_edge_ndcg_min")
        if f_edge is not None and report["mean_edge_ndcg"] + 1e-9 < f_edge:
            bad.append(f"mean 融合增益nDCG {report['mean_edge_ndcg']} "
                       f"< floor {f_edge}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="排序质量度量（金标合成数据）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--check", action="store_true",
                    help="门禁模式：任一 case 低于其 floor 则退出码 1")
    ap.add_argument("--case", help="只看某条 case 的明细")
    args = ap.parse_args()

    golden = {c["id"]: c for c in _load_golden()}
    report = evaluate_all()

    if args.case:
        c = golden.get(args.case)
        if not c:
            print(f"未知 case: {args.case}", file=sys.stderr)
            return 2
        r = next(x for x in report["cases"] if x["id"] == args.case)
        ranked = _run_pipeline(c)
        print(_fmt_row(r, c))
        for pos, item in enumerate(ranked, 1):
            dims = item.get("rerank_dims") or {}
            print(f"   #{pos} {item.get('url', '')[:60]}")
            print(f"      score={item.get('score')} dims={dims}")
        return 0

    if args.json:
        print(dumps(report))
        return 0

    print(f"排序金标评测：{report['n_cases']} 条 | "
          f"mean MRR@10={report['mean_mrr']:.4f} | "
          f"mean nDCG@10={report['mean_ndcg']:.4f}")
    for r in report["cases"]:
        print(_fmt_row(r, golden[r["id"]]))

    if args.check:
        golden_doc = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        bad = check_floors(report, golden_doc)
        if bad:
            print("\n".join(f"FAIL {b}" for b in bad), file=sys.stderr)
            return 1
        print("全部 case ≥ 金标 floor")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""eval_rank.py — local-seek 排序质量分级评测 + 计时基准（离线，零依赖）。

与 eval_seek.py 的分工：eval_seek 检查「有没有命中」（hit/no-hit 契约），
本文件回答「命中的**排序**对不对」——内容重排（BM25F）与文件名 fzf V2 的
改动前后用同一批用例对比，产出两类指标：

  质量  每个用例记录 top-N (path, line) 实况，并按期望文件集算
        topk_hit（期望文件进 top-K 的用例占比）与 MRR（倒数排名均值）。
  速度  固定查询集在固定语料上跑 run_query 的中位耗时（毫秒）。

期望值来自「实现所在文件」这一弱真值（改动前用 rg -l 逐一核实过），
不是人工标注的全序金标——它的用途是**守护不退步 + 观察位移**，
不是证明绝对质量。绝对质量请配合 eval_seek.py 的命中契约一起读。

用法：
  python3 eval_rank.py                        # 人读报告（stdout）
  python3 eval_rank.py --json out.json        # 同时写 JSON（供 A/B 对比）
  python3 eval_rank.py --compare a.json b.json  # 对比两份报告，给出逐用例位移
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import seek as s  # noqa: E402

# 语料根从本文件位置推导（scripts/ → local-seek/ → sub-skills/ → 仓根），
# 此前硬编码 ~/.agents/skills/argo：换机器/克隆路径不同就全 miss（MRR 全 0
# 却看不出是路径病）。SKILLS_ROOT 保留给「跨仓搜索」类用例的常见安装位，
# 不存在时相关用例自然 0 命中（诚实反映环境，不伪造）。
_ARGO_ROOT = Path(__file__).resolve().parents[3]
ARGO_ROOT = str(_ARGO_ROOT)
SKILLS_ROOT = str(_ARGO_ROOT.parent)

# ── 分级用例 ─────────────────────────────────────────────────────────────────
# expect：文件路径后缀集合，任一命中即算该用例 top 命中（MRR 取最高排名）。
#   期望文件在改动前经 rg -l 核实确实包含查询词。
CASES = [
    # 锚点用例（topk=5）：实现文件是该查询的明确最优，排名恶化即回归
    {"name": "文件级-噪声档实现", "argv": ["噪声档 降权", "--path", ARGO_ROOT],
     "expect": ["sub-skills/local-seek/scripts/seek.py"], "topk": 8},
    {"name": "文件级-pinyin实现", "argv": ["pinyin_initials 拼音", "--path", ARGO_ROOT],
     "expect": ["sub-skills/local-seek/scripts/seek.py"], "topk": 5},
    {"name": "文件级-rg_search定义", "argv": ["def rg_search", "--path", ARGO_ROOT],
     "expect": ["sub-skills/local-seek/scripts/seek.py"], "topk": 5},
    {"name": "文件级-cache键域隔离", "argv": ["cache_key_vdom", "--path", ARGO_ROOT],
     "expect": ["scripts/cache_key_vdom.py"], "topk": 5},
    {"name": "文件级-hash_token实现", "argv": ["def _hash_token", "--path", ARGO_ROOT],
     "expect": ["scripts/cache.py", "scripts/cache_similarity.py"], "topk": 5},
    # 守卫用例（topk=8）：行级语义下文档密集行同样合法，只守「实现文件仍在
    # top-8 内可被看见」，防实现被彻底挤出视野的回归
    {"name": "守卫-语义缓存minhash", "argv": ["语义缓存 minhash 软命中", "--path", ARGO_ROOT],
     "expect": ["scripts/cache.py", "scripts/search_rank.py"], "topk": 8},
    {"name": "守卫-BM25实现", "argv": ["BM25Okapi k1", "--path", SKILLS_ROOT],
     "expect": ["sub-skills/local-search/bm25.py"], "topk": 8},
    {"name": "守卫-中文聚合", "argv": ["本地搜索 聚合", "--path", SKILLS_ROOT],
     "expect": ["sub-skills/local-search/search_v3.py"], "topk": 8},
    {"name": "守卫-混排查询", "argv": ["性能 asyncio 教程", "--path", ARGO_ROOT],
     "expect": ["sub-skills"], "topk": 8},
    # 行级用例：期望「定义块」而非首次提及出现在 top 行
    {"name": "行级-NOISE_TIER定义", "argv": ["NOISE_TIER = [", "--path", ARGO_ROOT],
     "expect_line_range": ("sub-skills/local-seek/scripts/seek.py", 70, 130), "topk": 5},
    {"name": "行级-K1常数", "argv": ["_K1 = 1.5", "--path", SKILLS_ROOT],
     "expect_line_range": ("bm25.py", 40, 90),
     "expect_alt_line_range": ("seek_rank.py", 40, 90), "topk": 5},
]

# ── 计时用例（中位数，含 1 次预热不计入）─────────────────────────────────────
TIMING = [
    {"name": "中文短词", "argv": ["噪声档", "--path", ARGO_ROOT]},
    {"name": "英文高频词", "argv": ["import", "--path", ARGO_ROOT]},
    {"name": "多词混排", "argv": ["def run_query timeout", "--path", ARGO_ROOT]},
    {"name": "skills根宽查询", "argv": ["search", "--path", SKILLS_ROOT, "--dot"]},
    {"name": "文件名查询", "argv": ["seek", "--path", ARGO_ROOT, "--filename"]},
]

N_RUNS = 5
TOP_N = 8


def _run_case(argv: list[str]) -> tuple[list[dict], int]:
    """跑一次 run_query --json，返回 (results 原序, elapsed_ms)。

    自污染防护（与 eval_seek.py 同款）：用例词会命中本文件与 eval_seek.py
    的用例定义行，统一注入 --exclude 排除自身。
    """
    text, rc = s.run_query(argv + ["--exclude", "eval_rank.py",
                                   "--exclude", "eval_seek.py",
                                   "--json", "--max", str(TOP_N)], time_budget=10)
    try:
        payload = json.loads(text)
        return payload.get("results", []), int(payload.get("elapsed_ms", 0))
    except (json.JSONDecodeError, TypeError):
        return [], rc


def run_cases() -> dict:
    out = {}
    for case in CASES:
        results, _ = _run_case(case["argv"])
        top = [(r.get("path", ""), r.get("line", 0)) for r in results[:TOP_N]]
        entry: dict = {"top": top}
        if "expect" in case:
            rank = 0
            for i, (p, _ln) in enumerate(top, 1):
                if any(p.endswith(e) or e in p for e in case["expect"]):
                    rank = i
                    break
            entry["expect_rank"] = rank        # 0 = 未进 top-N
        elif "expect_line_range" in case:
            ranges = [case["expect_line_range"]]
            alt = case.get("expect_alt_line_range")
            if alt:
                ranges.append(alt)
            rank = 0
            for i, (p, ln) in enumerate(top, 1):
                if any(p.endswith(fp) and lo <= int(ln or 0) <= hi
                       for fp, lo, hi in ranges):
                    rank = i
                    break
            entry["expect_rank"] = rank
        out[case["name"]] = entry
    return out


def run_timing() -> dict:
    out = {}
    for case in TIMING:
        _run_case(case["argv"])                       # 预热（页缓存/解释器路径）
        samples = []
        for _ in range(N_RUNS):
            t0 = time.perf_counter()
            _run_case(case["argv"])
            samples.append((time.perf_counter() - t0) * 1000)
        out[case["name"]] = round(statistics.median(samples), 1)
    return out


def summarize(cases: dict) -> dict:
    """topk_hit 按每用例自己的 topk 门槛计（锚点 5 / 守卫 8）。"""
    by_name = {c["name"]: c for c in CASES}
    hits, total, mrr = 0, 0, 0.0
    for name, v in cases.items():
        rank = v.get("expect_rank", 0)
        topk = by_name.get(name, {}).get("topk", 5)
        total += 1
        if 0 < rank <= topk:
            hits += 1
        if rank:
            mrr += 1.0 / rank
    return {"topk_hit": hits, "n_cases": total, "MRR": round(mrr / total, 4) if total else 0.0}


def compare(a: dict, b: dict) -> None:
    """对比两份 JSON 报告：b 相对 a 的逐用例位移（负 = 排名变好）。"""
    ca, cb = a.get("cases", {}), b.get("cases", {})
    print(f"{'用例':24s} {'before':>8s} {'after':>8s}  位移")
    for name in ca:
        ra = ca[name].get("expect_rank", 0)
        rb = cb.get(name, {}).get("expect_rank", 0)
        delta = (rb - ra) if (ra and rb) else 0
        mark = "↑" if rb and (not ra or rb < ra) else ("↓" if rb and ra and rb > ra
                                                       else ("=" if rb == ra else "!"))
        print(f"{name:24s} {ra or '—':>8} {rb or '—':>8}  {mark} {delta:+d}")
    ta, tb = a.get("timing", {}), b.get("timing", {})
    print("\n计时（中位 ms，after / before，比值）：")
    for k in ta:
        va, vb = ta[k], tb.get(k, 0)
        ratio = f"{vb / va:.2f}x" if va else "—"
        print(f"  {k:16s} {vb:8.1f} / {va:8.1f}  ({ratio})")
    print(f"\n质量汇总 before: {summarize(ca)}")
    print(f"质量汇总 after : {summarize(cb)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="local-seek 排序质量分级评测")
    ap.add_argument("--json", metavar="PATH", help="同时写 JSON 报告")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"),
                    help="对比两份 JSON 报告后退出")
    args = ap.parse_args()
    if args.compare:
        a = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        b = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        compare(a, b)
        return 0
    cases = run_cases()
    timing = run_timing()
    report = {"cases": cases, "timing": timing, "summary": summarize(cases),
              "top_n": TOP_N, "n_runs": N_RUNS}
    print(f"质量汇总: {report['summary']}")
    for name, v in cases.items():
        rank = v.get("expect_rank", 0)
        print(f"  [{rank if rank else '—':>2}] {name}")
        for p, ln in v["top"][:5]:
            print(f"        {p}:{ln}")
    print("\n计时（中位 ms）:")
    for k, v in timing.items():
        print(f"  {k:16s} {v:8.1f}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 已写 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

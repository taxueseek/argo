#!/usr/bin/env python3
"""gen_routing_snapshot.py — 路由决策快照：生成与对账（--check 供测试调用）。

治的是慢性病：近 90 天 `fix(route)` 13 次——每次动域正则都可能碰翻另一条
查询的路由，而现有防线只有手工维护的 ~100 条标注用例（ROUTE_MATRIX +
precision 测试），没有「这次改动翻转了哪些查询」的总视图。本脚本把路由
决策变成可 diff 的快照：

  语料（三处标注集的并集，去重）：
    1. matrix_search_eval.ROUTE_MATRIX（59 条，带期望域）
    2. test_domain_pattern_precision.LEGITIMATE_ROUTES（41 条，期望域）
    3. 同文件 SUBSTRING_ACCIDENTS（~15 条，不得命中的域）
  每条记录 {query, domain, kind, expect, engines}。**比对键只有 domain 与
  kind**（regex/tfidf）：引擎组合随熔断/配额/健康态波动，参与比对会让门禁
  被环境噪声天天假红，直到被人 --no-verify——那比没有门禁更坏。

  生成（改完路由先跑它 review diff 再提交）：
    python3 scripts/gen_routing_snapshot.py
  对账（tests/test_routing_snapshot.py 调用；diff 即 exit 1）：
    python3 scripts/gen_routing_snapshot.py --check
  本地附加（不入快照、不对账）：把本机 usage_log 的 Top-N 真实查询过一遍
    路由，只看不锁——生产形态查询的漂移观察窗：
    python3 scripts/gen_routing_snapshot.py --local 50

与 gen_engine_catalog.py 同一模式：真源在 config.yaml 与 route，快照是投影，
过期由测试拦下。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# 与 tests/conftest.py 对齐：快照对账必须**确定性**。默认状态目录
# （~/.cache/unified-search）含本机历史（学习器分数 / 熔断器计数 / 配额），
# 会让「生成时的路由」与「pytest 里的路由」不一致——实测 2026-10-22
# 「settlement 结算 周期」在两种环境下分别是 regex 与 tfidf，门禁稳定假红
# （单独跑 --check 通过、pytest 里 3/3 红，根因就是两边状态目录不同）。
# 隔离到临时目录后两边都从空状态出发，逐条一致。调用方显式设置
# ARGO_STATE_DIR 时尊重之（便于复现某个具体状态）。
os.environ.setdefault("ARGO_STATE_DIR", tempfile.mkdtemp(prefix="argo-snapshot-"))

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
SNAPSHOT = TESTS / "golden" / "routing_snapshot.json"


def _collect_corpus() -> list[dict]:
    """三处标注集合并去重。期望域/禁入域作上下文记录，不参与对账。"""
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(TESTS))
    entries: dict[str, dict] = {}

    from matrix_search_eval import ROUTE_MATRIX
    for c in ROUTE_MATRIX:
        q = (c.get("q") or "").strip()
        if q:
            entries[q] = {"query": q, "expect": c.get("domain"),
                          "src": "ROUTE_MATRIX"}

    import test_domain_pattern_precision as precision
    for q, want in precision.LEGITIMATE_ROUTES:
        q = q.strip()
        if q:
            entries.setdefault(q, {"query": q, "expect": want,
                                   "src": "LEGITIMATE_ROUTES"})
    for q, forbidden in precision.SUBSTRING_ACCIDENTS:
        q = q.strip()
        if q:
            e = entries.get(q) or {"query": q, "src": "SUBSTRING_ACCIDENTS"}
            e["forbid"] = forbidden
            entries[q] = e
    return [entries[k] for k in sorted(entries)]


def _route_one(query: str) -> dict:
    from route import route_query
    d = route_query(query, mode="auto", depth="fast")
    reason = d.get("reason") or ""
    kind = "tfidf" if "TF-IDF" in reason else "regex"
    return {
        "domain": d.get("domain"),
        "kind": kind,
        "engines": (d.get("engines_combo") or [])[:2],
    }


def _build() -> list[dict]:
    out = []
    for e in _collect_corpus():
        r = _route_one(e["query"])
        out.append({"query": e["query"], **r,
                    "expect": e.get("expect"), "forbid": e.get("forbid"),
                    "src": e.get("src")})
    return out


def _load_snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def _diff(current: list[dict], recorded: list[dict]) -> list[str]:
    old = {e["query"]: e for e in recorded.get("entries", [])}
    problems = []
    for e in current:
        prev = old.get(e["query"])
        if prev is None:
            problems.append(f"  新增查询未入快照: {e['query']!r}")
            continue
        for key in ("domain", "kind"):
            if prev.get(key) != e.get(key):
                problems.append(
                    f"  {e['query']!r}: {key} {prev.get(key)!r} -> {e.get(key)!r}"
                    f"（期望域 {e.get('expect')!r}，来源 {e.get('src')}）")
    for q in sorted(set(old) - {e["query"] for e in current}):
        problems.append(f"  快照有而语料没有: {q!r}（语料被删？若是故意，重新生成）")
    # 期望域失配（标注集自己抓的那层，快照顺带报告）
    for e in current:
        if e.get("expect") and e["domain"] != e["expect"]:
            problems.append(f"  期望域失配: {e['query']!r} 期望 {e['expect']!r}"
                            f" 实际 {e['domain']!r}（precision 测试也应红）")
    return problems


def _local_report(n: int) -> None:
    """usage_log Top-N 真实查询的路由观察窗（不入快照，仅打印）。"""
    import collections
    import glob
    counts: collections.Counter = collections.Counter()
    for f in glob.glob(str(Path.home() / ".cache" / "unified-search"
                           / "usage_log" / "*.jsonl")):
        try:
            for line in open(f, encoding="utf-8"):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("stream") == "query" and d.get("query"):
                    counts[d["query"].strip()] += 1
        except OSError:
            continue
    top = counts.most_common(n)
    print(f"== usage_log Top-{len(top)} 真实查询路由（仅观察，不对账）==")
    for q, c in top:
        r = _route_one(q)
        print(f"  ×{c:<3} {r['kind']:<6} {r['domain']:<18} {q[:40]!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="路由决策快照生成/对账")
    ap.add_argument("--check", action="store_true",
                    help="对账模式：与已提交快照 diff，有漂移即 exit 1")
    ap.add_argument("--local", type=int, metavar="N", default=0,
                    help="附加：把本机 usage_log Top-N 查询过一遍路由（仅观察）")
    args = ap.parse_args()

    current = _build()
    if args.local:
        _local_report(args.local)
        return 0

    if args.check:
        if not SNAPSHOT.exists():
            print(f"快照不存在: {SNAPSHOT}\n先跑: python3 scripts/gen_routing_snapshot.py")
            return 1
        problems = _diff(current, _load_snapshot())
        if problems:
            print(f"路由快照漂移 {len(problems)} 处（改路由请重新生成并 review diff）：")
            print("\n".join(problems))
            print("\n重新生成: python3 scripts/gen_routing_snapshot.py")
            return 1
        print(f"路由快照对账通过（{len(current)} 条查询，domain+kind 零漂移）")
        return 0

    payload = {
        "version": 1,
        "generated_by": "scripts/gen_routing_snapshot.py",
        "compare_keys": ["domain", "kind"],
        "note": "engines 仅存档不比对（随熔断/配额波动）；expect/forbid 是语料"
                "自带的标注，失配由 precision/negative 测试与 --check 双报",
        "entries": current,
    }
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print(f"已写入 {SNAPSHOT}（{len(current)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

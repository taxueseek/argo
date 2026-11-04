#!/usr/bin/env python3
"""build_route_labels.py — 从本机真实运行数据构建路由回归语料（方案 A）。

## 为什么需要它

路由准确性的评测集此前只有三处手工维护的语料（ROUTE_MATRIX 135 +
LEGITIMATE_ROUTES 43 + SUBSTRING_ACCIDENTS 26 = 快照 201 条）加 13 条人工
标注集。13 条无法区分概念路由的净收益（实测 2 好 2 差），201 条快照只判
「domain+kind 不漂移」，不判「意图对不对」。

本机 `usage_log/query.jsonl` 里有 ~670 条去重真实查询，`badcase.db` 里有
259 条已归因坏例。本脚本把它们转成可入库的**回归语料**。

## 关键定位：回归门，不是准确率门

**弱标注的 `domain` 是历史路由的输出，不是真值。** 2026-11-04 实测：
31 条「路由输出 != 弱标注期望」的失配里，人工复核 20 条，**19 条是弱标注错、
路由对**（例：`Transformer 注意力机制 原理` 弱标注 academic，路由
chinese_tech_deep，而 13 条人工集判路由是对的）。

因此本脚本产出的语料用于：

1. **回归门**：判路由决策在真实查询上**不漂移**（同一条查询的路由输出
   应与语料记录的 `expect_domain` 一致——注意这是「与历史输出一致」，
   不是「与真值一致」）；
2. **分布视图**：看真实查询落在哪些域、哪些域从没被真实查询命中过；
3. **badcase 锚**：`badcase.db` 的坏例是已确认的失败，用于回归验证。

**不用于准确率**。准确率由 `test_route_accuracy_labeled`（13 条人工集）
和后续扩充的人工集承担。把弱标注当准确率会得出虚高或虚低的错误结论。

## 证据分档

| 档 | 来源 | 判据 | 用途 |
|---|---|---|---|
| A | `badcase.db` 的 query_error / empty_result / recovery | 真实失败，已归因 | badcase 锚（最高优先） |
| B | `usage_log` 里 errors 非空或 recovered=true | 有失败信号 | 回归 + 失败模式分析 |
| C | `usage_log` 里正常返回（无 errors、count>0） | 无失败信号 | 分布视图 + 回归 |

## 输出

`tests/golden/route_labeled.jsonl`，每行：

    {"query": ..., "expect_domain": ..., "expect_family": ...,
     "evidence": "badcase|usage_error|usage_ok",
     "quality": {...}, "source": "badcase.db|usage_log"}

**只入库查询与期望，不含本机路径、状态目录、时间戳**——语料是可公开的
评测资产，本机状态不是。

## 用法

    python3 scripts/build_route_labels.py            # 构建（写 jsonl + 打印统计）
    python3 scripts/build_route_labels.py --dry-run  # 只统计不写
    python3 scripts/build_route_labels.py --check    # 与已提交文件对账
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

OUTPUT = ROOT / "tests" / "golden" / "route_labeled.jsonl"

# usage_log 目录：与 badcase_ledger 同源（走 argo_paths，可被 ARGO_STATE_DIR 覆盖）
def _usage_log_dir() -> Path:
    import argo_paths
    return Path(argo_paths.state_path("usage_log"))


def _badcase_db() -> Path:
    import argo_paths
    return Path(argo_paths.state_path("badcase.db"))


# ── 读取本机数据 ────────────────────────────────────────────────────────────

def _read_usage_log() -> list[dict[str, Any]]:
    """读 usage_log/query.jsonl（可能多份轮转文件）。"""
    d = _usage_log_dir()
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(d.glob("query*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("stream") == "query" and (r.get("query") or "").strip():
                    out.append(r)
        except OSError:
            continue
    return out


def _read_badcases() -> list[dict[str, Any]]:
    """读 badcase.db（只取查询类坏例，breaker 无查询）。"""
    p = _badcase_db()
    if not p.is_file():
        return []
    try:
        con = sqlite3.connect(str(p), timeout=8)
        rows = list(con.execute(
            "SELECT kind, query, engine, detail FROM badcases "
            "WHERE kind IN ('query_error','empty_result','recovery') "
            "AND query <> ''"))
        con.close()
        return [{"kind": r[0], "query": r[1], "engine": r[2] or "",
                 "detail": r[3] or ""} for r in rows]
    except sqlite3.Error:
        return []


# ── 构建标注 ────────────────────────────────────────────────────────────────

def _family_of(engine: str) -> str | None:
    try:
        from engine_families import family_of
        return family_of(engine)
    except Exception:
        return None


def build() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """构建标注集，返回 (条目列表, 统计)。"""
    usage = _read_usage_log()
    badcases = _read_badcases()

    # usage_log 按查询聚合：同查询多次出现时，domain 是否稳定
    by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in usage:
        by_query[(r.get("query") or "").strip()].append(r)

    entries: dict[str, dict[str, Any]] = {}

    # ── A 档：badcase.db（最高优先，覆盖 usage_log 的同查询条目）──
    for b in badcases:
        q = (b.get("query") or "").strip()
        if not q:
            continue
        # badcase 记录的是「失败时的引擎」，期望域要从 usage_log 里找该查询的
        # domain；找不到就跳过（没有期望域的坏例无法当标注用）。
        dom = None
        for r in by_query.get(q, []):
            if r.get("domain"):
                dom = r["domain"]
                break
        if not dom:
            continue
        entries[q] = {
            "query": q,
            "expect_domain": dom,
            "expect_family": _family_of((b.get("engine") or "").split(",")[0]),
            "evidence": "badcase",
            "quality": {"errors": True, "recovered": b["kind"] == "recovery",
                        "count": 0, "occurrences": len(by_query.get(q, [])),
                        "badcase_kind": b["kind"]},
            "source": "badcase.db",
        }

    # ── B/C 档：usage_log ──
    for q, rows in by_query.items():
        if not q:
            continue
        domains = [r.get("domain") for r in rows if r.get("domain")]
        if not domains:
            continue
        # 稳定性：同查询多次出现时 domain 必须一致（否则是噪声，跳过）
        if len(set(domains)) > 1:
            continue
        dom = domains[0]
        has_error = any(r.get("errors") for r in rows)
        recovered = any(r.get("recovered") for r in rows)
        counts = [r.get("count") or 0 for r in rows]
        # C 档（无错误信号）要求至少出现 2 次或 count 中位数 > 0
        # ——单次出现的正常条目证据太弱
        evidence = "usage_error" if (has_error or recovered) else "usage_ok"
        if evidence == "usage_ok" and len(rows) < 2 and max(counts) == 0:
            continue
        # A/B 档已覆盖的查询不重复入（badcase 优先）
        if q in entries:
            continue
        # 期望主源族：取该查询出现最多的引擎的族
        eng_counter: Counter[str] = Counter()
        for r in rows:
            for e in (r.get("engines_used") or []):
                eng_counter[e] += 1
        top_eng = eng_counter.most_common(1)[0][0] if eng_counter else None
        entries[q] = {
            "query": q,
            "expect_domain": dom,
            "expect_family": _family_of(top_eng) if top_eng else None,
            "evidence": evidence,
            "quality": {"errors": has_error, "recovered": recovered,
                        "count": max(counts), "occurrences": len(rows)},
            "source": "usage_log",
        }

    out = [entries[k] for k in sorted(entries)]
    stats = {
        "total": len(out),
        "by_evidence": dict(Counter(e["evidence"] for e in out)),
        "by_source": dict(Counter(e["source"] for e in out)),
        "domains": len(set(e["expect_domain"] for e in out)),
        "usage_log_raw": len(usage),
        "usage_log_unique": len(by_query),
        "badcases": len(badcases),
    }
    return out, stats


def _write(entries: list[dict[str, Any]]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _check() -> int:
    if not OUTPUT.is_file():
        print(f"[build_route_labels] 标注集不存在：{OUTPUT}", file=sys.stderr)
        return 1
    recorded = {}
    for line in OUTPUT.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            recorded[d["query"]] = d
    entries, stats = build()
    current = {e["query"]: e for e in entries}
    problems = []
    for q in sorted(set(current) - set(recorded)):
        problems.append(f"  新增: {q!r}")
    for q in sorted(set(recorded) - set(current)):
        problems.append(f"  消失: {q!r}（本机数据已清理？）")
    for q in sorted(set(current) & set(recorded)):
        if current[q]["expect_domain"] != recorded[q]["expect_domain"]:
            problems.append(
                f"  期望域漂移: {q!r} {recorded[q]['expect_domain']!r}"
                f" -> {current[q]['expect_domain']!r}")
    if problems:
        print("标注集漂移：\n  " + "\n  ".join(problems[:40]), file=sys.stderr)
        return 1
    print(f"[build_route_labels] 一致（{len(recorded)} 条）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从本机真实运行数据构建路由标注集")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写")
    ap.add_argument("--check", action="store_true", help="与已提交文件对账")
    args = ap.parse_args()

    entries, stats = build()
    print(f"[build_route_labels] 原始 usage_log {stats['usage_log_raw']} 行 / "
          f"去重 {stats['usage_log_unique']} 条；badcase {stats['badcases']} 条")
    print(f"[build_route_labels] 标注集 {stats['total']} 条，"
          f"覆盖 {stats['domains']} 个域")
    print(f"[build_route_labels] 证据分档: {stats['by_evidence']}")
    print(f"[build_route_labels] 来源: {stats['by_source']}")

    if args.check:
        return _check()
    if args.dry_run:
        return 0
    _write(entries)
    print(f"[build_route_labels] 已写入 {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

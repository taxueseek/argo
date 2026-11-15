#!/usr/bin/env python3
"""route_telemetry_export.py — 本地路由遥测脱敏导出飞轮（零原文/零隐私泄露）。

只从本机状态目录读取 `usage_log/route.jsonl`、`usage_log/query.jsonl` 与
`adaptive.db`，输出严格聚合的脱敏指标，用于离线诊断与回归设计，严禁包含：
  - 原始查询文本（query / final_query）
  - 本机绝对路径、用户名或环境变量值
  - URL 凭据与特定实体标识

用法：
  python3 scripts/route_telemetry_export.py
  python3 scripts/route_telemetry_export.py --out tmp/telemetry_summary.json --min-samples 5
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import argo_paths as _paths


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def _adaptive_summary(db_path: Path, min_samples: int = 5) -> dict[str, Any]:
    """统计 adaptive.db 的空语言行占比与高空结果场景桶（无敏感文本）。"""
    out = {
        "total_rows": 0,
        "empty_lang_rows": 0,
        "high_empty_buckets": [],
    }
    if not db_path.is_file():
        return out
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(engine_perf)").fetchall()}
        if not cols:
            return out
        has_lang = "lang" in cols
        has_domain = "domain" in cols
        has_empty = "empty" in cols
        total = conn.execute("SELECT COUNT(*) FROM engine_perf").fetchone()[0]
        out["total_rows"] = int(total or 0)
        if has_lang:
            empty_lang = conn.execute(
                "SELECT COUNT(*) FROM engine_perf WHERE lang = '' OR lang IS NULL"
            ).fetchone()[0]
            out["empty_lang_rows"] = int(empty_lang or 0)
        if has_domain and has_lang and has_empty:
            rows = conn.execute(
                "SELECT engine, domain, lang, COUNT(*) AS n, SUM(empty) AS emp "
                "FROM engine_perf "
                "GROUP BY engine, domain, lang "
                "HAVING n >= ? "
                "ORDER BY CAST(emp AS REAL) / n DESC, n DESC "
                "LIMIT 20",
                (max(1, int(min_samples)),),
            ).fetchall()
            alerts = []
            for eng, dom, lang, n, emp in rows:
                emp_i = int(emp or 0)
                rate = round(emp_i / max(n, 1), 3)
                if rate >= 0.5:
                    alerts.append({
                        "engine": str(eng),
                        "domain": str(dom or "(none)"),
                        "effective_lang": str(lang or "(empty)"),
                        "samples": int(n),
                        "empty_count": emp_i,
                        "empty_rate": rate,
                    })
            out["high_empty_buckets"] = alerts
    except sqlite3.Error:
        return out
    finally:
        if conn is not None:
            conn.close()
    return out


def export_anonymized_telemetry(
    state_dir: Path | str | None = None,
    *,
    min_samples: int = 5,
) -> dict[str, Any]:
    """导出严格脱敏的路由健康度与分桶统计报告（不含任何 query 或本机路径）。"""
    root = Path(state_dir) if state_dir else _paths.state_root()
    usage_override = os.environ.get("ARGO_USAGE_LOG_DIR", "").strip()
    log_dir = Path(os.path.expanduser(usage_override)) if (usage_override and not state_dir) else (root / "usage_log")

    route_rows = _iter_jsonl(log_dir / "route.jsonl")
    query_rows = _iter_jsonl(log_dir / "query.jsonl")

    lang_counts: collections.Counter[str] = collections.Counter()
    domain_counts: collections.Counter[str] = collections.Counter()
    intent_counts: collections.Counter[str] = collections.Counter()
    skeleton_counts: collections.Counter[tuple[str, str, tuple[str, ...], str]] = collections.Counter()
    fallback_count = 0

    for row in route_rows:
        dom = str(row.get("domain") or "general_search")
        if dom in ("general_search", "fallback", ""):
            fallback_count += 1
        eff_lang = str(row.get("effective_lang") or row.get("lang_override") or row.get("primary_lang") or "unknown")
        intents = tuple(sorted(str(x) for x in (row.get("intents") or []) if x))
        eng = str(row.get("engine") or "unknown")
        lang_counts[eff_lang] += 1
        domain_counts[dom] += 1
        for it in intents:
            intent_counts[it] += 1
        skeleton_counts[(dom, eff_lang, intents, eng)] += 1

    total_route = len(route_rows)
    zero_count_queries = sum(1 for r in query_rows if int(r.get("count") or 0) == 0)

    skeletons = []
    for (dom, lang, intents, eng), cnt in skeleton_counts.most_common(25):
        sig_src = f"{dom}|{lang}|{','.join(intents)}|{eng}"
        skel_id = hashlib.sha256(sig_src.encode("utf-8")).hexdigest()[:12]
        skeletons.append({
            "skeleton_id": skel_id,
            "domain": dom,
            "effective_lang": lang,
            "intents": list(intents),
            "engine": eng,
            "count": cnt,
        })

    return {
        "schema_version": 1,
        "privacy_guarantee": "aggregated_no_raw_query_no_paths",
        "summary": {
            "route_events": total_route,
            "query_events": len(query_rows),
            "query_zero_result_rate": round(zero_count_queries / len(query_rows), 3) if query_rows else 0.0,
            "route_fallback_rate": round(fallback_count / total_route, 3) if total_route else 0.0,
            "adaptive": _adaptive_summary(root / "adaptive.db", min_samples=min_samples),
        },
        "lang_distribution": dict(lang_counts.most_common()),
        "domain_distribution": dict(domain_counts.most_common(20)),
        "intent_distribution": dict(intent_counts.most_common()),
        "anonymized_fingerprints": skeletons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导出脱敏后的 Argo 路由遥测聚合报告")
    parser.add_argument("--state-dir", default="", help="指定状态目录（默认自动发现）")
    parser.add_argument("--min-samples", type=int, default=5, help="高空结果桶最小样本数")
    parser.add_argument("--out", default="", help="可选输出 JSON 文件路径")
    args = parser.parse_args()

    payload = export_anonymized_telemetry(
        args.state_dir or None,
        min_samples=args.min_samples,
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"已写入脱敏遥测摘要: {out_path}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

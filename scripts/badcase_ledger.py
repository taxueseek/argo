#!/usr/bin/env python3
"""badcase_ledger — 坏例账本：把分散三处的失败统一入账、可查、可做发布门禁。

定位（2026-10-11 方案丙）：argo 的失败证据分散在三处、形态各异——
  1. usage_log/query.jsonl：errors[] 非空或 count=0 的真实查询（含 serp_guard
     整页误杀外露，见 search.py drain 处）；
  2. usage_log/recovery.jsonl：触发过恢复链的查询（triggered=true）；
  3. circuit_breaker.json：state != closed 的引擎（点状快照，引擎级坏例）。
本工具把它们归一进 SQLite 账本（<state>/badcase.db），支持 ingest（幂等去重）、
list、stats 与 check（发布前门禁：近 N 天新增坏例超阈值即非零退出）。

与 replay_eval.py 的分工：replay_eval 用录制重放整条管线评打分质量（离线
仲裁）；本账本只收纳「真实发生的失败」并让它们可归因、可回归——对应评测
体系的 badcase 管理环节：发现 → 入账 → 归因 → 修复后 check 不复发。

数据卫生：engines_used 含表外引擎名（夹具/拼写错）的记录不入账——账本只收
真实坏例，与 usage_log_clean 的隔离口径同源（engines.available_engines）。

用法：
  python3 scripts/badcase_ledger.py ingest            # 三源归一入账（幂等）
  python3 scripts/badcase_ledger.py list --kind query_error --limit 10
  python3 scripts/badcase_ledger.py stats             # 按 kind/引擎聚合
  python3 scripts/badcase_ledger.py check --since 7d --max 50   # 发布门禁
"""
from __future__ import annotations

import engine_states  # 状态词表唯一真源（2026-11-09 收口）

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argo_paths  # noqa: E402
from engines import available_engines  # noqa: E402
from usage_log import usage_log_dir  # noqa: E402

_CANON = frozenset(available_engines())

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS badcases ("
    " id INTEGER PRIMARY KEY,"
    " kind TEXT NOT NULL,"        # query_error | empty_result | recovery | breaker
    " ts TEXT NOT NULL,"          # 原始 ISO 时间串；breaker 快照用入账时刻
    " query TEXT NOT NULL DEFAULT '',"
    " engine TEXT NOT NULL DEFAULT '',"
    " detail TEXT NOT NULL DEFAULT '',"
    " fingerprint TEXT NOT NULL UNIQUE)"
)


def db_path() -> Path:
    return argo_paths.state_path("badcase.db")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path()), timeout=8)
    con.execute(_SCHEMA)
    return con


def _rows_from_query_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_used = r.get("engines_used") or []
        used = [e for e in raw_used if e in _CANON]
        if not used and raw_used:
            continue  # 表外名占满 → 夹具行，不入账（口径同 usage_log_clean）
        raw_errors = r.get("errors")
        # 生产日志里 errors 有两种形态：字符串列表（主流）与 int 计数（部分
        # 旧条目）——统一成字符串列表再取前几条
        errors = (raw_errors if isinstance(raw_errors, list)
                  else ([str(raw_errors)] if raw_errors else []))
        if not errors and r.get("count"):
            continue  # 正常条目不是坏例
        if errors:
            out.append({"kind": "query_error", "ts": r.get("ts", ""),
                        "query": r.get("query", ""),
                        "engine": ",".join(used[:3]),
                        "detail": " | ".join(str(e)[:160] for e in errors[:3])})
        else:
            out.append({"kind": "empty_result", "ts": r.get("ts", ""),
                        "query": r.get("query", ""),
                        "engine": ",".join(used[:3]),
                        "detail": f"count=0, mode={r.get('mode')}"})
    return out


def _rows_from_recovery_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("triggered"):
            continue
        out.append({"kind": "recovery", "ts": r.get("ts", ""),
                    "query": r.get("query", ""),
                    "engine": "",
                    "detail": (f"level={r.get('level_used')} "
                               f"strategy={r.get('strategy_used')} "
                               f"recovered={r.get('recovered')}")})
    return out


def _rows_from_breaker(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        engines = json.loads(path.read_text(encoding="utf-8")).get("engines", {})
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for name, st in sorted(engines.items()):
        if st.get("state") == engine_states.BREAKER_CLOSED:
            continue
        out.append({"kind": "breaker", "ts": now, "query": "", "engine": name,
                    "detail": (f"state={st.get('state')} "
                               f"failures={st.get('failures')} "
                               f"opens={st.get('opens')}")})
    return out


def ingest() -> dict:
    log_dir = usage_log_dir()
    rows = (_rows_from_query_log(log_dir / "query.jsonl")
            + _rows_from_recovery_log(log_dir / "recovery.jsonl")
            + _rows_from_breaker(argo_paths.state_path("circuit_breaker.json")))
    con = _connect()
    added = 0
    try:
        for r in rows:
            fp = hashlib.sha1("|".join(
                [str(r["kind"]), str(r["ts"]), str(r["query"]),
                 str(r["engine"]), str(r["detail"])[:120]])
                .encode("utf-8")).hexdigest()
            cur = con.execute(
                "INSERT OR IGNORE INTO badcases(ts, query, engine, detail,"
                " kind, fingerprint) VALUES (?,?,?,?,?,?)",
                (r["ts"], r["query"], r["engine"], r["detail"],
                 r["kind"], fp))
            added += cur.rowcount
        total = con.execute("SELECT COUNT(*) FROM badcases").fetchone()[0]
        con.commit()
    finally:
        con.close()
    return {"sources": len(rows), "added": added, "total": total,
            "db": str(db_path())}


def _parse_since(since: str) -> datetime:
    """7d / 24h / 2026-10-01 → datetime（本地时区）。"""
    since = since.strip().lower()
    now = datetime.now().astimezone()
    if since.endswith("d") and since[:-1].isdigit():
        return now - timedelta(days=int(since[:-1]))
    if since.endswith("h") and since[:-1].isdigit():
        return now - timedelta(hours=int(since[:-1]))
    return datetime.fromisoformat(since).astimezone()


def _fetch(kind: str | None, since: datetime | None,
           limit: int) -> list[dict[str, Any]]:
    con = _connect()
    try:
        sql = ("SELECT ts, kind, query, engine, detail FROM badcases"
               " WHERE 1=1")
        params: list = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since.isoformat(timespec="seconds"))
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        keys = ["ts", "kind", "query", "engine", "detail"]
        return [dict(zip(keys, row)) for row in con.execute(sql, params)]
    finally:
        con.close()


def check(since: str, max_new: int) -> dict:
    rows = _fetch(None, _parse_since(since), limit=10_000)
    ok = len(rows) <= max_new
    return {"since": since, "new_badcases": len(rows), "max": max_new,
            "ok": ok}


def stats() -> dict:
    con = _connect()
    try:
        by_kind = dict(con.execute(
            "SELECT kind, COUNT(*) FROM badcases GROUP BY kind").fetchall())
        by_engine = dict(con.execute(
            "SELECT engine, COUNT(*) FROM badcases WHERE engine != ''"
            " GROUP BY engine ORDER BY COUNT(*) DESC LIMIT 8").fetchall())
        total = con.execute("SELECT COUNT(*) FROM badcases").fetchone()[0]
    finally:
        con.close()
    return {"total": total, "by_kind": by_kind, "top_engines": by_engine}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="坏例账本（badcase 管理）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    p_list = sub.add_parser("list")
    p_list.add_argument("--kind", default=None,
                        choices=["query_error", "empty_result",
                                 "recovery", "breaker"])
    p_list.add_argument("--limit", type=int, default=10)
    sub.add_parser("stats")
    p_check = sub.add_parser("check")
    p_check.add_argument("--since", default="7d")
    p_check.add_argument("--max", type=int, default=50)
    args = ap.parse_args(argv)
    if args.cmd == "ingest":
        print(json.dumps(ingest(), ensure_ascii=False))
    elif args.cmd == "list":
        for row in _fetch(args.kind, None, args.limit):
            print(json.dumps(row, ensure_ascii=False))
    elif args.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False))
    elif args.cmd == "check":
        report = check(args.since, args.max)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

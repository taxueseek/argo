#!/usr/bin/env python3
"""badcase_ledger — 坏例账本：把分散三处的失败统一入账、可查、可归因、可做发布门禁。

定位（2026-10-11 方案丙）：argo 的失败证据分散在三处、形态各异——
  1. usage_log/query.jsonl：errors[] 非空或 count=0 的真实查询（含 serp_guard
     整页误杀外露，见 search.py drain 处）；
  2. usage_log/recovery.jsonl：触发过恢复链的查询（triggered=true）；
  3. circuit_breaker.json：state != closed 的引擎（点状快照，引擎级坏例）。
本工具把它们归一进 SQLite 账本（<state>/badcase.db），支持 ingest（幂等去重）、
list、stats、check（发布前门禁：近 N 天新增坏例超阈值即非零退出）与
lessons（把反复出现的根因导成 LESSONS.md 格式的清单）。

与 replay_eval.py 的分工：replay_eval 用录制重放整条管线评打分质量（离线
仲裁）；本账本只收纳「真实发生的失败」并让它们可归因、可回归——对应评测
体系的 badcase 管理环节：发现 → 入账 → 归因 → 修复后 check 不复发。

## 2026-11-10 补的归因层：root_cause + Seen N×

原实现只到「发现 → 入账」：`check` 数近 N 天新增坏例的**条数**，不看**根因
是否反复出现**。同一类根因连续 7 天各出 3 条，与 7 天共出 3 条，在这道门
眼里完全一样。而「同一类问题反复出现」才是该升级成硬规则的信号——这正是
外部实践（magpie 的 LESSONS.md）里最值钱的一条：**同一条教训在 3 个不同
日子的复盘里都出现，就从错题本挪进代码规范**。

补的三件：
  1. 每条坏例入账时按证据判一个 `root_cause`（词表见 scripts/badcase_causes.py），
     判不出就 `unattributed`，不硬猜；
  2. 账本记录每条坏例的**首次/末次出现日期**，同一根因聚合出 `seen_days`
     （在多少个不同日期出现过）；
  3. `check` 增加一条判定：**同一 root_cause 的 seen_days ≥ SEEN_PROMOTE_DAYS
     且该根因属于「argo 自己的问题」→ 非零退出**，并在报告里点名「这条该从
     错题本挪进硬规则了」。

`external_ranking`（活网排名漂移）被排除在升级判定之外：它不是 argo 的
问题，算成复发会制造长期噪音（2026-10-28 issue #35 的误报根因，见
scripts/ci_drift_gate.py 文件头）。

数据卫生：engines_used 含表外引擎名（夹具/拼写错）的记录不入账——账本只收
真实坏例，与 usage_log_clean 的隔离口径同源（engines.available_engines）。

用法：
  python3 scripts/badcase_ledger.py ingest            # 三源归一入账（幂等）
  python3 scripts/badcase_ledger.py list --kind query_error --limit 10
  python3 scripts/badcase_ledger.py stats             # 按 kind/引擎/根因聚合
  python3 scripts/badcase_ledger.py check --since 7d --max 50   # 发布门禁
  python3 scripts/badcase_ledger.py check --promote-days 3       # Seen N× 门
  python3 scripts/badcase_ledger.py lessons [--out LESSONS.md]    # 导出错题本
"""
from __future__ import annotations

import engine_states  # 状态词表唯一真源（2026-11-09 收口）

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argo_paths  # noqa: E402
import badcase_causes  # 坏例根因词表（第 27 台状态机，2026-11-10 拆出）
from engines import available_engines  # noqa: E402
from usage_log import usage_log_dir  # noqa: E402

# 根因常量从 badcase_causes 取（词表单一真源），此处只做别名绑定，
# 让本模块的其余代码与测试仍写 ROOT_CAUSE_* 的等价形态。
ROOT_CAUSE_ROUTE = badcase_causes.ROOT_CAUSE_ROUTE
ROOT_CAUSE_PARSE_EMPTY = badcase_causes.ROOT_CAUSE_PARSE_EMPTY
ROOT_CAUSE_AUTH = badcase_causes.ROOT_CAUSE_AUTH
ROOT_CAUSE_QUOTA = badcase_causes.ROOT_CAUSE_QUOTA
ROOT_CAUSE_RATE_LIMITED = badcase_causes.ROOT_CAUSE_RATE_LIMITED
ROOT_CAUSE_BLOCKED = badcase_causes.ROOT_CAUSE_BLOCKED
ROOT_CAUSE_UPSTREAM = badcase_causes.ROOT_CAUSE_UPSTREAM
ROOT_CAUSE_NETWORK = badcase_causes.ROOT_CAUSE_NETWORK
ROOT_CAUSE_RECOVERY = badcase_causes.ROOT_CAUSE_RECOVERY
ROOT_CAUSE_BREAKER = badcase_causes.ROOT_CAUSE_BREAKER
ROOT_CAUSE_EXTERNAL = badcase_causes.ROOT_CAUSE_EXTERNAL
ROOT_CAUSE_UNKNOWN = badcase_causes.ROOT_CAUSE_UNKNOWN
ROOT_CAUSES = badcase_causes.ROOT_CAUSES
OWN_ROOT_CAUSES = badcase_causes.OWN_ROOT_CAUSES

_CANON = frozenset(available_engines())

# Seen N× 升级阈值：同一根因在多少个**不同日期**出现就该挪进硬规则。
# 3 是「事不过三」——一次是偶然，两次可能是巧合，三次是规律。
SEEN_PROMOTE_DAYS = 3

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS badcases ("
    " id INTEGER PRIMARY KEY,"
    " kind TEXT NOT NULL,"        # query_error | empty_result | recovery | breaker
    " ts TEXT NOT NULL,"          # 原始 ISO 时间串；breaker 快照用入账时刻
    " query TEXT NOT NULL DEFAULT '',"
    " engine TEXT NOT NULL DEFAULT '',"
    " detail TEXT NOT NULL DEFAULT '',"
    " root_cause TEXT NOT NULL DEFAULT '',"   # ROOT_CAUSE_*
    " fingerprint TEXT NOT NULL UNIQUE)"
)

# 老库（2026-11-10 之前建的）没有 root_cause 列：补列而不是重建表——
# 账本里已有的坏例是历史证据，重建等于把它们丢掉。
_MIGRATIONS = (
    ("ALTER TABLE badcases ADD COLUMN root_cause TEXT NOT NULL DEFAULT ''",
     "root_cause"),
)


def db_path() -> Path:
    return argo_paths.state_path("badcase.db")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path()), timeout=8)
    con.execute(_SCHEMA)
    cols = {row[1] for row in con.execute("PRAGMA table_info(badcases)")}
    for stmt, need in _MIGRATIONS:
        if need not in cols:
            con.execute(stmt)
    return con


# ── 根因判定 ────────────────────────────────────────────────────────────────
# 判据只用坏例自带的证据（kind / engine / detail），不联网、不读别的库。
# 顺序即裁决：更具体的信号先于更泛的信号。

# detail 里的关键词 → 根因。顺序即裁决，先命中先算。
_DETAIL_RULES: tuple[tuple[str, str], ...] = (
    # ── failure_kinds 词表（search_attribution 写入，当前日志的语义主来源）──
    # 顺序即优先级：先判更具体的类别，避免 "unknown" 这类泛词抢判。
    ("missing-key", ROOT_CAUSE_AUTH),        # 未配置密钥 → 认证类
    ("failure_kinds=auth", ROOT_CAUSE_AUTH),
    ("failure_kinds=quota", ROOT_CAUSE_QUOTA),
    ("failure_kinds=rate-limit", ROOT_CAUSE_RATE_LIMITED),
    ("failure_kinds=cooldown", ROOT_CAUSE_RATE_LIMITED),  # 冷却=限流的同族
    ("failure_kinds=bot-wall", ROOT_CAUSE_BLOCKED),
    ("failure_kinds=transient", ROOT_CAUSE_NETWORK),      # 瞬时故障=网络层
    ("failure_kinds=unknown", ROOT_CAUSE_UNKNOWN),
    # ── 文本错误信息（早期日志的 errors 是字符串列表时）──
    # 认证（401/login required/cookie）先于 blocked —— 带 auth 语义的 403
    # 不得被误判成被封锁，与 engine_failure 的裁决顺序一致。
    ("login required", ROOT_CAUSE_AUTH),
    ("401", ROOT_CAUSE_AUTH),
    ("unauthorized", ROOT_CAUSE_AUTH),
    ("cookie", ROOT_CAUSE_AUTH),
    ("auth", ROOT_CAUSE_AUTH),
    ("402", ROOT_CAUSE_QUOTA),
    ("quota", ROOT_CAUSE_QUOTA),
    ("insufficient", ROOT_CAUSE_QUOTA),
    ("balance", ROOT_CAUSE_QUOTA),
    ("429", ROOT_CAUSE_RATE_LIMITED),
    ("rate limit", ROOT_CAUSE_RATE_LIMITED),
    ("retry-after", ROOT_CAUSE_RATE_LIMITED),
    # 上游改版：404 / 结构失配 / 解析器过期
    ("404", ROOT_CAUSE_UPSTREAM),
    ("no longer match", ROOT_CAUSE_UPSTREAM),
    ("parse", ROOT_CAUSE_PARSE_EMPTY),
    ("schema", ROOT_CAUSE_PARSE_EMPTY),
    # 网络
    ("timeout", ROOT_CAUSE_NETWORK),
    ("timed out", ROOT_CAUSE_NETWORK),
    ("dns", ROOT_CAUSE_NETWORK),
    ("connection", ROOT_CAUSE_NETWORK),
    ("ssl", ROOT_CAUSE_NETWORK),
    # 源站拦截
    ("captcha", ROOT_CAUSE_BLOCKED),
    ("waf", ROOT_CAUSE_BLOCKED),
    ("anti-bot", ROOT_CAUSE_BLOCKED),
    ("blocked", ROOT_CAUSE_BLOCKED),
    ("403", ROOT_CAUSE_BLOCKED),
    ("451", ROOT_CAUSE_BLOCKED),
    # 依赖缺失
    ("not found: ", ROOT_CAUSE_ROUTE),  # command not found
    ("no such file", ROOT_CAUSE_ROUTE),
    ("command not found", ROOT_CAUSE_ROUTE),
    ("not installed", ROOT_CAUSE_ROUTE),
    ("missing dependency", ROOT_CAUSE_ROUTE),
)

# kind 兜底：detail 没给出线索时按坏例类型给一个粗分类。
_KIND_CAUSE: dict[str, str] = {
    "recovery": ROOT_CAUSE_RECOVERY,
    "breaker": ROOT_CAUSE_BREAKER,
}


def classify_root_cause(kind: str, detail: str, query: str = "",
                        engine: str = "") -> str:
    """按证据把一条坏例归到某个 root_cause（判不出返回 unattributed）。

    纯函数，不读 IO。返回值恒为 badcase_causes.ROOT_CAUSES 的成员。
    """
    text = f"{detail} {query} {engine}".lower()
    for needle, cause in _DETAIL_RULES:
        if needle in text:
            return cause
    # 外部排名漂移：活网结果换血，不是 argo 的问题。放最后判——它的证据
    # （ranking/drift 字样）比前面几条泛，先判会误伤真正的上游改版。
    if "ranking drift" in text or "results drift" in text:
        return ROOT_CAUSE_EXTERNAL
    return _KIND_CAUSE.get(kind, ROOT_CAUSE_UNKNOWN)


# ── 三源读取 ────────────────────────────────────────────────────────────────

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
        # 生产日志里 errors 有两种形态：字符串列表（早期）与 int 计数（现在
        # 主流）——后者不含错误文本，归因规则表匹配不到任何关键词，259 条坏例
        # 的 root_cause 全空（2026-11-11 实测）。真正的语义类别在 failure_kinds
        # 字段（search_attribution 写入：quota / rate-limit / auth / bot-wall…），
        # 故 detail 优先拼 failure_kinds，让归因有据可依。
        raw_kinds = r.get("failure_kinds") or []
        kinds = [str(k) for k in raw_kinds if k] if isinstance(raw_kinds, list) else []
        errors = (raw_errors if isinstance(raw_errors, list)
                  else ([str(raw_errors)] if raw_errors else []))
        if not errors and not kinds and r.get("count"):
            continue  # 正常条目不是坏例
        if errors or kinds:
            parts = [f"failure_kinds={','.join(kinds)}"] if kinds else []
            parts += [str(e)[:160] for e in errors[:3]]
            out.append({"kind": "query_error", "ts": r.get("ts", ""),
                        "query": r.get("query", ""),
                        "engine": ",".join(used[:3]),
                        "detail": " | ".join(parts)})
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
            cause = classify_root_cause(r["kind"], r["detail"],
                                        r["query"], r["engine"])
            fp = hashlib.sha1("|".join(
                [str(r["kind"]), str(r["ts"]), str(r["query"]),
                 str(r["engine"]), str(r["detail"])[:120]])
                .encode("utf-8")).hexdigest()
            cur = con.execute(
                "INSERT OR IGNORE INTO badcases(ts, query, engine, detail,"
                " kind, root_cause, fingerprint) VALUES (?,?,?,?,?,?,?)",
                (r["ts"], r["query"], r["engine"], r["detail"],
                 r["kind"], cause, fp))
            added += cur.rowcount
        total = con.execute("SELECT COUNT(*) FROM badcases").fetchone()[0]
        con.commit()
    finally:
        con.close()
    return {"sources": len(rows), "added": added, "total": total,
            "db": str(db_path())}


# ── 时间解析 ────────────────────────────────────────────────────────────────

def _day_of(ts: str) -> str:
    """取 ts 的日期部分（YYYY-MM-DD）；解析不了返回空串。"""
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", ts)
        return m.group(1) if m else ""


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
        sql = ("SELECT ts, kind, query, engine, detail, root_cause FROM badcases"
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
        keys = ["ts", "kind", "query", "engine", "detail", "root_cause"]
        return [dict(zip(keys, row)) for row in con.execute(sql, params)]
    finally:
        con.close()


# ── Seen N× 聚合 ────────────────────────────────────────────────────────────

def root_cause_stats() -> list[dict[str, Any]]:
    """按 root_cause 聚合：出现次数、涉及引擎数、**不同日期数**、首末日期。

    `seen_days` 是升级判定的依据：同一条教训在 3 个不同日子的复盘里都出现，
    就该从错题本挪进硬规则。跨天的重复才是真正的「复发」，同一天内的 10 条
    只是同一件事的余波。
    """
    con = _connect()
    try:
        rows = con.execute(
            "SELECT root_cause, ts, engine FROM badcases").fetchall()
    finally:
        con.close()
    agg: dict[str, dict[str, Any]] = {}
    for cause, ts, engine in rows:
        day = _day_of(ts)
        a = agg.setdefault(cause, {
            "root_cause": cause, "count": 0, "days": set(),
            "engines": set(), "first_seen": day, "last_seen": day,
        })
        a["count"] += 1
        if day:
            a["days"].add(day)
            a["first_seen"] = min(a["first_seen"] or day, day)
            a["last_seen"] = max(a["last_seen"] or day, day)
        if engine:
            a["engines"].update(e for e in engine.split(",") if e)
    out = []
    for a in agg.values():
        days = sorted(a.pop("days"))
        a["days"] = days
        a["seen_days"] = len(days)
        a["engines"] = sorted(a["engines"])
        out.append(a)
    out.sort(key=lambda x: (-x["seen_days"], -x["count"], x["root_cause"]))
    return out


def promotion_candidates(promote_days: int = SEEN_PROMOTE_DAYS) -> list[dict]:
    """seen_days ≥ 阈值、且属于「argo 自己的问题」的根因——该挪进硬规则了。"""
    out = []
    for a in root_cause_stats():
        if a["root_cause"] == ROOT_CAUSE_EXTERNAL:
            continue  # 活网漂移不是我们的问题，不参与升级判定
        if a["seen_days"] >= promote_days:
            out.append(a)
    return out


# ── 门禁 ────────────────────────────────────────────────────────────────────

def check(since: str, max_new: int,
          promote_days: int = SEEN_PROMOTE_DAYS) -> dict:
    rows = _fetch(None, _parse_since(since), limit=10_000)
    volume_ok = len(rows) <= max_new
    candidates = promotion_candidates(promote_days)
    # 复发判定与条数判定是两条独立的门：条数门拦「量」，复发门拦「同一类问题
    # 反复出现」。任一不过即非零退出，但报告里分开列，避免混淆根因。
    return {
        "since": since, "new_badcases": len(rows), "max": max_new,
        "volume_ok": volume_ok,
        "promote_days": promote_days,
        "recurrence": candidates,
        "recurrence_ok": not candidates,
        "ok": volume_ok and not candidates,
    }


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
    return {"total": total, "by_kind": by_kind, "top_engines": by_engine,
            "by_root_cause": root_cause_stats()}


# ── LESSONS.md 导出 ─────────────────────────────────────────────────────────

_CAUSE_LABEL = {
    ROOT_CAUSE_ROUTE: "路由选错源",
    ROOT_CAUSE_PARSE_EMPTY: "解析层零交付",
    ROOT_CAUSE_AUTH: "认证失效",
    ROOT_CAUSE_QUOTA: "配额耗尽",
    ROOT_CAUSE_RATE_LIMITED: "限流",
    ROOT_CAUSE_BLOCKED: "源站拦截",
    ROOT_CAUSE_UPSTREAM: "上游改版",
    ROOT_CAUSE_NETWORK: "网络",
    ROOT_CAUSE_RECOVERY: "恢复链未兜住",
    ROOT_CAUSE_BREAKER: "引擎被停用",
    ROOT_CAUSE_EXTERNAL: "外部排名漂移",
    ROOT_CAUSE_UNKNOWN: "未归因",
}


def render_lessons() -> str:
    """把账本导成 LESSONS.md 格式的清单（规则一句话 + 证据 + Seen N×）。"""
    stats_ = root_cause_stats()
    lines = [
        "# Argo 错题本（badcase LESSONS）",
        "",
        "> 由 `scripts/badcase_ledger.py lessons` 从坏例账本自动生成，**不要手改**。",
        "> 每条含根因、证据（条数/引擎/日期）与 `Seen N×`。",
        f"> 同一根因在 {SEEN_PROMOTE_DAYS} 个不同日期出现 → 该挪进硬规则。",
        "",
        f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
    ]
    if not stats_:
        lines += ["_账本为空：本地使用日志里还没有坏例。_", ""]
        return "\n".join(lines)
    lines += ["## 按复发天数排序", ""]
    for a in stats_:
        label = _CAUSE_LABEL.get(a["root_cause"], a["root_cause"])
        days = "、".join(sorted(a["days"] if "days" in a else [])) if "days" in a else ""
        engines = "、".join(a["engines"][:5]) or "（未记录）"
        mark = "**该挪进硬规则**" if (a["seen_days"] >= SEEN_PROMOTE_DAYS
                                    and a["root_cause"] != ROOT_CAUSE_EXTERNAL) else ""
        lines.append(
            f"### {label}（`{a['root_cause']}`）{mark}")
        lines.append("")
        lines.append(f"- 出现 {a['count']} 条，涉及引擎：{engines}")
        lines.append(f"- 首次 {a['first_seen'] or '未知'}，末次 {a['last_seen'] or '未知'}")
        lines.append(f"- Seen {a['seen_days']}×（{a['first_seen'] or '?'} → {a['last_seen'] or '?'}）")
        lines.append("")
    return "\n".join(lines)


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
    p_check.add_argument("--promote-days", type=int, default=SEEN_PROMOTE_DAYS,
                         help="Seen N× 升级阈值（默认 3，即事不过三）")
    p_lessons = sub.add_parser("lessons")
    p_lessons.add_argument("--out", default=None,
                           help="写出到文件（默认打印到 stdout）")
    args = ap.parse_args(argv)
    if args.cmd == "ingest":
        print(json.dumps(ingest(), ensure_ascii=False))
    elif args.cmd == "list":
        for row in _fetch(args.kind, None, args.limit):
            print(json.dumps(row, ensure_ascii=False))
    elif args.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False))
    elif args.cmd == "check":
        report = check(args.since, args.max, args.promote_days)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    elif args.cmd == "lessons":
        text = render_lessons()
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"已写出 {args.out}（{text.count(chr(10))} 行）")
        else:
            print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

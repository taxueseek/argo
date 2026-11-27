#!/usr/bin/env python3
"""table_query.py — 本地结构化报表与日志只读分析原语（argo table）。

设计边界（第一性原理 + 奥卡姆剃刀）：
  - 可选依赖 `duckdb` 严格函数内惰性导入，未安装时主流程零影响，
    触发本模块时 fail-fast 给出明确安装提示。
  - 严格只读 + 白名单隔离：
    1) 仅允许显式声明的输入文件注册为内存会话只读视图；
    2) 禁用自动下载/加载扩展与网络文件系统，杜绝离线失败与出网；
    3) SQL 层强制单语句 + 只读前缀 + 拦截任何写语句与直接文件表函数；
    4) 强制行数上限（默认 200）与内存上限，输出紧凑 JSON 供 Agent 消费。
  - `.xlsx` 通过可选 `openpyxl` 流式转临时 CSV 注册，避免 DuckDB 联网下载
    `excel` 扩展破坏离线与断网契约。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from cli_io import dumps

_ALLOWED_EXTS = frozenset({
    ".csv", ".tsv", ".parquet", ".json", ".jsonl", ".ndjson",
    ".xlsx", ".sqlite", ".sqlite3", ".db",
})

# 仅允许只读查询起始关键字
_ALLOWED_FIRST_TOKENS = frozenset({
    "SELECT", "WITH", "DESCRIBE", "DESC", "SUMMARIZE", "SHOW", "EXPLAIN",
})

# 禁止在查询 SQL 中出现的写操作、扩展操作或绕过白名单的文件/网络函数
_FORBIDDEN_SQL_TOKENS = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE",
    "COPY", "ATTACH", "DETACH", "INSTALL", "LOAD", "PRAGMA", "CALL",
    "EXPORT", "IMPORT", "SET", "RESET", "VACUUM", "CHECKPOINT", "MERGE",
    "GRANT", "REVOKE", "EXECUTE", "PREPARE", "DEALLOCATE",
    # 阻止绕过白名单视图直接读盘或探测路径
    "READ_CSV", "READ_CSV_AUTO", "READ_PARQUET", "READ_JSON", "READ_JSON_AUTO",
    "READ_NDJSON", "READ_NDJSON_AUTO", "READ_NDJSON_OBJECTS", "READ_JSON_OBJECTS",
    "READ_TEXT", "READ_BLOB", "READ_XLSX", "ST_READ", "GLOB",
    "SQLITE_SCAN", "SQLITE_ATTACH", "POSTGRES_SCAN", "MYSQL_SCAN",
    "PARQUET_SCAN", "PARQUET_METADATA", "PARQUET_SCHEMA", "CSV_AUTO",
    "HTTP_GET", "GETENV", "CURRENT_SETTING",
})

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 5000
_DEFAULT_SAMPLE = 5


class TableQueryError(RuntimeError):
    """结构化表格分析的可预期业务错误（缺依赖、非法 SQL、越权路径等）。"""


def _require_duckdb():
    """函数内惰性导入 duckdb；缺失时 fail-fast。"""
    try:
        import duckdb  # type: ignore[import-not-found]
        return duckdb
    except ImportError as exc:
        raise TableQueryError(
            "argo table 需要可选依赖 duckdb。请先安装："
            "uv pip install duckdb 或 pip install duckdb"
        ) from exc


def _sha256_prefix(path: Path, max_bytes: int = 1024 * 1024) -> str:
    """计算文件前 1MB 内容哈希前缀（12 位），用于取证留痕。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(max_bytes))
    return h.hexdigest()[:12]


def _sanitize_view_name(stem: str, used: set[str], idx: int) -> str:
    """将文件名规范为安全的 SQL 标识符（ASCII 字母/数字/下划线）。"""
    clean = re.sub(r"[^0-9a-zA-Z_]+", "_", stem).strip("_").lower()
    if not clean or clean[0].isdigit():
        clean = f"t{idx}_{clean}" if clean else f"t{idx}"
    base = clean[:48]
    name = base
    n = 2
    while name in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    return name


def _strip_sql_literals_and_comments(sql: str) -> str:
    """剥离 SQL 注释与引号字符串，用于安全的静态关键字检查。"""
    # 块注释 /* ... */
    out = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    # 行注释 -- ...
    out = re.sub(r"--[^\n]*", " ", out)
    # 单引号字符串（含 '' 转义）
    out = re.sub(r"'(?:''|[^'])*'", " '' ", out)
    # 双引号标识符保留结构但避免内容触发分号误判
    out = re.sub(r'"(?:""|[^"])*"', ' "" ', out)
    return out


def validate_readonly_sql(sql: str) -> str:
    """校验 SQL 严格为单条只读查询，不合法则抛 TableQueryError。"""
    if not isinstance(sql, str) or not sql.strip():
        raise TableQueryError("SQL 不能为空")
    raw = sql.strip()
    # 允许末尾带一个分号
    if raw.endswith(";"):
        raw = raw[:-1].strip()
    if not raw:
        raise TableQueryError("SQL 不能为空")

    cleaned = _strip_sql_literals_and_comments(raw)
    if ";" in cleaned:
        raise TableQueryError("仅允许执行单条 SQL 语句，禁止分号拼接多语句")

    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cleaned)
    if not tokens:
        raise TableQueryError("SQL 缺少有效关键字")

    first = tokens[0].upper()
    if first not in _ALLOWED_FIRST_TOKENS:
        raise TableQueryError(
            f"仅允许只读查询（允许开头: {', '.join(sorted(_ALLOWED_FIRST_TOKENS))}），"
            f"收到: {first}"
        )

    for tok in tokens:
        up = tok.upper()
        if up in _FORBIDDEN_SQL_TOKENS:
            raise TableQueryError(f"只读沙箱禁止使用 SQL 关键字或函数: {up}")

    return raw


def _resolve_input(raw_path: str) -> Path:
    p = Path(raw_path).expanduser().resolve()
    if not p.exists():
        raise TableQueryError(f"输入文件不存在: {raw_path}")
    if not p.is_file():
        raise TableQueryError(f"输入路径不是普通文件: {raw_path}")
    if not os.access(p, os.R_OK):
        raise TableQueryError(f"输入文件不可读: {raw_path}")
    if p.suffix.lower() not in _ALLOWED_EXTS:
        raise TableQueryError(
            f"不支持的文件类型: {p.suffix or '(无后缀)'}"
            f"（支持: {', '.join(sorted(_ALLOWED_EXTS))}）"
        )
    return p


def _xlsx_to_temp_csv(
    xlsx_path: Path,
    tmpdir: Path,
    sheet: str | None = None,
) -> tuple[Path, list[str], str]:
    """用可选 openpyxl 将 .xlsx 指定工作表转为临时 CSV，避免 DuckDB 联网下扩展。"""
    try:
        import openpyxl  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TableQueryError(
            "读取 .xlsx 需要可选依赖 openpyxl（离线安全，避免联网下载扩展）："
            "uv pip install openpyxl 或 pip install openpyxl"
        ) from exc

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        sheet_names = list(wb.sheetnames)
        if not sheet_names:
            raise TableQueryError(f"Excel 文件无工作表: {xlsx_path}")
        if sheet:
            if sheet not in sheet_names:
                raise TableQueryError(
                    f"工作表 {sheet!r} 不存在（可选: {', '.join(sheet_names)}）"
                )
            ws = wb[sheet]
            chosen = sheet
        else:
            ws = wb[sheet_names[0]]
            chosen = sheet_names[0]

        out_csv = tmpdir / f"{xlsx_path.stem}_{chosen}.csv"
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for row in ws.iter_rows(values_only=True):
                if any(cell is not None and str(cell).strip() != "" for cell in row):
                    writer.writerow(["" if c is None else c for c in row])
        return out_csv, sheet_names, chosen
    finally:
        wb.close()


def _sqlite_table_to_temp_csv(
    db_path: Path,
    tmpdir: Path,
    table: str | None = None,
) -> tuple[Path, list[str], str]:
    """只读方式列出 SQLite 表并将目标表导出为临时 CSV（零扩展下载、纯 stdlib）。"""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        ]
        if not tables:
            raise TableQueryError(f"SQLite 数据库中没有可查询的表: {db_path}")
        chosen = table or tables[0]
        if chosen not in tables:
            raise TableQueryError(
                f"SQLite 表 {chosen!r} 不存在（可选: {', '.join(tables)}）"
            )
        cur = conn.execute(f'SELECT * FROM "{chosen.replace(chr(34), chr(34)*2)}"')
        col_names = [d[0] for d in cur.description or []]
        out_csv = tmpdir / f"{db_path.stem}_{chosen}.csv"
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(col_names)
            for row in cur:
                writer.writerow([
                    "" if v is None else (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                    for v in row
                ])
        return out_csv, tables, chosen
    finally:
        conn.close()


def _sql_quote_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _open_sandboxed_connection(max_mem_mb: int = 512, threads: int = 2):
    """创建内存 DuckDB 连接并关闭扩展自动下载与网络文件系统。"""
    duckdb = _require_duckdb()
    conn = duckdb.connect(":memory:")
    conn.execute(f"SET memory_limit = '{max(64, int(max_mem_mb))}MB'")
    conn.execute(f"SET threads = {max(1, min(int(threads), 8))}")
    conn.execute("SET autoinstall_known_extensions = false")
    conn.execute("SET autoload_known_extensions = false")
    conn.execute("SET allow_community_extensions = false")
    # 禁用常见网络文件系统（若当前 DuckDB 版本支持）
    try:
        conn.execute("SET disabled_filesystems = 'HTTPFileSystem,S3FileSystem'")
    except Exception:
        pass
    return conn


def _register_input_view(
    conn: Any,
    path: Path,
    view_name: str,
    tmpdir: Path,
    *,
    sheet: str | None = None,
    sqlite_table: str | None = None,
    all_varchar: bool = False,
) -> dict[str, Any]:
    """将单个白名单输入文件注册为 DuckDB 只读视图，并返回元信息。"""
    ext = path.suffix.lower()
    extra: dict[str, Any] = {}

    if ext in (".csv", ".tsv"):
        opts = ["auto_detect=true"]
        if ext == ".tsv":
            opts.append("delim='\\t'")
        if all_varchar:
            opts.append("all_varchar=true")
        expr = f"read_csv_auto({_sql_quote_str(str(path))}, {', '.join(opts)})"
    elif ext == ".parquet":
        expr = f"read_parquet({_sql_quote_str(str(path))})"
    elif ext in (".json", ".jsonl", ".ndjson"):
        expr = f"read_json_auto({_sql_quote_str(str(path))})"
    elif ext == ".xlsx":
        csv_path, sheets, chosen = _xlsx_to_temp_csv(path, tmpdir, sheet=sheet)
        extra["sheets"] = sheets
        extra["sheet"] = chosen
        opts = ["auto_detect=true"]
        if all_varchar:
            opts.append("all_varchar=true")
        expr = f"read_csv_auto({_sql_quote_str(str(csv_path))}, {', '.join(opts)})"
    elif ext in (".sqlite", ".sqlite3", ".db"):
        csv_path, tables, chosen = _sqlite_table_to_temp_csv(
            path, tmpdir, table=sqlite_table
        )
        extra["tables"] = tables
        extra["table"] = chosen
        opts = ["auto_detect=true"]
        if all_varchar:
            opts.append("all_varchar=true")
        expr = f"read_csv_auto({_sql_quote_str(str(csv_path))}, {', '.join(opts)})"
    else:
        raise TableQueryError(f"不支持的文件类型: {ext}")

    conn.execute(f'CREATE VIEW "{view_name}" AS SELECT * FROM {expr}')
    return {
        "view": view_name,
        "path": str(path),
        "kind": ext.lstrip("."),
        "size_bytes": path.stat().st_size,
        "sha256_prefix": _sha256_prefix(path),
        **extra,
    }


def _json_safe(val: Any) -> Any:
    """将 DuckDB 返回值转为可 JSON 序列化类型。"""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    if isinstance(val, (list, tuple)):
        return [_json_safe(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _json_safe(v) for k, v in val.items()}
    return str(val)


def profile_table(
    raw_path: str,
    *,
    sheet: str | None = None,
    sqlite_table: str | None = None,
    all_varchar: bool = False,
    sample_rows: int = _DEFAULT_SAMPLE,
    max_mem_mb: int = 512,
) -> dict[str, Any]:
    """探测本地结构化文件：列结构、类型、总行数、空值率与样例行。"""
    t0 = time.perf_counter()
    path = _resolve_input(raw_path)
    sample_n = max(1, min(int(sample_rows), 50))

    with tempfile.TemporaryDirectory(prefix="argo-table-") as td:
        conn = _open_sandboxed_connection(max_mem_mb=max_mem_mb)
        try:
            meta = _register_input_view(
                conn, path, "t1", Path(td),
                sheet=sheet, sqlite_table=sqlite_table, all_varchar=all_varchar,
            )
            desc = conn.execute('DESCRIBE "t1"').fetchall()
            row_count = int(conn.execute('SELECT COUNT(*) FROM "t1"').fetchone()[0])

            # 一次聚合拿到每列空值数（限制列数防止超宽表爆炸）
            col_infos: list[dict[str, Any]] = []
            col_names = [str(r[0]) for r in desc]
            null_counts: list[int] = []
            if col_names and len(col_names) <= 120:
                agg_exprs = ", ".join(
                    f'SUM(CASE WHEN "{c.replace(chr(34), chr(34)*2)}" IS NULL THEN 1 ELSE 0 END)'
                    for c in col_names
                )
                agg_row = conn.execute(f'SELECT {agg_exprs} FROM "t1"').fetchone()
                null_counts = [int(v or 0) for v in (agg_row or [])]

            for i, row in enumerate(desc):
                cname, ctype = str(row[0]), str(row[1])
                info: dict[str, Any] = {"name": cname, "type": ctype}
                if i < len(null_counts):
                    nc = null_counts[i]
                    info["null_count"] = nc
                    info["null_pct"] = round(nc / row_count * 100, 2) if row_count else 0.0
                col_infos.append(info)

            sample_cur = conn.execute(f'SELECT * FROM "t1" LIMIT {sample_n}')
            samples = [
                [_json_safe(v) for v in r]
                for r in sample_cur.fetchall()
            ]
        finally:
            conn.close()

    return {
        "ok": True,
        "action": "profile",
        **meta,
        "row_count": row_count,
        "column_count": len(col_infos),
        "columns": col_infos,
        "sample_rows": samples,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


def query_tables(
    sql: str,
    raw_inputs: list[str],
    *,
    limit: int = _DEFAULT_LIMIT,
    sheet: str | None = None,
    sqlite_table: str | None = None,
    all_varchar: bool = False,
    max_mem_mb: int = 512,
) -> dict[str, Any]:
    """在白名单输入文件上执行只读 SQL 查询，返回结构化结果。"""
    if not raw_inputs:
        raise TableQueryError("query 必须至少提供一个输入文件（--inputs）")
    clean_sql = validate_readonly_sql(sql)
    lim = max(1, min(int(limit), _MAX_LIMIT))
    t0 = time.perf_counter()

    resolved = [_resolve_input(p) for p in raw_inputs]
    used_names: set[str] = set()
    inputs_meta: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="argo-table-") as td:
        conn = _open_sandboxed_connection(max_mem_mb=max_mem_mb)
        try:
            for idx, p in enumerate(resolved, 1):
                vname = _sanitize_view_name(p.stem, used_names, idx)
                meta = _register_input_view(
                    conn, p, vname, Path(td),
                    sheet=sheet, sqlite_table=sqlite_table, all_varchar=all_varchar,
                )
                # 同时提供 t1, t2... 位置别名，方便 Agent 省去记长文件名
                alias = f"t{idx}"
                if alias != vname and alias not in used_names:
                    conn.execute(f'CREATE VIEW "{alias}" AS SELECT * FROM "{vname}"')
                    meta["alias"] = alias
                inputs_meta.append(meta)

            # 对 SELECT / WITH 自动包一层 LIMIT+1 判断是否截断
            first_tok = re.findall(r"[A-Za-z_]+", _strip_sql_literals_and_comments(clean_sql))[0].upper()
            if first_tok in ("SELECT", "WITH"):
                exec_sql = f"SELECT * FROM ({clean_sql}) AS _argo_q LIMIT {lim + 1}"
            else:
                exec_sql = clean_sql

            cur = conn.execute(exec_sql)
            cols = [d[0] for d in (cur.description or [])]
            fetched = cur.fetchall()
            truncated = len(fetched) > lim
            rows = [[_json_safe(v) for v in r] for r in fetched[:lim]]
        finally:
            conn.close()

    return {
        "ok": True,
        "action": "query",
        "sql": clean_sql,
        "inputs": inputs_meta,
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "limit": lim,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


def _emit(payload: dict[str, Any], compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(dumps(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="argo table",
        description="Argo table — 本地结构化报表与日志只读分析（基于可选 DuckDB）",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_prof = sub.add_parser("profile", help="探测本地表格/日志结构与样例行")
    p_prof.add_argument("path", help="输入文件路径（csv/tsv/parquet/json/jsonl/xlsx/sqlite）")
    p_prof.add_argument("--sheet", default=None, help="Excel 工作表名（默认首表）")
    p_prof.add_argument("--table", dest="sqlite_table", default=None, help="SQLite 表名（默认首表）")
    p_prof.add_argument("--all-varchar", action="store_true", help="全部按文本读取（脏表头兜底）")
    p_prof.add_argument("--sample", type=int, default=_DEFAULT_SAMPLE, help="样例行数（默认 5）")
    p_prof.add_argument("--max-mem-mb", type=int, default=512, help="内存上限 MB（默认 512）")
    p_prof.add_argument("--json", action="store_true", help="JSON 输出")
    p_prof.add_argument("--agent", action="store_true", help="紧凑 JSON 输出")

    p_q = sub.add_parser("query", help="在白名单本地文件上执行只读 SQL 查询")
    p_q.add_argument("sql", help="只读 SQL（可用表名 t1/t2 或文件去后缀名）")
    p_q.add_argument("--inputs", "-i", nargs="+", required=True, help="白名单输入文件路径")
    p_q.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help=f"最大返回行数（默认 {_DEFAULT_LIMIT}）")
    p_q.add_argument("--sheet", default=None, help="Excel 工作表名")
    p_q.add_argument("--table", dest="sqlite_table", default=None, help="SQLite 表名")
    p_q.add_argument("--all-varchar", action="store_true", help="全部按文本读取")
    p_q.add_argument("--max-mem-mb", type=int, default=512, help="内存上限 MB（默认 512）")
    p_q.add_argument("--json", action="store_true", help="JSON 输出")
    p_q.add_argument("--agent", action="store_true", help="紧凑 JSON 输出")

    args = parser.parse_args(argv)
    is_compact = bool(args.agent) and not bool(args.json)
    try:
        if args.action == "profile":
            res = profile_table(
                args.path,
                sheet=args.sheet,
                sqlite_table=args.sqlite_table,
                all_varchar=args.all_varchar,
                sample_rows=args.sample,
                max_mem_mb=args.max_mem_mb,
            )
        else:
            res = query_tables(
                args.sql,
                args.inputs,
                limit=args.limit,
                sheet=args.sheet,
                sqlite_table=args.sqlite_table,
                all_varchar=args.all_varchar,
                max_mem_mb=args.max_mem_mb,
            )
        _emit(res, compact=is_compact)
        return 0
    except TableQueryError as exc:
        _emit({"ok": False, "error": str(exc)}, compact=is_compact)
        return 1
    except Exception as exc:
        _emit(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            compact=is_compact,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

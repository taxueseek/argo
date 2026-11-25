#!/usr/bin/env python3
"""test_table_query.py — 方案一（argo table + recompute.sql）、方案二（PDF 抽表 + seek 提示）
与本地图片优化（FTS5 + 内容哈希去重）的确定性单测。"""

from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None


class TestReadonlySqlValidation:
    """只读 SQL 护栏：不依赖 duckdb，任何环境必跑。"""

    def test_allows_select_and_cte(self):
        from table_query import validate_readonly_sql

        assert validate_readonly_sql("SELECT region, SUM(rev) FROM t1 GROUP BY 1;") == (
            "SELECT region, SUM(rev) FROM t1 GROUP BY 1"
        )
        cte = "WITH x AS (SELECT * FROM t1) SELECT COUNT(*) FROM x"
        assert validate_readonly_sql(cte) == cte

    @pytest.mark.parametrize("bad_sql", [
        "",
        "   ",
        "INSERT INTO t1 VALUES (1)",
        "DELETE FROM t1",
        "DROP TABLE t1",
        "COPY t1 TO '/tmp/leak.csv'",
        "ATTACH '/etc/passwd' AS x",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA version",
        "SET enable_external_access=true",
        "SELECT 1; DROP TABLE t1",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        "SELECT * FROM read_parquet('/tmp/a.parquet')",
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT getenv('PATH')",
    ])
    def test_blocks_unsafe_sql(self, bad_sql: str):
        from table_query import TableQueryError, validate_readonly_sql

        with pytest.raises(TableQueryError):
            validate_readonly_sql(bad_sql)

    def test_string_literal_with_semicolon_is_allowed(self):
        from table_query import validate_readonly_sql

        sql = "SELECT * FROM t1 WHERE note = 'a; DROP TABLE x'"
        assert validate_readonly_sql(sql) == sql


class TestRecomputeWorkPackageSql:
    """工作包 recompute.sql 契约校验（不依赖 duckdb）。"""

    def test_normalize_recompute_sql_and_mutual_exclusion(self):
        from research_work_packages import normalize_recompute

        rec = normalize_recompute({"sql": "SELECT COUNT(*) FROM t1", "limit": 50})
        assert rec is not None
        assert rec["sql"] == "SELECT COUNT(*) FROM t1"
        assert rec["script"] == ""
        assert rec["limit"] == 50

        with pytest.raises(ValueError, match="不能同时指定"):
            normalize_recompute({"script": "print(1)", "sql": "SELECT 1"})

        with pytest.raises(ValueError, match="缺少 script 或 sql"):
            normalize_recompute({})

    def test_run_recompute_sql_default_deny(self, tmp_path: Path):
        from recompute import run_recompute_sql

        p = tmp_path / "sales.csv"
        p.write_text("region,rev\n华东,100\n", encoding="utf-8")
        res = run_recompute_sql(
            "SELECT SUM(rev) FROM t1",
            [{"path": str(p), "kind": "csv"}],
            allow_exec=False,
        )
        assert res["ok"] is False
        assert "默认拒绝" in res["skipped_reason"]


@pytest.mark.skipif(not HAS_DUCKDB, reason="duckdb 可选依赖未安装")
class TestDuckDBTableQuery:
    """真实 DuckDB 端到端行为验证。"""

    def test_profile_and_query_csv_and_jsonl(self, tmp_path: Path):
        from table_query import profile_table, query_tables

        csv_p = tmp_path / "sales.csv"
        csv_p.write_text(
            "region,sku,rev,cost\n"
            "华东,A1,1000,700\n"
            "华东,A2,500,450\n"
            "华南,A1,800,500\n",
            encoding="utf-8",
        )
        prof = profile_table(str(csv_p))
        assert prof["ok"] is True
        assert prof["row_count"] == 3
        assert prof["column_count"] == 4
        assert [c["name"] for c in prof["columns"]] == ["region", "sku", "rev", "cost"]
        assert len(prof["sample_rows"]) == 3

        res = query_tables(
            "SELECT region, SUM(rev) AS total_rev, ROUND((SUM(rev)-SUM(cost))*100.0/SUM(rev), 2) AS margin_pct "
            "FROM t1 GROUP BY region ORDER BY total_rev DESC",
            [str(csv_p)],
        )
        assert res["ok"] is True
        assert res["row_count"] == 2
        assert res["truncated"] is False
        assert res["rows"][0] == ["华东", 1500, 23.33]
        assert res["rows"][1] == ["华南", 800, 37.5]

        # JSONL 日志聚合
        jl = tmp_path / "usage.jsonl"
        jl.write_text(
            "\n".join([
                json.dumps({"engine": "bing", "ms": 120, "ok": True}),
                json.dumps({"engine": "bing", "ms": 180, "ok": True}),
                json.dumps({"engine": "arxiv", "ms": 300, "ok": False}),
            ]),
            encoding="utf-8",
        )
        q2 = query_tables(
            "SELECT engine, COUNT(*) AS n, AVG(ms) AS avg_ms FROM t1 GROUP BY engine ORDER BY n DESC",
            [str(jl)],
        )
        assert q2["rows"] == [["bing", 2, 150.0], ["arxiv", 1, 300.0]]

    def test_query_sqlite_and_truncation(self, tmp_path: Path):
        from table_query import profile_table, query_tables

        db = tmp_path / "demo.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE orders (id INT, city TEXT, amount REAL)")
        conn.executemany(
            "INSERT INTO orders VALUES (?,?,?)",
            [(i, "上海" if i % 2 == 0 else "北京", i * 10.0) for i in range(1, 11)],
        )
        conn.commit()
        conn.close()

        prof = profile_table(str(db))
        assert prof["tables"] == ["orders"]
        assert prof["row_count"] == 10

        res = query_tables(
            "SELECT id, city, amount FROM t1 ORDER BY id",
            [str(db)],
            limit=3,
        )
        assert res["row_count"] == 3
        assert res["truncated"] is True
        assert res["rows"][0] == [1, "北京", 10.0]

    def test_recompute_sql_executes_when_authorized(self, tmp_path: Path):
        from recompute import run_recompute_sql

        csv_p = tmp_path / "metrics.csv"
        csv_p.write_text("q,val\nQ1,120\nQ2,180\n", encoding="utf-8")
        res = run_recompute_sql(
            "SELECT SUM(val) AS total FROM t1",
            [{"path": str(csv_p), "kind": "csv"}],
            allow_exec=True,
        )
        assert res["ok"] is True
        assert res["mode"] == "sql"
        assert res["rows"] == [[300]]


class TestPdfTableExportAndSeekHint:
    """方案二：PDF 表格导出为 CSV 与 local-seek --agent 提示。"""

    def test_export_tables_to_csv(self, tmp_path: Path):
        from pdf_extract import export_tables_to_csv

        tables = [
            [["科目", "2025", "2026"], ["营收", "100", "125"], ["净利润", "20", "28"]],
            [[None, None]],  # 空表应跳过
        ]
        exported = export_tables_to_csv(tables, str(tmp_path / "out"), prefix="annual")
        assert len(exported) == 1
        assert exported[0]["rows"] == 3
        assert exported[0]["columns"] == 3
        assert exported[0]["header"] == ["科目", "2025", "2026"]

        with open(exported[0]["path"], encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows == [["科目", "2025", "2026"], ["营收", "100", "125"], ["净利润", "20", "28"]]


class TestLocalImageFtsAndDedupe:
    """本地图片优化：FTS5 中文检索与内容哈希去重。"""

    def test_fts5_search_and_migration(self, tmp_path: Path):
        import local_image as li

        db = tmp_path / "img.db"
        conn = li.open_db(db)
        rows = [
            ("/tmp/a_发票.png", 1.0, 1024, 800, 600, 1, '[{"id":"document","conf":0.9}]',
             "增值税电子普通发票 金额 350.00", None, None, 1.0, "hash1"),
            ("/tmp/b_sunset.jpg", 1.0, 2048, 1920, 1080, 2, '[{"id":"sunset","conf":0.95}]',
             "", None, None, 1.0, "hash2"),
        ]
        conn.executemany(
            "INSERT INTO images "
            "(path,mtime,size,width,height,inode,labels,ocr,fp_slot,fp_dim,indexed_at,content_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

        # 重新 open_db 应自动从 images 回填 images_fts
        conn2 = li.open_db(db)
        fts_cnt = conn2.execute("SELECT COUNT(*) FROM images_fts").fetchone()[0]
        assert fts_cnt == 2

        res = li.search_local(conn2, "发票 350")
        assert len(res) == 1
        assert res[0]["path"] == "/tmp/a_发票.png"

        res_en = li.search_local(conn2, "sunset")
        assert len(res_en) == 1
        assert res_en[0]["path"] == "/tmp/b_sunset.jpg"
        conn2.close()

    def test_quick_content_hash_matches_identical_files(self, tmp_path: Path):
        import local_image as li

        p1 = tmp_path / "one.png"
        p2 = tmp_path / "two.png"
        p3 = tmp_path / "three.png"
        data = b"PNGDATA" * 100
        p1.write_bytes(data)
        p2.write_bytes(data)
        p3.write_bytes(data + b"X")

        h1 = li._quick_content_hash(p1, p1.stat().st_size)
        h2 = li._quick_content_hash(p2, p2.stat().st_size)
        h3 = li._quick_content_hash(p3, p3.stat().st_size)
        assert h1 == h2
        assert h1 != h3

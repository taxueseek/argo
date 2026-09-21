#!/usr/bin/env python3
"""tests/test_state_hygiene.py — 本地状态目录的卫生门禁（2026-09-17）

三件事，都是「只增不减」导致的资源占用：

  1. **SQLite WAL 不回缩**：默认 `journal_size_limit=-1`，检查点后 `-wal` 停在
     自动检查点阈值（1000 页 ≈ 3.94 MB）。实测 cache.db + adaptive.db 两库合计
     约 7.9 MB；`limit=1 MB + autocheckpoint=256 页` 后稳态约 1 MB。
  2. **遥测无轮转**：append-only 而无上限，三个流实测 771 KB 且只增不减。
  3. **配置缓存槽成孤儿**：槽按 config.yaml 路径分（多 checkout 安全所必需），
     临时 checkout（测试/基准跑出来的 /tmp 副本）留下的槽永不回收，实测 8 个槽
     里 6 个是孤儿、合计 1.13 MB。
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import argo_paths  # noqa: E402
import config  # noqa: E402
import telemetry  # noqa: E402


# ── 1. SQLite WAL 策略 ────────────────────────────────────────────────────────

def test_apply_state_pragmas_sets_wal_policy(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    argo_paths.apply_state_pragmas(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] \
        == argo_paths.WAL_SIZE_LIMIT_BYTES
    assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] \
        == argo_paths.WAL_AUTOCHECKPOINT_PAGES
    conn.close()


def test_wal_stays_bounded_under_repeated_writes(tmp_path):
    """持续写入后 -wal 不得停在自动检查点阈值上（这条是「回缩」的可观测断言）。"""
    db = tmp_path / "wal.db"
    conn = sqlite3.connect(str(db))
    argo_paths.apply_state_pragmas(conn)
    conn.execute("CREATE TABLE t(k TEXT PRIMARY KEY, v TEXT)")
    blob = "x" * 4000
    for i in range(3000):
        conn.execute("INSERT OR REPLACE INTO t VALUES(?,?)", (f"k{i}", blob))
        conn.commit()
    wal = db.with_name(db.name + "-wal")
    size_mb = wal.stat().st_size / 1024 / 1024 if wal.exists() else 0.0
    conn.close()
    assert size_mb < 3.5, f"WAL 未回缩：{size_mb:.2f} MB"


def test_both_state_dbs_use_the_shared_policy(tmp_path, monkeypatch):
    """三处 SQLite 连接必须走同一份策略——各自写一份 PRAGMA 必然漂移。"""
    import adaptive
    import cache
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(adaptive, "DB_PATH", tmp_path / "adaptive.db")

    conn = cache.SQLiteCache(db_path=str(tmp_path / "cache.db"))._connect()
    assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] \
        == argo_paths.WAL_AUTOCHECKPOINT_PAGES

    aconn = adaptive.AdaptiveLearner()._connect()
    assert aconn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] \
        == argo_paths.WAL_AUTOCHECKPOINT_PAGES


# ── 2. 遥测轮转 ───────────────────────────────────────────────────────────────

@pytest.fixture
def tele_dir(tmp_path, monkeypatch):
    d = tmp_path / "telemetry"
    monkeypatch.setenv("ARGO_TELEMETRY_DIR", str(d))
    monkeypatch.setenv("ARGO_TELEMETRY", "1")
    return d


def _lines(d: Path, stream: str) -> list[str]:
    p = d / f"{stream}.jsonl"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def test_telemetry_trims_when_over_cap(tele_dir, monkeypatch):
    """超过上限就回缩到最近 N 行，且不丢最新一条。"""
    monkeypatch.setattr(telemetry, "_MAX_BYTES", 300)
    monkeypatch.setattr(telemetry, "_KEEP_LINES", 5)
    for i in range(60):
        telemetry.emit("rotation", {"i": i, "pad": "y" * 40})
    lines = _lines(tele_dir, "rotation")
    assert len(lines) <= 6, f"未回缩：{len(lines)} 行"
    assert json.loads(lines[-1])["i"] == 59, "最新一条不能丢"


def test_telemetry_untouched_under_cap(tele_dir, monkeypatch):
    """没超上限时一次读取都不该发生（只付 stat 的代价）。"""
    monkeypatch.setattr(telemetry, "_MAX_BYTES", 10 ** 9)
    for i in range(20):
        telemetry.emit("small", {"i": i})
    assert len(_lines(tele_dir, "small")) == 20


def test_telemetry_trim_failure_is_silent(tele_dir, monkeypatch):
    """回缩失败不得把异常抛到搜索主路径上。"""
    monkeypatch.setattr(telemetry, "_MAX_BYTES", 1)

    def boom(*_a, **_k):
        raise OSError("只读文件系统")

    monkeypatch.setattr(telemetry._paths, "atomic_write_text", boom)
    assert telemetry.emit("broken", {"i": 1}) is True


# ── 3. 配置缓存槽回收 ─────────────────────────────────────────────────────────

def _write_slot(path: Path, config_path: str | None) -> None:
    key = {} if config_path is None else {"config_path": config_path}
    path.write_text(json.dumps({"schema": 1, "key": key, "config": {}}),
                    encoding="utf-8")


def test_sweep_removes_only_dead_path_slots(tmp_path, monkeypatch):
    """只回收 config_path 已消失的槽：活着的 checkout 与读不懂的槽一律不动。"""
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))

    dead = tmp_path / "config-cache-dead000000000000.json"
    live = tmp_path / "config-cache-1111222233334444.json"
    corrupt = tmp_path / "config-cache-cafebabe00000000.json"
    keep_me = tmp_path / "config-cache-9999000011112222.json"

    _write_slot(dead, "/tmp/argo-gone-forever/config.yaml")
    _write_slot(live, str(config.CONFIG_PATH))
    corrupt.write_text("{ 这不是 JSON", encoding="utf-8")
    _write_slot(keep_me, "/definitely/not/here/config.yaml")
    (tmp_path / "not-a-slot.json").write_text("{}", encoding="utf-8")

    config._sweep_orphan_cache_slots(keep_me)

    assert not dead.exists(), "死路径的槽应被回收"
    assert live.exists(), "活着的 checkout 的槽不能动"
    assert corrupt.exists(), "读不懂的槽不能删（判定不了就不删）"
    assert keep_me.exists(), "keep 参数指定的槽不能删"
    assert (tmp_path / "not-a-slot.json").exists(), "不匹配命名的文件不能动"


def test_sweep_is_wired_into_the_save_path():
    """回收必须挂在写缓存路径上——只留一个没人调的函数，孤儿照样堆积。"""
    # 写缓存路径住在 config_cache（配置的跨进程磁盘缓存已拆出）
    tree = ast.parse((SCRIPT_DIR / "config_cache.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_save_config_disk_cache":
            assert "_sweep_orphan_cache_slots" in ast.unparse(node)
            return
    pytest.fail("找不到 _save_config_disk_cache——写缓存路径被搬走了？")


def test_sweep_disabled_when_cache_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGO_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ARGO_CONFIG_CACHE", "0")
    dead = tmp_path / "config-cache-dead000000000000.json"
    _write_slot(dead, "/tmp/argo-gone-forever/config.yaml")
    config._sweep_orphan_cache_slots(tmp_path / "config-cache-other.json")
    assert dead.exists(), "缓存整体关闭时不该做任何清扫"

#!/usr/bin/env python3
"""tests/test_seek_inprocess.py — seek 进程内化（2026-09-29 H2）。

守四件事：
  1. seek.run_query 进程内核心与 CLI 行为等价（JSON/outline/lines/无匹配
     四种模式的输出与退出码；CLI 真子进程行为由 test_seek_dot_mode 等
     既有用例覆盖，此处锁 run_query ↔ main 的一致性）
  2. time_budget 下传：进程内调用的超时约束真实生效（防挂死占死单线程
     executor——子进程可硬杀，线程不可杀）
  3. _run_local_seek 进程内优先、子进程回退（能力不回退，只是慢）
  4. seek_query_payload 公共入口（include-local 与 MCP argo_local_search
     共用同一实现，单一来源）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SEEK = ROOT / "sub-skills" / "local-seek" / "scripts" / "seek.py"
for p in (ROOT / "scripts",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import local_seek  # noqa: E402
from seek_locator import resolve_seek_py  # noqa: E402

SCRIPTS = ROOT / "scripts"


@pytest.fixture(autouse=True)
def _clean_caches():
    local_seek._LOCAL_SEEK_CACHE.clear()
    yield
    local_seek._LOCAL_SEEK_CACHE.clear()


def _seek_module():
    mod = local_seek._load_seek_module(str(SEEK))
    assert mod is not None
    return mod


# ── 1. run_query 与 CLI 等价 ──────────────────────────────────────────────────

def test_run_query_json_mode_parity():
    text, rc = _seek_module().run_query(
        ["cache_key", "--json", "--max", "3", "--path", str(SCRIPTS)])
    assert rc == 0
    payload = json.loads(text)
    assert payload["count"] >= 1
    assert payload["engine"] == "rg"


def test_run_query_no_match_rc1():
    text, rc = _seek_module().run_query(
        ["zzz_nonexistent_xyz_98765", "--path", str(SCRIPTS)])
    assert rc == 1
    assert text.startswith("local-seek: 未找到匹配")


def test_run_query_outline_and_lines_modes():
    text, rc = _seek_module().run_query(["--outline", str(SEEK)])
    assert rc == 0 and "结构" in text
    text, rc = _seek_module().run_query(["--lines", "1-3", str(SEEK)])
    assert rc == 0 and "第 1-3 行" in text


def test_run_query_no_args_returns_help():
    text, rc = _seek_module().run_query([])
    assert rc == 0
    assert "usage" in text.lower()


def test_main_prints_run_query_text(capsys):
    """main = print(run_query) + rc：CLI 壳与进程内核心语义一致。

    不做逐字节对照，且查询必须选**命中数低于收集上限**的——cap 截断下
    「哪些命中进池」是遍历序依赖的（既有设计），两次独立运行本就不保证
    同一集合。`_SEEK_MODULE_CACHE` 全仓仅 local_seek.py 数处命中，确定。
    """
    argv = ["_SEEK_MODULE_CACHE", "--json", "--max", "3", "--path", str(SCRIPTS)]
    direct_text, direct_rc = _seek_module().run_query(argv)
    main_rc = _seek_module().main(argv)
    captured = capsys.readouterr()
    assert main_rc == direct_rc == 0
    a, b = json.loads(direct_text), json.loads(captured.out)
    assert a["query"] == b["query"] and a["count"] == b["count"] >= 1
    _sig = lambda rs: sorted((r["path"], r["line"]) for r in rs)  # noqa: E731
    assert _sig(a["results"]) == _sig(b["results"])


# ── 2. time_budget 下传 ──────────────────────────────────────────────────────

def test_time_budget_actually_bounds_search():
    """预算 0.1ms 必然掐死 rg → 报错路径返回 rc1（证明下传真实生效）。"""
    text, rc = _seek_module().run_query(
        ["cache_key", "--json", "--max", "3", "--path", str(SCRIPTS)],
        time_budget=0.0001)
    assert rc == 1
    assert "rg" in text


# ── 3. _run_local_seek 进程内优先 + 子进程回退 ────────────────────────────────

def test_run_local_seek_inprocess_returns_scored_hits():
    hits = local_seek._run_local_seek("cache_key", 3, search_dir=str(SCRIPTS))
    assert len(hits) == 3
    assert hits[0]["score"] == 0.9
    assert hits[0]["source"] == "local_files"
    assert hits[0]["url"].startswith("file://")


def test_run_local_seek_falls_back_to_subprocess(monkeypatch):
    """进程内执行炸掉时回退子进程，命中不丢（能力不回退）。"""
    seek_py = resolve_seek_py()
    mod = local_seek._load_seek_module(seek_py)

    def _boom(*a, **k):
        raise RuntimeError("simulated in-process failure")

    monkeypatch.setattr(mod, "run_query", _boom)
    hits = local_seek._run_local_seek("cache_key", 3, search_dir=str(SCRIPTS))
    assert len(hits) == 3
    assert hits[0]["score"] == 0.9


# ── 4. 公共入口 ──────────────────────────────────────────────────────────────

def test_seek_query_payload_shared_entry():
    payload = local_seek.seek_query_payload("cache_key", str(SCRIPTS), 2)
    assert payload is not None
    assert payload["count"] == 2
    assert payload["mode"] == "fast"


def test_seek_query_payload_exact_flag_passthrough():
    payload = local_seek.seek_query_payload("cache_key", str(SCRIPTS), 2,
                                            exact=True)
    assert payload is not None  # --exact 透传不炸

#!/usr/bin/env python3
"""tests/test_local_seek_promotion.py — 本地文件搜索扶正（2026-09-29 方案E）。

守五个修复，对应「本地内容搜索被忽略」审查结论：
  1. include-local 三态智能默认：None=自动（fast/budget 开，auto/deep 关），
     显式 True/False 恒尊重
  2. 评分统一：CLI include-local 路径此前恒 score=0.0，与 MCP
     argo_local_search 的 0.9/0.7 双轨——现同口径
  3. 宽根判据跨平台：/mnt（WSL）、/cygdrive、Windows 盘符根
  4. 提示不再指向不存在的 `argo local-search` 子命令
  5. MCP include_local 缺省 True（源码钉住，schema 与行为成对）
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import local_seek  # noqa: E402
from search import _resolve_include_local  # noqa: E402


# ── 1. 三态智能默认 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,expected", [
    ("fast", True), ("budget", True),
    ("auto", False), ("deep", False),
])
def test_auto_default_by_mode(mode, expected):
    assert _resolve_include_local(None, mode) is expected


def test_explicit_overrides_auto():
    assert _resolve_include_local(True, "deep") is True
    assert _resolve_include_local(False, "fast") is False
    assert _resolve_include_local(True, "auto") is True


# ── 2. 评分统一（CLI 路径 0.0 → 0.9/0.7）─────────────────────────────────────

def _fake_seek_run(mode: str):
    """伪造 seek.py 子进程输出（JSON、带 mode）。"""
    payload = json.dumps({
        "query": "q", "engine": "rg", "mode": mode, "count": 1,
        "results": [{"path": "/tmp/proj/a.py", "line": 3,
                     "snippet": "def main():", "mtime": "2026-09-29 10:00:00"}],
    }, ensure_ascii=False)

    class P:
        returncode = 0
        stdout = payload
        stderr = ""

    def _run(*a, **k):
        return P()
    return _run


@pytest.fixture(autouse=True)
def _clean_cache():
    local_seek._LOCAL_SEEK_CACHE.clear()
    yield
    local_seek._LOCAL_SEEK_CACHE.clear()


@pytest.fixture()
def _fake_seeker(monkeypatch):
    def _install(mode: str):
        monkeypatch.setattr(local_seek.os.path, "isfile", lambda p: True)
        import seek_locator
        monkeypatch.setattr(seek_locator, "resolve_seek_py", lambda: "/fake/seek.py")
        monkeypatch.setattr(local_seek.subprocess if hasattr(local_seek, "subprocess")
                            else subprocess, "run", _fake_seek_run(mode))
        # local_seek 内部是 `import subprocess as _sp`，patch 模块属性即可
        monkeypatch.setattr(subprocess, "run", _fake_seek_run(mode))
    return _install


def test_exact_mode_scores_09(monkeypatch, _fake_seeker):
    _fake_seeker("fast")
    hits = local_seek._run_local_seek("q", 5, search_dir=str(ROOT / "scripts"))
    assert hits and hits[0]["score"] == 0.9
    assert hits[0]["source"] == "local_files"


def test_expanded_mode_scores_07(monkeypatch, _fake_seeker):
    _fake_seeker("fast+扩展")
    hits = local_seek._run_local_seek("q", 5, search_dir=str(ROOT / "scripts"))
    assert hits and hits[0]["score"] == 0.7


# ── 3. 宽根判据跨平台 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/mnt", "/cygdrive", "C:\\", "C:", "C:/"])
def test_broad_roots_rejected(path):
    assert local_seek._is_broad_local_root(path) is True, path


@pytest.mark.parametrize("path", ["/tmp/proj", "/home/u/notes", "C:\\Users\\me\\docs"])
def test_specific_paths_allowed(path):
    assert local_seek._is_broad_local_root(path) is False, path


# ── 4. 提示不再指向幻觉子命令 ─────────────────────────────────────────────────

def test_hint_references_real_paths_only():
    src = (ROOT / "scripts" / "local_seek.py").read_text(encoding="utf-8")
    assert "argo local-search" not in src  # 该子命令不存在（bin/argo 只有 local-image）
    assert "seek.py --path" in src


# ── 5. MCP 缺省 True（源码钉住）───────────────────────────────────────────────

def test_mcp_include_local_defaults_true():
    src = (ROOT / "scripts" / "mcp_handlers.py").read_text(encoding="utf-8")
    assert 'arguments.get("include_local", True)' in src


def test_cli_has_no_local_flag():
    src = (ROOT / "scripts" / "search_cli.py").read_text(encoding="utf-8")
    assert '"--no-local"' in src


# ── 6. 缓存命中路径等待预算（H1）──────────────────────────────────────────────

def test_cache_hit_wait_budget_wiring():
    """H1 源码钉住：cached 结果的本地等待收窄到宽限窗，且 deadline 跨
    两个等待点共享（预等待 + shape_response 合并等待共用同一预算）。"""
    src = (ROOT / "scripts" / "search.py").read_text(encoding="utf-8")
    assert "_LOCAL_SEEK_GRACE_S" in src
    assert 'result.get("cached")' in src
    # 两个等待点都必须走 deadline（剩余预算），不能各自拿满超时
    assert src.count("_local_deadline - time.monotonic()") == 2
    # 宽限窗必须显著小于常规超时（快路径保护语义）
    import search as search_mod
    assert search_mod._LOCAL_SEEK_GRACE_S < search_mod._LOCAL_SEEK_TIMEOUT_S / 4

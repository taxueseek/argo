#!/usr/bin/env python3
"""tests/test_envelope_default.py — 输出默认档契约（2026-09-17）

背景：默认附 envelope 时，一次 5 条结果的 JSON 输出 15454 B，其中 14117 B
（82%）是同一批结果的三视图重复——snippet 被写三遍（5701 B，占全文 40%）。
而三个视图的角色是**归档与来源追溯**：归档路径本来就会把 candidates 落成
candidates.jsonl、并在缺 sources 时从 results 回填，所以「归档要全量」由
`--archive` 保证，默认翻转不损失能力。此前方向是「默认全量、调用者记得减」，
于是忘记加开关的调用者每次多付约 2.4k token。

两条门禁：
  1. 源码形态锁——`use_envelope` 的计算里不得出现 `no_envelope`，否则默认档
     等于又被改回「全量」，而这个改动不会让任何既有断言变红；
  2. 功能锁——`envelope=False` 不得带 sources/candidates，`envelope=True` 必须带。
"""

from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import search  # noqa: E402


# ── 1. 源码形态锁（读 search_cli.py：CLI 的默认档判据住在那里）────────────────

def test_use_envelope_does_not_default_to_full():
    tree = ast.parse((SCRIPT_DIR / "search_cli.py").read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "use_envelope"
                for t in node.targets):
            found.append(ast.unparse(node.value))
    assert found, "找不到 use_envelope 的赋值——CLI 的默认档判据被搬走了？"
    for expr in found:
        assert "no_envelope" not in expr, (
            "use_envelope 又变回「默认全量」了："
            f"{expr}。默认档应服务最常见用途（取答案），"
            "归档由 --archive 强制打开 envelope。"
        )
        assert "envelope" in expr and "archive" in expr, expr


# ── 2. 功能锁（离线：假引擎顶替网络出口） ─────────────────────────────────────

@pytest.fixture
def offline(monkeypatch):
    def fake_engine_search(query, engine, n=5, timeout=None, depth="fast",
                           mode="auto", **_):
        return [{
            "title": f"distinct document {engine}",
            "url": f"https://example.invalid/{engine}/article",
            "snippet": f"unique corpus {engine}",
            "source": engine,
        }]

    monkeypatch.setattr(search, "engine_search", fake_engine_search)
    monkeypatch.setattr(search, "_missing_env_for", lambda _eng: [])
    monkeypatch.setattr(search, "get_engines", lambda: {})
    monkeypatch.setattr(search, "get_execution_config",
                        lambda: {"retry_count": 0, "per_engine_budget_s": 10.0})
    monkeypatch.setattr(search, "get_cost_factor", lambda _eng: 1.0)


def test_default_output_has_no_archive_views(offline):
    r = search.super_search(f"默认档-{time.time_ns()}", n=3, depth="fast",
                            mode="fast", envelope=False)
    assert "results" in r and r["results"], "答案视图必须在"
    for view in ("sources", "candidates", "coverage"):
        assert view not in r, f"默认档不该带 {view}（要时用 --envelope）"
    # 质量信号不受影响：局限声明与漏斗与归档开关无关
    assert "limitations" in r
    assert isinstance(r.get("funnel"), dict)


def test_envelope_opt_in_restores_all_views(offline):
    r = search.super_search(f"归档档-{time.time_ns()}", n=3, depth="fast",
                            mode="fast", envelope=True)
    assert r.get("sources"), "envelope 必须带 sources"
    assert r.get("candidates"), "envelope 必须带 candidates"
    assert "coverage" in r


def test_archive_implies_envelope_via_cli_flag():
    """`--archive` 必须强制打开 envelope——归档的候选列表靠它。"""
    src = (SCRIPT_DIR / "search_cli.py").read_text(encoding="utf-8")
    assert "use_envelope = args.envelope or args.archive" in src, (
        "归档路径依赖 envelope；--archive 必须强制打开，否则 candidates.jsonl 会空"
    )

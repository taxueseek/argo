#!/usr/bin/env python3
"""P0/P1/P2 回归测试：契约、正确性、缓存边界、结构（全部无网络，mock 掉 IO）

覆盖：
  - 域名归一化（www 前缀只剥一次，不损坏域名）
  - evidence score=0 不被抬升为 0.5
  - extract 嵌套表格保留外层数据
  - crawl BFS 从原始 HTML 发现链接（raw=True）
  - crawl sitemap elapsed_ms 是耗时而非时间戳
  - pdf_extract 页码解析
  - MCP schema 契约：单真源、无死 actions 参数
  - argo_local_search 零结果契约（count=0 而非报错）
  - P2：_understand_cached lru_cache 语义、死模块 fetch_v2/mcp_payload 已移除、
    社交并行搜索超时返回已完成部分
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import importlib.util

import pytest

from research import detect_cross_references
from evidence import compute_credibility
from extract import extract_tables
from pdf_extract import _parse_pages
from mcp_server import TOOLS, execute_tool
import crawl as crawl_mod


# ── 域名归一化（regression: strip("www.") 曾把 weibo.com→eibo.com）────────────

SAME_TEXT_TITLE = "python async best practices guide"
SAME_TEXT_SNIP = "python async best practices guide tutorial"


def test_domain_normalization_same_site_counts_once():
    """www 子域与裸域归一为同一域名，不得计为两个来源。"""
    results = [
        {"url": "https://www.weibo.com/a", "title": SAME_TEXT_TITLE, "snippet": SAME_TEXT_SNIP},
        {"url": "https://weibo.com/b", "title": SAME_TEXT_TITLE, "snippet": SAME_TEXT_SNIP},
    ]
    cross = detect_cross_references(results, min_sources=2)
    # 旧实现：www.weibo.com → "eibo.com"，两个域名 → 误判为交叉引用
    assert cross == []


def test_domain_normalization_cross_site_detected():
    """真正跨站时仍应检出交叉引用（控制组）。"""
    results = [
        {"url": "https://www.weibo.com/a", "title": SAME_TEXT_TITLE, "snippet": SAME_TEXT_SNIP},
        {"url": "https://en.wikipedia.org/wiki/x", "title": SAME_TEXT_TITLE, "snippet": SAME_TEXT_SNIP},
    ]
    cross = detect_cross_references(results, min_sources=2)
    assert len(cross) >= 1
    assert "weibo.com" in cross[0]["domains"]
    assert "en.wikipedia.org" in cross[0]["domains"]


# ── evidence score=0 不被抬升（regression: r.get("score",0.5) or 0.5）─────────

def test_evidence_score_zero_not_inflated():
    base = {"url": "https://docs.example.org/a", "title": "t", "snippet": "s" * 60, "source": "manual"}
    r0 = compute_credibility([{**base, "score": 0}], "q")
    r5 = compute_credibility([{**base, "score": 0.5}], "q")
    f0 = r0["results"][0]["credibility"]["final"]
    f5 = r5["results"][0]["credibility"]["final"]
    # score 权重 0.10，0.5 与 0 的贡献差恰为 0.05（旧实现会把它当 0.5）
    assert abs(f5 - f0 - 0.05) < 0.001, (f0, f5)


# ── extract 嵌套表格（regression: 内层 table 曾重置 _rows 丢外层）─────────────

def test_extract_nested_table_preserves_outer():
    html = (
        "<table><tr><th>H1</th><th>H2</th></tr>"
        "<tr><td>a<table><tr><td>inner</td></tr></table></td><td>b</td></tr></table>"
    )
    tables = extract_tables(html)
    assert len(tables) == 1
    assert tables[0][0] == {"H1": "ainner", "H2": "b"}


def test_extract_two_sibling_tables():
    html = "<table><tr><td>1</td></tr></table><table><tr><td>2</td></tr></table>"
    tables = extract_tables(html)
    assert len(tables) == 2


# ── crawl BFS（regression: 在纯文本 content 搜 href 永远找不到链接）────────────

def test_crawl_bfs_discovers_links_from_html(monkeypatch):
    calls = []

    def fake_fetch(url, max_chars, timeout, raw=False):
        calls.append((url, raw))
        if url == "http://example.com/":
            return {
                "success": True,
                "content": "no href here",
                "html": "<html><a href='/a'>A</a><a href='/b'>B</a></html>",
                "url": url,
            }
        return {"success": True, "content": "leaf", "html": "", "url": url}

    monkeypatch.setattr(crawl_mod, "fetch_page", fake_fetch)
    r = crawl_mod.crawl_bfs("http://example.com/", max_pages=3, max_depth=1)
    urls = [p["url"] for p in r["pages"]]
    assert urls == ["http://example.com/", "http://example.com/a", "http://example.com/b"]
    # BFS 必须以 raw=True 抓取才能看到 HTML
    assert calls[0][1] is True


def test_crawl_sitemap_elapsed_ms_is_duration(monkeypatch):
    def fake_fetch(url, max_chars=0, timeout=0, raw=False):
        return {
            "success": True,
            "html": "<urlset><url><loc>http://example.com/1</loc></url></urlset>",
            "content": "",
            "url": url,
        }

    monkeypatch.setattr(crawl_mod, "fetch_page", fake_fetch)
    r = crawl_mod.crawl_sitemap("http://example.com")
    # 旧实现存的是当前时间戳（~1.7e12），修复后是毫秒级耗时
    assert 0 <= r["elapsed_ms"] < 60_000


# ── pdf_extract 页码解析 ─────────────────────────────────────────────────────

def test_parse_pages_specs():
    assert _parse_pages(None, 10) == list(range(1, 11))
    assert _parse_pages("1-5", 10) == [1, 2, 3, 4, 5]
    assert _parse_pages("1,3,5-7", 10) == [1, 3, 5, 6, 7]
    assert _parse_pages("8-12", 10) == [8, 9, 10]
    assert _parse_pages("7-2", 10) == [2, 3, 4, 5, 6, 7]
    assert _parse_pages("0,99,abc", 10) == []
    assert _parse_pages("2", 10) == [2]


# ── MCP schema 契约（单真源；argo_fetch 无死 actions）────────────────────────

def test_mcp_tools_ten_tools_single_source():
    names = [t["name"] for t in TOOLS]
    assert len(TOOLS) == 10
    assert names == [
        "argo_search", "argo_local_search", "argo_research", "argo_evidence",
        "argo_clarify", "argo_crawl", "argo_fetch", "argo_screenshot",
        "argo_pdf", "argo_social_search",
    ]


def test_mcp_fetch_schema_has_no_dead_actions():
    fetch = next(t for t in TOOLS if t["name"] == "argo_fetch")
    props = fetch["inputSchema"]["properties"]
    assert "actions" not in props
    assert "timeout" in props


# ── argo_local_search 零结果契约（count=0 而非错误）──────────────────────────

def test_local_search_zero_results_contract(monkeypatch):
    import mcp_server

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "no results"

    monkeypatch.setattr(mcp_server.subprocess, "run", lambda *a, **k: FakeProc())
    resp = execute_tool("argo_local_search", {"query": "zzz不存在的词xyz", "max_results": 5})
    payload = json.loads(resp["content"][0]["text"])
    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["errors"], "零结果应以 errors 提示，而非静默成功"


# ── P2 结构：lru_cache / 死模块 / 社交超时 ───────────────────────────────────

def test_understand_cached_lru_semantics():
    """_understand_cached 改名 lru_cache 后行为不变：命中即重建、结果一致。"""
    from query_understanding import _understand_cached, _understand_cached_dict
    from query_understanding import understand as raw_understand

    _understand_cached_dict.cache_clear()
    q = "除了百度的搜索引擎"
    a = _understand_cached(q)
    b = _understand_cached(q)
    assert a.exclude_terms == ["百度"]
    assert a.to_dict() == b.to_dict() == raw_understand(q).to_dict()
    info = _understand_cached_dict.cache_info()
    assert info.hits >= 1, "重复查询应命中缓存"
    assert info.misses == 1 and info.currsize == 1, info


def test_dead_modules_fetch_v2_mcp_payload_removed():
    """fetch_v2.py / mcp_payload.py 已删除，且全仓库无任何代码引用。"""
    import re

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    for dead in ("fetch_v2.py", "mcp_payload.py"):
        assert not (scripts / dead).exists(), f"{dead} 应为死模块被移除"
    # 只查代码引用（import / 引号字符串），不查 docstring 里的历史说明文字
    code_ref = re.compile(
        r"\b(?:from|import)\s+(?:fetch_v2|mcp_payload)\b"
        r'|["\'](?:fetch_v2|mcp_payload)["\']',
    )
    for path in scripts.rglob("*.py"):
        if "social_engines" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        hit = code_ref.search(text)
        assert hit is None, f"{path} 仍引用已删除模块: {hit.group(0)}"


def test_social_search_timeout_returns_partial(monkeypatch):
    """社交并行搜索超时：返回已完成平台 + 超时平台空结果 + errors，不抛错。"""
    import time
    import types

    import mcp_server

    def fast_search(query, n=None):
        time.sleep(0.1)
        return [{"title": "ok", "url": "http://x", "source": "fast"}]

    def slow_search(query, n=None):
        time.sleep(3)
        return [{"title": "slow", "url": "http://y", "source": "slow"}]

    fast_mod = types.ModuleType("social_engines.fast_engine")
    fast_mod.search = fast_search
    slow_mod = types.ModuleType("social_engines.slow_engine")
    slow_mod.search = slow_search
    monkeypatch.setitem(mcp_server._module_cache, "social_engines.fast_engine", fast_mod)
    monkeypatch.setitem(mcp_server._module_cache, "social_engines.slow_engine", slow_mod)

    t0 = time.monotonic()
    results, used, errors = mcp_server._search_social_platforms(
        ["fast", "slow"], "q", 5, timeout=1.0,
    )
    elapsed = time.monotonic() - t0
    assert results["fast"], "已完成平台应返回结果"
    assert results["slow"] == [], "超时平台应为空结果"
    assert used == ["fast"], "超时平台不应计入 engines_used"
    assert any("timeout" in e for e in errors), errors
    assert elapsed < 2.0, f"应在超时后立即返回，实际 {elapsed:.2f}s"

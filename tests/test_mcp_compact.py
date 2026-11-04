#!/usr/bin/env python3
"""MCP 响应压缩与 research profile 解析（无网络）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from mcp_server import (
    _compact_search_result,
    _compact_research_result,
    _dumps,
    _resolve_research_profile,
    handle_rpc,
)


def test_dumps_compact_smaller_than_pretty():
    obj = {"a": 1, "b": [{"x": "hello" * 20} for _ in range(5)]}
    compact = _dumps(obj, pretty=False)
    pretty = _dumps(obj, pretty=True)
    assert len(compact) < len(pretty)
    print(f"  ✅ dumps compact {len(compact)} < pretty {len(pretty)}")


def test_compact_search_strips_heavy_fields():
    raw = {
        "query": "q",
        "results": [
            {
                "title": "t",
                "url": "https://ex.com",
                "snippet": "s" * 500,
                "source": "zhihu",
                "score": 0.9,
                "selection": 0.8,
                "huge_blob": "x" * 10000,
            }
        ],
        "plan": {"huge": True},
        "candidates": list(range(100)),
        "sources": [{"ref": 1, "title": "t", "url": "https://ex.com", "engine": "zhihu"}],
        "count": 1,
        "engines_used": ["zhihu"],
    }
    out = _compact_search_result(raw, summary=True)
    assert "plan" not in out
    assert "candidates" not in out
    assert "huge_blob" not in out["results"][0]
    assert len(out["results"][0]["snippet"]) <= 80
    assert out["sources"][0]["url"] == "https://ex.com"
    print("  ✅ compact search strips heavy + snip")


def test_compact_research_keeps_gates():
    raw = {
        "query": "CRISPR",
        "key_findings": [
            {
                "aspect": "a",
                "top_result": {"title": "t", "url": "https://a.com", "snippet": "z" * 400},
                "citation_refs": [1],
            }
        ],
        "sources": [{"ref": 1, "title": "t", "url": "https://a.com", "snippet": "z" * 200}],
        "quality_gates": ["gate1"],
        "report_sections": ["Q", "A"],
        "discipline": "academic",
        "sub_results_raw": {"huge": True},
    }
    out = _compact_research_result(raw, summary=True)
    assert out["quality_gates"] == ["gate1"]
    assert out["discipline"] == "academic"
    assert "sub_results_raw" not in out
    assert len(out["sources"][0]["snippet"]) <= 100
    print("  ✅ compact research keeps gates, drops raw")


def test_compact_search_keeps_image_fields():
    """图搜/反搜图结果的图链必须到达 Agent——摘要档也不例外。"""
    raw = {
        "query": "https://files.yande.re/x.jpg",
        "results": [
            {
                "title": "白丝袜",
                "url": "https://www.pixiv.net/artworks/68259314",
                "snippet": "相似度 95.55%",
                "source": "saucenao",
                "score": 0.95,
                "image_url": "https://img1.saucenao.com/res/pixiv/x.jpg?auth=k5JCeqOr",
                "image_license": "",
            }
        ],
        "count": 1,
        "engines_used": ["saucenao"],
    }
    out = _compact_search_result(raw, summary=True)
    row = out["results"][0]
    assert row["image_url"].startswith("https://img1.saucenao.com/")
    assert row["url"] == "https://www.pixiv.net/artworks/68259314"
    print("  ✅ compact search keeps image_url")


def test_compact_search_does_not_invent_image_fields():
    """没有图链的结果不得凭空长出空键（避免每条多 40 字节空字段）。"""
    raw = {
        "query": "q",
        "results": [{"title": "t", "url": "https://ex.com", "snippet": "s", "source": "zhihu"}],
        "count": 1,
        "engines_used": ["zhihu"],
    }
    out = _compact_search_result(raw, summary=True)
    row = out["results"][0]
    assert "image_url" not in row
    assert "image_license" not in row
    print("  ✅ compact search leaves image fields absent")


def test_resolve_topic_academic():
    prof, key = _resolve_research_profile({"query": "文献综述 CRISPR", "auto_topic": True})
    assert key == "academic"
    assert prof is not None
    assert prof["discipline"] == "academic"
    print(f"  ✅ auto topic → {key}")


def test_initialize_short_instructions_and_warm():
    from mcp_transport import ARGO_MCP_VERSION
    r = handle_rpc("initialize", {})
    assert r["serverInfo"]["version"] == ARGO_MCP_VERSION
    assert len(r["instructions"]) < 500
    assert "argo_research" in r["instructions"]
    assert "外部 skill" in r["instructions"] or "内建" in r["instructions"]
    assert "results[].url" in r["instructions"]
    assert "信源在 sources" not in r["instructions"]
    print(f"  ✅ initialize instructions len={len(r['instructions'])}")


def test_extract_respects_caller_max_chars():
    """extract 不得把 schema 默认 8000 顶成硬编码 50000。"""
    from unittest.mock import patch
    import mcp_handlers

    seen = {}

    class FakeFetch:
        @staticmethod
        def fetch_page_v3(url, max_chars=8000, timeout=8, raw=False):
            seen["max_chars"] = max_chars
            return {"url": url, "html": "<html></html>", "success": True, "error": None}

    class FakeExtract:
        extract_tables = staticmethod(lambda h: [])
        extract_metadata = staticmethod(lambda h: {})
        extract_jsonld = staticmethod(lambda h: [])

    def fake_lazy(name):
        return FakeFetch if name == "fetch_v3" else FakeExtract

    with patch.object(mcp_handlers, "_lazy_cached", fake_lazy):
        mcp_handlers.execute_tool("argo_fetch", {
            "url": "https://example.com/x", "mode": "extract", "max_chars": 1200,
        })
        assert seen["max_chars"] == 1200
        mcp_handlers.execute_tool("argo_fetch", {
            "url": "https://example.com/x", "mode": "extract",
        })
        assert seen["max_chars"] == 8000


if __name__ == "__main__":
    tests = [
        test_dumps_compact_smaller_than_pretty,
        test_compact_search_strips_heavy_fields,
        test_compact_research_keeps_gates,
        test_compact_search_keeps_image_fields,
        test_compact_search_does_not_invent_image_fields,
        test_resolve_topic_academic,
        test_initialize_short_instructions_and_warm,
        test_extract_respects_caller_max_chars,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:
            print(f"  ❌ {fn.__name__}: {e}")
            failed += 1
    print("=" * 40)
    print(f"结果: {len(tests)-failed}/{len(tests)} 通过, {failed} 失败")
    sys.exit(0 if failed == 0 else 1)

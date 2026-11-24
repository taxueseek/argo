#!/usr/bin/env python3
"""tests/test_plan1_unified_cli_and_fetch.py — 优化版方案一契约门禁。

覆盖三项核心能力：
  1. fetch_dispatch 统一 URL 自动分发（wechat / tweet / paper / pdf + 失败优雅回落）；
  2. bin/argo 分层 --help 默认视图瘦身与 --help --all 全量展开（100% 保留工作流）；
  3. 保留工作流管道衔接：`argo fetch --json | argo note study` 与 `argo search --agent | argo note study`。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
BIN_ARGO = ROOT / "bin" / "argo"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fetch_dispatch  # noqa: E402
import research_notes  # noqa: E402


# ── 1. URL 分类与专用提取分发 ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mp.weixin.qq.com/s/abc123", "wechat"),
        ("https://mp.weixin.qq.com/s?__biz=123", "wechat"),
        ("https://x.com/OpenAI/status/1800000000000000000", "tweet"),
        ("https://twitter.com/karpathy/status/1800000000000000001", "tweet"),
        ("https://arxiv.org/abs/1706.03762", "paper"),
        ("https://arxiv.org/html/2401.00001v1", "paper"),
        ("https://arxiv.org/pdf/1706.03762", "pdf"),
        ("https://example.org/docs/report.pdf", "pdf"),
        ("https://docs.python.org/3/library/asyncio.html", None),
    ],
)
def test_classify_specialized_url(url: str, expected: str | None):
    assert fetch_dispatch.classify_specialized_url(url) == expected


def test_dispatch_paper_and_pdf_and_fallback(monkeypatch):
    # paper 正常分发
    import paper as paper_mod
    monkeypatch.setattr(
        paper_mod,
        "fetch_meta",
        lambda aid: {
            "arxiv_id": aid,
            "title": "Attention Is All You Need",
            "authors": ["Vaswani", "Shazeer"],
            "published": "2017-06-12",
            "pdf_url": "https://arxiv.org/pdf/1706.03762",
            "summary": "The dominant sequence transduction models...",
        },
    )
    res_paper = fetch_dispatch.dispatch_specialized_fetch("https://arxiv.org/abs/1706.03762")
    assert res_paper is not None
    assert res_paper["success"] is True
    assert res_paper["dispatch_handler"] == "paper"
    assert "Attention Is All You Need" in res_paper["content"]

    # pdf 正常分发
    import pdf_extract
    monkeypatch.setattr(
        pdf_extract,
        "extract_pdf",
        lambda _url: {"success": True, "title": "Spec", "content": "PDF body text", "pages": 3},
    )
    res_pdf = fetch_dispatch.dispatch_specialized_fetch("https://example.org/spec.pdf")
    assert res_pdf is not None
    assert res_pdf["success"] is True
    assert res_pdf["dispatch_handler"] == "pdf"
    assert res_pdf["content"] == "PDF body text"

    # pdf 提取失败必须**显式失败**，不返回 None（2026-11-24 契约修正）。
    #
    # 旧契约是「失败返回 None，交由 fetch_v3 常规降级链兜底」。实测那条回落
    # 路径正是假成功来源：PDF 二进制被当 HTML 抓回 → 正文抽取为空 → 各抓取层
    # 只按 HTTP 状态码报 success → 调用方拿到 `success=True, length=0`。
    # 依赖缺失（pdfplumber/PyMuPDF 属可选 extras）与 URL 抓取失败都在此列。
    monkeypatch.setattr(pdf_extract, "extract_pdf",
                        lambda _url: {"success": False, "error": "boom"})
    res_bad = fetch_dispatch.dispatch_specialized_fetch("https://example.org/bad.pdf")
    assert res_bad is not None
    assert res_bad["success"] is False
    assert res_bad["dispatch_handler"] == "pdf"
    assert res_bad["error"] == "boom"

    # PDF 提取成功但正文为空（如扫描件）同样显式失败，不回落通用 HTML 链
    monkeypatch.setattr(pdf_extract, "extract_pdf",
                        lambda _url: {"success": True, "title": "Scan", "content": "   "})
    res_empty = fetch_dispatch.dispatch_specialized_fetch("https://example.org/scan.pdf")
    assert res_empty is not None and res_empty["success"] is False
    assert "为空" in res_empty["error"]

    # PDF 提取抛出异常同样显式失败（不返回 None 落回 HTML 链空跑）
    def _boom(_url):
        raise RuntimeError("corrupt pdf header")

    monkeypatch.setattr(pdf_extract, "extract_pdf", _boom)
    res_exc = fetch_dispatch.dispatch_specialized_fetch("https://example.org/broken.pdf")
    assert res_exc is not None and res_exc["success"] is False
    assert res_exc["dispatch_handler"] == "pdf"
    assert "RuntimeError" in res_exc["error"] and "corrupt pdf header" in res_exc["error"]

    # 普通网页直接返回 None
    assert fetch_dispatch.dispatch_specialized_fetch("https://example.org/index.html") is None


def test_guard_empty_unknown_result_lives_in_fetch_quality():
    import fetch_quality

    # 成功 + 空正文 + unknown → 翻为显式失败
    bad = fetch_quality.guard_empty_unknown_result({
        "success": True, "content": "  ", "page_type": "unknown",
    })
    assert bad["success"] is False and "正文为空" in bad["error"]

    # page_type 已识别（article）即使正文为空也不强翻
    typed = fetch_quality.guard_empty_unknown_result({
        "success": True, "content": "", "page_type": "article",
    })
    assert typed["success"] is True

    # 正常有正文的结果保持原样
    ok = fetch_quality.guard_empty_unknown_result({
        "success": True, "content": "hello", "page_type": "unknown",
    })
    assert ok["success"] is True


# ── 2. bin/argo 分层 --help 视图 ──────────────────────────────────────────────

def test_bin_argo_layered_help_and_all_flag():
    r_default = subprocess.run(
        [sys.executable, str(BIN_ARGO), "--help"],
        capture_output=True, text=True, check=True,
    )
    r_all = subprocess.run(
        [sys.executable, str(BIN_ARGO), "--help", "--all"],
        capture_output=True, text=True, check=True,
    )
    out_def = r_default.stdout
    out_all = r_all.stdout
    # 默认视图显著更精简，但保留核心原语与工作流索引
    assert len(out_def) < len(out_all) * 0.65, (
        f"默认 --help ({len(out_def)} chars) 应显著瘦于 --help --all ({len(out_all)} chars)"
    )
    assert "核心原语" in out_def
    assert "保留工作流" in out_def
    assert "argo --help --all" in out_def
    # --all 视图完整保留全部工作流与运维命令
    for cmd in ("argo note study", "argo watch", "argo job", "argo local-image", "argo daemon"):
        assert cmd in out_all


# ── 3. 工作流管道衔接：fetch/search JSON → argo note study ────────────────────

def test_note_study_accepts_fetch_and_search_json_via_stdin(monkeypatch, tmp_path, capsys):
    # 3a. fetch --json 管道直传
    fetch_payload = json.dumps({
        "url": "https://example.org/post-1",
        "title": "Deep Dive Post",
        "content": "Full extracted body content.",
        "fetch_method": "http",
        "success": True,
    })
    monkeypatch.setattr(sys, "stdin", io.StringIO(fetch_payload))
    rc = research_notes.main(["study", "--root", str(tmp_path)])
    assert rc == 0
    res1 = json.loads(capsys.readouterr().out)
    note_text1 = Path(res1["note"]).read_text(encoding="utf-8")
    assert "Deep Dive Post" in note_text1
    assert "Full extracted body content." in note_text1

    # 3b. search --agent 管道直传（自动取 Top 1 并将其他结果填入 related）
    search_payload = json.dumps({
        "query": "python asyncio",
        "results": [
            {"title": "Asyncio Docs", "url": "https://docs.python.org/3/library/asyncio.html", "snippet": "Official docs"},
            {"title": "PEP 3156", "url": "https://peps.python.org/pep-3156/", "snippet": "PEP spec"},
        ],
    })
    monkeypatch.setattr(sys, "stdin", io.StringIO(search_payload))
    rc2 = research_notes.main(["study", "--root", str(tmp_path)])
    assert rc2 == 0
    res2 = json.loads(capsys.readouterr().out)
    note_text2 = Path(res2["note"]).read_text(encoding="utf-8")
    assert "Asyncio Docs" in note_text2
    assert "https://peps.python.org/pep-3156/" in note_text2

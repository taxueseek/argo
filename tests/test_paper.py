#!/usr/bin/env python3
"""tests/test_paper.py — P1 论文深读三能力（分节/LaTeX 源/引文图）的离线门禁。

全部离线：PDF 用 fitz 现造、tar.gz 用 tarfile 现造、HTTP 用 monkeypatch 假响应。
覆盖：
  - chunk_text 分页契约（start/next_start/is_truncated）
  - build_sections：toc 路 + 无目录页级兜底路，偏移单调、end 闭合
  - read_section 切片与不存在 id
  - parse_eprint：主 tex 判定 / 路径穿越成员拒绝 / 单文件 gz / 成员数上限 / PDF 误传
  - _guard_xml：DOCTYPE/ENTITY 拒绝；fetch_meta 正常解析（假响应）
  - fetch_citations：429 fail-fast（不重试）+ 正常计数
  - extract_pdf / fetch_eprint 的 fetch 缓存二读零下载
"""
from __future__ import annotations

import gzip
import io
import json
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import pdf_extract  # noqa: E402
import arxiv_source  # noqa: E402
import paper  # noqa: E402


# ── chunk_text 分页契约 ──────────────────────────────────────────────────────

def test_chunk_text_math():
    text = "abcdefghij" * 3  # 30 字符
    c1 = pdf_extract.chunk_text(text, 0, 12)
    assert c1["content"] == "abcdefghijab" and c1["next_start"] == 12 and c1["is_truncated"]
    c2 = pdf_extract.chunk_text(text, 12, 12)
    assert c2["content"] == "cdefghijabcd" and c2["next_start"] == 24
    c3 = pdf_extract.chunk_text(text, 24, 12)
    assert c3["content"] == "efghij" and c3["next_start"] is None and not c3["is_truncated"]
    over = pdf_extract.chunk_text(text, 999, 12)
    assert over["content"] == "" and over["next_start"] is None


# ── build_sections / read_section ────────────────────────────────────────────

def _mk_result(toc: list | None, pages: dict[int, str]) -> dict:
    """构造提取结果：content 按 pdf_extract 的页标记拼接。"""
    parts, toc_list = [], []
    for n in sorted(pages):
        parts.append(f"--- Page {n} ---\n\n{pages[n]}")
    for level, title, page in (toc or []):
        toc_list.append({"level": level, "title": title, "page": page})
    return {"content": "\n\n".join(parts), "toc": toc_list,
            "page_count": max(pages) if pages else 0}


def test_build_sections_from_toc():
    res = _mk_result([(1, "Abstract", 1), (1, "Method", 2)],
                     {1: "Abstract\nwe propose", 2: "Method\nwe use x", 3: "appendix"})
    secs = pdf_extract.build_sections(res)
    assert [s["id"] for s in secs] == ["1", "2"]
    assert secs[0]["title"] == "Abstract" and secs[1]["title"] == "Method"
    starts = [s["start"] for s in secs]
    assert starts == sorted(starts), "分节 start 必须单调"
    assert secs[0]["end"] == secs[1]["start"], "上一节 end 闭合到下一节 start"
    assert secs[1]["end"] == len(res["content"])
    body = res["content"][secs[1]["start"]:secs[1]["end"]]
    assert "Method" in body and "we use x" in body


def test_build_sections_page_fallback_and_read():
    res = _mk_result(None, {1: "alpha text", 2: "beta text"})
    res["sections"] = pdf_extract.build_sections(res)
    assert [s["title"] for s in res["sections"]] == ["Page 1", "Page 2"]
    sec = pdf_extract.read_section(res, "2")
    assert sec and "beta text" in sec["content"]
    assert pdf_extract.read_section(res, "99") is None


# ── parse_eprint：主文件判定 / 恶意成员 / 上限 ────────────────────────────────

def _mk_tar(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_parse_eprint_main_and_traversal():
    body = _mk_tar({
        "utils.tex": b"\\usepackage{x}",
        "main.tex": b"\\documentclass{article}\nhello",
        "refs.bbl": b"bibitem...",
        "../evil.tex": b"evil",
        "/abs.tex": b"abs",
    })
    out = arxiv_source.parse_eprint(body)
    assert out["main_file"] == "main.tex", "主 tex 应按 \\documentclass 判定"
    names = {f["name"] for f in out["files"]}
    assert "main.tex" in names and "refs.bbl" in names
    assert out["texts"]["main.tex"].startswith("\\documentclass")
    assert not any("evil" in n or "abs.tex" in n for n in names)
    assert any("evil" in r for r in out["rejected"])


def test_parse_eprint_single_gz():
    body = gzip.compress(b"\\documentclass{article}\nold style")
    out = arxiv_source.parse_eprint(body)
    assert out["format"] == "gz" and out["main_file"] == "main.tex"
    assert "old style" in out["texts"]["main.tex"]


def test_parse_eprint_member_cap():
    body = _mk_tar({f"f{i}.tex": b"x" for i in range(5)})
    with pytest.raises(arxiv_source.EprintError):
        arxiv_source.parse_eprint(body, max_members=2)


def test_parse_eprint_rejects_pdf():
    with pytest.raises(arxiv_source.EprintError):
        arxiv_source.parse_eprint(b"%PDF-1.7 fake")


def test_latex_read_unknown_file():
    src = {"texts": {"main.tex": "abc"}, "main_file": "main.tex"}
    with pytest.raises(arxiv_source.EprintError):
        arxiv_source.latex_read(src, "nope.tex")


# ── _guard_xml / fetch_meta（假响应） ────────────────────────────────────────

_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:ar="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <title>  Attention Is
    All You Need </title>
    <summary>The dominant sequence transduction models...</summary>
    <author><name>Ashish Vaswani</name></author>
    <published>2017-06-12T17:57:34Z</published>
    <updated>2023-08-02T16:51:31Z</updated>
    <category term="cs.CL"/>
    <ar:primary_category term="cs.CL"/>
    <link href="https://arxiv.org/pdf/1706.03762v7" type="application/pdf" rel="related"/>
  </entry>
</feed>"""


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, n: int = -1) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_guard_xml_rejects_doctype():
    evil = _ATOM.replace('<?xml version="1.0" encoding="UTF-8"?>',
                         '<!DOCTYPE feed [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>')
    with pytest.raises(ValueError):
        paper._guard_xml(evil)


def test_fetch_meta_parses_atom(monkeypatch):
    monkeypatch.setattr(paper, "open_url",
                        lambda req, timeout=20: _FakeResp(_ATOM.encode()))
    meta = paper.fetch_meta("1706.03762")
    assert meta["title"] == "Attention Is All You Need"
    assert meta["authors"] == ["Ashish Vaswani"]
    assert meta["primary_category"] == "cs.CL"
    assert meta["pdf_url"].endswith("/pdf/1706.03762v7")


# ── fetch_citations：429 fail-fast + 正常计数 ────────────────────────────────

def test_citations_429_fail_fast(monkeypatch):
    calls = {"n": 0}

    def fake_open(req, timeout=15):
        calls["n"] += 1
        raise urllib.error.HTTPError("https://api.semanticscholar.org/x", 429,
                                     "Too Many Requests", hdrs=None, fp=None)

    monkeypatch.setattr(paper, "open_url", fake_open)
    out = paper.fetch_citations("1706.03762", limit=3)
    assert out["status"] == "rate_limited" and out["hint"]
    assert calls["n"] == 1, "429 必须 fail-fast，不重试（P0 实测 57s 挂着的教训）"


def test_citations_counts(monkeypatch):
    def fake_open(req, timeout=15):
        url = req.full_url
        if "/citations?" in url:
            payload = {"data": [{"citingPaper": {"title": "Cited work", "year": 2018,
                                                 "paperId": "abc"}}]}
        else:
            payload = {"title": "Attention Is All You Need", "citationCount": 90000,
                       "referenceCount": 47}
        return _FakeResp(json.dumps(payload).encode())

    monkeypatch.setattr(paper, "open_url", fake_open)
    out = paper.fetch_citations("1706.03762", limit=5)
    assert out["citation_count"] == 90000
    assert out["returned"] == 1 and out["citations"][0]["title"] == "Cited work"


def test_normalize_id():
    assert paper.normalize_arxiv_id("1706.03762") == "1706.03762"
    assert paper.normalize_arxiv_id("1706.03762v7") == "1706.03762v7"
    assert paper.normalize_arxiv_id("https://arxiv.org/abs/1706.03762v7") == "1706.03762v7"
    assert paper.normalize_arxiv_id("https://arxiv.org/pdf/1706.03762") == "1706.03762"
    assert paper.normalize_arxiv_id("cs/0301012") == "cs/0301012"
    with pytest.raises(ValueError):
        paper.normalize_arxiv_id("not an arxiv id at all!")


# ── 缓存二读零下载（pdf 提取 + e-print 拉取） ────────────────────────────────

def test_extract_pdf_cache_reuse(monkeypatch):
    """缓存层测试：提取器换成假实现（不依赖本机 PDF 库），纯验「二读零下载」。"""
    fake_result = {"content": "--- Page 1 ---\n\nbody text", "title": "T",
                   "page_count": 1, "toc": [], "tables": [], "metadata": {},
                   "quality_score": 1.0, "content_ok": True}
    calls = {"n": 0}

    def fake_extract(body, pages, password):
        return dict(fake_result)

    def fake_open(url, timeout=30):
        calls["n"] += 1
        return _FakeResp(b"%PDF-1.7 minimal")

    monkeypatch.setattr(pdf_extract, "_extract_with_pdfplumber", fake_extract)
    monkeypatch.setattr(pdf_extract, "open_url", fake_open)
    # uuid 键：首调必须冷启动，不依赖会话缓存为空（防顺序依赖）
    url = f"https://arxiv.org/pdf/{__import__('uuid').uuid4().hex[:12]}"
    r1 = pdf_extract.extract_pdf(url)
    assert calls["n"] == 1 and r1.get("content_ok"), f"r1={str(r1)[:200]} calls={calls['n']}"
    r2 = pdf_extract.extract_pdf(url)
    assert calls["n"] == 1, "第二次应命中 fetch 缓存，不再下载"
    assert r2.get("_cached") and r2["sections"], "缓存命中要带回分节大纲"


def test_fetch_eprint_cache_reuse(monkeypatch):
    body = _mk_tar({"main.tex": b"\\documentclass{article}\nbody"})
    calls = {"n": 0}

    def fake_open(url, timeout=60):
        calls["n"] += 1
        return _FakeResp(body)

    monkeypatch.setattr(arxiv_source, "open_url", fake_open)
    aid = f"9999.{__import__('uuid').uuid4().hex[:5]}"
    s1 = arxiv_source.fetch_eprint(aid)
    assert calls["n"] == 1 and s1["main_file"] == "main.tex"
    s2 = arxiv_source.fetch_eprint(aid)
    assert calls["n"] == 1 and s2.get("_cached")

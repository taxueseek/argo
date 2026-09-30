"""argo paper — arXiv 论文深读编排（P1 论文深读入口）。

统一三个深读能力的入口（2026-09-30 P0 实测后立项，原创实现、不引外部依赖）：
  --outline / --section   PDF 分节阅读（pdf_extract 的 outline/section + 提取缓存）
  --latex / --list        arXiv e-print LaTeX 源精读（arxiv_source）
  --cited-by              Semantic Scholar 引文图（key 轨 + 失败分型，fail-fast）

安全边界：URL 只打 arxiv.org / export.arxiv.org / api.semanticscholar.org 的
https 端点；arXiv Atom XML 解析前拒绝 DOCTYPE/ENTITY；密钥只从 env 轨读取，
永不回吐。本地数据纪律不变：缓存的是摘要/结构/行号偏移，不建全文库。
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from cli_io import dumps
from net_proxy import open_url
from engine_env import get_env
from pdf_extract import CONTENT_WARNING, DEFAULT_MAX_CHARS

_TIMEOUT_META = 20
_TIMEOUT_S2 = 15
_S2_PAPER = "https://api.semanticscholar.org/graph/v1/paper/arXiv:{id}"
_S2_FIELDS_COUNT = "title,year,citationCount,referenceCount,externalIds"
_S2_FIELDS_LIST = "title,year,externalIds"

_ID_RE = re.compile(r"(?:([a-z\-]+)/(\d{7})|(\d{4}\.\d{4,5}))(v\d+)?$", re.I)


def normalize_arxiv_id(raw: str) -> str:
    """接受 1706.03762 / 1706.03762v7 / cs/0301012 / arxiv.org/(abs|pdf)/... 形态。"""
    s = (raw or "").strip()
    m = re.match(r"https?://(?:export\.|www\.)?arxiv\.org/(?:abs|pdf)/([^\s?#]+)", s, re.I)
    if m:
        s = m.group(1)
    s = s.rstrip("/")
    if not _ID_RE.search(s):
        raise ValueError(f"arXiv id 形态不对：{raw[:60]}（例：1706.03762 或 cs/0301012）")
    return s


def _guard_xml(text: str) -> str:
    """解析前拒绝 DOCTYPE/ENTITY（外部 XML 不开外部实体，fail-closed）。"""
    head = text[:4096].lower()
    if "<!doctype" in head or "<!entity" in head:
        raise ValueError("XML 含 DOCTYPE/ENTITY 声明，已拒绝解析")
    return text


def fetch_meta(arxiv_id: str) -> dict[str, Any]:
    """arXiv Atom API 取单篇元数据（标题/作者/摘要/日期/分类/PDF 链接）。"""
    url = (f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
           f"&max_results=1")
    req = urllib.request.Request(url, headers={"User-Agent": "argo-paper/1.0"})
    with open_url(req, timeout=_TIMEOUT_META) as resp:
        text = resp.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
    root = ET.fromstring(_guard_xml(text))
    ns = {"a": "http://www.w3.org/2005/Atom",
          "ar": "http://arxiv.org/schemas/atom"}
    entry = root.find("a:entry", ns)
    if entry is None:
        return {"error": f"arXiv 无此 id 的条目：{arxiv_id}"}
    def _t(path: str) -> str:
        el = entry.find(path, ns)
        return re.sub(r"\s+", " ", (el.text or "").strip()) if el is not None and el.text else ""
    pdf_url = ""
    for link in entry.findall("a:link", ns):
        if link.get("type") == "application/pdf":
            pdf_url = link.get("href") or ""
            break
    categories = [c.get("term") for c in entry.findall("a:category", ns) if c.get("term")]
    return {
        "id": arxiv_id,
        "title": _t("a:title"),
        "authors": [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)],
        "abstract": _t("a:summary"),
        "published": _t("a:published")[:10],
        "updated": _t("a:updated")[:10],
        "primary_category": (entry.find("ar:primary_category", ns).get("term")
                              if entry.find("ar:primary_category", ns) is not None
                              else (categories[0] if categories else "")),
        "categories": categories,
        "pdf_url": pdf_url,
    }


def fetch_citations(arxiv_id: str, limit: int = 0) -> dict[str, Any]:
    """Semantic Scholar 引文图（key 轨 + fail-fast：429 不重试，直接给 hint）。

    P0 实测教训：免认证档会被限流晾 57s——这里超时 15s、无重试、429 即返回
    可执行的提示，把「等」换成「告诉用户怎么办」。
    """
    key = ""
    try:
        key = (get_env("SEMANTIC_SCHOLAR_API_KEY", "") or "").strip()
    except Exception:
        key = ""
    headers = {"User-Agent": "argo-paper/1.0"}
    if key:
        headers["x-api-key"] = key
    out: dict[str, Any] = {"provider": "semantic_scholar", "keyed": bool(key)}

    def _get(url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=headers)
        try:
            with open_url(req, timeout=_TIMEOUT_S2) as resp:
                return json_loads(resp.read(4 * 1024 * 1024))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return {"status": "rate_limited",
                        "hint": "export SEMANTIC_SCHOLAR_API_KEY=<key> 后重试，或稍后再试"}
            return {"error": f"HTTP {e.code}"}
        except Exception as e:
            return {"error": str(e)[:150]}

    paper = _get(_S2_PAPER.format(id=arxiv_id) + f"?fields={_S2_FIELDS_COUNT}")
    if paper.get("status") == "rate_limited" or paper.get("error"):
        return {**out, **paper}
    out["title"] = paper.get("title")
    out["citation_count"] = paper.get("citationCount")
    out["reference_count"] = paper.get("referenceCount")
    if limit > 0:
        cites = _get(_S2_PAPER.format(id=arxiv_id)
                     + f"/citations?fields={_S2_FIELDS_LIST}&limit={min(limit, 999)}")
        if not cites.get("error") and cites.get("status") != "rate_limited":
            rows = []
            for c in (cites.get("data") or [])[:limit]:
                p = (c or {}).get("citingPaper") or {}
                rows.append({"title": p.get("title"), "year": p.get("year"),
                             "paperId": p.get("paperId")})
            out["citations"] = rows
            out["returned"] = len(rows)
        else:
            out["citations_error"] = (cites.get("hint") or cites.get("error")
                                      or "citations 列表获取失败")
    return out


def json_loads(raw: bytes) -> dict[str, Any]:
    import json
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {"data": data}
    except ValueError as e:
        return {"error": f"JSON 解析失败: {e}"}


def main() -> int:
    p = argparse.ArgumentParser(
        description="Argo paper — arXiv 论文深读（元数据/分节/LaTeX/引文图）")
    p.add_argument("paper", help="arXiv id 或 URL（1706.03762 / arxiv.org/abs/...）")
    p.add_argument("--outline", action="store_true", help="PDF 分节大纲")
    p.add_argument("--section", default=None, metavar="ID", help="按大纲 id 读单节")
    p.add_argument("--latex", action="store_true", help="LaTeX 源（主 tex，分页）")
    p.add_argument("--latex-file", default=None, metavar="NAME", help="读指定源文件")
    p.add_argument("--list", action="store_true", help="LaTeX 源文件清单")
    p.add_argument("--cited-by", nargs="?", const=10, type=int, default=None,
                   metavar="N", help="引文图（默认列 10 条，--cited-by 0 只看计数）")
    p.add_argument("--pages", default=None, help="PDF 页码范围（供 --outline/--section）")
    p.add_argument("--start", type=int, default=0, help="分页起始偏移")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="分页块大小")
    p.add_argument("--no-cache", action="store_true", help="跳过提取/源码缓存")
    p.add_argument("--json", action="store_true", help="JSON 输出（默认人类可读）")
    args = p.parse_args()

    out: dict[str, Any] = {"content_warning": CONTENT_WARNING}
    try:
        aid = normalize_arxiv_id(args.paper)
    except ValueError as e:
        print(dumps({"error": str(e)}))
        return 1
    out["paper_id"] = aid
    failed = False

    meta = fetch_meta(aid)
    if meta.get("error"):
        out["meta_error"] = meta["error"]
        failed = True
    else:
        out["meta"] = meta

    def _pdf() -> dict[str, Any]:
        from pdf_extract import extract_pdf
        return extract_pdf(f"https://arxiv.org/pdf/{aid}", pages=args.pages,
                           use_cache=not args.no_cache)

    if not failed:
        if args.outline or args.section:
            pr = _pdf()
            if pr.get("error"):
                out["outline_error"] = pr["error"]
            elif args.section:
                from pdf_extract import read_section, chunk_text
                sec = read_section(pr, args.section)
                if sec is None:
                    out["section_error"] = f"分节 {args.section} 不存在（先 --outline）"
                else:
                    seg = chunk_text(sec["content"], args.start, args.max_chars)
                    out["section"] = {k: sec[k] for k in ("id", "level", "title")}
                    out.update(seg)
            else:
                out["total_sections"] = len(pr.get("sections") or [])
                out["sections"] = pr.get("sections") or []
        if args.list or args.latex or args.latex_file:
            import arxiv_source
            try:
                src = arxiv_source.fetch_eprint(aid, use_cache=not args.no_cache)
                if args.list:
                    out["latex_files"] = {"main_file": src.get("main_file"),
                                          "format": src.get("format"),
                                          "files": src.get("files"),
                                          "rejected": src.get("rejected")}
                else:
                    out.update(arxiv_source.latex_read(
                        src, args.latex_file, args.start, args.max_chars))
            except arxiv_source.EprintError as e:
                out["latex_error"] = str(e)
        if args.cited_by is not None:
            out["citations"] = fetch_citations(aid, args.cited_by)

    if args.json or failed:
        print(dumps(out))
        return 1 if failed and not out.get("meta") else 0
    _print_human(out, args)
    return 0


def _print_human(out: dict[str, Any], args: argparse.Namespace) -> None:
    meta = out.get("meta") or {}
    if meta.get("title"):
        print(f"# {meta['title']}")
        if meta.get("authors"):
            print("> " + " · ".join(meta["authors"][:8])
                  + (" 等" if len(meta["authors"]) > 8 else ""))
        if meta.get("abstract"):
            print("\n" + meta["abstract"][:600])
        if meta.get("pdf_url"):
            print(f"\nPDF: {meta['pdf_url']}")
    if out.get("sections"):
        print(f"\n## 大纲（{out.get('total_sections')} 节）")
        for s in out["sections"]:
            print(f"  [{s['id']}] {'  ' * (s['level'] - 1)}{s['title']}")
    if out.get("section") and "content" in out:
        s = out["section"]
        print(f"\n## [{s['id']}] {s['title']}\n")
        print(out["content"])
    if out.get("file"):
        print(f"\n## LaTeX: {out['file']}（{out.get('content_length')} 字符）\n")
        print(out["content"])
    if out.get("latex_files"):
        lf = out["latex_files"]
        print(f"\n## LaTeX 源（{lf.get('format')}，主文件 {lf.get('main_file')}）")
        for f in (lf.get("files") or [])[:20]:
            print(f"  {f['name']}  {f['size']}B")
    c = out.get("citations") or {}
    if c.get("citation_count") is not None:
        print(f"\n## 引文（被引 {c.get('citation_count')} / 参考文献 {c.get('reference_count')}）")
        for r in (c.get("citations") or [])[:10]:
            print(f"  - ({r.get('year')}) {(r.get('title') or '')[:90]}")
    if c.get("hint"):
        print(f"  ⚠️ {c['hint']}")
    for k in ("meta_error", "outline_error", "section_error", "latex_error"):
        if out.get(k):
            print(f"⚠️ {k}: {out[k]}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

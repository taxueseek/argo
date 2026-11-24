#!/usr/bin/env python3
"""fetch_dispatch.py — argo fetch 统一 URL 专用提取器自动分发层。

让调用方只需记住 `argo fetch <url>`，由本模块按 URL 特征自动路由至对应专有提取器：
  - wechat : mp.weixin.qq.com/s...         → wechat_article.fetch_wechat_article
  - tweet  : x.com|twitter.com/*/status/*  → tweet.build_bundle + _render_text
  - paper  : arxiv.org/abs|html/<id>       → paper.fetch_meta（结构化摘要与元数据）
  - pdf    : *.pdf 或 arxiv.org/pdf/<id>   → pdf_extract.extract_pdf

回落原则：
  - 微信链接保持既有契约（专用链三通道全败即返回 FETCH_WECHAT_FAILED，不落回通用 JS 骨架）；
  - tweet / paper / pdf 专用提取失败时返回 None，交由 fetch_v3 常规多级降级链兜底。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import engine_states

_TWEET_URL_RE = re.compile(
    r"^https?://(?:(?:www|mobile)\.)?(?:x\.com|twitter\.com)/[^/]+/status/\d+",
    re.IGNORECASE,
)
_ARXIV_ABS_RE = re.compile(
    r"^https?://(?:www\.)?arxiv\.org/(?:abs|html)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)",
    re.IGNORECASE,
)
_ARXIV_PDF_RE = re.compile(
    r"^https?://(?:www\.)?arxiv\.org/pdf/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)(?:\.pdf)?",
    re.IGNORECASE,
)


def classify_specialized_url(url: str) -> str | None:
    """识别适合走专有提取器的 URL 类型；普通网页返回 None。"""
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    try:
        parsed = urlparse(u)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host == "mp.weixin.qq.com" and (path == "/s" or path.startswith("/s/")):
        return "wechat"
    if _TWEET_URL_RE.match(u):
        return "tweet"
    if _ARXIV_PDF_RE.match(u) or path.lower().endswith(".pdf"):
        return "pdf"
    if _ARXIV_ABS_RE.match(u):
        return "paper"
    return None


def _dispatch_wechat(url: str, max_chars: int) -> dict[str, Any]:
    try:
        from wechat_article import fetch_wechat_article
        wx = fetch_wechat_article(url)
    except Exception as exc:
        wx = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if wx and wx.get("ok"):
        content = (wx.get("content") or "")[:max_chars]
        return {
            "url": url,
            "title": wx.get("title") or "",
            "content": content,
            "html": "",
            "length": int(wx.get("char_count") or len(content)),
            "success": True,
            "fetch_method": engine_states.FETCH_WECHAT + str(
                wx.get("fetch_method") or engine_states.FETCH_HTTP_DIRECT
            ),
            "author": wx.get("author") or "",
            "publish_time": wx.get("publish_time") or "",
            "dispatch_handler": "wechat",
        }
    return {
        "url": url,
        "title": "",
        "content": "",
        "html": "",
        "length": 0,
        "success": False,
        "error": (wx or {}).get("error")
        or "微信公众号文章抓取失败（HTTP/浏览器/搜狗三通道均未取到正文）",
        "fetch_method": engine_states.FETCH_WECHAT_FAILED,
        "dispatch_handler": "wechat",
    }


def _dispatch_tweet(url: str, max_chars: int, timeout: float) -> dict[str, Any] | None:
    try:
        import tweet as tweet_mod
        status_id, bundle = tweet_mod.build_bundle(
            url, mode="status", timeout=max(5, int(timeout))
        )
        if not bundle or not bundle.get("main"):
            return None
        rendered = tweet_mod._render_text(status_id, "status", bundle)
        main_post = bundle.get("main") or {}
        author = tweet_mod._author_handle(main_post)
        content = (rendered or "")[:max_chars]
        if not content.strip():
            return None
        return {
            "url": url,
            "title": f"X 帖子 @{author} ({status_id})" if author else f"X 帖子 ({status_id})",
            "content": content,
            "html": "",
            "length": len(content),
            "success": True,
            "fetch_method": engine_states.FETCH_X_ARTICLE,
            "author": author,
            "dispatch_handler": "tweet",
        }
    except Exception:
        return None


def _dispatch_paper(url: str, max_chars: int) -> dict[str, Any] | None:
    m = _ARXIV_ABS_RE.match(url.strip())
    if not m:
        return None
    arxiv_id = m.group(1)
    try:
        import paper as paper_mod
        meta = paper_mod.fetch_meta(paper_mod.normalize_arxiv_id(arxiv_id))
        if not meta or not meta.get("title"):
            return None
        authors = ", ".join(meta.get("authors") or [])
        abstract = str(meta.get("abstract") or meta.get("summary") or "").strip()
        lines = [
            f"# {meta['title']}",
            f"- arXiv: {meta.get('id') or meta.get('arxiv_id') or arxiv_id}",
        ]
        if authors:
            lines.append(f"- Authors: {authors}")
        if meta.get("published"):
            lines.append(f"- Published: {meta['published']}")
        if meta.get("pdf_url"):
            lines.append(f"- PDF: {meta['pdf_url']}")
        if abstract:
            lines.extend(["", "## Abstract", abstract])
        content = "\n".join(lines)[:max_chars]
        return {
            "url": url,
            "title": str(meta["title"]),
            "content": content,
            "html": "",
            "length": len(content),
            "success": True,
            "fetch_method": engine_states.FETCH_HTTP,
            "author": authors,
            "publish_time": str(meta.get("published") or ""),
            "dispatch_handler": "paper",
        }
    except Exception:
        return None


def _dispatch_pdf(url: str, max_chars: int) -> dict[str, Any] | None:
    try:
        from pdf_extract import extract_pdf
        res = extract_pdf(url)
        if not res:
            return None
        # 依赖缺失是**明确失败**，不是「交给通用链」：pdfplumber / PyMuPDF
        # 属可选 extras，未装时 extract_pdf 返回 success=False + error。
        # 旧实现一律 return None，于是调用方回落到通用抓取链——那条链把 PDF
        # 二进制当 HTML 抓回、正文抽取为空，最终给出 `success=True, length=0`
        # 的假成功（2026-11-24 实测）。这里把失败显式传回去，让 fetch_v3
        # 至少能报「为什么没拿到」。
        if not res.get("success"):
            err = str(res.get("error") or "PDF 提取失败")
            return {
                "url": url,
                "title": "",
                "content": "",
                "html": "",
                "length": 0,
                "success": False,
                "error": err,
                "fetch_method": engine_states.FETCH_PDF_FAILED,
                "dispatch_handler": "pdf",
            }
        content = str(res.get("content") or "")[:max_chars]
        if not content.strip():
            return None
        return {
            "url": url,
            "title": str(res.get("title") or url.rsplit("/", 1)[-1]),
            "content": content,
            "html": "",
            "length": len(content),
            "success": True,
            "fetch_method": engine_states.FETCH_HTTP,
            "pages": res.get("pages"),
            "dispatch_handler": "pdf",
        }
    except Exception:
        return None


def dispatch_specialized_fetch(
    url: str,
    *,
    max_chars: int = 8000,
    timeout: float = 15.0,
) -> dict[str, Any] | None:
    """按 URL 特征自动分发至专有提取器；非专用链接或可回落失败时返回 None。"""
    kind = classify_specialized_url(url)
    if kind is None:
        return None
    if kind == "wechat":
        return _dispatch_wechat(url, max_chars)
    if kind == "tweet":
        return _dispatch_tweet(url, max_chars, timeout)
    if kind == "paper":
        return _dispatch_paper(url, max_chars)
    if kind == "pdf":
        return _dispatch_pdf(url, max_chars)
    return None

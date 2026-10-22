#!/usr/bin/env python3
"""parsers.py — local-search 多源响应解析器（HTML / RSS / XML / JSON）。

拆分自 search_v3.py：负责选择器匹配、启发式链接回退（_fallback_extract）、
XML 命名空间解析与 JSON 字典路径提取。无网络 IO。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any


def _select_with_bs4(soup: Any, selector: str):
    try:
        return soup.select(selector)
    except Exception:
        return []


def _select_first_with_bs4(soup: Any, selector: str) -> Any:
    items = _select_with_bs4(soup, selector)
    return items[0] if items else None


def _fallback_extract(html: str, engine_name: str, base: str,
                      default_score: float = 0.45, max_items: int = 8) -> list[dict[str, Any]]:
    """通用解析保底：选择器 0 命中时，用链接 + 锚文本启发式回退。"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    for sel in ("nav", "header", "footer", "script", "style", "noscript"):
        try:
            for tag in soup.select(sel):
                tag.decompose()
        except Exception:
            pass
    candidates: list[tuple[str, str, str]] = []
    for a in soup.find_all("a", href=True):
        try:
            href = str(a.get("href") or "").strip()
            title = a.get_text(strip=True)
        except Exception:
            continue
        if not title or len(title) < 6:
            continue
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        low = href.lower()
        if any(x in low for x in ("login", "signin", "signup", "cookie", "privacy", "terms")):
            continue
        parent = a.parent
        snippet = ""
        try:
            if parent is not None:
                snippet = parent.get_text(" ", strip=True)[:300]
                if snippet == title:
                    gp = parent.parent
                    snippet = gp.get_text(" ", strip=True)[:300] if gp is not None else ""
        except Exception:
            snippet = ""
        if href.startswith("/"):
            try:
                href = urllib.parse.urljoin(base, href)
            except Exception:
                pass
        candidates.append((title[:200], href[:500], snippet))
        if len(candidates) >= max_items * 3:
            break
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for title, url, snippet in candidates:
        key = url or title
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({
            "title": title,
            "url": url,
            "snippet": snippet,
            "score": round(max(default_score - len(out) * 0.05, 0.1), 3),
            "source": engine_name,
            "_fallback": True,
        })
        if len(out) >= max_items:
            break
    return out


def _parse_html(engine_name: str, html: str, spec: dict[str, Any],
                maps: dict[str, Any]) -> list[dict[str, Any]]:
    """HTML 解析映射：选择器容器抽取与属性提取。"""
    html_maps = maps.get("html", {})
    mapping = html_maps.get(engine_name, html_maps.get("default", {}))
    container_sel = mapping.get("container")
    title_sel = mapping.get("title")
    url_sel = mapping.get("url")
    snippet_sel = mapping.get("snippet")
    url_attr = mapping.get("url_attr", "href")
    default_score = mapping.get("score", 0.5)

    if not container_sel:
        return []

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        containers = _select_with_bs4(soup, container_sel)
    except Exception:
        return []

    results: list[dict[str, Any]] = []
    base = spec.get("_base", spec.get("url", ""))
    for idx, item in enumerate(containers):
        try:
            title_el = _select_first_with_bs4(item, title_sel) if title_sel else None
            url_el = _select_first_with_bs4(item, url_sel) if url_sel else None
            snippet_el = _select_first_with_bs4(item, snippet_sel) if snippet_sel else None

            title = title_el.get_text(strip=True)[:200] if title_el else ""
            url = ""
            if url_el and url_el.has_attr(url_attr):
                url = url_el[url_attr]
            snippet = snippet_el.get_text(strip=True)[:300] if snippet_el else ""

            if not title and not url:
                continue

            if url and url.startswith("/"):
                url = urllib.parse.urljoin(base, url)

            score = max(default_score - idx * 0.05, 0.1)
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "score": round(score, 3),
                "source": engine_name,
            })
        except Exception:
            continue
    if not results:
        try:
            fb = _fallback_extract(html, engine_name, base, default_score=0.45, max_items=8)
            if fb:
                return fb
        except Exception:
            pass
    return results


def _parse_xml(engine_name: str, text: str, maps: dict[str, Any],
               is_rss: bool = False, normalize_date_fn=None) -> list[dict[str, Any]]:
    """XML / RSS 解析：支持命名空间与 pubDate 提取。"""
    if is_rss:
        mapping = maps.get("rss", {}).get("default", {})
    else:
        mapping = maps.get("xml", {}).get(engine_name, {})

    entry_path = mapping.get("entry_path") or mapping.get("item_path", ".//item")
    title_tag = mapping.get("title", "title")
    url_tag = mapping.get("url", "link")
    snippet_tag = mapping.get("snippet", "description")
    pub_tag = mapping.get("published_at", "pubDate")
    namespaces = mapping.get("namespaces", {})

    def _find_tag(entry: Any, tag: str) -> str:
        if tag.startswith("atom:"):
            tag_name = tag.split(":")[1]
            ns = namespaces.get("atom")
            node = entry.find(f"{{{ns}}}{tag_name}") if ns else None
            return (node.text or "").strip() if node is not None else ""
        node = entry.findtext(tag, default="")
        return (node or "").strip()

    results: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return results

    if entry_path.startswith(".//{"):
        entries = root.findall(entry_path, namespaces)
    else:
        entries = root.findall(entry_path)

    for idx, entry in enumerate(entries):
        try:
            title = _find_tag(entry, title_tag)
            url = _find_tag(entry, url_tag)
            snippet = _find_tag(entry, snippet_tag)
            raw_pub = _find_tag(entry, pub_tag) if pub_tag else ""

            title = re.sub(r"\s+", " ", title)[:200]
            snippet = re.sub(r"\s+", " ", snippet)[:300]
            score = max(0.7 - idx * 0.05, 0.1)
            pub_at = normalize_date_fn(raw_pub) if normalize_date_fn else raw_pub
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "score": round(score, 3),
                "source": engine_name,
                "published_at": pub_at,
            })
        except Exception:
            continue
    return results


def _get_path(data: Any, path: str) -> Any:
    """提取嵌套字典/列表路径，如 'items.0.title'。"""
    if path == ".":
        return data
    obj = data
    for part in path.split("."):
        if part.isdigit() and isinstance(obj, (list, tuple)):
            idx = int(part)
            obj = obj[idx] if idx < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _format_url(template: str | None, item: dict[str, Any], default: str = "") -> str:
    """按模板格式化 URL 字段。"""
    if not template:
        return default
    try:
        return template.format(**item)
    except (KeyError, IndexError):
        return default


def _parse_json(engine_name: str, text: str, maps: dict[str, Any],
                normalize_date_fn=None) -> list[dict[str, Any]]:
    """JSON 响应解析：提取标题、URL、摘要及时间。"""
    mapping = maps.get("json", {}).get(engine_name, {})
    items_path = mapping.get("items", ".")
    title_key = mapping.get("title")
    url_key = mapping.get("url")
    snippet_key = mapping.get("snippet")
    url_template = mapping.get("url_template")
    published_key = mapping.get("published_at")

    results: list[dict[str, Any]] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return results

    items = _get_path(data, items_path)
    if not isinstance(items, list):
        return results

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            if engine_name == "local_pubmed" and isinstance(item, str):
                item = {"pmid": item}
            else:
                continue

        title = url = snippet = ""
        if title_key:
            raw = _get_path(item, title_key)
            if isinstance(raw, list):
                raw = raw[0] if raw else ""
            title = str(raw or "")[:200]
        if url_key:
            raw = _get_path(item, url_key)
            url = str(raw or "")[:500]
        elif url_template:
            url = _format_url(url_template, item)
        if snippet_key:
            raw = _get_path(item, snippet_key)
            snippet = str(raw or "")[:300]
        raw_pub = _get_path(item, published_key) if published_key else None
        published_at = normalize_date_fn(raw_pub) if normalize_date_fn and raw_pub else raw_pub

        title = re.sub(r"<[^>]+>", " ", title)
        title = re.sub(r"\s+", " ", title).strip()
        snippet = re.sub(r"<[^>]+>", " ", snippet)
        snippet = re.sub(r"\s+", " ", snippet).strip()

        score = max(0.7 - idx * 0.05, 0.1)
        results.append({
            "title": title,
            "url": url,
            "snippet": snippet,
            "score": round(score, 3),
            "source": engine_name,
            "published_at": published_at,
        })
    return results

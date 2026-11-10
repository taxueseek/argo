#!/usr/bin/env python3
"""x_article_fetch.py — X Article 正文提取（Draft.js 重建）。

X Article 是 X 的长文功能，URL 格式 https://x.com/i/article/{id}。
FxTwitter API 对 X Article 返回 Draft.js blocks，需重建为 Markdown。
entityMap 里 MEDIA/MARKDOWN 原子块还原为带内嵌图片 Markdown。

为什么单独成模块：Draft.js 重建是纯函数逻辑，与 fetch_v3.py 的降级链
职责不同。拆出后 fetch_v3.py 只留调用点，祖父文件行数门禁可各自独立管理。
"""

from __future__ import annotations

import engine_states
import json
import os
import re
import sys
from typing import Any

_X_ARTICLE_URL_RE = re.compile(r"x\.com/i/article/(\d+)", re.I)


def is_x_article_url(url: str) -> bool:
    """URL 是否是 X Article 链接。"""
    return bool(_X_ARTICLE_URL_RE.search(url or ""))


def reconstruct_draftjs(blocks: list[dict], entity_map: dict) -> str:
    """把 Draft.js blocks 重建为 Markdown 文本。

    entityMap 里的原子块（IMAGE/VIDEO）还原为 ![](url) 内嵌语法，
    链接实体还原为 [text](url) 语法，普通文本直接保留。
    """
    if not blocks:
        return ""
    parts: list[str] = []
    for block in blocks:
        text = block.get("text", "") or ""
        block_type = block.get("type", "unstyled")
        if block_type == "atomic":
            entity_ranges = block.get("entityRanges") or []
            for er in entity_ranges:
                key = str(er.get("key", ""))
                entity = entity_map.get(key, {})
                etype = entity.get("type", "")
                data = entity.get("data", {})
                if etype == "IMAGE" and data.get("url"):
                    parts.append(f"![]({data['url']})")
                elif etype == "VIDEO" and data.get("url"):
                    parts.append(f"[视频]({data['url']})")
            continue
        if block_type in ("unordered-list-item", "ordered-list-item"):
            parts.append(f"- {text}")
            continue
        if block_type == "header-one":
            parts.append(f"# {text}")
            continue
        if block_type == "header-two":
            parts.append(f"## {text}")
            continue
        if block_type == "header-three":
            parts.append(f"### {text}")
            continue
        if block_type == "blockquote":
            parts.append(f"> {text}")
            continue
        if block_type == "code-block":
            parts.append(f"```\n{text}\n```")
            continue
        entity_ranges = block.get("entityRanges") or []
        if entity_ranges:
            sorted_ranges = sorted(entity_ranges, key=lambda r: r.get("offset", 0))
            last_end = 0
            for er in sorted_ranges:
                offset = er.get("offset", 0)
                length = er.get("length", 0)
                key = str(er.get("key", ""))
                entity = entity_map.get(key, {})
                etype = entity.get("type", "")
                data = entity.get("data", {})
                if offset > last_end:
                    parts.append(text[last_end:offset])
                if etype == "LINK" and data.get("url"):
                    parts.append(f"[{text[offset:offset+length]}]({data['url']})")
                else:
                    parts.append(text[offset:offset+length])
                last_end = offset + length
            if last_end < len(text):
                parts.append(text[last_end:])
        else:
            parts.append(text)
    return "\n\n".join(parts)


def fetch_x_article(url: str, max_chars: int = 8000,
                    timeout: float = 15.0) -> dict | None:
    """通过 FxTwitter API 获取 X Article 的 Draft.js blocks 并重建为 Markdown。

    成功返回 result dict（含 url/content/title/success/fetch_method），失败返回 None。
    """
    m = _X_ARTICLE_URL_RE.search(url or "")
    if not m:
        return None
    article_id = m.group(1)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fxtwitter_api import http_get
        api_url = f"https://api.fxtwitter.com/i/article/{article_id}"
        body, _ = http_get(api_url, timeout=timeout)
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        blocks = data.get("blocks") or []
        entity_map = data.get("entityMap") or {}
        if not blocks:
            return None
        content = reconstruct_draftjs(blocks, entity_map)
        if not content.strip():
            return None
        return {
            "url": url,
            "content": content[:max_chars],
            "title": _extract_title(content),
            "length": min(len(content), max_chars),
            "success": True,
            "error": None,
            "fetch_method": engine_states.FETCH_X_ARTICLE,
        }
    except Exception:
        return None


def _extract_title(content: str) -> str:
    """从 Markdown 正文提取标题（第一个 # 标题或前 100 字）。"""
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()[:200]
        if line and not line.startswith("![]"):
            return line[:100]
    return ""

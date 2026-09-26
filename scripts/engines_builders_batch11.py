#!/usr/bin/env python3
"""批次十一构建器：图片检索能力的两个补强源（2026-09-26）。

本批只收图源，目标是修「搜到图源却拿不到图」这一类缺陷的通用面：

  wikimedia_commons  Wikimedia Commons（免 key，通用图库，带 CC 许可与宽高）

为什么单独建一个 builder 而不是声明式 spec：Commons 的 `query.pages` 是
**以 pageid 为键的字典**，不是数组——`_extract_items` 遇到 dict 会把它当成
「单条结果」包成 `[{pageid: {...}}]`，声明式 output_map 取不到 title/url
（实测返回空列表）。这属于查询形状不兼容，只能用代码展开。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from engines_base import (
    finalize_image_fields,
    rank_score,
    safe_search,
    http_open,
)

logger = logging.getLogger("unified_search.engines")

_UA = "argo-search/2.9 (+batch11; wikimedia_commons)"
_API = "https://commons.wikimedia.org/w/api.php"

# Commons 的 extmetadata 是一层「字段 → {value, source, hidden}」的包装，
# 取值要过两道：raw["LicenseShortName"]["value"]。漏掉里层取键会拿到 dict
# 本身，_coerce_field 会把它丢成空串——表现为「许可字段时有时无」。
#
# 许可取两个字段：`LicenseShortName` 是给人看的（`CC BY-SA 4.0`），
# `License` 是机器码（`cc-by-sa-4.0`）。两者都要——实测有约 8% 的文件
# shortname 只写「Attribution」（Commons 在无法确定具体协议时的泛称），
# 而机器码为空；反过来也有机器码齐全而 shortname 缺失的。任一可用即可判定。
_META_FIELDS = ("LicenseShortName", "License", "Artist", "ImageDescription",
                "LicenseUrl")


def _meta(ext: dict[str, Any], key: str) -> str:
    """取 extmetadata 里的纯文本值（内层是 {value, source, hidden}）。"""
    node = ext.get(key)
    if isinstance(node, dict):
        node = node.get("value")
    if node is None:
        return ""
    text = str(node)
    # 上游描述字段带 HTML（<a href=...>、<span>），剥标签留文本——
    # 交付出去的 snippet 里带标签既占字数又不可读。
    if "<" in text and ">" in text:
        import re
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
    return text.strip()


def _build_wikimedia_commons_engine(spec: dict[str, Any]) -> Any:
    """Wikimedia Commons 通用图库（免 key、CC 素材、字段齐全）。"""
    timeout = spec.get("timeout", 20)
    eng = spec.get("_name", "wikimedia_commons")

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        # filetype:bitmap 排除 PDF/DjVu/SVG 等非位图文件——不筛的话「猫」这类
        # 查询会混进大量古籍扫描 PDF，它们没有可直接展示的位图。
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {q}",
            "gsrnamespace": "6",          # namespace 6 = File
            "gsrlimit": str(max(1, min(int(n), 50))),
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": "480",          # 缩略图宽度，直接可嵌入
            "format": "json",
            "formatversion": "2",
        }
        url = f"{_API}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        try:
            with http_open(req, timeout=to, engine=eng) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            logger.warning(f"Wikimedia Commons 失败: {e}")
            return []

        pages: Any = ((data or {}).get("query") or {}).get("pages")
        # formatversion=2 返回 list，=1 返回 dict。两种都收——上游改默认值
        # 是常事，只认一种会让引擎在版本切换时静默归零。
        if isinstance(pages, dict):
            pages = list(pages.values())
        if not isinstance(pages, list):
            return []

        results: list[dict[str, Any]] = []
        for rk, page in enumerate(pages[: max(1, int(n))]):
            if not isinstance(page, dict):
                continue
            info = page.get("imageinfo")
            if isinstance(info, list):
                info = info[0] if info else None
            if not isinstance(info, dict):
                continue
            title = str(page.get("title") or "").strip()
            # File: 前缀是 MediaWiki 的命名空间标记，不是文件名的一部分
            title = title[5:].strip() if title.startswith("File:") else title
            img = str(info.get("url") or "").strip()
            if not title or not img:
                continue

            ext = info.get("extmetadata")
            ext = ext if isinstance(ext, dict) else {}
            license_label = _meta(ext, "LicenseShortName")
            # shortname 是泛称（Commons 对无法确定具体协议的件写「Attribution」）
            # 或缺失时，退到机器码 `cc-by-sa-4.0`——它虽然不好读，但能被
            # finalize_image_fields 归一成标准标注，比一个认不出的泛称有用。
            if not license_label or license_label.strip().lower() in (
                    "attribution", "copyrighted", "unknown"):
                license_label = _meta(ext, "License") or license_label
            artist = _meta(ext, "Artist")
            desc = _meta(ext, "ImageDescription")
            page_url = str(page.get("descriptionurl") or "").strip()
            if not page_url:
                page_url = f"https://commons.wikimedia.org/wiki/File:{urllib.parse.quote(title.replace(' ', '_'))}"

            snippet_bits = [b for b in (artist and f"作者 {artist}", desc) if b]
            snippet = " · ".join(snippet_bits) or "Wikimedia Commons 开放素材"

            row: dict[str, Any] = {
                "title": title[:200],
                "url": page_url,
                "snippet": snippet[:300],
                "source": eng,
                "score": rank_score(0.85, rk),
                # 图片直链用 imageinfo.url（原图），thumburl 是缩略图——
                # 交付时给原图，需要小图的一方自己缩（反过来则拿不到原图）。
                "image_url": img,
                "image_license": license_label,
                "image_license_url": _meta(ext, "LicenseUrl"),
                "image_width": info.get("width"),
                "image_height": info.get("height"),
            }
            results.append(row)
        # 尺寸转 int / 许可归一 / 算商用判据——与声明式路径同一个收尾函数，
        # 保证两类引擎交付的字段契约完全一致。
        return finalize_image_fields(results, spec.get("image_license") or "")

    return _engine

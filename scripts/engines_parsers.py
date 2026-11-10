#!/usr/bin/env python3
"""engines_parsers.py — 响应解析器注册表（L2 解耦基础）。

## 为什么需要

当前自定义 type 的 164 个源里，L1（标准 HTTP 协议变体）约 90 个，
L2（协议标准但响应结构需特殊解析）约 50 个，L3（真自定义）约 27 个。

L2 的问题是：每个源在 `engines_base.py` 里写一个 `_parse_xxx` 函数，
注册进 `_CUSTOM_JSON_PARSERS` 字典。这个字典**按引擎名索引**，导致：
  - 同一套解析逻辑（如「数组在第二个元素」「单对象根」）被复制多份；
  - 新增一个 L2 源要改 engines_base.py（1567 行的大文件）；
  - 解析器无法被第三方插件复用（插件只能贡献 builder，不能贡献解析器）。

本模块把解析器从「按引擎名索引」改为「按解析能力注册」：
  - 解析器是纯函数：`parse(data: Any, spec: dict) -> list[dict]`
  - spec 用 `response.parser: <name>` 引用，而不是靠引擎名隐式匹配
  - 解析器可以组合：`response.parser: [worldbank, strip_fields]`

## 与 engines_base.py 的分工

- engines_base.py：通用 HTTP/RSS/CLI 引擎的构建 + output_map 解析（覆盖 L1）
- engines_parsers.py：注册解析器（覆盖 L2）
- engines_builders_*.py：真自定义 builder（覆盖 L3）

## 解析器契约

```python
def parse_xxx(data: Any, spec: dict) -> list[dict]:
    \"\"\"输入原始解析后的 JSON/XML，输出标准结果行。\"\"\"
    return [{"title": ..., "url": ..., "snippet": ..., "source": ...}, ...]
```

标准结果行字段：title / url / snippet / source / published_at / score /
image_url / upstream（均为可选，缺省由下游补）。
"""

from __future__ import annotations

import html as _htmllib
from typing import Any, Callable

# 解析器签名：(原始数据, spec) -> 标准结果行列表
Parser = Callable[[Any, dict], list[dict]]

_PARSERS: dict[str, Parser] = {}


class UnknownParserError(ValueError):
    """引用未注册的解析器时抛出。"""


def register(name: str, parser: Parser) -> None:
    """注册一个解析器。重复注册同名视为错误（防止静默覆盖）。"""
    if name in _PARSERS:
        raise ValueError(f"解析器 '{name}' 已注册，重复注册请改名")
    _PARSERS[name] = parser


def get(name: str) -> Parser:
    """取解析器；未注册直接报错（fail-closed，不返回空函数）。"""
    if name not in _PARSERS:
        raise UnknownParserError(
            f"未知解析器 '{name}'，已注册: {sorted(_PARSERS)}"
        )
    return _PARSERS[name]


def has(name: str) -> bool:
    """解析器是否已注册。"""
    return name in _PARSERS


def list_parsers() -> dict[str, str]:
    """列出全部已注册解析器（name -> docstring 首行）。"""
    out: dict[str, str] = {}
    for name, fn in _PARSERS.items():
        doc = (fn.__doc__ or "").strip().split("\n")[0]
        out[name] = doc
    return out


def resolve(spec: dict) -> list[Parser]:
    """从 spec 的 response.parser 字段解析出解析器列表。

    支持三种写法：
      response.parser: worldbank          # 单个
      response.parser: [worldbank, strip] # 组合（按顺序应用）
      response: {parser: null}            # 显式无解析器
    """
    response = spec.get("response") or {}
    if not isinstance(response, dict):
        return []
    raw = response.get("parser")
    if raw is None:
        return []
    names = [raw] if isinstance(raw, str) else list(raw)
    return [get(n) for n in names]


# ─────────────────────────────────────────────────────────────────────────────
# 内置解析器
# ─────────────────────────────────────────────────────────────────────────────

def _std_row(item: dict, source: str) -> dict:
    """把一个原始条目转成标准结果行的最小骨架。"""
    return {
        "title": str(item.get("title") or "").strip(),
        "url": str(item.get("url") or "").strip(),
        "snippet": str(item.get("snippet") or item.get("summary") or "").strip(),
        "source": source,
    }


def parse_array_at(data: Any, spec: dict) -> list[dict]:
    """通用：数组在指定索引或路径下（worldbank 的数组在第二个元素）。

    spec.response.items_path 指定路径，如 "1" 或 "data.results"。
    """
    path = (spec.get("response") or {}).get("items_path", "")
    if isinstance(data, list) and not path:
        rows = data
    else:
        rows = _dig(data, path)
    if not isinstance(rows, list):
        return []
    source = spec.get("engine_id", "?")
    return [_std_row(r, source) for r in rows if isinstance(r, dict)]


def parse_object_root(data: Any, spec: dict) -> list[dict]:
    """通用：响应是单对象根（unpaywall / doi 类）。

    spec.response.fields 映射标准字段到对象路径。
    """
    if not isinstance(data, dict):
        return []
    fields = (spec.get("response") or {}).get("fields") or {}
    source = spec.get("engine_id", "?")
    row = {}
    for std, path in fields.items():
        row[std] = _dig(data, path)
    row["source"] = source
    return [row]


def parse_strip_fields(data: Any, spec: dict) -> list[dict]:
    """组合用：剥 HTML 标签、截断 snippet。"""
    rows = data if isinstance(data, list) else []
    import html as _htmllib
    for r in rows:
        sn = r.get("snippet")
        if isinstance(sn, str):
            sn = _strip_tags(sn)
            if "&" in sn:
                sn = _htmllib.unescape(sn)
            if len(sn) > 300:
                sn = sn[:300]
            r["snippet"] = sn
    return rows


_TAG_RE = __import__("re").compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text)


def _dig(obj: Any, path: str) -> Any:
    """按点分路径取嵌套值；路径段为数字时按数组索引。"""
    if not path:
        return obj
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return cur


def _flatten_segments(val: Any) -> str:
    """把 [{value, is_bold}, ...] 数组展平成纯文本。

    Mwmbl 的 title/extract 是高亮片段数组，标准 output_map 只会取到
    第一个 dict 然后被 _coerce_field 丢成空串。这个解析器做展平。
    """
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return "".join(
            str(seg.get("value", "")) if isinstance(seg, dict) else str(seg)
            for seg in val
        )
    if isinstance(val, dict):
        return str(val.get("value", ""))
    return ""


def parse_mwmbl(data: Any, spec: dict) -> list[dict]:
    """Mwmbl 响应解析（L2 试点：字段展平）。

    上游 title/extract 是 [{value, is_bold}] 数组，标准 output_map 无法
    展平，故注册专用解析器。这是「协议标准、解析变体」的典型形态。
    """
    if not isinstance(data, list):
        return []
    source = spec.get("engine_id", "mwmbl")
    results = []
    for _rk, r in enumerate(data):
        if not isinstance(r, dict):
            continue
        title = _flatten_segments(r.get("title"))
        extract = _flatten_segments(r.get("extract"))
        url_ = r.get("url") or ""
        if not title and not url_:
            continue
        results.append({
            "title": title[:200] or url_[:200],
            "url": url_,
            "snippet": extract[:300],
            "source": source,
            "score": max(0.0, 0.7 - _rk * 0.05),
        })
    return results


# 注册通用解析器
register("array_at", parse_array_at)
register("object_root", parse_object_root)
register("strip_fields", parse_strip_fields)
register("mwmbl", parse_mwmbl)


def parse_smart_generic(data: Any, engine_name: str = "?",
                        limit: int = 10, spec: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """通用智能自省推断解析器：自适应探测常见列表路径、单层嵌套展平与字段候选降级。

    列表探测（最多 3 层）：
      - 顶层 results/items/data/works/search/hits/papers/documents/entries 等
      - 一层嵌套（data.results / data.value）
      - 二层嵌套（data.webPages.value 等）

    字段别名与候选降级：
      - title: title|name|heading|Title|titleNullable|headline|subject|topic
      - url: url|URL|html_url|link|href|urlNullable|web_url|canonical_url（支持 url_template/{id}）
      - snippet: snippet|summary|content|description|abstract|subtitleNullable|subtitle|extract|details|text
      - published_at: published_at|publishedAt|created_at|pubdate|date|time
    """
    if spec is None and isinstance(engine_name, dict):
        spec = engine_name
        engine_name = spec.get("engine_id", "?")
    spec = spec or {}

    list_keys = (
        "results", "items", "value", "works", "search", "data",
        "organic", "webPages", "hits", "documents", "entries", "papers",
    )

    def _find_items(obj: Any, depth: int = 0) -> list | None:
        if depth > 3 or obj is None:
            return None
        if isinstance(obj, list):
            if not obj or isinstance(obj[0], dict):
                return obj
            return None
        if not isinstance(obj, dict):
            return None
        for key in list_keys:
            if key not in obj:
                continue
            found = _find_items(obj[key], depth + 1)
            if found is not None:
                return found
        if depth < 2:
            for v in obj.values():
                if isinstance(v, dict):
                    found = _find_items(v, depth + 1)
                    if found is not None:
                        return found
        return None

    items = _find_items(data) if isinstance(data, (dict, list)) else None
    if not items or not isinstance(items, list):
        return []

    url_template = spec.get("url_template") or (spec.get("output_map") or {}).get("url_template")
    results: list[dict[str, Any]] = []

    for idx, i in enumerate(items):
        if not isinstance(i, dict):
            continue
        flat = dict(i)
        for nested_k in ("paper", "item", "data", "node", "article", "entry", "show", "bibjson"):
            if isinstance(i.get(nested_k), dict):
                for k, v in i[nested_k].items():
                    flat.setdefault(k, v)

        title = (
            flat.get("title") or flat.get("name") or flat.get("heading")
            or flat.get("Title") or flat.get("titleNullable") or flat.get("title_s")
            or flat.get("headline") or flat.get("subject") or flat.get("topic")
            or flat.get("text") or ""
        )
        if isinstance(title, dict):
            # 多语言字典（如 EU Open Data）：优先取 en/zh/fr/de，否则取首个
            for lang in ("en", "zh", "fr", "de"):
                if title.get(lang):
                    title = title[lang]
                    break
            if isinstance(title, dict) and title:
                title = next(iter(title.values()))
        elif isinstance(title, list):
            title = title[0] if title else ""
        raw_id = flat.get("id") or flat.get("objectID") or flat.get("ID") or ""
        url = (
            flat.get("url") or flat.get("URL") or flat.get("html_url")
            or flat.get("link") or flat.get("href") or flat.get("urlNullable")
            or flat.get("web_url") or flat.get("canonical_url") or flat.get("guid")
            or flat.get("uri_s") or flat.get("url_public_html")
            or flat.get("resource") or flat.get("landing_page") or ""
        )
        if isinstance(url, list) and url:
            if isinstance(url[0], dict):
                url = url[0].get("url") or url[0].get("href") or url[0].get("resource") or ""
            else:
                url = str(url[0])
        if not url and url_template and raw_id:
            url = url_template.replace("{id}", str(raw_id))

        snippet = (
            flat.get("snippet") or flat.get("summary") or flat.get("content")
            or flat.get("description") or flat.get("abstract") or flat.get("abstract_s")
            or flat.get("subtitleNullable") or flat.get("subtitle")
            or flat.get("extract") or flat.get("details") or flat.get("text") or ""
        )
        if isinstance(snippet, dict):
            for lang in ("en", "zh", "fr", "de"):
                if snippet.get(lang):
                    snippet = snippet[lang]
                    break
            if isinstance(snippet, dict) and snippet:
                snippet = next(iter(snippet.values()))
        elif isinstance(snippet, list):
            snippet = snippet[0] if snippet else ""

        # 文本标签剥离与 HTML 实体反转义
        if isinstance(title, str) and ("<" in title or "&" in title):
            title = _strip_tags(title)
            if "&" in title:
                title = _htmllib.unescape(title)
        if isinstance(snippet, str) and ("<" in snippet or "&" in snippet):
            snippet = _strip_tags(snippet)
            if "&" in snippet:
                snippet = _htmllib.unescape(snippet)

        published_at = (
            flat.get("published_at") or flat.get("publishedAt")
            or flat.get("created_at") or flat.get("pubdate") or flat.get("date")
            or flat.get("premiered") or flat.get("first_air_date") or ""
        )
        if published_at:
            try:
                from engines_base import _normalize_epoch
                published_at = _normalize_epoch(published_at)
            except Exception:
                published_at = str(published_at)

        # 统一分值契约：复用 engines_base.rank_score（保证 [0, 1] 夹紧、防 NaN/Inf、单调平滑衰减）
        raw_score = next((v for v in (flat.get("score"), flat.get("relevance_score")) if v not in (None, "")), 0.5)
        try:
            from engines_base import rank_score
            score = rank_score(raw_score, idx)
        except Exception:
            score = max(0.0, 0.7 - idx * 0.05)

        row: dict[str, Any] = {
            "title": str(title)[:200],
            "url": str(url),
            "snippet": str(snippet)[:300],
            "score": score,
            "source": engine_name,
        }
        if published_at:
            row["published_at"] = str(published_at)
        results.append(row)
    return results[:limit]


register("smart_generic", parse_smart_generic)


# 迁移 engines_base.py 里现有的 4 个专用解析器（保持向后兼容）
def _migrate_builtin() -> None:
    """把 engines_base 的 _CUSTOM_JSON_PARSERS 投影进注册表。

    为什么不直接删 engines_base 的实现：它们有更精细的字段处理
    （如 semantic_scholar 的引用数、作者拼接），迁移到声明式
    output_map 会丢信息。这里先投影，让 spec 可以显式引用；
    引擎名隐式匹配作为兜底保留，逐步切到显式。
    """
    try:
        import engines_base
    except ImportError:
        return
    mapping = {
        "uapi": engines_base._parse_uapi,
        "semantic_scholar": engines_base._parse_semantic_scholar,
        "doi": engines_base._parse_doi,
        "unpaywall": engines_base._parse_unpaywall,
    }
    for name, fn in mapping.items():
        if not has(name):
            register(name, fn)


_migrate_builtin()

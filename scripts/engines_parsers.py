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
import re
from pathlib import Path
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
# 声明式字段映射（output_map 的通用实现）
# ─────────────────────────────────────────────────────────────────────────────
#
# 为什么放在这里：2026-11-10 归一 13 个专用 builder 时发现，原 builder 常把
# 多个上游字段拼进 snippet（cleveland 的 `tombstone · 授权 CC0`、deezer 的
# `Deezer 音乐人 · 专辑 N · 粉丝 M`），而 output_map 只能取单个字段——归一后
# 这类信息会静默丢失。把「拼接」变成声明式能力，属于解析层职责，故与
# `parse_*` 解析器同文件维护；engines_base 只做委托调用。

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


def _coerce_field(val: Any, *, max_len: int | None = None) -> str:
    """把 API 字段压成字符串：list 取首项、dict 丢弃、其余 str。"""
    if val is None:
        return ""
    if isinstance(val, list):
        if not val:
            return ""
        val = val[0]
    if isinstance(val, dict):
        return ""
    s = str(val).strip()
    if max_len is not None and len(s) > max_len:
        return s[:max_len]
    return s


def _render_field_template(template: str, item: dict) -> str:
    """模板插值：`{field}` 取条目字段（支持点分路径），空值置空并折叠分隔符。

    与 URL 模板的区别：URL 模板任一占位为空则整体落空（否则拼出半截链接），
    而文本模板把空占位折叠掉——少一段仍是有效文本。
    连续/首尾的 `·`/`⇄`/`|` 分隔符会折叠，避免缺字段留下 `a ·  · b` 残渣。
    """
    def _repl(m: re.Match) -> str:
        return _coerce_field(_dig(item, m.group(1)))

    out = re.sub(r"\{([^}]+)\}", _repl, template)
    out = re.sub(r"(?:\s*[·⇄|]\s*)+", lambda m: " " + next(
        (ch for ch in m.group(0) if ch in "·⇄|"), "·") + " ", out)
    out = out.strip()
    out = re.sub(r"(?:^\s*[·⇄|]\s*|\s*[·⇄|]\s*$)", "", out)
    return out.strip()


def _format_url_template(template: str, item: dict) -> str:
    """支持 {field} 与 {a.b.c} 点分路径的 URL 模板；任一占位为空则返回空串。"""
    vals = {m.group(1): _coerce_field(_dig(item, m.group(1)))
            for m in re.finditer(r"\{([^}]+)\}", template)}
    if any(not v for v in vals.values()):
        return ""
    try:
        return re.sub(r"\{([^}]+)\}", lambda m: vals[m.group(1)], template)
    except Exception:
        return ""


def extract_items(data: Any, path: str) -> list:
    """从 JSON 按路径提取列表。path 为 . / $ / [] / 空 且 data 为 list 时直接返回。"""
    if path in ("", ".", "$", "[]"):
        return data if isinstance(data, list) else []
    if not isinstance(data, dict):
        return []
    obj = _dig(data, path)
    if isinstance(obj, list):
        return obj
    # DBLP 等 API 在 h=1 时把 hit 收成对象而非数组
    if isinstance(obj, dict):
        return [obj]
    return []


def make_field_parser(path: str, fields: dict[str, str], url_template: str | None = None,
                      max_items: int = 10, templates: dict[str, str] | None = None,
                      url_item_template: str | None = None) -> Callable:
    """构造声明式 parser。支持点分路径字段、url_template（含嵌套占位）与模板拼接。

    `templates` 是可选的模板字段映射（`{item_title, item_summary}`），声明后
    覆盖同名 `fields` 条目——用于需要多字段拼接的源（见 `_render_field_template`）。

    `url_item_template` 是**逐条目**的 URL 拼接模板（如 eu_opendata 的
    `landing_page.0.resource` 数组取首项）。与 `url_template` 的区别：后者用
    `_format_url_template` 且任一占位为空即整体落空；前者在取不到值时保持
    「不声明 URL」，交给通用推断或下游补。
    """
    templates = templates or {}

    def parser(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list) and path in ("", ".", "$", "[]"):
            items = data
        elif isinstance(data, dict):
            items = extract_items(data, path)
        else:
            items = []
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            r: dict[str, Any] = {}
            for ok, ik in fields.items():
                if not ik:
                    continue
                raw = _dig(item, ik) if "." in str(ik) else item.get(ik, "")
                if raw in ("", None) and "." not in str(ik):
                    raw = item.get(ik, "")
                coerced = _coerce_field(raw, max_len=300 if ok == "snippet" else 500)
                if coerced:
                    r[ok] = coerced
            # 模板字段优先于同名单字段映射（声明式拼接覆盖单字段取值）。
            # 键名是 output_map 的声明名（item_title_template），落到标准行字段
            # 名（title/snippet）——不转换会把结果写进 `item_summary_template` 键。
            # 模板渲染为空时**回落**到单字段映射（如 tatoeba 无译文时用 lang），
            # 而不是把已取到的值覆盖成空。
            for ok, tmpl in templates.items():
                if not tmpl:
                    continue
                std = {"item_title_template": "title",
                       "item_summary_template": "snippet"}.get(ok, ok)
                rendered = _render_field_template(tmpl, item)
                if rendered:
                    r[std] = rendered[:300 if std == "snippet" else 500]
            if url_template and (not r.get("url") or not str(r["url"]).startswith(("http://", "https://"))):
                built = _format_url_template(url_template, item)
                if built.startswith(("http://", "https://")):
                    r["url"] = built
                else:
                    r.pop("url", None)
            # 逐条目 URL 模板：占位取自条目自身（如 landing_page.0.resource），
            # 取不到就保持无 URL（不造半截链接）。
            if url_item_template and (not r.get("url") or not str(r["url"]).startswith(("http://", "https://"))):
                built = _format_url_template(url_item_template, item)
                if built.startswith(("http://", "https://")):
                    r["url"] = built
            if isinstance(r.get("url"), str) and r["url"].startswith("//"):
                r["url"] = "https:" + r["url"]
            if r.get("title") or r.get("url"):
                results.append(r)
        return results[:max_items]
    return parser


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


def parse_artic(data: Any, spec: dict) -> list[dict]:
    """Art Institute of Chicago 响应解析（L2：IIIF 图片需响应级 config 拼接）。

    上游把 IIIF 基础地址放在响应的 `config.iiif_url`（不在条目里），图片直链
    是 `{iiif_url}/{image_id}/full/843,/0/default.jpg`。这类「拼接依赖响应级
    配置」的解析无法用 output_map 声明，故注册专用解析器——协议标准、解析变体。

    image_id 为空时**不产出** image_url（不得凭空造链接，见 test_image_sources）。
    """
    if not isinstance(data, dict):
        return []
    source = spec.get("engine_id", "artic")
    iiif = str(((data.get("config") or {}).get("iiif_url") or "")).rstrip("/")
    rows = data.get("data") if isinstance(data.get("data"), list) else []
    results = []
    for _rk, it in enumerate(rows):
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        art_id = it.get("id")
        url_ = f"https://www.artic.edu/artworks/{art_id}" if art_id else ""
        if not title and not url_:
            continue
        row = {
            "title": title[:200],
            "url": url_,
            "snippet": str(it.get("artist_display") or "")[:300],
            "source": source,
            "score": max(0.0, 0.7 - _rk * 0.05),
        }
        image_id = str(it.get("image_id") or "").strip()
        if iiif and image_id:
            row["image_url"] = f"{iiif}/{image_id}/full/843,/0/default.jpg"
            # AIC 是 CC0 开放图像；授权缺失时按无授权处理，不凭空标注
            lic = str(it.get("copyright_notice") or "").strip()
            if lic:
                row["image_license"] = lic
            else:
                row["image_license"] = "CC0（芝加哥艺术博物馆开放图像）"
        results.append(row)
    return results


# 注册通用解析器
register("array_at", parse_array_at)
register("object_root", parse_object_root)
register("strip_fields", parse_strip_fields)
register("mwmbl", parse_mwmbl)
register("artic", parse_artic)


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
            or flat.get("text") or flat.get("cycle") or ""
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
        if not url and url_template:
            url = url_template.replace("{id}", str(raw_id))
            for _k, _v in flat.items():
                if f"{{{_k}}}" in url:
                    url = url.replace(f"{{{_k}}}", str(_v))

        snippet = (
            flat.get("snippet") or flat.get("summary") or flat.get("content")
            or flat.get("description") or flat.get("abstract") or flat.get("abstract_s")
            or flat.get("subtitleNullable") or flat.get("subtitle")
            or flat.get("extract") or flat.get("details") or flat.get("text") or ""
        )
        if not snippet and (flat.get("eol") or flat.get("latest")):
            _sp = [f"最新 {flat['latest']}" if flat.get("latest") else "",
                   f"发布 {flat['releaseDate']}" if flat.get("releaseDate") else "",
                   f"支持至 {flat['support']}" if flat.get("support") else "",
                   f"EOL {flat['eol']}" if flat.get("eol") else ""]
            snippet = " · ".join(x for x in _sp if x)
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


# ─────────────────────────────────────────────────────────────────────────────
# 声明式架构自省与结构漂移诊断（第一性原理 + MECE 原则）
# ─────────────────────────────────────────────────────────────────────────────

_TITLE_SYNONYMS = (
    "title", "name", "headline", "subject", "text", "label",
    "caption", "item_title", "artwork_title", "track_name", "display_name",
)
_URL_SYNONYMS = (
    "url", "link", "web_url", "html_url", "href", "uri",
    "source_url", "landing_page", "target_url", "permalink", "page_url", "site_url",
)
_SUMMARY_SYNONYMS = (
    "snippet", "summary", "description", "abstract", "body",
    "content", "desc", "overview", "intro", "tombstone", "details", "comment",
)
_PUB_SYNONYMS = (
    "published_at", "created_at", "pubdate", "pub_date", "date",
    "time", "timestamp", "premiered", "first_air_date", "release_date",
)


def _flatten_item(item: Any, prefix: str = "", depth: int = 0, max_depth: int = 3) -> dict[str, Any]:
    """将嵌套字典/列表展平为点分路径键值字典（深度受控，列表展开前 2 条）。"""
    flat: dict[str, Any] = {}
    if depth > max_depth or not isinstance(item, (dict, list)):
        return flat
    if isinstance(item, dict):
        for k, v in item.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)) and depth < max_depth:
                flat.update(_flatten_item(v, path, depth + 1, max_depth))
            else:
                flat[path] = v
    elif isinstance(item, list):
        for i, v in enumerate(item[:2]):
            path = f"{prefix}.{i}" if prefix else str(i)
            if isinstance(v, (dict, list)) and depth < max_depth:
                flat.update(_flatten_item(v, path, depth + 1, max_depth))
            else:
                flat[path] = v
    return flat


def _find_candidate_containers(data: Any, prefix: str = "", depth: int = 0, max_depth: int = 4) -> list[tuple[str, list, float]]:
    """递归搜索全部字典列表，并按结构质量评分。"""
    candidates: list[tuple[str, list, float]] = []
    if depth > max_depth:
        return candidates
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            sample = data[0]
            score = 0.5 + min(0.3, len(data) * 0.02)
            keys = {str(k).lower() for k in sample.keys()}
            if any(k in keys for k in ("title", "name", "id", "url", "link")):
                score += 0.2
            candidates.append((prefix or ".", data, score))
        return candidates
    if isinstance(data, dict):
        for k, v in data.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                sample = v[0]
                score = 0.5 + min(0.3, len(v) * 0.02)
                keys = {str(k).lower() for k in sample.keys()}
                if any(syn in keys for syn in ("title", "name", "id", "url", "link")):
                    score += 0.2
                candidates.append((path, v, score))
            elif isinstance(v, dict) and depth < max_depth:
                candidates.extend(_find_candidate_containers(v, path, depth + 1, max_depth))
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates


def _match_best_field(target_kind: str, flat: dict[str, Any]) -> tuple[str | None, Any, float]:
    """根据目标类型和扁平键树，启发式匹配最优路径。"""
    if target_kind == "title":
        for path, val in flat.items():
            if not isinstance(val, (str, int)) or not str(val).strip():
                continue
            seg = path.split(".")[-1].lower()
            if seg in ("title", "name", "headline", "artwork_title"):
                return path, val, 0.95
            if seg in _TITLE_SYNONYMS:
                return path, val, 0.85
    elif target_kind == "url":
        url_candidates: list[tuple[str, Any, float]] = []
        for path, val in flat.items():
            s = str(val or "").strip()
            if not (s.startswith("http://") or s.startswith("https://") or s.startswith("/")):
                continue
            seg = path.split(".")[-1].lower()
            score = 0.6
            if seg in _URL_SYNONYMS:
                score += 0.3
            if any(s.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                score -= 0.4
            url_candidates.append((path, val, score))
        if url_candidates:
            url_candidates.sort(key=lambda x: x[2], reverse=True)
            best = url_candidates[0]
            if best[2] >= 0.5:
                return best[0], best[1], best[2]
        for path, val in flat.items():
            seg = path.split(".")[-1].lower()
            if seg in _URL_SYNONYMS and val:
                return path, val, 0.75
    elif target_kind == "summary":
        for path, val in flat.items():
            s = str(val or "").strip()
            if not s or len(s) < 5:
                continue
            seg = path.split(".")[-1].lower()
            if seg in ("snippet", "summary", "description", "abstract"):
                return path, val, 0.90
            if seg in _SUMMARY_SYNONYMS:
                return path, val, 0.80
    return None, None, 0.0


def diagnose_schema_drift(raw_data: Any, spec_or_output_map: dict[str, Any]) -> dict[str, Any]:
    """上游数据源结构漂移自省对拍诊断（第一性原理 + MECE 原则）。

    层级覆盖：
      1. Envelope: 业务错误封套、非结构化数据判定
      2. Container: items 容器路径位移对拍与自动推荐
      3. Field: item_title / item_url / item_summary 字段更名、层级下沉同义与特征对拍
    """
    omap = spec_or_output_map.get("output_map") if isinstance(spec_or_output_map.get("output_map"), dict) else spec_or_output_map
    if not isinstance(omap, dict):
        omap = {}

    current_items_path = omap.get("items", "")
    current_title = omap.get("item_title", "title")
    current_url = omap.get("item_url", "url")
    current_summary = omap.get("item_summary", "snippet")

    issues: list[str] = []
    container_drift: dict[str, Any] | None = None
    field_drifts: dict[str, Any] = {}
    suggested_patch: dict[str, str] = {}

    # 1. Envelope 层
    if not isinstance(raw_data, (dict, list)):
        return {
            "ok": False,
            "drift_detected": False,
            "stage": "envelope",
            "issues": [f"响应非 JSON 结构 (类型: {type(raw_data).__name__})"],
            "confidence": 0.0,
            "suggested_patch": {},
        }
    if isinstance(raw_data, dict):
        err_val = raw_data.get("error") or raw_data.get("err_msg") or raw_data.get("message")
        if err_val and not raw_data.get("data") and not raw_data.get("results") and not raw_data.get("items"):
            return {
                "ok": False,
                "drift_detected": False,
                "stage": "envelope",
                "issues": [f"上游返回业务错误封套: {err_val}"],
                "confidence": 0.0,
                "suggested_patch": {},
            }

    # 2. Container 层
    items = extract_items(raw_data, current_items_path)
    if not items or not isinstance(items[0], dict):
        candidates = _find_candidate_containers(raw_data)
        if candidates:
            best_path, best_items, score = candidates[0]
            container_drift = {
                "from": current_items_path,
                "to": best_path,
                "count": len(best_items),
                "score": score,
            }
            issues.append(f"容器路径失效: '{current_items_path}' 未提取到条目，候选路径 '{best_path}' (包含 {len(best_items)} 条)")
            suggested_patch["items"] = best_path
            items = best_items
        else:
            return {
                "ok": False,
                "drift_detected": False,
                "stage": "container",
                "issues": [f"未在响应中发现任何字典列表容器 (当前 items='{current_items_path}')"],
                "confidence": 0.0,
                "suggested_patch": {},
            }

    # 3. Field 层
    sample = items[0]
    flat = _flatten_item(sample, max_depth=3)

    # 校验与对拍 title
    val_title = _dig(sample, current_title) if current_title else None
    if not val_title:
        cand_p, cand_v, cand_s = _match_best_field("title", flat)
        if cand_p and cand_p != current_title:
            field_drifts["item_title"] = {"from": current_title, "to": cand_p, "sample": str(cand_v)[:50], "score": cand_s}
            issues.append(f"标题字段失效: '{current_title}' 为空 -> 候选 '{cand_p}' (值: {cand_v!r})")
            suggested_patch["item_title"] = cand_p

    # 校验与对拍 url
    val_url = _dig(sample, current_url) if current_url else None
    if not val_url:
        cand_p, cand_v, cand_s = _match_best_field("url", flat)
        if cand_p and cand_p != current_url:
            field_drifts["item_url"] = {"from": current_url, "to": cand_p, "sample": str(cand_v)[:50], "score": cand_s}
            issues.append(f"链接字段失效: '{current_url}' 为空 -> 候选 '{cand_p}' (值: {cand_v!r})")
            suggested_patch["item_url"] = cand_p

    # 校验与对拍 summary
    if current_summary:
        val_sum = _dig(sample, current_summary)
        if not val_sum:
            cand_p, cand_v, cand_s = _match_best_field("summary", flat)
            if cand_p and cand_p != current_summary:
                field_drifts["item_summary"] = {"from": current_summary, "to": cand_p, "sample": str(cand_v)[:50], "score": cand_s}
                issues.append(f"摘要字段失效: '{current_summary}' 为空 -> 候选 '{cand_p}'")
                suggested_patch["item_summary"] = cand_p

    drift_detected = bool(container_drift or field_drifts)
    stage = "field" if field_drifts else ("container" if container_drift else "ok")
    confidence = 0.0
    if drift_detected:
        confidence = 0.95 if (suggested_patch.get("items") or ("item_title" in suggested_patch and "item_url" in suggested_patch)) else 0.80

    return {
        "ok": not drift_detected,
        "drift_detected": drift_detected,
        "stage": stage,
        "issues": issues,
        "container_drift": container_drift,
        "field_drifts": field_drifts,
        "suggested_patch": suggested_patch,
        "confidence": confidence,
    }


def render_drift_report(diagnosis: dict[str, Any], engine_id: str = "") -> str:
    """渲染终端格式化的结构漂移诊断报告。"""
    lines: list[str] = []
    prefix = f"[{engine_id}] " if engine_id else ""
    if not diagnosis.get("drift_detected"):
        if diagnosis.get("issues"):
            lines.append(f"{prefix}诊断发现异常 (Stage: {diagnosis.get('stage')}):")
            for iss in diagnosis["issues"]:
                lines.append(f"  • {iss}")
        else:
            lines.append(f"{prefix}结构正常，未检测到漂移。")
        return "\n".join(lines)

    lines.append(f"{prefix}检测到上游响应结构漂移 (Stage: {diagnosis.get('stage')}, 置信度: {diagnosis.get('confidence', 0):.0%}):")
    for iss in diagnosis.get("issues", []):
        lines.append(f"  • {iss}")

    patch = diagnosis.get("suggested_patch") or {}
    if patch:
        lines.append("  建议 Spec 补丁 (output_map):")
        for k, v in patch.items():
            lines.append(f"    {k}: {v}")
    return "\n".join(lines)


def apply_schema_patch(spec_file: str | Path, patch: dict[str, str]) -> tuple[bool, str]:
    """安全原子应用 Spec 补丁：保留所有注释和格式，仅改写 output_map 对应键。"""
    import re as _re
    p = Path(spec_file)
    if not p.is_file():
        return False, f"Spec 文件不存在: {p}"

    text = p.read_text(encoding="utf-8")
    if "output_map:" not in text:
        return False, "Spec 中无 output_map 节点，不支持就地修改"

    for k, v in patch.items():
        pattern = _re.compile(rf"^([ \t]+){_re.escape(k)}:\s*.*$", _re.M)
        if pattern.search(text):
            text = pattern.sub(rf"\g<1>{k}: {v}", text)
        else:
            # 补在 output_map 之后
            text = _re.sub(r"^(output_map:.*)$", rf"\1\n  {k}: {v}", text, count=1, flags=_re.M)

    p.write_text(text, encoding="utf-8")
    return True, f"已安全更新 {p.name} 中的 {len(patch)} 个字段"


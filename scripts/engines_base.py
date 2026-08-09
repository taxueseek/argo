#!/usr/bin/env python3
"""engines_base — 公共工具 + cli/http/html 构建器 + 通用解析。"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

try:
    from config import load_config, get_engines
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from config import load_config, get_engines

logger = logging.getLogger("unified_search.engines")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler(sys.stderr))


def _lang_param(param: str, query: str) -> str:
    """按查询主语言返回引擎语言参数；表在 lang_detect 单真源维护。

    弱信号查询（mixed/other）时注入 lang_pref 的 engine_lang（习惯/系统/中英基线），
    强查询信号仍由 detect_language 主导，不因系统 locale 覆盖。
    """
    try:
        from lang_detect import engine_lang_param, detect_language
        preferred = ""
        try:
            from lang_pref import effective_engine_lang
            preferred = effective_engine_lang(detect_language(query))
        except ImportError:
            pass
        return engine_lang_param(param, query, preferred_lang=preferred)
    except ImportError:
        return ""


def safe_search(fn: Callable) -> Callable:
    """统一错误处理装饰器。所有异常返回 []，细粒度异常先于通用 Exception 匹配。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> list[dict[str, Any]]:
        name = fn.__name__.replace("_engine", "").strip("_")
        try:
            return fn(*args, **kwargs)
        except subprocess.TimeoutExpired:
            logger.warning(f"引擎 {name} 超时")
        except FileNotFoundError as e:
            logger.warning(f"引擎 {name} 命令不存在: {e}")
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            logger.warning(f"引擎 {name} HTTP 错误: {e}")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"引擎 {name} 解析错误: {e}")
        except Exception as e:
            logger.error(f"引擎 {name} 未预期异常: {type(e).__name__}: {e}", exc_info=True)
        return []
    return wrapper


def _run(cmd: list[str], timeout: float = 8, engine_name: str = "?") -> str:
    """执行命令，超时/异常不抛。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout
        tail = (r.stderr or "").strip()[:200]
        logger.warning(f"引擎 {engine_name} 失败 (rc={r.returncode}): {tail}")
        return r.stdout if r.stdout.strip() else ""
    except subprocess.TimeoutExpired:
        logger.warning(f"引擎 {engine_name} 超时 (>{timeout}s)")
    except FileNotFoundError as e:
        logger.error(f"引擎 {engine_name} CLI 缺失: {e}")
    except Exception as e:
        logger.error(f"引擎 {engine_name} 异常: {type(e).__name__}: {e}")
    return ""


def _resolve(template: list[str] | str, query: str, n: int, **extra: Any) -> list[str] | str:
    """替换模板占位符。"""
    if isinstance(template, list):
        return [_resolve(item, query, n, **extra) for item in template]
    s = template.replace("{query}", query).replace("{n}", str(n))
    s = s.replace("{TIMESTAMP}", str(int(time.time())))
    for key, val in extra.items():
        s = s.replace(f"{{{key}}}", str(val))
    if s.startswith("~"):
        s = str(Path.home() / s[1:])
    return re.sub(r"\{([A-Z_][A-Z0-9_]*)\}", lambda m: os.environ.get(m.group(1), m.group(0)), s)


def _get_path(obj: Any, path: str) -> Any:
    """按点分路径取值，支持 list 下标（如 authors.0.name）。空路径返回 obj。"""
    if path in ("", ".", "$", "[]", None):
        return obj
    cur = obj
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
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


def _format_url_template(template: str, item: dict) -> str:
    """支持 {field} 与 {a.b.c} 点分路径的 URL 模板。"""
    def repl(m: re.Match) -> str:
        return _coerce_field(_get_path(item, m.group(1)))
    try:
        return re.sub(r"\{([^}]+)\}", repl, template)
    except Exception:
        return ""


def _extract_items(data: Any, path: str) -> list:
    """从 JSON 按路径提取列表。path 为 . / $ / [] / 空 且 data 为 list 时直接返回。"""
    if path in ("", ".", "$", "[]"):
        return data if isinstance(data, list) else []
    if not isinstance(data, dict):
        return []
    obj = _get_path(data, path)
    if isinstance(obj, list):
        return obj
    # DBLP 等 API 在 h=1 时把 hit 收成对象而非数组
    if isinstance(obj, dict):
        return [obj]
    return []


def _make_field_parser(path: str, fields: dict[str, str], url_template: str | None = None) -> Callable:
    """构造声明式 parser。支持点分路径字段与 url_template（含嵌套占位）。"""
    def parser(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list) and path in ("", ".", "$", "[]"):
            items = data
        elif isinstance(data, dict):
            items = _extract_items(data, path)
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
                raw = _get_path(item, ik) if "." in str(ik) else item.get(ik, "")
                # 兼容顶层点分键名不存在时回退 get
                if raw in ("", None) and "." not in str(ik):
                    raw = item.get(ik, "")
                coerced = _coerce_field(raw, max_len=300 if ok == "snippet" else 500)
                if coerced:
                    r[ok] = coerced
            if url_template and (not r.get("url") or not str(r["url"]).startswith(("http://", "https://"))):
                built = _format_url_template(url_template, item)
                if built.startswith(("http://", "https://")):
                    r["url"] = built
            if isinstance(r.get("url"), str) and r["url"].startswith("//"):
                r["url"] = "https:" + r["url"]
            if r.get("title") or r.get("url"):
                results.append(r)
        return results[:10]
    return parser


def _build_cli_engine(spec: dict[str, Any]) -> Any:
    cmd_template = spec.get("cmd", [])
    search_args = spec.get("search_args", [])
    env_overrides = spec.get("env", {})
    output_format = spec.get("output_format", "")   # "yaml" | ""（JSON/文本自动）
    filter_args = spec.get("filter_args", {})       # {kwarg: [参数模板...]}，kwargs 携带时追加

    @safe_search
    def _engine(query: str, n: int = 5, timeout: float = 8, mode: str = "fast", **kwargs) -> list[dict[str, Any]]:
        cmd = _resolve(cmd_template, query, n, mode=mode, **kwargs)
        args = _resolve(search_args, query, n, mode=mode, **kwargs)
        if not cmd:
            return []
        for key, tmpl in filter_args.items():
            if kwargs.get(key) not in (None, ""):
                args += _resolve(tmpl, query, n, mode=mode, **kwargs)
        env = os.environ.copy()
        env.update(env_overrides)
        return _parse_text_output(_run(cmd + args, timeout=timeout, engine_name=spec.get("_name", "cli")),
                                  spec.get("_name", "cli"), output_format=output_format, n=n)
    return _engine


def _build_http_engine(spec: dict[str, Any]) -> Any:
    """统一 HTTP 引擎构造（GET/POST）。"""
    url_template = spec.get("url", "")
    headers = spec.get("headers", {"Content-Type": "application/json"})
    query_param = spec.get("query_param", "q")
    fmt = spec.get("format", "")
    timeout = spec.get("timeout", 8)
    extra_params = spec.get("extra_params", {})
    output_map = spec.get("output_map", {})
    is_get = spec.get("method", "GET") == "GET"
    body_template = spec.get("body", {})

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        import urllib.parse as up

        if is_get:
            resolved_url = _resolve(url_template, query, n, **kwargs)
            parts: list[str] = []
            if query_param:  # 空字符串表示该 API 不用查询参数名（仅 extra_params）
                parts.append(f"{query_param}={up.quote(query)}")
            if fmt:
                parts.append(f"format={up.quote(str(fmt))}")
            for k, v in extra_params.items():
                # 语言参数动态化（v2.7）：按查询主语言覆盖静态 setlang/hl/lang
                if k in ("setlang", "hl", "lang", "uselang"):
                    v = _lang_param(k, query) or v
                parts.append(f"{k}={up.quote(_resolve(str(v), query, n, **kwargs))}")
            if parts:
                separator = "&" if "?" in resolved_url else "?"
                full_url = resolved_url + separator + "&".join(parts)
            else:
                full_url = resolved_url
            req = urllib.request.Request(full_url, headers={k: _resolve(v, query, n, **kwargs) for k, v in headers.items()})
        else:
            body: dict[str, Any] = {}
            for k, v in body_template.items():
                resolved = _resolve(str(v), query, n, **kwargs)
                if k == "search_depth":
                    body[k] = depth
                elif resolved.lower() == "true":
                    body[k] = True
                elif resolved.lower() == "false":
                    body[k] = False
                else:
                    try:
                        body[k] = int(resolved)
                    except ValueError:
                        try:
                            body[k] = float(resolved)
                        except ValueError:
                            body[k] = resolved
            req = urllib.request.Request(url_template, data=json.dumps(body).encode("utf-8"),
                                         headers={k: _resolve(v, query, n, **kwargs) for k, v in headers.items()})

        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                raw = resp.read().decode("utf-8")
                if fmt == "xml":
                    return _parse_xml(raw, spec.get("_name", ""))
                data = json.loads(raw)
                eng = spec.get("_name", "")
                limit = max(1, int(n or 5))
                # 专用 JSON 解析器优先（DDG Instant Answer / UAPI / Semantic Scholar 等）
                custom = _CUSTOM_JSON_PARSERS.get(eng)
                if custom:
                    return _ensure_engine_source(custom(data), eng)[:limit]
                if output_map:
                    items_path = output_map.get("items", "")
                    # 根节点即为数组时（HF / dev.to / polymarket），items 用 "."
                    if isinstance(data, list) and not items_path:
                        items_path = "."
                    parsed = _make_field_parser(items_path, {
                        "title": output_map.get("item_title", "title"),
                        "url": output_map.get("item_url", "url"),
                        "snippet": output_map.get("item_summary", "snippet"),
                        "source": output_map.get("item_source", "source"),
                        "published_at": output_map.get("item_published_at", "published_at"),
                    }, url_template=output_map.get("url_template"))(data)
                    for r in parsed:
                        r.setdefault("source", eng)
                        if isinstance(r.get("snippet"), str) and len(r["snippet"]) > 300:
                            r["snippet"] = r["snippet"][:300]
                    # preserve_source（声明式 spec）：保留 API 返回的真实来源标注
                    return _ensure_engine_source(
                        parsed, eng, preserve=bool(spec.get("preserve_source"))
                    )[:limit]
                if isinstance(data, list):
                    return _ensure_engine_source(
                        _parse_generic({"results": data}, eng), eng
                    )[:limit]
                return _ensure_engine_source(_parse_generic(data, eng), eng)[:limit]
        except Exception as e:
            logger.warning(f"HTTP 引擎失败: {e}")
            return []
    return _engine


# ── HTML 网页解析引擎 ─────────────────────────────────────────────────────────

def _load_parse_maps() -> dict:
    """加载 parse_maps.yaml（声明式 CSS 选择器映射）。"""
    maps_path = Path(__file__).parent.parent / "sub-skills" / "local-search" / "parse_maps.yaml"
    if not maps_path.exists():
        return {}
    try:
        import yaml
        with open(maps_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _detect_anti_bot(html: str) -> bool:
    """检测反爬/拦截页面。只检查关键区域，避免正文误判。"""
    if not html:
        return True
    if len(html.strip()) < 500:
        return True
    # 只在前 2000 字符（head 区域）检测反爬标记
    head_section = html[:2000].lower()
    anti_bot_head = [
        "captcha", "challenge", "cf-browser-verification",
        "access denied", "rate limit", "too many requests",
        "checking your browser", "ddos-guard", "perimeterx",
    ]
    for marker in anti_bot_head:
        if marker in head_section:
            return True
    # 如果页面有大量链接且内容充实，判定为正常结果页
    if len(html) > 50000:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            if len(soup.find_all("a")) > 20:
                return False
        except Exception:
            pass
    return False


def _build_html_engine(spec: dict[str, Any]) -> Any:
    """HTML 网页解析引擎：HTTP 抓取 + BeautifulSoup CSS 选择器解析。"""
    url_template = spec.get("url", "")
    # 注意：不设置 Accept-Encoding，让 urllib 自动处理 gzip/deflate
    # 设 Accept-Encoding: br 会导致收到 Brotli 压缩但 urllib 无法解压
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    # 覆盖自定义 headers
    headers.update(spec.get("headers", {}))
    query_param = spec.get("query_param", "q")
    timeout = spec.get("timeout", 8)
    extra_params = spec.get("extra_params", {})
    engine_name = spec.get("_name", "html")
    _parse_maps_cache: dict = {}

    def _get_parse_maps() -> dict:
        if not _parse_maps_cache:
            _parse_maps_cache.update(_load_parse_maps())
        return _parse_maps_cache

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        resolved_url = _resolve(url_template, query, n)
        separator = "&" if "?" in resolved_url else "?"
        full_url = f"{resolved_url}{separator}{query_param}={up.quote(query)}"
        for k, v in extra_params.items():
            # 语言参数动态化（v2.7）：按查询主语言覆盖静态 setlang/hl/lang，
            # 让 local_bing/local_google 等对非中文查询返回对应语言结果。
            if k in ("setlang", "hl", "lang", "uselang"):
                v = _lang_param(k, query) or v
            full_url += f"&{k}={up.quote(_resolve(str(v), query, n))}"
        try:
            req = urllib.request.Request(full_url, headers={k: _resolve(v, query, n) for k, v in headers.items()})
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return []
        if _detect_anti_bot(html):
            return []
        maps = _get_parse_maps()
        html_maps = maps.get("html", {})
        mapping = html_maps.get(engine_name, html_maps.get("default", {}))
        container_sel = mapping.get("container")
        title_sel = mapping.get("title", "h2 a, h3 a")
        url_sel = mapping.get("url", "a")
        snippet_sel = mapping.get("snippet")
        url_attr = mapping.get("url_attr", "href")
        default_score = mapping.get("score", 0.7)
        if not container_sel:
            return []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            containers = soup.select(container_sel)
        except Exception:
            return []
        from bs4 import NavigableString

        def _el_text(el) -> str:
            """提取元素文本。兼容 bs4 4.13+ 将页面文本标记为 TemplateString、
            get_text()/strings() 失效（如萌娘百科搜索结果页）的情况。"""
            if el is None:
                return ""
            if isinstance(el, str):
                return el
            parts = []
            stack = list(el.contents)
            while stack:
                node = stack.pop(0)
                if isinstance(node, NavigableString):
                    parts.append(str(node))
                elif hasattr(node, "contents"):
                    stack = node.contents + stack
            return "".join(parts)

        results = []
        for idx, item in enumerate(containers[:n * 2]):
            try:
                title_el = item.select_one(title_sel) if title_sel else None
                url_el = item.select_one(url_sel) if url_sel else None
                snippet_el = item.select_one(snippet_sel) if snippet_sel else None
                title = _el_text(title_el).strip()[:200] if title_el else ""
                url = ""
                if url_el and url_el.has_attr(url_attr):
                    url = url_el[url_attr]
                elif item.has_attr(url_attr):
                    # 容器自身带链接属性（如 <a class="item" href="..."> 自引用结构）
                    url = item[url_attr]
                snippet = _el_text(snippet_el).strip()[:300] if snippet_el else ""
                if not title and not url:
                    continue
                if url and url.startswith("/"):
                    from urllib.parse import urljoin
                    url = urljoin(resolved_url, url)
                score = max(default_score - idx * 0.05, 0.1)
                results.append({"title": title, "url": url, "snippet": snippet, "score": round(score, 3), "source": engine_name})
            except Exception:
                continue
        return results[:n]
    return _engine

# ── 通用解析器 ─────────────────────────────────────────────────────────────────

def _parse_text_output(text: str, engine_name: str, output_format: str = "", n: int = 10) -> list[dict[str, Any]]:
    """通用 CLI 文本解析：YAML（声明式）/ JSON / 结构化文本。n 限制返回条数。"""
    if not text or not text.strip():
        return []
    text = text.strip()
    if output_format == "yaml":
        return _parse_yaml_output(text, engine_name, n=n)
    try:
        data = json.loads(text)
        limit = max(1, n)
        if isinstance(data, list):
            out = []
            for i in data[:limit]:
                if not isinstance(i, dict):
                    continue
                r = {"title": i.get("title", ""), "url": i.get("url", ""),
                     "snippet": i.get("snippet", i.get("content", ""))[:300],
                     "source": engine_name}
                if i.get("published_at"):
                    r["published_at"] = str(i["published_at"])[:64]
                out.append(r)
            return out
        if isinstance(data, dict):
            items = data.get("results", data.get("items", data.get("data", [])))
            if isinstance(items, list):
                out = []
                for i in items[:limit]:
                    if not isinstance(i, dict):
                        continue
                    r = {"title": i.get("title", ""), "url": i.get("url", ""),
                         "snippet": i.get("snippet", i.get("content", ""))[:300],
                         "source": engine_name}
                    if i.get("published_at"):
                        r["published_at"] = str(i["published_at"])[:64]
                    out.append(r)
                return out
    except (json.JSONDecodeError, ValueError):
        pass

    results, cur = [], {}
    seen_url = False
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("### "):
            if cur:
                results.append(cur)
            cur = {"title": re.sub(r'^\d+\.\s*', '', s[4:].strip()), "source": engine_name,
                   "score": max(1.0 - len(results) * 0.1, 0.1)}
            seen_url = False
        elif s.startswith("- **URL**: ") and cur:
            cur["url"] = s[11:].strip()
            seen_url = True
        elif s.startswith("- ") and not s.startswith("- **") and seen_url and cur:
            cur["snippet"] = " ".join(s[2:].strip().split())[:300]
            seen_url = False
    if cur:
        results.append(cur)
    return results[:max(1, n)]


def _parse_yaml_output(text: str, engine_name: str, n: int = 10) -> list[dict[str, Any]]:
    """解析 YAML 输出（结构化 CLI 数据源的默认格式）。

    支持顶层 list，或 dict 携带 results/items/data 列表；
    字段别名：snippet|description；保留 published_at 时间维度。
    """
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception:
        return []
    if isinstance(data, dict):
        items = data.get("results", data.get("items", data.get("data", [])))
    elif isinstance(data, list):
        items = data
    else:
        return []
    if not isinstance(items, list):
        return []
    results = []
    for i in items:
        if not isinstance(i, dict):
            continue
        title = i.get("title") or i.get("name") or ""
        url = i.get("url") or i.get("link") or ""
        snippet = i.get("snippet") or i.get("description") or i.get("content") or ""
        if not title and not url:
            continue
        r = {
            "title": str(title)[:200],
            "url": str(url),
            "snippet": str(snippet)[:300],
            "source": engine_name,
        }
        published = i.get("published_at")
        if published:
            r["published_at"] = str(published)[:64]
        results.append(r)
    return results[:max(1, n)]


def _parse_xml(text: str, engine_name: str) -> list[dict[str, Any]]:
    """解析 Atom XML（arXiv 等）。"""
    import xml.etree.ElementTree as ET
    results = []
    try:
        root = ET.fromstring(text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//atom:entry", ns) or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for entry in entries:
            title = entry.findtext("atom:title", "", ns).strip().replace("\n", " ")[:200]
            summary = entry.findtext("atom:summary", "", ns).strip().replace("\n", " ")[:300]
            entry_id = entry.findtext("atom:id", "", ns)
            url = entry_id
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "pdf":
                    url = link.get("href", url)
                    break
            if title:
                results.append({"title": title, "url": url, "snippet": summary, "source": engine_name})
    except ET.ParseError:
        pass
    return results


def _parse_generic(data: dict[str, Any], engine_name: str = "?") -> list[dict[str, Any]]:
    """通用 JSON 解析：自动探测常见列表路径与字段别名。

    列表探测（最多 3 层，只走已知键）：
      - 顶层 results/items/data/works/search 等
      - 一层嵌套（data.results / data.value）
      - 二层嵌套（data.webPages.value —— 博查等 AI 搜索 API）

    字段别名：title|name|heading；url|URL|html_url|link|href；
    snippet|summary|content|description|abstract。
    """
    list_keys = (
        "results", "items", "value", "works", "search", "data",
        "organic", "webPages", "hits", "documents", "entries",
    )

    def _find_items(obj: Any, depth: int = 0) -> list | None:
        if depth > 3 or obj is None:
            return None
        if isinstance(obj, list):
            # 空列表或 dict 元素列表视为结果集；纯标量列表跳过
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
        # 浅层扫描嵌套 dict（避免深扫整棵树误抓无关数组）
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

    results: list[dict[str, Any]] = []
    for i in items:
        if not isinstance(i, dict):
            continue
        title = i.get("title") or i.get("name") or i.get("heading") or i.get("Title") or ""
        if isinstance(title, list):
            title = title[0] if title else ""
        url = (
            i.get("url") or i.get("URL") or i.get("html_url")
            or i.get("link") or i.get("href") or ""
        )
        snippet = (
            i.get("snippet") or i.get("summary") or i.get("content")
            or i.get("description") or i.get("abstract") or ""
        )
        if isinstance(snippet, list):
            snippet = snippet[0] if snippet else ""
        score = i.get("score", i.get("relevance_score", 0.5))
        results.append({
            "title": str(title)[:200],
            "url": str(url),
            "snippet": str(snippet)[:300],
            "score": score,
            "source": engine_name,
        })
    return results[:10]


def _ensure_engine_source(
    results: list[dict[str, Any]] | Any, engine_name: str, preserve: bool = False
) -> list[dict[str, Any]]:
    """纠正结果 source，避免 HTTP 解析器错标（如 uapi→stackoverflow）。

    规则：
      - 空 / generic → 设为引擎名
      - source 既不等于引擎名、也不以「引擎名/」开头 → 纠正为引擎名
      - wigolo_npx 允许保留 wigolo/... 子源标注
      - preserve=True（声明式 spec 的 preserve_source）时保留 API 返回的
        真实来源标注（如聚合资讯引擎的上游发布方），只对空/generic 兜底
    """
    if not isinstance(results, list) or not engine_name:
        return results if isinstance(results, list) else []
    for r in results:
        if not isinstance(r, dict) or "error" in r:
            continue
        src = str(r.get("source") or "")
        if engine_name == "wigolo_npx" and src.startswith("wigolo"):
            continue
        if not src or src == "generic":
            r["source"] = engine_name
        elif not preserve and src != engine_name and not src.startswith(engine_name + "/"):
            r["source"] = engine_name
    return results


def _parse_duckduckgo(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 DuckDuckGo Instant Answer API 响应。"""
    if not isinstance(data, dict):
        return []
    results: list[dict[str, Any]] = []

    abstract = data.get("Abstract", "")
    if abstract:
        results.append({
            "title": data.get("Heading", "DuckDuckGo Answer"),
            "url": data.get("AbstractURL", ""),
            "snippet": abstract[:300],
            "source": "duckduckgo",
        })

    for topic in data.get("RelatedTopics", [])[:5]:
        if isinstance(topic, dict) and "Text" in topic:
            results.append({
                "title": topic.get("Text", "")[:100],
                "url": topic.get("FirstURL", ""),
                "snippet": topic.get("Text", "")[:300],
                "source": "duckduckgo",
            })

    return results[:5]


def _parse_uapi(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 UAPI 搜索响应。"""
    if not isinstance(data, dict):
        return []
    results = data.get("results", [])
    if not isinstance(results, list):
        return []
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", "")[:300],
            "source": "uapi",
        }
        for r in results if isinstance(r, dict)
    ][:10]


def _parse_semantic_scholar(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 Semantic Scholar API 响应。"""
    if not isinstance(data, dict):
        return []
    papers = data.get("data") or []
    if not isinstance(papers, list):
        return []
    results: list[dict[str, Any]] = []

    for paper in papers:
        if not isinstance(paper, dict):
            continue

        title = paper.get("title", "")
        abstract = paper.get("abstract", "") or ""
        citation_count = paper.get("citationCount", 0) or 0

        pdf_info = paper.get("openAccessPdf", {})
        url = pdf_info.get("url", "") if isinstance(pdf_info, dict) else ""

        authors = paper.get("authors", [])
        author_names = [a.get("name", "") for a in authors if isinstance(a, dict)][:3]
        author_str = ", ".join(author_names)

        snippet = f"{abstract[:200]}"
        if citation_count:
            snippet += f" [引用: {citation_count}]"
        if author_str:
            snippet += f" [作者: {author_str}]"

        results.append({
            "title": title,
            "url": url,
            "snippet": snippet[:300],
            "score": min(1.0, citation_count / 1000) if citation_count else 0.5,
            "source": "semantic_scholar",
        })

    return results[:10]


# 引擎名 → 专用 JSON 解析器（无 output_map 时的精确格式）
_CUSTOM_JSON_PARSERS: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] = {
    "duckduckgo": _parse_duckduckgo,
    "uapi": _parse_uapi,
    "semantic_scholar": _parse_semantic_scholar,
}



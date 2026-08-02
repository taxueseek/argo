#!/usr/bin/env python3
"""engines.py — Unified Search v2 引擎适配层

配置驱动 + 声明式 output_map 字段提取 + 通用 parser 兜底。
支持 cli / http(GET/POST) 类型，所有异常吞没返回 []。
"""

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

    @safe_search
    def _engine(query: str, n: int = 5, timeout: float = 8, mode: str = "fast", **kwargs) -> list[dict[str, Any]]:
        cmd = _resolve(cmd_template, query, n, mode=mode)
        args = _resolve(search_args, query, n, mode=mode)
        if not cmd:
            return []
        env = os.environ.copy()
        env.update(env_overrides)
        return _parse_text_output(_run(cmd + args, timeout=timeout, engine_name=spec.get("_name", "cli")),
                                  spec.get("_name", "cli"))
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
            resolved_url = _resolve(url_template, query, n)
            parts: list[str] = []
            if query_param:  # 空字符串表示该 API 不用查询参数名（仅 extra_params）
                parts.append(f"{query_param}={up.quote(query)}")
            if fmt:
                parts.append(f"format={up.quote(str(fmt))}")
            for k, v in extra_params.items():
                parts.append(f"{k}={up.quote(_resolve(str(v), query, n))}")
            if parts:
                separator = "&" if "?" in resolved_url else "?"
                full_url = resolved_url + separator + "&".join(parts)
            else:
                full_url = resolved_url
            req = urllib.request.Request(full_url, headers={k: _resolve(v, query, n) for k, v in headers.items()})
        else:
            body: dict[str, Any] = {}
            for k, v in body_template.items():
                resolved = _resolve(str(v), query, n)
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
                                         headers={k: _resolve(v, query, n) for k, v in headers.items()})

        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                raw = resp.read().decode("utf-8")
                if fmt == "xml":
                    return _parse_xml(raw, spec.get("_name", ""))
                data = json.loads(raw)
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
                    }, url_template=output_map.get("url_template"))(data)
                    eng = spec.get("_name", "")
                    for r in parsed:
                        r.setdefault("source", eng)
                        if isinstance(r.get("snippet"), str) and len(r["snippet"]) > 300:
                            r["snippet"] = r["snippet"][:300]
                    return parsed[: max(1, int(n or 5))]
                if isinstance(data, list):
                    return _parse_generic({"results": data}, spec.get("_name", ""))
                return _parse_generic(data, spec.get("_name", ""))
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


# ── Exa 专用引擎 ──────────────────────────────────────────────────────────────

def _build_exa_engine(spec: dict[str, Any]) -> Any:
    """Exa 语义搜索专用引擎（embedding 匹配 + 内容摘要）"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        api_key = os.environ.get("EXA_API_KEY", "")
        if not api_key:
            logger.warning("EXA_API_KEY 未设置")
            return []
        url = "https://api.exa.ai/search"
        body = json.dumps({
            "query": query,
            "type": "auto",
            "numResults": min(n, 10),
            "contents": {"text": {"maxCharacters": 400}},
        }).encode("utf-8")
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = []
                for r in data.get("results", []):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("text", "")[:300] if r.get("text") else r.get("snippet", ""),
                        "source": "exa",
                        "score": r.get("score", 0.0),
                    })
                return results
        except Exception as e:
            logger.warning(f"Exa 引擎失败: {e}")
            return []
    return _engine


# ── 搜狗微信搜索引擎 ─────────────────────────────────────────────────────────

def _build_wechat_sogou_engine(spec: dict[str, Any]) -> Any:
    """搜狗微信搜索引擎（weixin.sogou.com）

    抓取搜狗微信搜索结果页，提取公众号文章标题、链接、摘要、公众号名。
    无需登录，无需 API key，纯 HTML 解析。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://weixin.sogou.com/weixin?type=2&query={up.quote(query)}&ie=utf8"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8")
            results = []
            li_pattern = re.compile(
                r'<li\s+id="sogou_vr_11002601_box_\d+"[^>]*>(.*?)</li>', re.DOTALL
            )
            for li in li_pattern.findall(html)[:n]:
                title_match = re.search(
                    r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', li, re.DOTALL
                )
                if not title_match:
                    continue
                href = title_match.group(1).replace("&amp;", "&")
                title = re.sub(r"<[^>]+>", "", title_match.group(2)).strip()
                title = title.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")

                summary_match = re.search(
                    r'<p[^>]*class="txt-info"[^>]*>(.*?)</p>', li, re.DOTALL
                )
                summary = re.sub(r"<[^>]+>", "", summary_match.group(1)).strip() if summary_match else ""
                summary = summary.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")

                account_match = re.search(
                    r'<span[^>]*class="all-time-y2"[^>]*>(.*?)</span>', li, re.DOTALL
                )
                account = re.sub(r"<[^>]+>", "", account_match.group(1)).strip() if account_match else ""

                results.append({
                    "title": title[:80],
                    "url": "https://weixin.sogou.com" + href if href.startswith("/") else href,
                    "snippet": summary[:200],
                    "account": account,
                    "source": "wechat_sogou",
                })
            return results
        except Exception as e:
            logger.warning(f"搜狗微信搜索失败: {e}")
            return []
    return _engine


# ── Hacker News 搜索引擎 ──────────────────────────────────────────────────────

def _build_hackernews_engine(spec: dict[str, Any]) -> Any:
    """Hacker News 搜索（Algolia API）"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://hn.algolia.com/api/v1/search?query={up.quote(query)}&tags=story&hitsPerPage={min(n, 10)}"
        headers = {"User-Agent": "argo-search/1.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read())
            results = []
            for h in data.get("hits", []):
                results.append({
                    "title": h.get("title", ""),
                    "url": h.get("url", f"https://news.ycombinator.com/item?id={h.get('objectID', '')}"),
                    "snippet": f"score: {h.get('points', 0)} | comments: {h.get('num_comments', 0)} | by: {h.get('author', '')}",
                    "source": "hackernews",
                })
            return results
        except Exception as e:
            logger.warning(f"HackerNews 引擎失败: {e}")
            return []
    return _engine


# ── Stack Overflow 搜索引擎 ───────────────────────────────────────────────────

def _build_stackoverflow_engine(spec: dict[str, Any]) -> Any:
    """Stack Overflow 搜索（Stack Exchange API）"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&q={up.quote(query)}&site=stackoverflow&pagesize={min(n, 10)}"
        headers = {"User-Agent": "argo-search/1.0", "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                import gzip
                raw = resp.read()
                try:
                    data = json.loads(gzip.decompress(raw))
                except Exception:
                    data = json.loads(raw)
            results = []
            for item in data.get("items", []):
                tags = ", ".join(item.get("tags", [])[:3])
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": f"score: {item.get('score', 0)} | answers: {item.get('answer_count', 0)} | tags: {tags}",
                    "source": "stackoverflow",
                })
            return results
        except Exception as e:
            logger.warning(f"StackOverflow 引擎失败: {e}")
            return []
    return _engine


# ── Google Scholar 搜索引擎 ───────────────────────────────────────────────────

def _build_google_scholar_engine(spec: dict[str, Any]) -> Any:
    """Google Scholar 搜索（HTTP 页面解析）"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://scholar.google.com/scholar?q={up.quote(query)}&hl=en&as_sdt=0%2C5&num={min(n, 10)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8")
            results = []
            titles = re.findall(r'<h3[^>]*class="[^"]*gs_rt[^"]*"[^>]*>(.*?)</h3>', html, re.DOTALL)
            snippets = re.findall(r'<div[^>]*class="[^"]*gs_rs[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
            for i, t in enumerate(titles[:n]):
                title = re.sub(r'<[^>]+>', '', t).strip()
                snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
                if title:
                    results.append({
                        "title": title[:100],
                        "url": f"https://scholar.google.com/scholar?q={up.quote(title[:50])}",
                        "snippet": snippet[:200],
                        "source": "google_scholar",
                    })
            return results
        except Exception as e:
            logger.warning(f"Google Scholar 引擎失败: {e}")
            return []
    return _engine


# ── V2EX 搜索引擎 ─────────────────────────────────────────────────────────────

def _build_v2ex_engine(spec: dict[str, Any]) -> Any:
    """V2EX 社区搜索"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://www.v2ex.com/search?q={up.quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8")
            results = []
            titles = re.findall(r'<span[^>]*class="[^"]*item_title[^"]*"[^>]*>(.*?)</span>', html, re.DOTALL)
            for t in titles[:n]:
                title = re.sub(r'<[^>]+>', '', t).strip()
                if title:
                    results.append({
                        "title": title[:80],
                        "url": f"https://www.v2ex.com/search?q={up.quote(query)}",
                        "snippet": "V2EX 社区讨论",
                        "source": "v2ex",
                    })
            return results
        except Exception as e:
            logger.warning(f"V2EX 引擎失败: {e}")
            return []
    return _engine


# ── 同花顺热点引擎 ─────────────────────────────────────────────────────────────

def _build_ths_hot_engine(spec: dict[str, Any]) -> Any:
    """同花顺当日强势股 + 题材归因（独家能力）

    不只告诉你"哪些走强"，还告诉你"为什么走强"——同花顺编辑部人工运营的题材标签。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        from datetime import date as _date
        trade_date = _date.today().strftime("%Y-%m-%d")

        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{trade_date}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "Chrome/117.0.0.0 Safari/537.36"
            )
        }
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read())
            if data.get("errocode", 0) != 0:
                return []
            rows = data.get("data") or []
            results = []
            for r in rows[:n]:
                results.append({
                    "title": f"{r.get('name', '')}({r.get('code', '')}) +{r.get('zhangfu', 0)}%",
                    "url": f"https://quote.eastmoney.com/{r.get('code', '')}.html",
                    "snippet": f"题材: {r.get('reason', '未知')} | 换手{r.get('huanshou', 0)}% | 成交额{r.get('chengjiaoe', 0)/1e8:.1f}亿",
                    "source": "ths_hot",
                })
            return results
        except Exception as e:
            logger.warning(f"同花顺热点引擎失败: {e}")
            return []
    return _engine


# ── 财联社电报引擎 ─────────────────────────────────────────────────────────────

def _build_cls_telegraph_engine(spec: dict[str, Any]) -> Any:
    """财联社电报（全市场实时快讯，v1 API + 本地签名，零 key）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import hashlib
        from datetime import datetime
        to = _timeout or timeout
        params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
                  "last_time": "", "refresh_type": "1", "rn": str(n)}
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
        url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("roll_data", []) or []:
                ts = item.get("ctime")
                t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
                title = item.get("title", "") or item.get("brief", "")
                # 关键词过滤
                if query and query.strip():
                    keywords = query.strip().split()
                    if not any(kw.lower() in (title + item.get("content", "")).lower() for kw in keywords):
                        continue
                results.append({
                    "title": title[:80],
                    "url": "https://www.cls.cn/",
                    "snippet": f"{t} | {(item.get('content', '') or item.get('brief', ''))[:150]}",
                    "source": "cls_telegraph",
                })
            return results[:n]
        except Exception as e:
            logger.warning(f"财联社电报引擎失败: {e}")
            return []
    return _engine


# ── 东财全球资讯引擎 ─────────────────────────────────────────────────────────

def _build_em_global_news_engine(spec: dict[str, Any]) -> Any:
    """东财全球财经资讯（7×24 滚动）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import uuid
        to = _timeout or timeout
        url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        params = {
            "client": "web", "biz": "web_724",
            "fastColumn": "102", "sortEnd": "",
            "pageSize": str(n * 2),  # 多拉一些用于过滤
            "req_trace": str(uuid.uuid4()),
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://kuaixun.eastmoney.com/"}
        try:
            req = urllib.request.Request(url + "?" + "&".join(f"{k}={v}" for k, v in params.items()), headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("fastNewsList", []):
                title = item.get("title", "")
                # 关键词过滤
                if query and query.strip():
                    keywords = query.strip().split()
                    if not any(kw.lower() in (title + item.get("summary", "")).lower() for kw in keywords):
                        continue
                results.append({
                    "title": title[:80],
                    "url": "https://kuaixun.eastmoney.com/",
                    "snippet": f"{item.get('showTime', '')} | {(item.get('summary', '') or '')[:150]}",
                    "source": "em_global_news",
                })
            return results[:n]
        except Exception as e:
            logger.warning(f"东财全球资讯引擎失败: {e}")
            return []
    return _engine



# ── 东财财经搜索引擎 ─────────────────────────────────────────────────────────

def _build_eastmoney_engine(spec: dict[str, Any]) -> Any:
    """东财经搜搜索（纯 HTTP API，零外部依赖）

    支持：
    - 个股新闻搜索（按股票代码或关键词）
    - 东财全球资讯（7×24 财经快讯）
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        import re
        to = _timeout or timeout

        # 判断是股票代码（6位数字）还是关键词
        is_stock_code = re.match(r'^\d{6}$', query.strip())

        if is_stock_code:
            # 按股票代码搜新闻
            return _eastmoney_stock_news(query.strip(), n, to)
        else:
            # 按关键词搜全球资讯
            return _eastmoney_keyword_news(query, n, to)

    def _eastmoney_stock_news(code: str, n: int, to: float) -> list[dict[str, Any]]:
        """按股票代码搜新闻"""
        import json as _json
        import urllib.parse as up
        cb = "jQuery_news"
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        inner_params = _json.dumps({
            "uid": "", "keyword": code, "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                      "pageIndex": 1, "pageSize": n, "preTag": "", "postTag": ""}},
        }, separators=(',', ':'))
        params = {"cb": cb, "param": inner_params}
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com/"}
        try:
            full_url = url + "?" + "&".join(f"{k}={up.quote(str(v))}" for k, v in params.items())
            req = urllib.request.Request(full_url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                text = resp.read().decode("utf-8")
            json_str = text[text.index("(") + 1:text.rindex(")")]
            d = _json.loads(json_str)
            articles = d.get("result", {}).get("cmsArticleWebOld", []) or []
            results = []
            for a in articles[:n]:
                results.append({
                    "title": re.sub(r'<[^>]+>', '', a.get("title", ""))[:80],
                    "url": a.get("url", ""),
                    "snippet": re.sub(r'<[^>]+>', '', a.get("content", ""))[:200],
                    "source": "eastmoney",
                })
            return results
        except Exception as e:
            logger.warning(f"东财个股新闻搜索失败: {e}")
            return []

    def _eastmoney_keyword_news(query: str, n: int, to: float) -> list[dict[str, Any]]:
        """按关键词搜全球资讯"""
        import uuid
        url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        params = {
            "client": "web", "biz": "web_724", "fastColumn": "102",
            "sortEnd": "", "pageSize": str(n * 2),
            "req_trace": str(uuid.uuid4()),
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://kuaixun.eastmoney.com/"}
        try:
            full_url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
            req = urllib.request.Request(full_url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("fastNewsList", [])[:n]:
                title = item.get("title", "")
                summary = (item.get("summary", "") or "")[:200]
                if query:
                    keywords = query.strip().split()
                    if not any(kw.lower() in (title + summary).lower() for kw in keywords):
                        continue
                results.append({
                    "title": title[:80],
                    "url": "https://kuaixun.eastmoney.com/",
                    "snippet": f"{item.get('showTime', '')} | {summary}",
                    "source": "eastmoney",
                })
            return results
        except Exception as e:
            logger.warning(f"东财资讯搜索失败: {e}")
            return []

    return _engine


# ── itotii 梗百科引擎 ────────────────────────────────────────────────────────

def _build_itotii_engine(spec: dict[str, Any]) -> Any:
    """itotii 梗百科（中文流行语/网络梗词条，WordPress REST API，免认证）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = (
            "https://geng.itotii.com/wp-json/wp/v2/posts?search="
            + up.quote(query) + f"&per_page={min(n, 10)}"
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            results = []
            for p in data:
                title = p.get("title", {})
                title = title.get("rendered", "") if isinstance(title, dict) else str(title)
                content = p.get("content", {})
                content = content.get("rendered", "") if isinstance(content, dict) else str(content)
                title = re.sub(r"<[^>]+>", "", title).strip()
                content = re.sub(r"<[^>]+>", "", content).strip()
                if not title:
                    continue
                results.append({
                    "title": title[:80],
                    "url": p.get("link", ""),
                    "snippet": f"{p.get('date', '')[:10]} | {content[:200]}",
                    "source": "itotii",
                    "score": max(1.0 - len(results) * 0.1, 0.1),
                })
            return results[:n]
        except Exception as e:
            logger.warning(f"itotii 梗百科失败: {e}")
            return []
    return _engine


# ── 百度热搜引擎 ─────────────────────────────────────────────────────────────

def _build_baidu_hot_engine(spec: dict[str, Any]) -> Any:
    """百度热搜（top.baidu.com 实时热搜榜，HTML 解析）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://top.baidu.com/board?tab=realtime"
        headers = {"User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                page = resp.read().decode("utf-8", "replace")
            words = re.findall(r'word":"([^"]+)"', page)
            results, seen = [], set()
            for w in words:
                if w in seen:
                    continue
                seen.add(w)
                results.append({
                    "title": w[:60],
                    "url": "https://www.baidu.com/s?wd=" + up.quote(w),
                    "snippet": "百度热搜",
                    "source": "baidu_hot",
                    "score": max(1.0 - len(results) * 0.05, 0.1),
                })
                if len(results) >= n:
                    break
            return results
        except Exception as e:
            logger.warning(f"百度热搜失败: {e}")
            return []
    return _engine


# ── 今日头条热榜引擎 ─────────────────────────────────────────────────────────

def _build_toutiao_hot_engine(spec: dict[str, Any]) -> Any:
    """今日头条热榜（hot-board JSON，免认证）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.toutiao.com/"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            results = []
            for i, item in enumerate(data.get("data", [])[:n]):
                hot = item.get("HotValue", "")
                results.append({
                    "title": item.get("Title", "")[:60],
                    "url": item.get("Url", ""),
                    "snippet": f"热度 {hot}" if hot else "今日头条热榜",
                    "source": "toutiao_hot",
                    "score": max(1.0 - i * 0.05, 0.1),
                })
            return results
        except Exception as e:
            logger.warning(f"今日头条热榜失败: {e}")
            return []
    return _engine


# ── B站热搜引擎 ──────────────────────────────────────────────────────────────

def _build_bilibili_hot_engine(spec: dict[str, Any]) -> Any:
    """B站热搜（search/square 热搜词，免认证）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://api.bilibili.com/x/web-interface/search/square?limit=20"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            items = data.get("data", {}).get("trending", {}).get("list", [])
            results = []
            for i, item in enumerate(items[:n]):
                kw = item.get("keyword", "")
                if not kw:
                    continue
                results.append({
                    "title": kw[:60],
                    "url": "https://search.bilibili.com/all?keyword=" + up.quote(kw),
                    "snippet": "B站热搜",
                    "source": "bilibili_hot",
                    "score": max(1.0 - i * 0.05, 0.1),
                })
            return results
        except Exception as e:
            logger.warning(f"B站热搜失败: {e}")
            return []
    return _engine


# ── Open Library 图书引擎 ────────────────────────────────────────────────────

def _build_open_library_engine(spec: dict[str, Any]) -> Any:
    """Open Library 图书搜索（openlibrary.org/search.json，免认证）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        # OpenLibrary 要求查询至少 3 个字符（如「三体」只有 2 字会 422）
        q = query if len(query) >= 3 else query * 2
        url = "https://openlibrary.org/search.json?" + up.urlencode({
            "q": q, "limit": min(n, 20),
        })
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "argo-search/2.4 (unified-search@local)"}), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"Open Library 失败: {e}")
            return []
        results = []
        for d in (data.get("docs") or [])[:n]:
            title = d.get("title", "")
            key = d.get("key", "")
            if not title and not key:
                continue
            authors = ", ".join((d.get("author_name") or [])[:3])
            year = d.get("first_publish_year") or ""
            parts = [p for p in (authors, str(year)) if p]
            results.append({
                "title": title,
                "url": f"https://openlibrary.org{key}" if key else "",
                "snippet": " · ".join(parts)[:300],
                "source": "open_library",
                "score": 0.7,
            })
        return results
    return _engine


# ── 英英词典引擎 ─────────────────────────────────────────────────────────────

def _build_free_dictionary_engine(spec: dict[str, Any]) -> Any:
    """英英词典（dictionaryapi.dev，免认证，响应为数组需专门解析）"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        q = re.sub(r"(?i)^(what\s+(does|is|do|are)\s+|define\s+|definition\s+of\s+|meaning\s+of\s+|the\s+word\s+)", "", query.strip())
        q = re.sub(r"(是什么意思|什么意思|怎么读|读音|英文|单词|的含义|的定义|咋读)$", "", q).strip()
        word = re.split(r"\s+", q)[0] if q else ""
        if not word:
            return []
        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{up.quote(word)}"
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "argo-search/2.4"}), timeout=to) as resp:
                entries = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []
        if not isinstance(entries, list):
            return []
        results = []
        for e in entries[:n]:
            w = e.get("word", "")
            if not w:
                continue
            urls = e.get("sourceUrls") or []
            eurl = urls[0] if urls else f"https://en.wiktionary.org/wiki/{w}"
            defs = []
            for m in (e.get("meanings") or [])[:3]:
                for d in (m.get("definitions") or [])[:2]:
                    defs.append(d.get("definition", ""))
            results.append({
                "title": w,
                "url": eurl,
                "snippet": " / ".join(x for x in defs if x)[:300],
                "source": "free_dictionary",
                "score": 0.8,
            })
        return results
    return _engine


# ── 百度百科（suggest 接口，免认证）──────────────────────────────────────────

def _build_baidu_baike_engine(spec: dict[str, Any]) -> Any:
    """百度百科词条搜索：优先 searchui/suggest，OpenAPI 卡片作精确补充。"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        q = query.strip()
        if not q:
            return []
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Referer": "https://baike.baidu.com/",
            "Accept": "application/json,text/plain,*/*",
        }
        results: list[dict[str, Any]] = []
        # 1) suggest 多候选
        sug_url = "https://baike.baidu.com/api/searchui/suggest?" + up.urlencode({"enc": "utf8", "wd": q})
        try:
            with urllib.request.urlopen(urllib.request.Request(sug_url, headers=headers), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            for item in (data.get("list") or [])[:n]:
                title = item.get("lemmaTitle") or ""
                lid = item.get("lemmaId") or ""
                desc = item.get("lemmaDesc") or ""
                if not title:
                    continue
                path = up.quote(title)
                url = f"https://baike.baidu.com/item/{path}/{lid}" if lid else f"https://baike.baidu.com/item/{path}"
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": str(desc)[:300],
                    "source": "baidu_baike",
                    "score": 0.85,
                })
        except Exception as e:
            logger.warning(f"baidu_baike suggest 失败: {e}")
        # 2) OpenAPI 卡片补充（精确词条摘要，可能 errno=2）
        if len(results) < n:
            card_url = "https://baike.baidu.com/api/openapi/BaikeLemmaCardApi?" + up.urlencode({
                "scope": "103", "format": "json", "appid": "379020",
                "bk_key": q, "bk_length": "600",
            })
            try:
                with urllib.request.urlopen(urllib.request.Request(card_url, headers=headers), timeout=to) as resp:
                    card = json.loads(resp.read().decode("utf-8", "replace"))
                if isinstance(card, dict) and card.get("errno") in (None, 0) and (card.get("title") or card.get("key")):
                    title = card.get("title") or card.get("key") or q
                    abstract = card.get("abstract") or card.get("desc") or ""
                    curl = card.get("url") or f"https://baike.baidu.com/item/{up.quote(str(title))}"
                    if not any(r.get("title") == title for r in results):
                        results.insert(0, {
                            "title": str(title),
                            "url": str(curl),
                            "snippet": str(abstract)[:300],
                            "source": "baidu_baike",
                            "score": 0.9,
                        })
            except Exception:
                pass
        return results[:n]
    return _engine


# ── PyPI（精确包名 JSON；搜索页有 JS challenge 故不做 HTML 搜）────────────────

def _build_pypi_engine(spec: dict[str, Any]) -> Any:
    """PyPI 包查询：从查询词抽取候选包名，逐个请求 /pypi/{name}/json。"""
    timeout = spec.get("timeout", 10)

    def _candidates(query: str) -> list[str]:
        q = query.strip()
        # 去掉常见修饰语
        q2 = re.sub(
            r"(?i)\b(pypi|python\s+package|python\s+lib(?:rary)?|pip\s+install|package|库|包|模块|依赖)\b",
            " ", q,
        )
        q2 = re.sub(r"\s+", " ", q2).strip()
        cands: list[str] = []
        for raw in (q2, q, *re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{1,80}", q2)):
            name = raw.strip().strip("\"'").lower().replace(" ", "-")
            name = re.sub(r"[^a-z0-9._-]", "", name)
            if len(name) >= 2 and name not in cands:
                cands.append(name)
        return cands[:8]

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        headers = {"User-Agent": "argo-search/2.5 (unified-search@local)", "Accept": "application/json"}
        for name in _candidates(query):
            if len(results) >= n:
                break
            url = f"https://pypi.org/pypi/{name}/json"
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=to) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception:
                continue
            info = data.get("info") if isinstance(data, dict) else None
            if not isinstance(info, dict):
                continue
            pkg = (info.get("name") or name).strip()
            key = pkg.lower()
            if key in seen:
                continue
            seen.add(key)
            summary = info.get("summary") or ""
            ver = info.get("version") or ""
            home = info.get("package_url") or info.get("project_url") or f"https://pypi.org/project/{pkg}/"
            snippet = f"{summary} · v{ver}".strip(" ·") if ver else summary
            results.append({
                "title": pkg,
                "url": home,
                "snippet": snippet[:300],
                "source": "pypi",
                "score": 0.9,
            })
        return results[:n]
    return _engine


# ── ClinicalTrials.gov v2 ────────────────────────────────────────────────────

def _build_clinicaltrials_engine(spec: dict[str, Any]) -> Any:
    """ClinicalTrials.gov API v2 试验检索（免认证）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://clinicaltrials.gov/api/v2/studies?" + up.urlencode({
            "query.term": query,
            "pageSize": min(n, 20),
            "format": "json",
        })
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "argo-search/2.5", "Accept": "application/json"}), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"clinicaltrials 失败: {e}")
            return []
        results = []
        for st in (data.get("studies") or [])[:n]:
            ps = st.get("protocolSection") or {}
            ident = ps.get("identificationModule") or {}
            desc = ps.get("descriptionModule") or {}
            status = (ps.get("statusModule") or {}).get("overallStatus") or ""
            nct = ident.get("nctId") or ""
            title = ident.get("briefTitle") or ident.get("officialTitle") or nct
            summary = desc.get("briefSummary") or status
            if not title and not nct:
                continue
            results.append({
                "title": str(title)[:200],
                "url": f"https://clinicaltrials.gov/study/{nct}" if nct else "",
                "snippet": str(summary).replace("\n", " ")[:300],
                "source": "clinicaltrials",
                "score": 0.85,
            })
        return results
    return _engine


# ── openFDA 药品标签 ─────────────────────────────────────────────────────────

def _build_openfda_engine(spec: dict[str, Any]) -> Any:
    """openFDA drug label 搜索（免认证）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        # 清洗查询，优先 brand/generic 字段检索
        q = re.sub(r"(?i)\b(drug|medicine|药品|药物|说明书|openfda|fda)\b", " ", query).strip()
        q = q or query.strip()
        search = f'openfda.brand_name:"{q}" openfda.generic_name:"{q}"'
        url = "https://api.fda.gov/drug/label.json?" + up.urlencode({
            "search": search,
            "limit": min(n, 20),
        })
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "argo-search/2.5", "Accept": "application/json"}), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            # 回退：全文检索
            try:
                url2 = "https://api.fda.gov/drug/label.json?" + up.urlencode({
                    "search": q, "limit": min(n, 20),
                })
                with urllib.request.urlopen(urllib.request.Request(
                        url2, headers={"User-Agent": "argo-search/2.5"}), timeout=to) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as e2:
                logger.warning(f"openfda 失败: {e2}")
                return []
        results = []
        for item in (data.get("results") or [])[:n]:
            of = item.get("openfda") or {}
            brands = of.get("brand_name") or []
            generics = of.get("generic_name") or []
            title = (brands[0] if brands else "") or (generics[0] if generics else "") or item.get("id", "drug label")
            purpose = item.get("purpose") or item.get("indications_and_usage") or []
            if isinstance(purpose, list):
                purpose = purpose[0] if purpose else ""
            set_id = (of.get("spl_set_id") or [None])[0] if isinstance(of.get("spl_set_id"), list) else of.get("spl_set_id")
            url_out = f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}" if set_id else "https://open.fda.gov/apis/drug/label/"
            results.append({
                "title": str(title)[:200],
                "url": url_out,
                "snippet": str(purpose).replace("\n", " ")[:300],
                "source": "openfda",
                "score": 0.8,
            })
        return results
    return _engine


# ── 掘金搜索 ─────────────────────────────────────────────────────────────────

def _build_juejin_engine(spec: dict[str, Any]) -> Any:
    """掘金文章搜索（search_api，免认证）。"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        # 官方搜索接口（GET）
        url = "https://api.juejin.cn/search_api/v1/search?" + up.urlencode({
            "query": query,
            "id_type": 0,
            "cursor": "0",
            "limit": min(n, 20),
            "search_type": 0,
            "sort_type": 0,
        })
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://juejin.cn/",
        }
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"juejin 失败: {e}")
            return []
        results = []
        for item in (data.get("data") or [])[: n * 2]:
            model = item.get("result_model") or {}
            info = model.get("article_info") or model
            title = info.get("title") or item.get("title_highlight") or ""
            # 去 HTML 高亮
            title = re.sub(r"<[^>]+>", "", str(title)).strip()
            aid = info.get("article_id") or model.get("article_id") or ""
            brief = info.get("brief_content") or info.get("content") or item.get("content_highlight") or ""
            brief = re.sub(r"<[^>]+>", "", str(brief)).strip()
            if not title:
                continue
            # 文章 / 专栏 / 其他
            aurl = f"https://juejin.cn/post/{aid}" if aid else "https://juejin.cn/search?query=" + up.quote(query)
            results.append({
                "title": title[:200],
                "url": aurl,
                "snippet": brief[:300],
                "source": "juejin",
                "score": 0.75,
            })
            if len(results) >= n:
                break
        return results
    return _engine



# ── models.dev AI 模型信息引擎 ──────────────────────────────────────────────────

_models_dev_cache: tuple[float, Any] = (0.0, None)  # (timestamp, data)
_MODELS_DEV_TTL = 86400  # 24h（evergreen，模型信息更新不频繁）


def _build_models_dev_engine(spec: dict[str, Any]) -> Any:
    """models.dev AI 模型信息搜索（结构化数据，零认证，全量缓存）。

    数据源：https://models.dev/api.json（176 provider × 5907+ 模型）
    策略：全量拉取后本地过滤，缓存 24h。
    过滤维度：模型名/family/description 关键词 + 能力维度（vision/tool/reasoning/context/免费/开源）。
    """

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        global _models_dev_cache
        to = _timeout or spec.get("timeout", 15)
        ql = query.lower().strip()

        # 拉取全量数据（带缓存）
        now = time.time()
        ts, cached = _models_dev_cache
        if cached is None or (now - ts) > _MODELS_DEV_TTL:
            req = urllib.request.Request(
                "https://models.dev/api.json",
                headers={"User-Agent": "argo-search/1.0 (+models.dev)"},
            )
            with urllib.request.urlopen(req, timeout=to) as resp:
                cached = json.loads(resp.read().decode("utf-8"))
            _models_dev_cache = (now, cached)

        # 能力维度过滤
        want_vision = any(k in ql for k in ("vision", "视觉", "图片", "多模态", "multimodal"))
        want_tool = any(k in ql for k in ("tool", "工具调用", "function call"))
        want_long = any(k in ql for k in ("1m", "100万", "长上下文", "long context", "百万"))
        want_free = any(k in ql for k in ("免费", "free", "zero cost"))
        want_open = any(k in ql for k in ("开源", "open weight", "open source"))

        # 关键词提取（去掉能力词后剩下的当搜索词）
        search_terms = ql
        for drop in ("vision", "视觉", "图片", "多模态", "multimodal", "tool", "工具调用",
                     "function call", "1m", "100万", "长上下文", "long context", "百万",
                     "免费", "free", "zero cost", "开源", "open weight", "open source",
                     "模型", "model", "价格", "price", "pricing", "多少钱", "api"):
            search_terms = search_terms.replace(drop, "")
        search_terms = search_terms.strip()

        results: list[dict[str, Any]] = []
        for prov_id, prov in (cached or {}).items():
            if not isinstance(prov, dict):
                continue
            prov_name = prov.get("name", prov_id)
            prov_doc = prov.get("doc", "https://models.dev")
            for mid, m in (prov.get("models") or {}).items():
                if not isinstance(m, dict):
                    continue
                # 能力过滤
                if want_vision and not m.get("attachment"):
                    continue
                if want_tool and not m.get("tool_call"):
                    continue
                if want_open and not m.get("open_weights"):
                    continue
                lim = m.get("limit") or {}
                cost = m.get("cost") or {}
                if want_long and (lim.get("context") or 0) < 500000:
                    continue
                if want_free and (cost.get("input") or 0) > 0:
                    continue

                # 关键词匹配
                name = m.get("name", mid)
                family = m.get("family", "")
                desc = m.get("description", "")
                haystack = f"{name} {family} {desc} {prov_name}".lower()
                if search_terms:
                    terms = [t for t in search_terms.split() if len(t) >= 2]
                    if terms and not any(t in haystack for t in terms):
                        continue
                elif not any(k in ql for k in ("模型", "model", "llm", "gpt", "claude",
                         "gemini", "glm", "llama", "mistral", "deepseek", "价格", "price")):
                    # 无明确搜索词也无能力词时，只返回大上下文热门模型（兜底）
                    if (lim.get("context") or 0) < 200000:
                        continue

                # 构建 snippet
                mods_in = m.get("modalities", {}).get("input", [])
                snippet = (
                    f"ctx={lim.get('context', '?')} | out={lim.get('output', '?')} | "
                    f"in=${cost.get('input', '?')}/M out=${cost.get('output', '?')}/M | "
                    f"tools={m.get('tool_call')} | vision={m.get('attachment')} | "
                    f"reasoning={m.get('reasoning')} | input={','.join(mods_in)} | "
                    f"{desc[:80]}"
                )
                results.append({
                    "title": f"{name} ({prov_name})",
                    "url": prov_doc,
                    "snippet": snippet[:300],
                    "source": "models_dev",
                    "score": 0.95,
                })
                if len(results) >= n:
                    break
            if len(results) >= n:
                break

        return results
    return _engine

# ── Finviz 美股筛选引擎（HTML 抓取）─────────────────────────────────────────────

def _build_finviz_engine(spec: dict[str, Any]) -> Any:
    """Finviz 个股快照（finviz.com/quote.ashx?t=TICKER）

    从 snapshot 表格提取：公司名、行业、市值、PE、价格、涨跌幅。
    query 视为 ticker（大写）；非纯字母时取首个字母 token 作 ticker。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        m = re.search(r"[A-Za-z]{1,6}", query or "")
        if not m:
            return []
        ticker = m.group(0).upper()
        url = f"https://finviz.com/quote.ashx?t={ticker}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=to) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        if _detect_anti_bot(html):
            return []

        # snapshot 表格是 key-value 交替单元格
        cells = re.findall(r'<td[^>]*class="[^"]*snapshot-td2[^"]*"[^>]*>(.*?)</td>', html, re.DOTALL)
        cleaned = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        kv: dict[str, str] = {}
        for i in range(0, len(cleaned) - 1, 2):
            kv[cleaned[i]] = cleaned[i + 1]

        # 公司名 / 行业
        name_m = re.search(r'<title>([^<]+)</title>', html)
        company = name_m.group(1).split("Stock")[0].strip() if name_m else ticker

        if not kv and company == ticker:
            return []

        price = kv.get("Price", "")
        change = kv.get("Change", "")
        market_cap = kv.get("Market Cap", "")
        pe = kv.get("P/E", "")
        snippet = (f"行业信息见页面 | 市值 {market_cap} | PE {pe} | "
                   f"价格 {price} | 涨跌幅 {change}")
        return [{
            "title": f"{company} ({ticker}) 美股快照",
            "url": url,
            "snippet": snippet[:300],
            "source": "finviz",
            "score": 0.9,
            "facts": {"ticker": ticker, "price": price, "change": change,
                      "market_cap": market_cap, "pe": pe},
        }]
    return _engine

# ── Seeking Alpha 美股分析引擎（HTML 抓取）───────────────────────────────────────

def _build_seeking_alpha_engine(spec: dict[str, Any]) -> Any:
    """Seeking Alpha 个股概览（seekingalpha.com/symbol/TICKER）

    提取：评级 / 目标价 / 分析摘要（尽力而为，站点反爬较强，失败返回 []）。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        m = re.search(r"[A-Za-z]{1,6}", query or "")
        if not m:
            return []
        ticker = m.group(0).upper()
        url = f"https://seekingalpha.com/symbol/{ticker}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=to) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        if _detect_anti_bot(html):
            return []

        desc_m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html)
        summary = desc_m.group(1).strip() if desc_m else ""
        rating_m = re.search(r'(Strong Buy|Buy|Hold|Sell|Strong Sell)', html)
        rating = rating_m.group(1) if rating_m else "N/A"
        target_m = re.search(r'Price Target[^0-9]{0,20}(\d+\.\d+)', html)
        target = target_m.group(1) if target_m else ""

        if not summary and rating == "N/A":
            return []
        snippet = f"评级 {rating}" + (f" | 目标价 {target}" if target else "") + \
                  (f" | {summary}" if summary else "")
        return [{
            "title": f"{ticker} — Seeking Alpha 分析",
            "url": url,
            "snippet": snippet[:300],
            "source": "seeking_alpha",
            "score": 0.85,
            "facts": {"ticker": ticker, "rating": rating, "target_price": target},
        }]
    return _engine

# ── 和风天气引擎（HTTP JSON API，需 QWEATHER_KEY）─────────────────────────────────

def _build_qweather_engine(spec: dict[str, Any]) -> Any:
    """和风天气实时天气（devapi.qweather.com）

    需环境变量 QWEATHER_KEY。先用 GeoAPI 将城市名解析为 LocationID，
    再查实时天气。提取：温度、天气描述、湿度、风力。
    无 key 时显式返回错误项（不静默）。
    """
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        key = os.environ.get("QWEATHER_KEY", "")
        if not key:
            logger.warning("QWEATHER_KEY 未设置，跳过和风天气")
            return [{"error": "QWEATHER_KEY 未设置", "source": "qweather"}]

        # 从 query 抽取城市名（去掉天气/预报等词）
        city = re.sub(r"(天气|预报|气温|温度|今天|明天|实时|怎么样|多少度)", "", query or "").strip()
        city = city or query.strip()

        # 1) GeoAPI: 城市 → LocationID
        geo_url = f"https://geoapi.qweather.com/v2/city/lookup?location={up.quote(city)}&key={key}"
        req = urllib.request.Request(geo_url, headers={"User-Agent": "argo-search/1.0"})
        with urllib.request.urlopen(req, timeout=to) as resp:
            geo = json.loads(resp.read().decode("utf-8"))
        locations = geo.get("location") or []
        if not locations:
            return []
        loc = locations[0]
        loc_id = loc.get("id", "")
        loc_name = loc.get("name", city)

        # 2) 实时天气
        now_url = f"https://devapi.qweather.com/v7/weather/now?location={loc_id}&key={key}"
        req2 = urllib.request.Request(now_url, headers={"User-Agent": "argo-search/1.0"})
        with urllib.request.urlopen(req2, timeout=to) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        now = data.get("now") or {}
        if not now:
            return []
        temp = now.get("temp", "")
        text = now.get("text", "")
        humidity = now.get("humidity", "")
        wind = f"{now.get('windDir', '')}{now.get('windScale', '')}级"
        snippet = f"{loc_name}：{text} {temp}°C | 湿度 {humidity}% | 风力 {wind}"
        return [{
            "title": f"{loc_name}实时天气",
            "url": now.get("fxLink", "https://www.qweather.com/"),
            "snippet": snippet[:300],
            "source": "qweather",
            "score": 1.0,
            "facts": {"temp": temp, "text": text, "humidity": humidity, "wind": wind},
        }]
    return _engine

# ── 中国裁判文书网引擎（HTML/JSON 抓取）──────────────────────────────────────────

def _build_wenshu_engine(spec: dict[str, Any]) -> Any:
    """中国裁判文书网（wenshu.court.gov.cn）法律文书检索。

    该站有较强反爬（参数加密），此处走公开列表接口，尽力而为；
    失败或被拦截时显式返回 []（不伪造结果）。
    提取：案号、案由、裁判日期、法院。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        # 公开检索页（列表 HTML）——政府站结构可能变化，解析失败即空
        url = f"https://wenshu.court.gov.cn/website/wenshu/181217BMTKHNT2W0/index.html?q={up.quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://wenshu.court.gov.cn/",
        }
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"裁判文书网访问失败: {e}")
            return []
        if _detect_anti_bot(html):
            return []

        results: list[dict[str, Any]] = []
        # 尝试解析嵌入的 JSON 数据块（站点结构变化时降级为空）
        blocks = re.findall(r'\{[^{}]*"caseName"[^{}]*\}', html)
        for b in blocks[:n]:
            try:
                item = json.loads(b)
            except Exception:
                continue
            results.append({
                "title": item.get("caseName", "")[:100],
                "url": "https://wenshu.court.gov.cn/",
                "snippet": (f"案号 {item.get('caseNo', '')} | 案由 {item.get('caseType', '')} | "
                            f"法院 {item.get('court', '')} | 裁判日期 {item.get('judgeDate', '')}")[:300],
                "source": "wenshu",
                "facts": {"case_no": item.get("caseNo", ""), "court": item.get("court", ""),
                          "judge_date": item.get("judgeDate", "")},
            })
        return results
    return _engine

# ── 金十数据引擎（HTTP JSON API 财经快讯）────────────────────────────────────────

def _build_jin10_engine(spec: dict[str, Any]) -> Any:
    """金十数据财经快讯（flash-api.jin10.com/get_flash_list）

    提取：时间、标题、内容摘要；按关键词过滤（query 命中）。
    """
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = f"https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1&t={int(time.time() * 1000)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "x-app-id": "bVBF4FyRTn5NJF5n",
            "x-version": "1.0.0",
            "Referer": "https://www.jin10.com/",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=to) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("data") or []
        keywords = [k for k in (query or "").strip().split() if k]
        # 去掉路由触发词，避免「金十」「快讯」本身当过滤条件
        stop = {"金十", "jin10", "财经快讯", "市场快讯", "7x24", "快讯", "电报"}
        keywords = [k for k in keywords if k.lower() not in stop and k not in stop]
        results: list[dict[str, Any]] = []
        for it in items:
            d = it.get("data") or {}
            content = d.get("content", "") or d.get("pic", "") or ""
            title = d.get("title", "") or content[:40]
            text = f"{title} {content}"
            if keywords and not any(k.lower() in text.lower() for k in keywords):
                continue
            results.append({
                "title": title[:80] or "金十快讯",
                "url": "https://www.jin10.com/",
                "snippet": f"{it.get('time', '')} | {content[:150]}",
                "source": "jin10",
            })
            if len(results) >= n:
                break
        # 关键词未命中当前快讯池时，回退最新 N 条（金十是滚动电报，非全库检索）
        if not results and items:
            for it in items[:n]:
                d = it.get("data") or {}
                content = d.get("content", "") or d.get("pic", "") or ""
                title = d.get("title", "") or content[:40]
                results.append({
                    "title": title[:80] or "金十快讯",
                    "url": "https://www.jin10.com/",
                    "snippet": f"{it.get('time', '')} | {content[:150]}",
                    "source": "jin10",
                })
        return results
    return _engine

# ── Octen AI 搜索引擎 ─────────────────────────────────────────────────────────

def _build_octen_engine(spec: dict[str, Any]) -> Any:
    """Octen AI 搜索（高速语义搜索 + broad-search 多查询分解）

    支持两种模式：
      - 标准搜索: /search（单次查询）
      - 广域搜索: /broad-search（自动分解为子查询并行执行）
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        api_key = os.environ.get("OCTEN_API_KEY", "")
        if not api_key:
            logger.warning("OCTEN_API_KEY 未设置")
            return []

        # broad-search 模式（多子查询分解），支持 depth 参数
        use_broad = depth in ("deep", "balanced")
        if use_broad:
            url = "https://api.octen.ai/broad-search"
            max_q = 3 if depth == "balanced" else 5
            body = json.dumps({
                "query": query,
                "max_queries": max_q,
                "search_options": {
                    "count": min(n, 5),
                    "topic": "general",
                    "safesearch": "off",
                    "highlight": {"enable": True, "max_tokens": 512},
                    "full_content": {"enable": False},
                    "include_images": False,
                },
            }).encode("utf-8")
        else:
            url = "https://api.octen.ai/search"
            body = json.dumps({
                "query": query,
                "count": min(n, 10),
                "topic": "general",
                "safesearch": "off",
                "highlight": {"enable": True, "max_tokens": 512},
                "full_content": {"enable": False},
                "include_images": False,
            }).encode("utf-8")

        headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=body, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                items = data.get("data", {})
                results = []

                if use_broad:
                    # broad-search: 遍历所有子查询结果
                    seen = set()
                    for group in items.get("search_results", []):
                        for r in group.get("results", []):
                            url_key = r.get("url", "")
                            if url_key and url_key not in seen:
                                seen.add(url_key)
                                results.append({
                                    "title": r.get("title", ""),
                                    "url": url_key,
                                    "snippet": r.get("highlight", "")[:300],
                                    "source": "octen",
                                    "score": 0.8,
                                })
                                if len(results) >= n:
                                    return results
                else:
                    # 标准搜索
                    for r in items.get("results", [])[:n]:
                        results.append({
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "snippet": r.get("highlight", "")[:300],
                            "source": "octen",
                            "score": r.get("score", 0.5),
                        })

                # 如果结果太少，补充 quick 数据源的查询（不走 broad 兜底）
                if not results and not use_broad:
                    logger.info("Octen 标准搜索无结果，兜底空列表返回")
                return results

        except Exception as e:
            logger.warning(f"Octen 引擎失败: {e}")
            return []
    return _engine

_BUILDERS = {
    "cli": _build_cli_engine,
    "http": _build_http_engine,
    "html": _build_html_engine,
    "exa": _build_exa_engine,
    "wechat_sogou": _build_wechat_sogou_engine,
    "hackernews": _build_hackernews_engine,
    "stackoverflow": _build_stackoverflow_engine,
    "google_scholar": _build_google_scholar_engine,
    "v2ex": _build_v2ex_engine,
    "ths_hot": _build_ths_hot_engine,
    "cls_telegraph": _build_cls_telegraph_engine,
    "em_global_news": _build_em_global_news_engine,
    "eastmoney": _build_eastmoney_engine,
    "itotii": _build_itotii_engine,
    "baidu_hot": _build_baidu_hot_engine,
    "toutiao_hot": _build_toutiao_hot_engine,
    "bilibili_hot": _build_bilibili_hot_engine,
    "open_library": _build_open_library_engine,
    "free_dictionary": _build_free_dictionary_engine,
    "baidu_baike": _build_baidu_baike_engine,
    "pypi": _build_pypi_engine,
    "clinicaltrials": _build_clinicaltrials_engine,
    "openfda": _build_openfda_engine,
    "juejin": _build_juejin_engine,
    "models_dev": _build_models_dev_engine,
    "finviz": _build_finviz_engine,
    "seeking_alpha": _build_seeking_alpha_engine,
    "qweather": _build_qweather_engine,
    "wenshu": _build_wenshu_engine,
    "jin10": _build_jin10_engine,
    "octen": _build_octen_engine,
}

# ── 通用解析器 ─────────────────────────────────────────────────────────────────

def _parse_text_output(text: str, engine_name: str) -> list[dict[str, Any]]:
    """通用 CLI 文本解析：优先 JSON，其次结构化文本。"""
    if not text or not text.strip():
        return []
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [{"title": i.get("title", ""), "url": i.get("url", ""),
                     "snippet": i.get("snippet", i.get("content", ""))[:300],
                     "source": engine_name} for i in data if isinstance(i, dict)]
        if isinstance(data, dict):
            items = data.get("results", data.get("items", data.get("data", [])))
            if isinstance(items, list):
                return [{"title": i.get("title", ""), "url": i.get("url", ""),
                         "snippet": i.get("snippet", i.get("content", ""))[:300],
                         "source": engine_name} for i in items if isinstance(i, dict)]
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
    return results[:10]


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
    """通用 JSON 解析：自动探测常见字段。"""
    items = None
    for key in ["results", "items", "data", "works", "search"]:
        if "." in key:
            parts = key.split(".")
            obj = data
            for p in parts:
                obj = obj.get(p, {}) if isinstance(obj, dict) else {}
            if isinstance(obj, list):
                items = obj
                break
        elif isinstance(data, dict) and key in data and isinstance(data[key], list):
            items = data[key]
            break

    if items is None and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, dict):
                for key in ["results", "items", "value", "search"]:
                    if key in v and isinstance(v[key], list):
                        items = v[key]
                        break
            if items:
                break

    if not items or not isinstance(items, list):
        return []

    results = []
    for i in items:
        if not isinstance(i, dict):
            continue
        title = i.get("title", "")
        if isinstance(title, list):
            title = title[0] if title else ""
        url = i.get("url", i.get("URL", i.get("html_url", "")))
        snippet = (i.get("snippet", i.get("content", i.get("summary", i.get("description", "")))))[:300]
        score = i.get("score", i.get("relevance_score", 0.5))
        results.append({"title": str(title)[:200], "url": str(url),
                        "snippet": str(snippet), "score": score, "source": engine_name})
    return results[:10]


# ── 引擎注册表 ─────────────────────────────────────────────────────────────────

_engine_registry: dict[str, Any] = {}
_engine_registry_loaded = False


def _load_registry():
    global _engine_registry, _engine_registry_loaded
    if _engine_registry_loaded:
        return
    cfg = load_config()
    engines = get_engines(cfg)
    registry = {}
    for name, spec in engines.items():
        spec = dict(spec)
        spec["_name"] = name
        builder = _BUILDERS.get(spec.get("type", "cli"))
        if builder:
            registry[name] = builder(spec)
        else:
            logger.warning(f"未知引擎类型: {spec.get('type')} (引擎 {name})")
    _engine_registry = registry
    _engine_registry_loaded = True


def get_registry() -> dict[str, Any]:
    _load_registry()
    return _engine_registry


def available_engines() -> list[str]:
    return sorted(get_registry().keys())


def search(query: str, engine: str, n: int = 5, timeout: float = 8, depth: str = "fast", mode: str = "fast") -> list[dict[str, Any]]:
    """统一引擎调用入口；失败返回空 list，不抛异常。"""
    registry = get_registry()
    fn = registry.get(engine)
    if not fn:
        logger.warning(f"未知引擎: {engine}")
        return []
    t0 = time.time()
    try:
        results = fn(query, n, timeout, depth=depth, mode=mode)
    except TypeError:
        try:
            results = fn(query, n, timeout)
        except Exception as e:
            logger.error(f"引擎 {engine} 异常: {type(e).__name__}: {e}")
            results = []
    except Exception as e:
        logger.error(f"引擎 {engine} 异常: {type(e).__name__}: {e}")
        results = []
    elapsed = time.time() - t0
    if results and isinstance(results, list):
        for r in results:
            if isinstance(r, dict) and "error" not in r:
                r["_engine"] = engine
                r["_elapsed"] = round(elapsed, 3)
    return results if isinstance(results, list) else []


def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="引擎适配层调试")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--engine", "-e", default="anysearch")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--timeout", "-t", type=float, default=8)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        print(json.dumps(available_engines(), ensure_ascii=False, indent=2))
        return
    if not args.query:
        parser.error("必须提供 query")
    print(json.dumps(search(args.query, args.engine, args.n, args.timeout), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()

#!/usr/bin/env python3
"""search_v3.py — local-search 主入口 v3

职责：
  - 解析命令行参数（兼容 unified-search CLI 调用）
  - 通过 smart_router 选择本地引擎组合
  - 通过 health_check 过滤不可用引擎（TTL 5min）
  - 并行抓取，解析 HTML/RSS/JSON/XML
  - 复用 unified-search/scripts/cache.py 的 L1/L2 缓存
  - 输出 strict unified-search schema
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 将 unified-search/scripts 加入路径，以复用 cache.py
SKILL_DIR = Path(__file__).resolve().parent
UNIFIED_SCRIPT_DIR = SKILL_DIR.parent.parent / "scripts"
# 确保当前目录优先于 scripts 目录，避免 health_check 等同名模块冲突
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))
if UNIFIED_SCRIPT_DIR.exists() and str(UNIFIED_SCRIPT_DIR) not in sys.path:
    sys.path.insert(1, str(UNIFIED_SCRIPT_DIR))

try:
    from cache import SearchCache
except ImportError:
    SearchCache = None  # type: ignore

from engine_registry import EngineRegistry, get_registry
from local_health_check import get_available_engines
from smart_router import route_query

logger = logging.getLogger("local_search.search_v3")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler(sys.stderr))

CONFIG_PATH = SKILL_DIR / "config.yaml"
PARSE_MAPS_PATH = SKILL_DIR / "parse_maps.yaml"

# 带发布时间能力（published_at）的本地子引擎集合。时间窗的缓存键隔离与
# 后过滤只对含这些引擎的组合生效：text/images/videos 类子引擎不返回日期，
# 隔离缓存键只会降低命中率（7d/30d 查同一子引擎本可共享缓存）。
_TIME_CAPABLE_ENGINES: frozenset[str] = frozenset({
    "local_bing_news", "local_google_news", "local_duckduckgo_news",
    "local_ddgs_news",
})

# ddgs CLI 可用性检查缓存：`ddgs --help` 每次 ~190ms 冷启动，
# 对同一命令在 TTL 内只检查一次，避免多 ddgs 引擎并行搜索重复付费。
_CLI_AVAIL_CACHE: dict[str, tuple[float, bool]] = {}
_CLI_AVAIL_TTL = 60.0


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"加载 YAML 失败 {path}: {e}")
        return {}


@functools.lru_cache(maxsize=1)
def _load_config() -> dict[str, Any]:
    return _load_yaml(CONFIG_PATH)


@functools.lru_cache(maxsize=1)
def _load_parse_maps() -> dict[str, Any]:
    return _load_yaml(PARSE_MAPS_PATH)


_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _resolve(template: str | list[str], query: str, n: int, **extra: Any) -> str | list[str]:
    if isinstance(template, list):
        return [_resolve(item, query, n, **extra) for item in template]  # type: ignore
    s = str(template).replace("{query}", query).replace("{n}", str(n))
    for k, v in extra.items():
        s = s.replace(f"{{{k}}}", str(v))
    return s


# ── 时间窗工具 ─────────────────────────────────────────────────────────────────

def _parse_time(s: str | None) -> str | None:
    """解析时间窗为 ISO 日期：7d/30d/12h/1w/1y（相对）或 2026-08-01 / ISO 8601（绝对）。

    相对时间按当前时刻向前偏移；绝对日期归一化为 YYYY-MM-DD。
    解析失败返回 None（调用方按未设置处理）。
    """
    if not s:
        return None
    s = str(s).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except ValueError:
        pass
    m = re.fullmatch(r"(\d+)([hdwmy])", s.lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    now = datetime.now()
    if unit == "h":
        delta = timedelta(hours=n)
    elif unit == "d":
        delta = timedelta(days=n)
    elif unit == "w":
        delta = timedelta(weeks=n)
    elif unit == "m":
        delta = timedelta(days=30 * n)
    else:  # y
        delta = timedelta(days=365 * n)
    return (now - delta).date().isoformat()


def _to_epoch(s: str | None) -> str | None:
    """ISO 日期 → unix 时间戳（字符串），供 API 引擎（如 StackExchange）使用。"""
    iso = _parse_time(s)
    if not iso:
        return None
    try:
        return str(int(datetime.fromisoformat(iso).timestamp()))
    except ValueError:
        return None


def _normalize_date(v: Any) -> str | None:
    """把各形态日期归一为 YYYY-MM-DD：2026-08-01 / 2026-08 / 2026 /
    [2026, 8, 1] / ISO 8601 / RFC 822（RSS pubDate）。失败返回 None。"""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = "-".join(str(x) for x in v if x is not None)
    s = str(v).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{4})-(\d{1,2})$", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})$", s)
    if m:
        return m.group(1)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except ValueError:
        pass
    # unix 秒级时间戳（StackExchange creation_date 等）
    if re.fullmatch(r"\d{9,11}", s):
        try:
            return datetime.fromtimestamp(int(s)).date().isoformat()
        except (ValueError, OSError):
            pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        return None


def _date_key(s: str) -> tuple[int, ...]:
    """日期 → 可比较元组（YYYY-MM 与 YYYY-MM-DD 可混合比较）。"""
    return tuple(int(x) for x in str(s).split("-"))


def _apply_time_window(results: list[dict[str, Any]],
                       since: str | None, until: str | None) -> list[dict[str, Any]]:
    """解析后时间窗过滤：仅保留 published_at 落在 [since, until] 内的结果。

    无日期字段的条目在时间窗模式下剔除（时间窗查询必须保证结果新鲜，
    无法验证时间的条目不纳入，与主技能实时索引行为一致）。
    """
    since_iso = _parse_time(since)
    until_iso = _parse_time(until)
    if not since_iso and not until_iso:
        return results
    kept: list[dict[str, Any]] = []
    for r in results:
        pa = r.get("published_at")
        if not pa:
            continue
        if since_iso and _date_key(pa) < _date_key(since_iso):
            continue
        if until_iso and _date_key(pa) > _date_key(until_iso):
            continue
        kept.append(r)
    return kept


def _sort_results(results: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """时间排序：oldest 升序 / newest 降序 / relevance 原序。

    与主技能同语义：排序是本地展示顺序，不改变结果集、不进入缓存键；
    无日期条目恒排最后；同时间保持原相对顺序（稳定排序，结果可复现）。
    """
    if sort not in ("oldest", "newest"):
        return results
    if len(results) <= 1:
        return results

    def _key(r: dict[str, Any]) -> tuple[int, tuple[int, ...]]:
        pa = r.get("published_at")
        if not pa:
            return (1, ())  # 无日期恒排最后
        try:
            dk = _date_key(str(pa)[:10])
        except ValueError:
            return (1, ())
        return (0, dk if sort == "oldest" else tuple(-x for x in dk))

    return sorted(results, key=_key)


def _fetch(url: str, method: str = "GET", data: bytes | None = None,
           headers: dict[str, str] | None = None, timeout: float = 8,
           user_agent: str = "") -> str:
    req_headers = dict(_HEADERS)
    if headers:
        req_headers.update(headers)
    if user_agent:
        req_headers["User-Agent"] = user_agent
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if raw.startswith(b"\x1f\x8b"):
                import gzip
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.warning(f"HTTP {e.code} for {url}")
    except urllib.error.URLError as e:
        logger.warning(f"URL error for {url}: {e.reason}")
    except Exception as e:
        logger.warning(f"Fetch error for {url}: {e}")
    return ""


def _build_url(spec: dict[str, Any], query: str, n: int,
               since: str | None = None, until: str | None = None) -> str:
    url = _resolve(spec["url"], query, n)
    qp = spec.get("query_param", "q")
    extra = spec.get("extra_params", {})
    params = {qp: query}
    # 语言参数动态化（v2.7）：根据查询主语言覆盖静态 setlang/hl/lang，
    # 表在 lang_detect 单真源维护。
    for k, v in extra.items():
        if k in ("setlang", "hl", "lang", "uselang"):
            v = _lang_param(k, query) or v
        params[k] = _resolve(str(v), query, n)

    # 时间窗参数下推（声明式 filter_args，仿主技能形态）：
    #   filter_args:
    #     since: [[param, "{since_iso}"]]
    #     until: [[param, "{until_iso}"]]
    # 占位符：{since}/{until} 原样；{since_iso}/{until_iso} 归一化日期；
    #         {since_epoch}/{until_epoch} unix 时间戳。
    # 参数名等于 query_param 时视为 query 追加（如 GitHub created:...）。
    filter_args = spec.get("filter_args", {})
    time_vals = {
        "since": since, "until": until,
        "since_iso": _parse_time(since), "until_iso": _parse_time(until),
        "since_epoch": _to_epoch(since), "until_epoch": _to_epoch(until),
    }
    for key, tmpls in filter_args.items():
        raw = time_vals.get(key)
        if raw in (None, ""):
            continue
        for pair in tmpls if isinstance(tmpls, list) else [tmpls]:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                continue
            pname, pvalue = pair
            pvalue = _resolve(str(pvalue), query, n, **time_vals)
            if pname == qp:
                base = params.get(pname, "")
                params[pname] = f"{base} {pvalue}".strip()
            else:
                params[pname] = pvalue
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urllib.parse.urlencode(params)}"


def _lang_param(param: str, query: str) -> str:
    """按查询主语言返回引擎语言参数；表在 lang_detect 单真源维护。"""
    try:
        from lang_detect import engine_lang_param
        return engine_lang_param(param, query)
    except ImportError:
        return ""


# ── HTML 解析 ──────────────────────────────────────────────────────────────────

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
    """通用解析兜底：选择器 0 命中时，用链接 + 锚文本启发式回退。

    零依赖：只用 BeautifulSoup 已有能力，抽页面内最像「结果卡片」的链接块，
    避免页面改版导致整引擎 0 结果。命中者 score 保守（0.45 起），不与主映射抢位。
    """
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


# ── XML / RSS 解析 ─────────────────────────────────────────────────────────────

def _parse_xml(engine_name: str, text: str, maps: dict[str, Any],
               is_rss: bool = False) -> list[dict[str, Any]]:
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
        """按标签取值，兼容 atom:xxx 命名空间前缀。"""
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
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "score": round(score, 3),
                "source": engine_name,
                "published_at": _normalize_date(raw_pub),
            })
        except Exception:
            continue
    return results


# ── JSON 解析 ──────────────────────────────────────────────────────────────────

def _get_path(data: Any, path: str) -> Any:
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
    if not template:
        return default
    try:
        return template.format(**item)
    except (KeyError, IndexError):
        return default


def _parse_json(engine_name: str, text: str, maps: dict[str, Any]) -> list[dict[str, Any]]:
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
        published_at = _normalize_date(_get_path(item, published_key)) if published_key else None

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


# ── 单个引擎执行 ─────────────────────────────────────────────────────────────────

# ddgs 各子命令支持的 timelimit 取值：videos 仅 d/w/m（无 y）。
# 传 y 会直接 crash（Invalid value for '-t'），必须按下推子命令过滤。
_DDGS_TIMELIMITS: dict[str, set[str]] = {
    "text": {"d", "w", "m", "y"},
    "news": {"d", "w", "m", "y"},
    "images": {"d", "w", "m", "y"},
    "videos": {"d", "w", "m"},
}


def _since_to_ddgs_timelimit(since: str | None, until: str | None) -> str | None:
    """Convert since/until to ddgs timelimit argument.
    
    ddgs supports: d (day), w (week), m (month), y (year)
    """
    if not since:
        return None
    
    # Parse relative time like 7d, 1m, 3w
    match = re.match(r'^(\d+)([dwmy])$', since.lower())
    if match:
        num, unit = match.groups()
        num = int(num)
        # Map to ddgs timelimit
        if unit == 'y':
            return 'y'
        elif unit == 'm':
            return 'm'
        elif unit == 'w':
            return 'w'
        elif unit == 'd':
            if num >= 365:
                return 'y'
            elif num >= 30:
                return 'm'
            elif num >= 7:
                return 'w'
            else:
                return 'd'
    
    return None


def _run_cli_engine(spec: dict[str, Any], query: str, n: int, timeout: float,
                     since: str | None = None, until: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """Run a CLI-based search engine (e.g., ddgs).
    
    Args:
        since: Time window start (7d, 1m, etc.)
        until: Time window end
    """
    cli_cmd = spec.get("cli_command", "")
    cli_args_template = spec.get("cli_args", [])
    key = spec.get("key", cli_cmd)
    
    if not cli_cmd:
        return [], "CLI engine missing cli_command"
    
    # Check if CLI is available
    if not _check_cli_available(cli_cmd):
        return [], f"{cli_cmd} not available"
    
    # Add time limit if since/until provided
    time_limit = _since_to_ddgs_timelimit(since, until)
    
    # Build command with query and result count substitution
    cli_args = []
    for arg in cli_args_template:
        formatted = arg.format(query=query, n=n)
        cli_args.append(formatted)
    
    # Insert time limit if supported and needed
    # 按子命令限定取值：videos 不支持 y，直接追加 -t y 会让 ddgs 报错退出。
    if time_limit and "-t" not in cli_args and "--timelimit" not in cli_args:
        sub = next((a for a in cli_args_template if not str(a).startswith("-")), None)
        allowed = _DDGS_TIMELIMITS.get(str(sub), _DDGS_TIMELIMITS["text"])
        if time_limit in allowed:
            cli_args.extend(["-t", time_limit])
    
    # 区域参数（ddgs -r region）：按查询主语言映射（zh→cn-zh 等），
    # 引擎声明 use_lang_region: true 时启用；显式 -r 不覆盖。
    if spec.get("use_lang_region") and not any(a in ("-r", "--region") for a in cli_args):
        region = _lang_param("region", query)
        if region:
            cli_args.extend(["-r", region])
    
    # 结构化 JSON 输出：ddgs -o <file> 把结果写入文件（stdout 无输出），
    # 避免脆弱的文本行解析；失败回退文本解析。引擎声明 output_format: json。
    output_format = spec.get("output_format", "")
    tmp_path: str | None = None
    if output_format == "json" and not any(
        a in ("-o", "--output") for a in cli_args
    ):
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="ddgs_")
        os.close(fd)
        cli_args.extend(["-o", tmp_path])
    
    cmd = [cli_cmd] + cli_args
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        
        # ddgs returns non-zero exit code when no results found
        if result.returncode != 0:
            combined_output = result.stdout + result.stderr
            if "No results" in combined_output or "DDGSException" in combined_output:
                return [], f"{cli_cmd}: no results"
            return [], f"{cli_cmd} exit {result.returncode}: {combined_output[:200]}"
        
        # 结构化 JSON 优先（ddgs -o json 输出到临时文件）
        if tmp_path is not None:
            try:
                with open(tmp_path, encoding="utf-8") as f:
                    data = json.load(f)
                parsed = _parse_cli_json(data, key)
                if parsed:
                    return parsed, ""
                # 空解析不视为失败：继续走文本路径兜底（理论上不会发生）
            except (OSError, ValueError):
                pass
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        
        # Parse output from CLI (handles text, news, images, videos formats)
        return _parse_cli_output(result.stdout, key)
    except subprocess.TimeoutExpired:
        return [], f"{cli_cmd} timed out after {timeout}s"
    except FileNotFoundError:
        return [], f"{cli_cmd} not found"
    except Exception as e:
        return [], f"{cli_cmd} error: {e}"


def _parse_cli_json(data: Any, engine_name: str) -> list[dict[str, Any]]:
    """解析 ddgs 结构化 JSON 输出（-o json 写入文件）。

    ddgs 各子命令的 JSON item 形态：
      text:   {title, href, body}
      news:   {date, title, body, url, image, source}
      images: {title, image, thumbnail, url, height, width, source}
      videos: {title, content, duration, provider}
    统一映射为 argo 结果字段；news 的 date 归一化为 published_at。
    """
    if not isinstance(data, list):
        return []
    results: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        url = item.get("href", item.get("url", item.get("link", "")))
        snippet = item.get("body", item.get("snippet",
                                            item.get("description", item.get("content", ""))))
        if not title and not url:
            continue
        r: dict[str, Any] = {
            "title": str(title),
            "url": str(url),
            "snippet": str(snippet),
            "source": engine_name,
        }
        for key in ("image", "thumbnail", "source", "duration", "provider",
                    "height", "width"):
            if key in item and item[key] not in (None, ""):
                r[key] = item[key]
        date = item.get("date")
        if date:
            r["published_at"] = str(date)[:64]
        results.append(r)
    return results


def _check_cli_available(cmd: str) -> bool:
    """Check if a CLI command is available（结果缓存 TTL=_CLI_AVAIL_TTL）。

    `ddgs --help` 冷启动 ~190ms/次；多 ddgs 引擎并行搜索时若每次都检查，
    单次搜索白付 N×190ms 纯检查开销。缓存按命令名 + 60s TTL。
    """
    now = time.time()
    hit = _CLI_AVAIL_CACHE.get(cmd)
    if hit is not None and now - hit[0] < _CLI_AVAIL_TTL:
        return hit[1]
    try:
        result = subprocess.run(
            [cmd, "--help"],
            capture_output=True,
            timeout=5,
        )
        ok = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        ok = False
    _CLI_AVAIL_CACHE[cmd] = (time.time(), ok)
    return ok


def _parse_cli_output(stdout: str, engine_name: str) -> tuple[list[dict[str, Any]], str]:
    """Parse output from CLI search tools like ddgs.
    
    Handles multiple output formats:
    - text: title/href/body
    - news: date/title/body/url/image/source
    - images: title/image/thumbnail/url/height/width/source
    - videos: title/content/duration/provider
    """
    results = []
    
    # Try JSON parse first
    try:
        data = json.loads(stdout)
        if isinstance(data, list):
            for item in data:
                result = {
                    "title": item.get("title", ""),
                    "url": item.get("href", item.get("url", item.get("link", ""))),
                    "snippet": item.get("body", item.get("snippet", item.get("description", item.get("content", "")))),
                    "source": engine_name,
                }
                for key in ["date", "image", "thumbnail", "source"]:
                    if key in item:
                        result[key] = item[key]
                results.append(result)
            return results, ""
    except json.JSONDecodeError:
        pass
    
    # Parse text format with flexible field detection
    current_result = {}
    current_field = None
    
    for line in stdout.split("\n"):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        
        # Detect numbered entry start: "1.", "2.", etc.
        # （此前 len<=4 的任意数字行都被当分隔符，正文数字会被误切；
        #   仅识别「数字+句点」形式的序号）
        if re.fullmatch(r"\d+\.\s*", line_stripped):
            if current_result:
                results.append(current_result)
                current_result = {}
                current_field = None
            continue
        
        # Detect field lines with flexible matching
        field_matched = False
        for field, target_key in [
            ("title", "title"),
            ("href", "url"),
            ("url", "url"),
            ("body", "snippet"),
            ("snippet", "snippet"),
            ("description", "snippet"),
            ("content", "snippet"),
            ("date", "date"),
            ("image", "image"),
            ("thumbnail", "thumbnail"),
            ("source", "source"),
            ("duration", "duration"),
            ("provider", "provider"),
            ("height", "height"),
            ("width", "width"),
        ]:
            if line_stripped.startswith(field) and len(line_stripped) > len(field):
                value = line_stripped[len(field):].strip()
                if value:
                    current_result[target_key] = value
                    current_field = field
                    field_matched = True
                    break
        
        if not field_matched and current_field:
            # Continuation of multi-line field value
            if current_field in ("body", "snippet", "description", "content"):
                current_result.setdefault("snippet", "")
                current_result["snippet"] += " " + line_stripped
    
    if current_result:
        results.append(current_result)
    
    # Ensure all results have required fields, score, and published_at
    for i, r in enumerate(results):
        r.setdefault("source", engine_name)
        r.setdefault("title", "")
        r.setdefault("url", "")
        r.setdefault("snippet", "")
        # Add score based on result position (higher = better)
        r.setdefault("score", round(0.9 - (i * 0.05), 2))
        # Add published_at for time-window filtering
        if "date" in r:
            r["published_at"] = r["date"]
        else:
            r.setdefault("published_at", "")
    
    return results, ""


def _search_one(engine_name: str, query: str, n: int = 5,
                timeout: float | None = None,
                since: str | None = None, until: str | None = None) -> tuple[list[dict[str, Any]], str]:
    cfg = _load_config()
    maps = _load_parse_maps()
    settings = cfg.get("settings", {})
    engines = cfg.get("engines", {})
    spec = engines.get(engine_name, {})
    if not spec:
        return [], f"未找到引擎配置: {engine_name}"
    if not spec.get("enabled", True):
        return [], f"引擎已禁用: {engine_name}"

    to = timeout or spec.get("timeout") or settings.get("default_timeout", 8)
    user_agent = settings.get("user_agent", "")
    fmt = spec.get("format", "html")
    method = spec.get("method", "GET")
    headers = spec.get("headers", {})

    # 时间窗后过滤只对带时间能力引擎执行：text/images/videos 子引擎
    # 不返回日期，过滤是空操作（与缓存键隔离同语义）。
    time_aware = engine_name in _TIME_CAPABLE_ENGINES

    # ── CLI engine path ───────────────────────────────────────────
    if spec.get("type") == "cli":
        results, err = _run_cli_engine(spec, query, n, to, since=since, until=until)
        # Fallback to HTML/JSON/RSS if CLI fails
        if not results and spec.get("fallback_type"):
            fmt = spec["fallback_type"]
            fb_url = spec.get("fallback_url", "")
            if fb_url:
                # 复用 _build_url 构造回退 URL：fallback_param 作为查询参数名
                # 拼入 query（此前未读取，fallback_url 无 {query} 占位符时会
                # 请求首页而非搜索结果）；fallback_params 并入 extra_params。
                fb_spec = dict(spec)
                fb_spec["url"] = fb_url
                if spec.get("fallback_param"):
                    fb_spec["query_param"] = spec["fallback_param"]
                fb_spec["extra_params"] = dict(spec.get("fallback_params", {}))
                url = _build_url(fb_spec, query, n, since=since, until=until)
                spec["_base"] = url
                try:
                    text = _fetch(url, method="GET", headers=headers, timeout=to, user_agent=user_agent)
                    if text:
                        if fmt == "html":
                            results = _parse_html(engine_name, text, spec, maps)
                        elif fmt == "json":
                            results = _parse_json(engine_name, text, maps)
                        elif fmt in ("rss", "xml"):
                            results = _parse_xml(engine_name, text, maps, is_rss=(fmt=="rss"))
                except Exception:
                    pass
                # 时间窗兜底过滤（与 HTTP 路径同语义；仅带时间能力引擎）
                if time_aware and (since or until) and results:
                    results = _apply_time_window(results, since, until)
        if not results:
            return results, err if err else f"{engine_name} CLI and fallback both failed"
        for r in results:
            r["_engine"] = engine_name
            r["_elapsed"] = 0
        return results[:n], ""

    # ── Standard HTTP path ─────────────────────────────────────────
    url = _build_url(spec, query, n, since=since, until=until)
    spec["_base"] = spec.get("url", "")

    t0 = time.time()
    try:
        text = _fetch(url, method=method, headers=headers, timeout=to, user_agent=user_agent)
    except Exception as e:
        return [], f"{engine_name} 请求异常: {e}"
    elapsed = round((time.time() - t0) * 1000, 2)

    if not text:
        return [], f"{engine_name} 返回空内容"

    if fmt == "html":
        results = _parse_html(engine_name, text, spec, maps)
    elif fmt == "xml":
        results = _parse_xml(engine_name, text, maps, is_rss=False)
    elif fmt == "rss":
        results = _parse_xml(engine_name, text, maps, is_rss=True)
    elif fmt == "json":
        results = _parse_json(engine_name, text, maps)
    else:
        results = []

    # 时间窗过滤（通用兜底）：URL 参数下推之外的引擎同样受益，
    # 仅保留 published_at 落在 [since, until] 内的结果；
    # 仅带时间能力引擎执行（其余引擎无 published_at 可滤）。
    if time_aware and (since or until):
        results = _apply_time_window(results, since, until)

    for r in results:
        r["_engine"] = engine_name
        r["_elapsed"] = elapsed
    return results[:n], ""


# ── 缓存 key ───────────────────────────────────────────────────────────────────

def _cache_key(engines: list[str]) -> str:
    return "local_search+" + "+".join(sorted(engines)) if engines else "local_search"


def _cache_domain(domain: str | None) -> str:
    return domain or "local_general"


# ── 批量执行 ─────────────────────────────────────────────────────────────────────

def search_engines(
    query: str,
    engines: list[str] | None = None,
    n: int = 5,
    timeout: float | None = None,
    max_parallel: int = 5,
    skip_cache: bool = False,
    registry: EngineRegistry | None = None,
    mode: str = "fast",
    since: str | None = None,
    until: str | None = None,
    sort: str = "relevance",
) -> dict[str, Any]:
    """local-search 主入口：批量调用本地引擎，返回 unified-search schema。

    since/until: 发布时间时间窗（如 7d / 2026-08-01），下推到支持时间参数的引擎
    （filter_args），并在解析后按 published_at 通用过滤。
    sort: 时间排序 relevance/oldest/newest，仅改变返回顺序，不进入缓存键。
    """
    reg = registry or get_registry()
    cfg = _load_config()
    settings = cfg.get("settings", {})
    max_parallel = max_parallel or settings.get("max_parallel_engines", 5)

    # 自动路由
    if not engines:
        decision = route_query(query, registry=reg, max_engines=3, require_available=False)
        engines = decision["engines"]
        domain = decision.get("domain")
    else:
        domain = None

    # 健康过滤（fast/budget 模式下更严格，只检查实际要用的引擎）
    if mode in ("fast", "budget"):
        try:
            available = set(get_available_engines(registry=reg, engine_names=engines))
            engines = [e for e in engines if e in available]
        except Exception as e:
            logger.warning(f"可用性检查失败: {e}")

    if not engines:
        # 全部不可用，回退到启用的引擎
        engines = reg.list_engines(enabled_only=True)[:3]

    # 缓存读取：使用 SearchCache 标准接口。
    # 时间窗并入缓存键：同一 query 不同 since/until 不串缓存
    # （与主技能 combo 层同模式；SearchCache.get/set 不透传时间窗，
    #  必须在 engine 段拼入，否则 7d/1y/无时间窗会命中同一缓存条目）。
    # 仅当组合内含带时间能力引擎时隔离：text/images/videos 子引擎忽略
    # 时间窗、结果相同，隔离只会降低命中率。
    time_aware = any(e in _TIME_CAPABLE_ENGINES for e in engines)
    cache = SearchCache() if SearchCache is not None else None
    cache_key = _cache_key(engines)
    if since and time_aware:
        cache_key += f"|since={since}"
    if until and time_aware:
        cache_key += f"|until={until}"
    cache_domain = _cache_domain(domain)
    if not skip_cache and cache is not None:
        hit = cache.get(query, engine=cache_key, max_results=n, domain=cache_domain,
                       mode=mode, depth="fast")
        if hit:
            hit_results = _sort_results(hit.get("results", []), sort)
            return {
                "query": query,
                "engine": engines[0] if engines else "local_search",
                "engines": engines,
                "engines_combo": engines,
                "cached": True,
                "cache_level": hit.get("_cache_level", "L?"),
                "domain": cache_domain,
                "elapsed_ms": 0,
                "tfidf_scores": [],
                "results": hit_results,
                "count": len(hit_results),
                "engines_used": engines,
                "errors": [],
                "mode": mode,
            }

    t0_all = time.time()
    all_results: list[dict[str, Any]] = []
    engines_used: list[str] = []
    errors: list[str] = []

    def _task(name: str) -> tuple[str, list[dict[str, Any]], str]:
        res, err = _search_one(name, query, n=n, timeout=timeout,
                               since=since, until=until)
        return name, res, err

    with ThreadPoolExecutor(max_workers=min(len(engines), max_parallel)) as ex:
        futures = {ex.submit(_task, name): name for name in engines}
        try:
            for fut in as_completed(futures, timeout=timeout or 30):
                name = futures[fut]
                try:
                    _, res, err = fut.result()
                    if res:
                        all_results.extend(res)
                        engines_used.append(name)
                    if err:
                        errors.append(err)
                except Exception as e:
                    errors.append(f"{name}: {e}")
        except TimeoutError:
            # 整体超时（如 ddgs 慢后端 > timeout）：保留已完成引擎的结果，
            # 未完成的标记 timeout 记入 errors，不整链崩溃（与主技能同构）。
            for fut, name in futures.items():
                if not fut.done():
                    fut.cancel()
                    errors.append(f"{name}: timeout")
                    continue
                try:
                    _, res, err = fut.result()
                    if res:
                        all_results.extend(res)
                        engines_used.append(name)
                    if err:
                        errors.append(err)
                except Exception as e:
                    errors.append(f"{name}: {e}")

    all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    elapsed = int((time.time() - t0_all) * 1000)
    final_results = all_results[: n * len(engines)] if engines else all_results[:n]

    payload = {
        "results": final_results,
        "engines_used": engines_used,
    }

    # 写缓存：使用 SearchCache 标准接口
    if not skip_cache and cache is not None:
        cache.set(query, engine=cache_key, max_results=n, results=payload,
                   domain=cache_domain, mode=mode, depth="fast")

    # 排序在返回前：sort 只改变本次展示顺序，缓存命中路径同样处理
    out_results = _sort_results(final_results, sort)

    return {
        "query": query,
        "engine": engines[0] if engines else "local_search",
        "engines": engines,
        "engines_combo": engines,
        "cached": False,
        "cache_level": None,
        "domain": cache_domain,
        "elapsed_ms": elapsed,
        "tfidf_scores": [],
        "results": out_results,
        "count": len(out_results),
        "engines_used": engines_used,
        "errors": errors,
        "mode": mode,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _parse_engine_list(value: str) -> list[str]:
    return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="local-search v3 子技能入口")
    parser.add_argument("query", nargs="?", help="搜索关键词")
    parser.add_argument("--engine", "-e", default="", help="引擎名，多个用逗号分隔")
    parser.add_argument("--n", type=int, default=5, help="每引擎结果数")
    parser.add_argument("--timeout", "-t", type=float, default=None, help="超时秒数")
    parser.add_argument("--max-parallel", type=int, default=5)
    parser.add_argument("--no-cache", action="store_true", help="跳过缓存")
    parser.add_argument("--mode", default="fast", choices=["fast", "auto", "deep", "budget"],
                        help="unified-search 模式透传")
    parser.add_argument("--since", default=None,
                        help="发布时间下限（7d / 2026-08-01），下推到支持时间参数的引擎")
    parser.add_argument("--until", default=None,
                        help="发布时间上限（7d / 2026-08-01），下推到支持时间参数的引擎")
    parser.add_argument("--sort", default="relevance",
                        choices=["relevance", "oldest", "newest"],
                        help="时间排序：relevance=相关度（默认）, oldest=最早在前（溯源）, newest=最新在前")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    if not args.query:
        parser.error("必须提供搜索关键词")

    engines = _parse_engine_list(args.engine) if args.engine else None
    result = search_engines(
        args.query,
        engines=engines,
        n=args.n,
        timeout=args.timeout,
        max_parallel=args.max_parallel,
        skip_cache=args.no_cache,
        mode=args.mode,
        since=args.since,
        until=args.until,
        sort=args.sort,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

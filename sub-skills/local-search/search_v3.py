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
import atexit
import functools
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ddgs -o 的结构化输出临时文件：正常路径在 _run_cli_engine 里即时 unlink；
# 这里登记一份进程级兜底——工作线程被弃置后进程若直接退出（见 search_engines
# 的「不 join」收尾），finally 不保证跑，未清理的临时文件会永远留在 /tmp。
# 一处 atexit 覆盖整类泄漏。
_TMP_FILES: set[str] = set()


def _cleanup_tmp_files() -> None:
    for p in list(_TMP_FILES):
        try:
            os.unlink(p)
        except OSError:
            pass
        _TMP_FILES.discard(p)


atexit.register(_cleanup_tmp_files)

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

# BM25 重排（D1）在导入期绑死，不走函数内惰性导入：主链进程内 builder
# （engines._sys_path_tmp）导入完本模块就撤掉临时路径，函数内 `from bm25
# import` 依赖「本目录仍在 sys.path」的残留，一撤即 No module named 'bm25'
# → 主链 local_search 全路径静默退化位次分（2026-10-11 实锤复现）。
# 导入期本目录必然在场：直接执行时 =sys.path[0]，进程内导入时临时路径未撤。
try:
    from bm25 import rerank as _bm25_rerank
except ImportError as _bm25_err:  # 与本文件同目录，缺阵=部署残缺
    _bm25_rerank = None
    logger.warning(f"bm25 模块缺失，BM25 重排全路径停用: {_bm25_err}")

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
        from yaml_load import load as _yaml_load
        with path.open("r", encoding="utf-8") as f:
            data = _yaml_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        try:
            import yaml  # type: ignore
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"加载 YAML 失败 {path}: {e}")
            return {}


_CACHE_INSTANCE: tuple[tuple[str, Any], Any] | None = None


def _get_search_cache() -> Any:
    """按当前 db_path 记忆化 SearchCache 实例（防进程内每次调用重新建连 + 跑 4 条 PRAGMA）。

    记忆化键 = (db_path, SearchCache 类对象本身)：db_path 变（ARGO_STATE_DIR
    切换）或 SearchCache 符号被替换（测试 monkeypatch、未来热替换）时都必须
    重建。只认 db_path 的那一版在类被换掉后仍返回旧实例——测试注入的假缓存
    永远轮不上号（test_local_cache_quality 两条顺序依赖红测的根因），生产上
    任何「同进程内替换缓存实现」的演化也会静默拿到旧对象。
    """
    global _CACHE_INSTANCE
    if SearchCache is None:
        return None
    try:
        import argo_paths
        db_p = str(argo_paths.db_path())
    except Exception:
        db_p = ""
    key = (db_p, SearchCache)
    if _CACHE_INSTANCE is not None and _CACHE_INSTANCE[0] == key:
        return _CACHE_INSTANCE[1]
    inst = SearchCache()
    _CACHE_INSTANCE = (key, inst)
    return inst


@functools.lru_cache(maxsize=1)
def _load_config() -> dict[str, Any]:
    reg = get_registry()
    if reg.config_path == CONFIG_PATH and reg._config:
        return reg._config
    return _load_yaml(CONFIG_PATH)


@functools.lru_cache(maxsize=1)
def _load_parse_maps() -> dict[str, Any]:
    reg = get_registry()
    if reg.parse_maps_path == PARSE_MAPS_PATH and reg._parse_maps:
        return reg._parse_maps
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
        # 入口归一化：CLI JSON 的 date 形如 2026-09-28T15:30:11+00:00（实测），
        # 裸串喂给 _date_key 会 ValueError 并炸掉整个引擎的结果收集。
        # _normalize_date 覆盖 ISO-T/RFC822/unix/年月全部形态，解析失败按
        # 无日期处理（剔除）——一行把「原始日期形态」这一类问题收口。
        pa = _normalize_date(r.get("published_at"))
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
    # 出口统一走 net_proxy.open_url（urllib 类出口的唯一入口）：裸 urlopen
    # 不认 argo 在 config.yaml network.proxy 里配置的 url/rules，在「必须
    # 经代理才能出网」的环境里本地子引擎会一直连不上，而症状与「源挂了」
    # 完全一样（主仓 issue #13 的形态，已在 13 个模块修掉，这里是漏网的一个）。
    # 失败语义与 urlopen 完全一致：原样抛出，下面的 except 分支不受影响。
    # scripts/ 不在路径（独立打包形态）时回退裸 urlopen，行为与改动前一致。
    try:
        from net_proxy import open_url
    except ImportError:
        open_url = urllib.request.urlopen  # type: ignore[assignment]
    try:
        with open_url(req, timeout=timeout) as resp:
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
    # 表在 lang_detect 单来源维护。
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
    """按查询主语言返回引擎语言参数；表在 lang_detect 单来源维护。"""
    try:
        from lang_detect import engine_lang_param
        return engine_lang_param(param, query)
    except ImportError:
        return ""


# ── HTML / XML / JSON 解析（解耦至 parsers.py） ──────────────────────────────
from parsers import (
    _fallback_extract,
    _format_url,
    _get_path,
    _parse_html,
    _parse_json as _raw_parse_json,
    _parse_xml as _raw_parse_xml,
    _select_first_with_bs4,
    _select_with_bs4,
)


def _parse_xml(engine_name: str, text: str, maps: dict[str, Any],
               is_rss: bool = False) -> list[dict[str, Any]]:
    return _raw_parse_xml(engine_name, text, maps, is_rss=is_rss, normalize_date_fn=_normalize_date)


def _parse_json(engine_name: str, text: str, maps: dict[str, Any]) -> list[dict[str, Any]]:
    return _raw_parse_json(engine_name, text, maps, normalize_date_fn=_normalize_date)


# ── CLI 工具引擎调度（解耦至 cli_runner.py） ────────────────────────────────────
from cli_runner import (
    _CLI_AVAIL_CACHE,
    _CLI_AVAIL_TTL,
    _CLI_ERROR_MARKERS,
    _CLI_RETRIES,
    _DDGS_TIMELIMITS,
    _RETRY_FAST_S,
    _attempt_loop,
    _check_cli_available,
    _parse_cli_json,
    _parse_cli_output,
    _run_cli_engine as _raw_run_cli_engine,
    _since_to_ddgs_timelimit,
)

# 第二引擎共识宽限（秒，2026-10-06）：够数早停从「死等 ≥2 引擎」改为
# 「先等 1s 收共识，宽限到点放行」。语义与实测依据见 search_engines 内
# 注释。轮询粒度 0.25s：宽限是时间到点判定，不需要更细。
_CONSENSUS_GRACE_S = 1.0
_CONSENSUS_POLL_S = 0.25


def _run_cli_engine(spec: dict[str, Any], query: str, n: int, timeout: float,
                    since: str | None = None, until: str | None = None) -> tuple[list[dict[str, Any]], str]:
    return _raw_run_cli_engine(spec, query, n, timeout, since=since, until=until, lang_param_fn=_lang_param)


def _resolve_redirect_results(results: list[dict[str, Any]], n: int,
                              engine_timeout: float) -> list[dict[str, Any]]:
    """解析结果里的跳转壳 URL（resolve_redirects 声明即契约，与 engines_base
    同名同义）；解不开的丢弃——不可核验的跳转壳不进结果，主链 serp_guard
    对它们本来就是整批判死（2026-10-06 实测：中文链 returned 10 → kept 1，
    9 条全是搜狗 /link）。批量并行、有界；解析器不可用时维持原样（fail-open）。
    """
    try:
        from jump_resolver import is_jump_url, resolve_jump_urls
    except ImportError:
        return results
    suspects = [r for r in results[:n] if is_jump_url(str(r.get("url") or ""))]
    if not suspects:
        return results
    resolved = resolve_jump_urls(
        [str(r.get("url") or "") for r in suspects],
        timeout=min(2.5, max(1.0, engine_timeout / 2)))
    kept: list[dict[str, Any]] = []
    for r in results[:n]:
        u = str(r.get("url") or "")
        real = resolved.get(u)
        if real:
            r["url"] = real
        elif is_jump_url(u):
            continue
        kept.append(r)
    return kept


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
        fb_err: str | None = None
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
                except Exception as e:
                    # 吞错类残留：fallback 失败此前静默 pass，引擎最后报的
                    # 仍是 CLI 错误，排查时看不到 fallback 也死了。
                    fb_err = f"{engine_name} fallback 抓取失败: {e}"
                # 时间窗保底过滤（与 HTTP 路径同语义；仅带时间能力引擎）
                if time_aware and (since or until) and results:
                    results = _apply_time_window(results, since, until)
        if not results:
            combined_err = "; ".join(x for x in (err, fb_err) if x)
            return results, combined_err or f"{engine_name} CLI and fallback both failed"
        # 时间窗后过滤（CLI 成功路径此前被跳过——until 完全失效、since 只剩
        # ddgs 粗粒度下推（2d 被放大成过去一周）、绝对日期 since 无效，三个
        # 症状同源于此）。日期归一化由 _apply_time_window 入口保证。
        if time_aware and (since or until) and results:
            results = _apply_time_window(results, since, until)
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

    # 跳转链落点解析：仅 spec 声明 resolve_redirects 的引擎（搜狗/百度），
    # 在时间窗过滤前做——后续去重/融合拿到的直接是真实正文 URL。
    if results and spec.get("resolve_redirects"):
        results = _resolve_redirect_results(results, n, to)

    # 时间窗过滤（通用保底）：URL 参数下推之外的引擎同样受益，
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


# 引擎分类 → 缓存域（DOMAIN_TIER_MAP 已有的 local_* 键，TTL 随域走：
# news 600s / academic、code research 2h / reference evergreen 24h）
_CATEGORY_CACHE_DOMAIN = {
    "news": "local_news",
    "academic": "local_academic",
    "code": "local_code",
    "reference": "local_reference",
}


def _infer_cache_domain(engines: list[str], reg: EngineRegistry) -> str:
    """显式引擎时按首个带可映射分类的引擎推断缓存域。

    此前显式引擎路径 domain 恒 None → cache_domain 恒 local_general →
    新闻类引擎结果被按 general 1h TTL 缓存（新鲜度税）。自动路由路径
    的 domain 由 route_query 给出，不受影响。
    """
    for e in engines:
        spec = reg.get_engine(e) or {}
        cats = spec.get("categories") or []
        if isinstance(cats, str):
            cats = [cats]
        for c in cats:
            if c in _CATEGORY_CACHE_DOMAIN:
                return _CATEGORY_CACHE_DOMAIN[c]
    return "local_general"


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
    total_budget: float | None = None,
) -> dict[str, Any]:
    """local-search 主入口：批量调用本地引擎，返回 unified-search schema。

    since/until: 发布时间时间窗（如 7d / 2026-08-01），下推到支持时间参数的引擎
    （filter_args），并在解析后按 published_at 通用过滤。
    sort: 时间排序 relevance/oldest/newest，仅改变返回顺序，不进入缓存键。
    total_budget: 整体聚合预算（秒），与 `timeout` 的单引擎 HTTP 超时是两个
    语义（此前同一变量承担两职）。None = 沿用 timeout or 30 的历史默认。
    它是**收集上限**：预算/早停到点即返回已收结果，未完成引擎记为 timeout；
    worker 跑在 daemon 线程上不被 join（2026-10-12 起，与 bounded_run 同法），
    CLI 进程不再被慢 ddgs 后端拖住退出（旧 ThreadPoolExecutor 的 non-daemon
    线程会在解释器退出前被 join，实测墙钟比内部耗时多 1.2–5.4s）；弃置线程
    受各自 HTTP/subprocess 超时约束自然结束。in-process 调用方（MCP/
    include-local）拿到的结果集与旧实现同口径（旧实现 shutdown(wait=False)
    同样是「返回时未完成的就不进这轮结果」），差别只在进程退出不再等它们。
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
        # 显式引擎时从引擎分类推断缓存域（此前恒 local_general，
        # 新闻结果被按 general 1h TTL 缓存），见 _infer_cache_domain。
        domain = _infer_cache_domain(engines, reg)

    # 健康过滤（fast/budget 模式下更严格，只检查实际要用的引擎）。
    # 被过滤掉的引擎必须上报（engines_dropped）——静默替换会让「我要的三
    # 个引擎」变成「你给的一个引擎」而无人知晓（2026-09-13 审查假设#7）。
    engines_dropped: list[str] = []
    if mode in ("fast", "budget"):
        try:
            available = set(get_available_engines(registry=reg, engine_names=engines))
            engines_dropped = [e for e in engines if e not in available]
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
    cache = _get_search_cache()
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
            # relevance 是写侧给退化守卫看的内部字段，命中返回时剥掉，
            # 保证命中/未命中两条路径的对外 schema 一致。
            hit_results = _sort_results(
                [{k: v for k, v in r.items() if k != "relevance"}
                 if isinstance(r, dict) else r
                 for r in hit.get("results", [])], sort)
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
    # 按引擎分组留存，供 rrf_merge 按列表融合（见下方融合段）
    by_engine: dict[str, list[dict[str, Any]]] = {}

    def _collect(name: str, res) -> None:
        if res:
            all_results.extend(res)
            by_engine.setdefault(name, []).extend(res)
            engines_used.append(name)

    def _task(name: str) -> tuple[str, list[dict[str, Any]], str]:
        res, err = _search_one(name, query, n=n, timeout=timeout,
                               since=since, until=until)
        return name, res, err

    budget = total_budget if total_budget is not None else (timeout or 30)
    # 够数早停：fast/auto 下已收结果 ≥ n 时不再干等慢引擎。
    # 旧规则：≥n 且 ≥2 引擎有结果才停——第二引擎是 ddgs 慢后端时最坏拖
    # 6s+（2026-10-06 实测 pg vacuum 4.0s：yandex 1.9s 已给足 5 条，
    # 死等 yahoo 4s 才收双引擎共识）。
    # 新规则（共识宽限）：够数后先等 _CONSENSUS_GRACE_S 收第二引擎共识；
    # 宽限到点仍够数即放行。yandex 实测 1.9s 是最快可靠后端，1s 宽限
    # 覆盖正常第二引擎到达窗口；单引擎放行仅在「其余引擎全部慢性子」时
    # 发生，此时等下去的边际共识收益低于墙钟成本。
    # deep/budget 仍收满（质量优先/配额契约不变）。
    #
    # 执行模型（2026-10-12 重写）：daemon 线程 + 轮询，替换 ThreadPoolExecutor。
    # 旧实现的早停只是「不再等结果」，但**解释器退出会被 executor 的非 daemon
    # 线程 join**（官方文档：all threads will be joined before the interpreter
    # can exit）——`shutdown(wait=False)` 挡不住，慢 ddgs 后端跑完才让 CLI 进程
    # 退出。实测墙钟比内部耗时多 1.2–5.4s（`argo search --engine local_search`
    # 7.4–10.0s vs 自报 2.5–5.7s）。daemon 线程到点即返回、不被 join，与
    # bounded_run.py（research/social/crawl/fetch 已迁移）和 mcp_transport 的
    # os._exit 硬退是同一套办法，这里是最后一个未推平的孤点。
    # worker 内仍持有各自引擎的 HTTP/subprocess 超时，弃置线程会自然结束；
    # 输出收集走 holder + 完成事件唤醒，与「已完成即收」逐位一致。
    enough = mode in ("fast", "auto")
    running: list[tuple[threading.Thread, str, dict]] = []
    waiting: list[str] = list(engines)
    slots = max(1, min(len(engines), max_parallel))
    # 完成唤醒事件：worker 结束即 set，主循环 wait 立刻醒——等价旧实现
    # wait(FIRST_COMPLETED) 的「完成即收」；纯 sleep 网格会把 53ms 的快引擎
    # 拖到 250ms+（2026-10-12 审查实测）。完成与否仍以 is_alive() 判定。
    done_evt = threading.Event()

    def _start_worker(name: str) -> None:
        holder: dict = {}

        def _work() -> None:
            try:
                holder["r"] = _task(name)
            except BaseException as e:  # noqa: BLE001 - 单引擎异常不得炸整组
                holder["r"] = (name, [], f"{name}: {e}")
            finally:
                done_evt.set()  # 完成即唤醒主循环（见下方等待注释）

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        running.append((t, name, holder))

    for _ in range(slots):
        if waiting:
            _start_worker(waiting.pop(0))

    def _ingest(entry: tuple[threading.Thread, str, dict]) -> None:
        _t, name, holder = entry
        _n, res, err = holder.get("r", (name, [], f"{name}: 线程结束但没有返回值"))
        _collect(_n, res)
        if err:
            errors.append(err)

    try:
        enough_at: float | None = None
        hit_budget = False
        while running or waiting:
            done_evt.clear()
            # 完成即收：退出运行的线程先入账，再补位排队任务（保持并发上限）
            for entry in list(running):
                if not entry[0].is_alive():
                    running.remove(entry)
                    _ingest(entry)
                    if waiting:
                        _start_worker(waiting.pop(0))
            now = time.time()
            # 够数共识判定（fast/auto）：与预算判定相互独立——曾把
            # 预算检查放进够数分支，结果不足 n 时预算永不生效
            # （test_total_budget_returns_before_slow_engine 红线）。
            if enough and len(all_results) >= n:
                if len(engines_used) >= 2:
                    break  # 双引擎共识达成
                if len(all_results) >= 2 * n:
                    break  # 单引擎已给足 2n：共识边际价值低于宽限墙钟（自适应免宽限）
                if enough_at is None:
                    enough_at = now
                elif now - enough_at >= _CONSENSUS_GRACE_S:
                    break  # 宽限到点：够数放行，不等慢引擎
            if now - t0_all >= budget:
                hit_budget = True
                break
            if running:
                # 完成事件即唤醒（不要 sleep 网格：实测 53ms 的快引擎会被
                # 0.25s 网格拖到 318ms——2026-10-12 审查阻塞项）。
                # 无完成事件时按 _CONSENSUS_POLL_S 节拍醒来复核宽限/预算
                # （宽限是「时间到点即放行」，不能被长任务拖过）。
                done_evt.wait(_CONSENSUS_POLL_S)
        # 收尾前置：末次轮询后完成的补收（不丢结果）——此时 running 里只剩
        # 真正还在跑的；已完成者入账后不再出现在后续统计里。
        for entry in list(running):
            if not entry[0].is_alive():
                running.remove(entry)
                _ingest(entry)
        if hit_budget:
            # 预算到点：未完成引擎记 timeout（与旧 as_completed(timeout) 同语义）
            for _t, name, _h in running:
                errors.append(f"{name}: timeout")
            for name in waiting:
                errors.append(f"{name}: timeout")
    except (TimeoutError, FutureTimeoutError):
        # 兜底：轮询模型不 throw，此分支仅防调用链意外（与旧主技能同构）
        pass
    # 早停/预算放行后仍在跑的线程不 join、不取消（daemon 随进程退出自然消亡；
    # 各自 HTTP/subprocess 超时是硬上限），排队未启动的丢弃——与旧实现
    # shutdown(wait=False, cancel_futures=True) 的对外可见行为一致。

    # engines_used 按路由序归一（此前按完成序追加：线程完成顺序不定，同输入
    # 两次运行的 JSON 输出里顺序不同，破坏可复现性与快照对比）。
    _used = set(engines_used)
    engines_used = [e for e in engines if e in _used]

    # 真实流量被动健康续期：实际产出结果的引擎刷新健康记录（60s 节流防写盘风暴），
    # 避免活跃引擎每隔 5 分钟被 get_available_engines 发起冗余的 "test" 探针请求。
    if engines_used:
        try:
            _reg = get_registry()
            _now_ts = time.time()
            _dirty = False
            for _eng_ok in engines_used:
                _h = _reg.get_health(_eng_ok)
                if (not _h or not _h.get("available", True)
                        or (_now_ts - float(_h.get("last_checked") or 0)) >= 60.0):
                    _reg.update_availability(_eng_ok, True, persist=False)
                    _dirty = True
            if _dirty:
                _reg._save_health()
        except Exception:
            pass

    # 相关度重排（D1，2026-09-27）：位次衰减 → BM25 相关度 × 引擎位次先验。
    # 旧口径 score=max(0.7-idx*0.05, 0.1) 是纯位次——「第 3 条」永远压
    # 「第 1 条不相关结果」，无论是否含查询词。这里在**融合前**把每个引擎
    # 的列表重排为相关度序：RRF 的输入序即相关度序，单引擎路径直接按新
    # score 排序（此前 B3 的 score/_rrf_score 双键分裂随之消解——score
    # 只有「混合相关度」一个含义）。
    # Fail-soft：查询无 token 或全部文档零词面重叠时退化为位次序，与升级前
    # 逐位一致（见 bm25.rerank docstring）；_bm25_rerank 为 None（模块缺失，
    # 导入期已告警过一次）时整段跳过，不再逐查询告警。
    if _bm25_rerank is not None:
        try:
            _bm25_rerank(by_engine, query)
        except Exception as e:
            logger.warning(f"BM25 重排不可用，保持位次分: {e}")

    # 融合：多引擎时接主仓唯一来源 rrf_merge——WG-RRF 引擎加权 +
    # canonical URL 跨引擎去重合并 + consensus_engines 标注。此前是
    # 「位次自行验证」排序：score=0.9-i*0.05 只是单引擎内位次，同位次必然
    # 同分，全局 sort 退化为提交顺序（稳定排序），且无跨引擎去重
    # （2026-09-13 审查 BUG-4）。D1（2026-09-27）起 RRF 的输入是 BM25
    # 重排过的相关度序（见上方 _bm25_rerank）；单引擎路径按同一把 score
    # 排序——score 自此只有「混合相关度」一个含义，B3 的双键分裂消解。
    # 主仓不可用时退回 score 排序（子技能仍可独立运行）。
    if len(by_engine) > 1:
        try:
            _root_scripts = Path(__file__).resolve().parents[2] / "scripts"
            if str(_root_scripts) not in sys.path:
                sys.path.insert(0, str(_root_scripts))
            from search import rrf_merge as _rrf_merge
            fused = _rrf_merge(list(by_engine.values()))
            fused.sort(key=lambda r: float(r.get("_rrf_score", 0) or 0),
                       reverse=True)
            all_results = fused
        except Exception as e:
            logger.warning(f"rrf_merge 融合不可用，退回相关度排序: {e}")
            all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    else:
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    elapsed = int((time.time() - t0_all) * 1000)
    final_results = all_results[: n * len(engines)] if engines else all_results[:n]

    payload = {
        "results": final_results,
        "engines_used": engines_used,
    }

    # 写缓存：使用 SearchCache 标准接口。
    # 写侧补 relevance=score：cache_guard._entry_relevance 只认
    # relevance/rerank_dims.relevance，local 结果只有 score——退化守卫对
    # local 缓存完全失明，「退化回声固化进缓存」原事故类在 local 侧不设防。
    # relevance 只进缓存载荷（命中返回时剥掉），对外 schema 不变。
    # set 整体 try 包裹：守卫（LoginCacheRejected/DegradedCacheRejected）
    # 与 IO 异常只剥夺本次缓存资格，不应炸掉已经拿到的搜索结果。
    if not skip_cache and cache is not None:
        cache_payload = {
            "results": [{**r, "relevance": r.get("score", 0.0)}
                        for r in payload.get("results", []) if isinstance(r, dict)],
            "engines_used": engines_used,
        }
        try:
            cache.set(query, engine=cache_key, max_results=n, results=cache_payload,
                      domain=cache_domain, mode=mode, depth="fast")
        except Exception as e:
            logger.warning(f"缓存写入被拒绝/失败，本次跳过: {e}")

    # 排序在返回前：sort 只改变本次展示顺序，缓存命中路径同样处理
    out_results = _sort_results(final_results, sort)

    return {
        "query": query,
        "engine": engines[0] if engines else "local_search",
        "engines": engines,
        "engines_combo": engines,
        "engines_dropped": engines_dropped,
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

#!/usr/bin/env python3
"""专用构建器：通用搜索 API（parallel.ai / you.com）——按官方文档保持一致

parallel（docs.parallel.ai/search/search-quickstart）：
  - objective（自然语言目标）+ search_queries（数组）为官方推荐组合
  - mode: turbo(~200ms, $1/千次, 仅英日文) / basic(~1s, $5/千次) / advanced(~3s, $5/千次)
    argo 默认 basic，deep 模式 advanced
  - advanced_settings: excerpt_settings.max_chars_per_result 与 argo snippet 截断保持一致

you.com（docs.you.com）：
  - 官方环境变量名 YDC_API_KEY
  - freshness（day/week/month/year）按查询时效敏感度动态化（同 bocha 逻辑）
  - language 按查询主语言下推（zh/en）
  - page_age 为 ISO 8601 时间戳 → 提取为 published_at
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any

from engines_base import safe_search, http_open, mcp_error_of as _mcp_error_of

logger = logging.getLogger("unified_search.engines")

# ── Parallel 搜索（api.parallel.ai）────────────────────────────────────────────

_PARALLEL_URL = "https://api.parallel.ai/v1/search"


def _parallel_key() -> str:
    return os.environ.get("PARALLEL_API_KEY", "")


def _parallel_mode(query: str, depth: str) -> str:
    """mode 选择：deep → advanced；中文查询不可用 turbo（官方仅英日文）→ basic；英文 fast/auto → basic。"""
    if depth == "deep":
        return "advanced"
    return "basic"


def _build_parallel_engine(spec: dict[str, Any]) -> Any:
    """Parallel Search：objective + search_queries + mode（官方推荐组合）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        key = _parallel_key()
        if not key:
            return [{"error": "PARALLEL_API_KEY 未设置", "source": "parallel"}]
        limit = max(1, int(n or 5))
        # 多路召回：原查询 + 无 LLM 变体（问句化/概念扩展等），官方最佳实践 2-3 个
        try:
            from query_variants import generate_query_variations
            queries = generate_query_variations(query)[:3] or [query]
        except Exception:
            queries = [query]
        body = {
            "objective": f"Find the latest, most relevant information about: {query}",
            "search_queries": queries,
            "mode": _parallel_mode(query, depth),
            "advanced_settings": {
                "max_results": min(limit, 20),
                "excerpt_settings": {"max_chars_per_result": 300},
            },
        }
        # argo 时间窗 --since → after_date（官方 --after-date）
        since = kwargs.get("since")
        if since:
            date_str = str(since)[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                body["advanced_settings"]["source_policy"] = {"after_date": date_str}
        req = urllib.request.Request(
            _PARALLEL_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"X-Api-Key": key, "Content-Type": "application/json"},
        )
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"parallel 请求失败: {e}")
            return []
        results: list[dict[str, Any]] = []
        for item in (data.get("results") or [])[:limit]:
            title = str(item.get("title") or "")[:200]
            url = str(item.get("url") or "")
            if not (title or url):
                continue
            excerpts = item.get("excerpts") or []
            snippet = str(excerpts[0])[:300] if excerpts else ""
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "source": "parallel",
                "published_at": str(item.get("publish_date") or "")[:64],
            })
        return results
    return _engine


# ── Parallel 免费通道（search.parallel.ai MCP，keyless）───────────────────────

_PARALLEL_FREE_MCP_URL = "https://search.parallel.ai/mcp"


def _build_parallel_free_engine(spec: dict[str, Any]) -> Any:
    """Parallel 免费搜索：官方免费 MCP 端点的 web_search 工具，恒走免 key 通道。

    与 parallel（REST + PARALLEL_API_KEY，按量计费）同上游、不同经济模型，
    故注册为独立引擎而非共用 cost_tier——「计费源不当免费」检查的语义
    对两条通道各自成立。本引擎**不看 key**：key 的有无/有效与否是 parallel
    的事；免费端点不收 key、按 key 计费，带了反而可能混淆计费归属。
    路由上以 daily_support 低优先级做补位：每家族 max_per_family=2 挡住
    与 parallel 的日常同查冗余，计费通道缺位（额度耗尽/失败/未配 key）时
    由本通道接住。
    2026-09-14 实测：无状态直调成立（免 initialize/session），响应
    content[0].text 内嵌 JSON，字段与 REST 响应同构（url/title/publish_date/
    excerpts）；响应 _meta.parallel.usage 自带上游计量（sku_search，免费
    通道 cost_usd 仅记账）。
    """
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        limit = max(1, int(n or 5))
        # 多路召回：同 REST 通道，官方最佳实践 2-3 个关键词探针
        try:
            from query_variants import generate_query_variations
            queries = generate_query_variations(query)[:3] or [query]
        except Exception:
            queries = [query]
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "web_search", "arguments": {
                "objective": f"Find the latest, most relevant information about: {query}",
                "search_queries": queries,
            }},
        }
        try:
            from http_client import HttpClient
            # max_retries=0：引擎内不做连接级重试（同 anysearch，防与调度层重试叠乘）
            client = HttpClient(timeout=to, max_retries=0, jitter=False)
            resp = client.post(_PARALLEL_FREE_MCP_URL, body=body,
                               extra_headers={"Content-Type": "application/json"})
        except ImportError:
            return []
        if resp.get("status", 0) >= 400 or not resp.get("text"):
            return []
        try:
            data = json.loads(resp["text"])
        except (ValueError, TypeError):
            return []
        mcp_err = _mcp_error_of(data, source="parallel_free")
        if mcp_err:
            logger.warning(f"parallel_free 上游错误: {mcp_err}")
            return [{"error": mcp_err, "source": "parallel_free"}]
        inner: dict[str, Any] = {}
        for block in (data.get("result", {}).get("content") or []):
            text = block.get("text", "") if isinstance(block, dict) else str(block)
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict) and parsed.get("results"):
                inner = parsed
                break
        results: list[dict[str, Any]] = []
        for item in (inner.get("results") or [])[:limit]:
            title = str(item.get("title") or "")[:200]
            url = str(item.get("url") or "")
            if not (title or url):
                continue
            excerpts = item.get("excerpts") or []
            snippet = str(excerpts[0])[:300] if excerpts else ""
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "source": "parallel_free",
                "published_at": str(item.get("publish_date") or "")[:64],
            })
        return results
    return _engine


# ── Seltz 搜索（api.seltz.ai，2026 新兴 agent 搜索）───────────────────────────

_SELTZ_URL = "https://api.seltz.ai/v1/search"


def _seltz_key() -> str:
    return os.environ.get("SELTZ_API_KEY", "")


_SELTZ_SCOPES = ("news", "wikipedia", "people", "companies")


def _build_seltz_engine(spec: dict[str, Any]) -> Any:
    """Seltz Search：POST {query, max_results, scope}，响应 {documents:[...]}。

    官方文档 docs.seltz.ai：x-api-key 头鉴权。响应无 title 字段（content
    即摘录正文，首行截作标题）；无 score/confidence 字段，排序即上游相关度。
    注册赠 20000 次搜索额度（一次性），故配额按 month/20000 保守看管。

    **scope 是语料选择，不是可选项**：上游只支持 news / wikipedia / people /
    companies 四个语料，不传一律落 news。2026-09-16 实测：不传 scope 时技术
    查询（「RRF 融合算法」）拿到的是 news 语料里的时政与播客条目，看上去像
    「引用跑偏」，实为语料选错而非上游坏了。四个语料实测：companies 查公司
    概况最好；news 在真新闻查询上可用但覆盖窄；wikipedia 干净；people 不可用
    （10 条全 linkedin.com，人名查不到）。故本引擎按 spec.scope 声明语料，
    config 里把 coverage 收到对应域，不冒充通用源。
    """
    timeout = spec.get("timeout", 15)
    scope = str(spec.get("scope") or "").strip().lower()
    if scope and scope not in _SELTZ_SCOPES:
        logger.warning(f"seltz: 未知 scope {scope!r}（上游会返回 404），本轮不传 scope")
        scope = ""

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        key = _seltz_key()
        if not key:
            return [{"error": "SELTZ_API_KEY 未设置", "source": "seltz"}]
        to = _timeout or timeout
        limit = max(1, int(n or 5))
        body: dict[str, Any] = {"query": query, "max_results": min(limit, 10)}
        if scope:
            body["scope"] = scope
        req = urllib.request.Request(
            _SELTZ_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"x-api-key": key, "Content-Type": "application/json"},
        )
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"seltz 请求失败: {e}")
            return []
        results: list[dict[str, Any]] = []
        for item in (data.get("documents") or [])[:limit]:
            url = str(item.get("url") or "")
            content = str(item.get("content") or "")
            # 官方响应无 title 字段；content 是 markdown，首行截标题时剥掉
            # 井号标题标记（实测冒烟首行形如 "# Building a Rust HTTP/1.1 server..."）
            title = str(item.get("title") or "") or content.split("\n")[0].lstrip("# ").strip()[:120]
            if not (title or url):
                continue
            results.append({
                "title": title[:200],
                "url": url,
                "snippet": content[:300],
                "source": "seltz",
                "published_at": str(item.get("published_date") or "")[:64],
            })
        return results
    return _engine


# ── You.com 搜索（ydc-index.io）───────────────────────────────────────────────
_YOU_URL = "https://ydc-index.io/v1/search"


def _you_key() -> str:
    return os.environ.get("YDC_API_KEY", "")


def _you_freshness(query: str) -> str:
    """freshness 动态化：时效敏感查询（复用缓存层检测）→ day；否则省略（全量）。"""
    try:
        from cache import is_freshness_sensitive_query
        if is_freshness_sensitive_query(query or ""):
            return "day"
    except Exception:
        pass
    return ""


def _you_language(query: str) -> str:
    """language 下推：含中文 → zh，其余 en。"""
    try:
        from lang_detect import detect_language
        lang = detect_language(query or "")
        if lang:
            return {"zh": "zh", "ja": "ja"}.get(lang, "en")
    except Exception:
        pass
    return "zh" if any("\u4e00" <= c <= "\u9fff" for c in (query or "")) else "en"


def _build_you_engine(spec: dict[str, Any]) -> Any:
    """You.com Web Search：web+news 合并，freshness/language 动态化，page_age → published_at。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        key = _you_key()
        if not key:
            return [{"error": "YDC_API_KEY 未设置", "source": "you"}]
        body: dict[str, Any] = {"query": query, "count": max(1, int(n or 5))}
        freshness = _you_freshness(query)
        if freshness:
            body["freshness"] = freshness
        body["language"] = _you_language(query)
        req = urllib.request.Request(
            _YOU_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"X-API-Key": key, "Content-Type": "application/json"},
        )
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"you 请求失败: {e}")
            return []
        results: list[dict[str, Any]] = []
        for kind in ("news", "web"):
            for item in (data.get("results", {}).get(kind) or [])[: max(1, int(n or 5))]:
                title = str(item.get("title") or "")[:200]
                url = str(item.get("url") or "")
                if not (title or url):
                    continue
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": str(item.get("description") or "")[:300],
                    "source": f"you_{kind}",
                    # page_age 是 ISO 8601 时间戳（如 2026-08-11T16:24:41）→ 取日期
                    "published_at": str(item.get("page_age") or "")[:10],
                })
        return results
    return _engine


# ── Qwant（欧盟 GDPR 合规）──────────────────────────────────────────────────

def _build_qwant_engine(spec: dict[str, Any]) -> Any:
    """Qwant 搜索（qwant.com/?q=，HTML 解析，欧盟 GDPR 合规）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://www.qwant.com/?q=" + up.quote(query)
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.qwant.com/"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                page = resp.read().decode("utf-8", "replace")
            results = []
            for m in re.finditer(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', page, re.S):
                url_m, title_m = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
                if not title_m or not url_m or "qwant" in url_m or len(title_m) < 5:
                    continue
                results.append({
                    "title": title_m[:80],
                    "url": url_m,
                    "snippet": "",
                    "source": "qwant",
                    "score": max(1.0 - len(results) * 0.1, 0.1),
                })
                if len(results) >= n:
                    break
            return results
        except Exception as e:
            logger.warning(f"Qwant 搜索失败: {e}")
            return []
    return _engine


# ── Ecosia（环保隐私引擎）───────────────────────────────────────────────────

def _build_ecosia_engine(spec: dict[str, Any]) -> Any:
    """Ecosia 搜索（ecosia.org/search?q=，HTML 解析，环保隐私引擎）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://www.ecosia.org/search?q=" + up.quote(query)
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.ecosia.org/"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                page = resp.read().decode("utf-8", "replace")
            results = []
            for m in re.finditer(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', page, re.S):
                url_m, title_m = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
                if not title_m or not url_m or "ecosia" in url_m or len(title_m) < 5:
                    continue
                results.append({
                    "title": title_m[:80],
                    "url": url_m,
                    "snippet": "",
                    "source": "ecosia",
                    "score": max(1.0 - len(results) * 0.1, 0.1),
                })
                if len(results) >= n:
                    break
            return results
        except Exception as e:
            logger.warning(f"Ecosia 搜索失败: {e}")
            return []
    return _engine

#!/usr/bin/env python3
"""专用构建器：开放数据 / 包索引 / 金融工具 / Octen"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html import unescape as _unescape
from typing import Any

from cjk_tokens import cjk_term_grams, signal_tokens
from engine_env import get_env
from engines_base import (
    rank_score,
    safe_search, _run, _resolve, _get_path, _coerce_field, _detect_anti_bot,
    http_open,
)

logger = logging.getLogger("unified_search.engines")

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
            with http_open(urllib.request.Request(
                    url, headers={"User-Agent": "argo-search/2.4 (unified-search@local)"}), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"Open Library 失败: {e}")
            return []
        results = []
        for _rk, d in enumerate((data.get("docs") or [])[:n]):
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
                "score": rank_score(0.7, _rk),
            })
        return results
    return _engine


# ── 微信读书图书引擎 ─────────────────────────────────────────────────────────

def _build_weread_engine(spec: dict[str, Any]) -> Any:
    """微信读书图书搜索（Agent Gateway /store/search，需 WEREAD_API_KEY）。

    返回中文书目为主，附评分/评分人数/在读人数，比 Open Library 更适合中文图书。
    """
    timeout = spec.get("timeout", 10)
    gateway = "https://i.weread.qq.com/api/agent/gateway"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        key = get_env(["ARGO_WEREAD_API_KEY", "WEREAD_API_KEY"])
        if not key:
            logger.warning("Weread 缺 WEREAD_API_KEY")
            return []
        body = json.dumps({
            "api_name": "/store/search",
            "keyword": query,
            "count": min(n, 10),
            "skill_version": "1.0.3",
        }).encode("utf-8")
        req = urllib.request.Request(
            gateway, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "argo-search/2.4 (unified-search@local)",
            },
        )
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"微信读书搜索失败: {e}")
            return []
        if isinstance(data, dict) and data.get("errcode"):
            logger.warning(f"微信读书 API 错误: {data.get('errcode')} {data.get('errmsg') or ''}")
            return []
        results = []
        seen: set[str] = set()
        for group in (data.get("results") or []):
            for item in (group.get("books") or [])[:n]:
                bi = item.get("bookInfo") or {}
                title = bi.get("title", "")
                if not title or title in seen:
                    continue
                seen.add(title)
                author = bi.get("author", "")
                rating = bi.get("newRating")
                rating_txt = f"{rating / 10:.1f}分" if isinstance(rating, (int, float)) and rating > 0 else ""
                rating_count = bi.get("newRatingCount")
                reading_count = item.get("readingCount")
                parts = [p for p in (
                    author,
                    rating_txt,
                    f"{rating_count}人评分" if rating_count else "",
                    f"{reading_count}人在读" if reading_count else "",
                ) if p]
                results.append({
                    "title": title,
                    "url": bi.get("deepLink") or f"https://weread.qq.com/book-detail?type=1&bookId={bi.get('bookId', '')}",
                    "snippet": " · ".join(parts)[:300],
                    "source": "weread",
                    "score": rank_score(0.85, len(results)),
                })
                if len(results) >= n:
                    break
            if len(results) >= n:
                break
        return results
    return _engine


# ── 豆瓣读书引擎 ─────────────────────────────────────────────────────────────

def _build_douban_book_engine(spec: dict[str, Any]) -> Any:
    """豆瓣读书搜索（search.douban.com/book/subject_search，免认证）。

    页面内嵌 window.__DATA__ JSON，含评分/评分人数/出版社/年份/价格，
    与微信读书互补（出版社维度）。无官方 API，走网页内嵌数据。
    """
    timeout = spec.get("timeout", 10)
    _UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://search.douban.com/book/subject_search?" + up.urlencode({
            "search_text": query, "cat": "1001",
        })
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                html = resp.read().decode("utf-8", "replace")
        except Exception as e:
            logger.warning(f"豆瓣读书搜索失败: {e}")
            return []
        marker = "window.__DATA__ = "
        i = html.find(marker)
        if i < 0:
            return []
        try:
            dec = json.JSONDecoder()
            data, _ = dec.raw_decode(html[i + len(marker):].lstrip())
        except Exception as e:
            logger.warning(f"豆瓣 __DATA__ 解析失败: {e}")
            return []
        results = []
        seen: set[str] = set()
        for _rk2, item in enumerate((data.get("items") or [])):
            if item.get("tpl_name") != "search_subject":
                continue
            title = item.get("title", "")
            if not title or title in seen:
                continue
            seen.add(title)
            rating = item.get("rating") or {}
            rv = rating.get("value")
            rc = rating.get("count")
            parts = [p for p in (
                item.get("abstract") or "",
                f"{rv}分" if rv else "",
                f"{rc}人评分" if rc else "",
            ) if p]
            results.append({
                "title": title,
                "url": item.get("url") or f"https://book.douban.com/subject/{item.get('id', '')}/",
                "snippet": " · ".join(parts)[:300],
                "source": "douban_book",
                "score": rank_score(0.85, _rk2),
            })
            if len(results) >= n:
                break
        return results
    return _engine


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
            with http_open(urllib.request.Request(
                    url, headers={"User-Agent": "argo-search/2.4"}), timeout=to, engine=spec.get("_name", "")) as resp:
                entries = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []
        if not isinstance(entries, list):
            return []
        results = []
        for _rk3, e in enumerate(entries[:n]):
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
                "score": rank_score(0.8, _rk3),
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
            with http_open(urllib.request.Request(sug_url, headers=headers), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            for _rk25, item in enumerate((data.get("list") or [])[:n]):
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
                    "score": rank_score(0.85, _rk25),
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
                with http_open(urllib.request.Request(card_url, headers=headers), timeout=to, engine=spec.get("_name", "")) as resp:
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
        # 相关度检查：零重叠词条丢弃（触发 recovery）。中文走 2-gram，见 cjk_tokens：
        # 旧写法对无空格中文把整句当一个 token，子串包含恒假，词条全被丢弃。
        q_keys = set(signal_tokens(q, stop={
            "the", "and", "for", "year", "founded", "founding", "headquarters",
            "where", "what", "when", "which", "with", "from", "that", "this",
            "年份", "时间", "成立", "创办", "创立", "总部", "职能", "简介",
        }))
        if q_keys:
            filtered = []
            for r in results:
                blob = f"{r.get('title','')} {r.get('snippet','')}".lower()
                if any(k in blob for k in q_keys):
                    filtered.append(r)
            results = filtered
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
        for _rk4, name in enumerate(_candidates(query)):
            if len(results) >= n:
                break
            url = f"https://pypi.org/pypi/{name}/json"
            try:
                with http_open(urllib.request.Request(url, headers=headers), timeout=to, engine=spec.get("_name", "")) as resp:
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
                "score": rank_score(0.9, _rk4),
            })
        return results[:n]
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
            with http_open(urllib.request.Request(url, headers=headers), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"juejin 失败: {e}")
            return []
        results = []
        for _rk7, item in enumerate((data.get("data") or [])[: n * 2]):
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
                "score": rank_score(0.75, _rk7),
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
                    # 无明确搜索词也无能力词 → 诚实零结果（不保底热门模型，
                    # 避免产出「看起来像结果」的噪声稀释融合）
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
                    # 可定位到具体模型页（prov_doc 是 provider 首页，多个结果
                    # 共享同一 URL，导致下游去重/引用全部失效）
                    "url": f"https://models.dev/?model={mid}",
                    "snippet": snippet[:300],
                    "source": "models_dev",
                    # 0.8 固定基线分（结构化目录条目，非语义搜索，不宜 0.95 压过真实相关结果）
                    "score": rank_score(0.8, len(results)),
                })
                if len(results) >= n:
                    break
            if len(results) >= n:
                break

        return results
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
        key = get_env(["ARGO_QWEATHER_KEY", "QWEATHER_KEY"])
        if not key:
            logger.warning("QWEATHER_KEY 未设置，跳过和风天气")
            return [{"error": "QWEATHER_KEY 未设置", "source": "qweather"}]

        # 从 query 抽取城市名（去掉天气/预报等词）
        city = re.sub(r"(天气|预报|气温|温度|今天|明天|实时|怎么样|多少度)", "", query or "").strip()
        city = city or query.strip()

        # 1) GeoAPI: 城市 → LocationID
        geo_url = f"https://geoapi.qweather.com/v2/city/lookup?location={up.quote(city)}&key={key}"
        req = urllib.request.Request(geo_url, headers={"User-Agent": "argo-search/1.0"})
        with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
        with http_open(req2, timeout=to, engine=spec.get("_name", "")) as resp:
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
        api_key = get_env(["ARGO_OCTEN_API_KEY", "OCTEN_API_KEY"])
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
                                    "score": rank_score(0.8, len(results)),
                                })
                                if len(results) >= n:
                                    return results
                else:
                    for r in items.get("results", [])[:n]:
                        results.append({
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "snippet": r.get("highlight", "")[:300],
                            "source": "octen",
                            "score": rank_score(next((v for v in (r.get("score"),) if v not in (None, "")), 0.5), len(results)),  # 哨兵判空：显式 0 分不得膨胀成 0.5
                        })

                # 如果结果太少，补充 quick 数据源的查询（不走 broad 保底）
                if not results and not use_broad:
                    logger.info("Octen 标准搜索无结果，兜底空列表返回")
                return results

        except Exception as e:
            logger.warning(f"Octen 引擎失败: {e}")
            return []
    return _engine


# ── RFC Editor / IETF 标准引擎（tech 域） ────────────────────────────────────

_DATATRACKER_API = "https://datatracker.ietf.org/api/v1"


def _build_rfc_editor_engine(spec: dict[str, Any]) -> Any:
    """RFC / IETF 标准文档检索（datatracker.ietf.org API，免认证）。

    「RFC 9000」直接定位单篇；「QUIC RFC」按标题搜索 RFC 类型文档。
    """
    timeout = spec.get("timeout", 15)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    def _jget(url: str, to: float) -> dict:
        req = urllib.request.Request(url, headers=_HEADERS)
        with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _mk(r: dict) -> dict[str, Any]:
        name = r.get("name", "")
        num = re.search(r"(\d+)", name)
        title = r.get("title") or ""
        url = (f"https://www.rfc-editor.org/rfc/rfc{num.group(1)}.txt"
               if num else "https://www.rfc-editor.org/")
        snip = "Internet 标准文档（IETF）"
        if r.get("abstract"):
            snip = r["abstract"][:300]
        return {
            "title": f"{name.upper()}: {title}" if num else title,
            "url": url,
            "snippet": snip,
            "source": "rfc_editor",
            "score": 0.93,
        }

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        # 1. 直接 RFC 编号
        m = re.search(r"(?i)(?:rfc\s*|RFC)(\d{2,5})", q)
        if m:
            num = m.group(1)
            try:
                d = _jget(f"{_DATATRACKER_API}/doc/document/rfc{num}/", to)
                if d.get("name"):
                    return [_mk(d)][: max(n, 3)]
            except Exception:
                pass
        # 2. 标题搜索：剥掉 RFC/标准等噪声词后按标题检索
        search_q = re.sub(r"(?i)\b(rfc|ietf|internet standard|standard)\b|互联网标准|标准文档|文档", " ", q)
        search_q = re.sub(r"\s+", " ", search_q).strip()
        if not search_q:
            return []
        url = (f"{_DATATRACKER_API}/doc/document/?title__icontains="
               f"{urllib.parse.quote(search_q)}&type__slug=rfc&limit={min(n, 10)}")
        try:
            d = _jget(url, to)
        except Exception as e:
            logger.warning(f"RFC 搜索失败: {e}")
            return []
        return [_mk(o) for o in (d.get("objects") or [])[:n]][: max(n, 3)]

    return _engine



# ── GDELT 全球事件数据库 ─────────────────────────────────────────────────────

# ── Marginalia 独立索引引擎 ──────────────────────────────────────────────────

def _build_marginalia_engine(spec: dict[str, Any]) -> Any:
    """Marginalia 独立爬虫索引搜索（old-search.marginalia.nu，免认证 HTML）。

    2026-11-08 搬家：api.marginalia.nu JSON 直出挂死（8s/25s 两轮复测 0 字节），
    主站 search.marginalia.nu 已改 SPA 空壳；old-search 服务端渲染仍活。
    上游 bot 门是文档化行为（等待页提示「wait 2 seconds」并给 sst 通行链接）：
    两步流程 = 先取等待页提取 sst，睡 2.2s 后复访该链接（实测 88 外链/页；
    令牌必须新鲜，复用过期令牌会被再次门住）。非大厂代理的独立索引，
    专挖长尾非商业页面。
    """
    timeout = spec.get("timeout", 16)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        base = "https://old-search.marginalia.nu"
        url = f"{base}/search?query={up.quote(query)}"
        try:
            with http_open(urllib.request.Request(
                    url, headers={"User-Agent": "argo-search/2.4 (unified-search@local)"}), timeout=to, engine=spec.get("_name", "")) as resp:
                page = resp.read().decode("utf-8", "replace")
        except Exception as e:
            logger.warning(f"Marginalia 失败: {e}")
            return []
        gate = re.search(r'href="(/search\?query=[^"]*sst=[^"]+)"', page)
        if gate:
            time.sleep(2.2)
            try:
                with http_open(urllib.request.Request(
                        base + gate.group(1).replace("&amp;", "&"),
                        headers={"User-Agent": "argo-search/2.4 (unified-search@local)"}), timeout=to, engine=spec.get("_name", "")) as resp:
                    page = resp.read().decode("utf-8", "replace")
            except Exception as e:
                logger.warning(f"Marginalia sst 复访失败: {e}")
                return []
        results = []
        for _rk20, block in enumerate(re.split(r'<section[^>]*class="card search-result"', page)[1:]):
            m = re.search(r'<h2>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not m:
                continue
            title = _unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            if not title:
                continue
            md = re.search(r'<p class="description">(.*?)</p>', block, re.S)
            desc = _unescape(re.sub(r"<[^>]+>", "", md.group(1))).strip() if md else ""
            results.append({
                "title": title[:200],
                "url": m.group(1),
                "snippet": desc[:300],
                "source": "marginalia",
                "score": rank_score(0.7, _rk20),
            })
            if len(results) >= n:
                return results
        # 补足：同域折叠的 additional-results 只有标题链接（无 description）
        for mu in re.finditer(r'<ul class="additional-results">(.*?)</ul>', page, re.S):
            for ma in re.finditer(r'<li><a href="(https?://[^"]+)"[^>]*>(.*?)</a></li>', mu.group(1), re.S):
                title = _unescape(re.sub(r"<[^>]+>", "", ma.group(2))).strip()
                if not title:
                    continue
                results.append({
                    "title": title[:200],
                    "url": ma.group(1),
                    "snippet": "",
                    "source": "marginalia",
                    "score": rank_score(0.55, len(results)),
                })
                if len(results) >= n:
                    break
            if len(results) >= n:
                break
        return results
    return _engine


# ── Wiby 手工网页索引引擎 ────────────────────────────────────────────────────

def _build_wiby_engine(spec: dict[str, Any]) -> Any:
    """Wiby 老式手工网页索引搜索（wiby.me/json，免认证，注意大写键）。

    专收非商业化内容的老式索引，与主流引擎互补。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://wiby.me/json/?q={up.quote(query)}"
        try:
            with http_open(urllib.request.Request(
                    url, headers={"User-Agent": "argo-search/2.4 (unified-search@local)"}), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"Wiby 失败: {e}")
            return []
        results = []
        for _rk21, r in enumerate((data if isinstance(data, list) else [])[:n]):
            if not isinstance(r, dict):
                continue
            title = r.get("Title", r.get("title", ""))
            url_ = r.get("URL", r.get("url", ""))
            if not title and not url_:
                continue
            results.append({
                "title": title,
                "url": url_,
                "snippet": r.get("Snippet", r.get("Description", ""))[:300],
                "source": "wiby",
                "score": rank_score(0.7, _rk21),
            })
        return results
    return _engine

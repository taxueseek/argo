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
from typing import Any

from engines_base import (
    safe_search, _run, _resolve, _get_path, _coerce_field, _detect_anti_bot,
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
        key = os.environ.get("WEREAD_API_KEY") or os.environ.get("ARGO_WEREAD_API_KEY")
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
            with urllib.request.urlopen(req, timeout=to) as resp:
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
                    "score": 0.85,
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
            with urllib.request.urlopen(req, timeout=to) as resp:
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
        for item in (data.get("items") or []):
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
                "score": 0.85,
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
        # 相关度门禁：英文实体问返回「我们选择登月」等零重叠词条时丢弃，触发 recovery
        q_keys = set()
        for t in re.findall(r"[A-Z]{2,}|[a-zA-Z]{3,}|[\u4e00-\u9fff]{2,}", q):
            if t.isupper() and len(t) >= 2:
                q_keys.add(t.lower())
            elif t.lower() not in {
                "the", "and", "for", "year", "founded", "founding", "headquarters",
                "where", "what", "when", "which", "with", "from", "that", "this",
                "年份", "时间", "成立", "创办", "创立", "总部", "职能", "简介",
            }:
                q_keys.add(t.lower())
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
                    # 无明确搜索词也无能力词 → 诚实零结果（不兜底热门模型，
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
                    "score": 0.8,
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


# ── PubChem + ChEMBL 化学引擎（chem 域） ────────────────────────────────────

_PUBCHEM_API = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
_CHEMBL_API = "https://www.ebi.ac.uk/chembl/api/data"
_RE_FORMULA = re.compile(r"^[A-Z][a-z]?\d*([A-Z][a-z]?\d*)*$")
# 检索噪声词：剥掉后用核心化合物名打 API（「阿司匹林分子式」→「阿司匹林」）
_CHEM_NOISE_RE = re.compile(
    r"(?i)\b(?:molecular\s+weight|chemical\s+formula|cas\s*number|iupac|smiles|"
    r"formula|weight|compound|molecule|mw)\b|"
    r"分子式|分子量|摩尔质量|化学式|结构式|化合物|药物|CAS号?"
)
# 常用中文药名 → PubChem 英文名（中文直查常 miss，ChEMBL 会乱返回无关分子）
_CHEM_CN_ALIASES: dict[str, str] = {
    "阿司匹林": "aspirin",
    "乙酰水杨酸": "aspirin",
    "布洛芬": "ibuprofen",
    "对乙酰氨基酚": "acetaminophen",
    "扑热息痛": "acetaminophen",
    "咖啡因": "caffeine",
    "葡萄糖": "glucose",
    "乙醇": "ethanol",
    "甲醇": "methanol",
    "胆固醇": "cholesterol",
    "青霉素": "penicillin",
    "阿莫西林": "amoxicillin",
    "二甲双胍": "metformin",
    "奥美拉唑": "omeprazole",
    "硝苯地平": "nifedipine",
    "氨氯地平": "amlodipine",
    "阿托伐他汀": "atorvastatin",
    "氯吡格雷": "clopidogrel",
    "华法林": "warfarin",
    "胰岛素": "insulin",
    "吗啡": "morphine",
    "可待因": "codeine",
    "尼古丁": "nicotine",
    "维生素c": "ascorbic acid",
    "维生素C": "ascorbic acid",
    "抗坏血酸": "ascorbic acid",
}
_CHEM_TOKEN_NOISE = frozenset({
    "分子量", "分子式", "化学", "化学式", "结构式", "化合物", "药物",
    "weight", "formula", "molecular", "chemical", "compound", "molecule",
    "cid", "chembl", "smiles", "iupac", "cas", "mw", "acid", "hydrochloride",
})


def _strip_chem_noise(query: str) -> str:
    """去掉「分子式/分子量」等修饰，保留化合物核心名。"""
    cleaned = _CHEM_NOISE_RE.sub(" ", query)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;|/")
    return cleaned or query.strip()


def _chem_lookup_names(query: str) -> list[str]:
    """生成 PubChem/ChEMBL 查询候选：核心名 + 中文别名英文化。"""
    raw = query.strip()
    core = _strip_chem_noise(raw)
    names: list[str] = []
    for candidate in (core, raw):
        if candidate and candidate not in names:
            names.append(candidate)
    # 中文别名：核心或原文含中文药名时追加英文
    hay = f"{core} {raw}"
    for cn, en in _CHEM_CN_ALIASES.items():
        if cn in hay and en not in names:
            names.append(en)
    return names


def _chem_tokens(text: str) -> set[str]:
    """化合物相关 token：英文词 + 连续中文 + 数字。"""
    if not text:
        return set()
    low = text.lower()
    tokens = set(re.findall(r"[a-z][a-z0-9\-]{1,}", low))
    tokens.update(re.findall(r"[\u4e00-\u9fff]{2,}", text))
    tokens.update(re.findall(r"\d{2,}", text))
    return {t for t in tokens if t not in _CHEM_TOKEN_NOISE and len(t) >= 2}


def _chem_result_overlaps(query: str, title: str, snippet: str = "",
                          pref_name: str = "") -> bool:
    """结果与查询是否相关：token 交集或核心名子串命中。

    无重叠则视为假阳性（ChEMBL 对中文常返回无关分子）。
    """
    q_tokens = _chem_tokens(query)
    core = _strip_chem_noise(query).lower()
    if core:
        q_tokens |= _chem_tokens(core)
    for cn, en in _CHEM_CN_ALIASES.items():
        if cn in query or cn in core:
            q_tokens.add(en.lower())
            for part in en.lower().split():
                if len(part) >= 2:
                    q_tokens.add(part)
    doc = _chem_tokens(f"{title} {snippet} {pref_name}")
    if not q_tokens:
        return True  # 无法判定时不误杀
    if q_tokens & doc:
        return True
    hay = f"{title} {snippet} {pref_name}".lower()
    if core and len(core) >= 2 and core in hay:
        return True
    # 英文别名整词
    for cn, en in _CHEM_CN_ALIASES.items():
        if cn in query and en.lower() in hay:
            return True
    return False


def _build_pubchem_engine(spec: dict[str, Any]) -> Any:
    """化学/药学化合物检索：PubChem PUG REST 主路径 + ChEMBL 兜底。

    支持化合物名、分子式、CAS 号、IUPAC 名查询，返回分子式/分子量/IUPAC/SMILES。
    质量闸门：ChEMBL 结果须与查询 token 重叠，否则丢弃当 no-results（宁空勿假）。
    """
    timeout = spec.get("timeout", 10)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    def _jget(url: str, to: float) -> dict:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=to) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        results: list[dict[str, Any]] = []
        lookup_names = _chem_lookup_names(q)
        display_name = _strip_chem_noise(q) or q

        # 1. PubChem 主路径：多候选名 → CID → 属性（formula 端点异步，不在此轮询）
        cid = ""
        hit_name = display_name
        for name in lookup_names:
            try:
                d = _jget(
                    f"{_PUBCHEM_API}/compound/name/{urllib.parse.quote(name)}/cids/JSON",
                    to,
                )
                cids = (d.get("IdentifierList") or {}).get("CID") or []
                if cids:
                    cid = str(cids[0])
                    hit_name = name
                    break
            except Exception:
                continue
        if cid:
            try:
                d = _jget(
                    f"{_PUBCHEM_API}/compound/cid/{cid}/property/"
                    f"MolecularFormula,MolecularWeight,IUPACName,CanonicalSMILES/JSON", to)
                props = ((d.get("PropertyTable") or {}).get("Properties") or [{}])[0]
                formula = props.get("MolecularFormula", "")
                mw = props.get("MolecularWeight", "")
                iupac = props.get("IUPACName", "") or ""
                smiles = props.get("CanonicalSMILES", "") or ""
                title = f"{display_name} (PubChem CID {cid})"
                if hit_name.lower() != display_name.lower():
                    title = f"{display_name} / {hit_name} (PubChem CID {cid})"
                if formula:
                    title += f" 分子式 {formula}"
                if mw:
                    title += f" 分子量 {mw}"
                snippet = f"CID {cid}"
                if iupac:
                    snippet += f" | IUPAC: {iupac}"
                if smiles:
                    snippet += f" | SMILES: {smiles}"
                results.append({
                    "title": title,
                    "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                    "snippet": snippet[:400],
                    "source": "pubchem",
                    "score": 0.95,
                })
            except Exception as e:
                logger.warning(f"PubChem 属性失败: {e}")

        # 2. ChEMBL 兜底：仅保留与查询重叠的结果（无重叠 = 空，禁止交差评）
        if not results:
            seen_ids: set[str] = set()
            for name in lookup_names:
                try:
                    d = _jget(
                        f"{_CHEMBL_API}/molecule/search.json?"
                        f"q={urllib.parse.quote(name)}&limit={min(n, 5)}",
                        to,
                    )
                except Exception as e:
                    logger.warning(f"ChEMBL 失败 ({name}): {e}")
                    continue
                for m in (d.get("molecules") or [])[:n]:
                    props = m.get("molecule_properties") or {}
                    chembl_id = m.get("molecule_chembl_id", "")
                    if chembl_id in seen_ids:
                        continue
                    pref = (m.get("pref_name") or "") or ""
                    title_base = pref or chembl_id or name
                    title = title_base
                    if props.get("mw_freebase"):
                        title += f" 分子量 {props['mw_freebase']}"
                    parts = [f"ChEMBL {chembl_id}"]
                    if props.get("full_molformula"):
                        parts.append(f"分子式 {props['full_molformula']}")
                    if props.get("canonical_smiles"):
                        parts.append(f"SMILES {props['canonical_smiles'][:100]}")
                    snippet = " | ".join(parts)[:400]
                    # 质量闸门：与原查询或当前 lookup 名无重叠则丢弃
                    if not (
                        _chem_result_overlaps(q, title, snippet, pref)
                        or _chem_result_overlaps(name, title, snippet, pref)
                    ):
                        continue
                    seen_ids.add(chembl_id)
                    results.append({
                        "title": title,
                        "url": (f"https://www.ebi.ac.uk/chembl/explore/compound/{chembl_id}"
                                if chembl_id else "https://www.ebi.ac.uk/chembl/"),
                        "snippet": snippet,
                        "source": "chembl",
                        "score": 0.9,
                    })
                if results:
                    break
            if not results:
                logger.info("ChEMBL 结果与查询无重叠，返回空（宁空勿假）")
        return results[: max(n, 3)]

    return _engine


# ── GBIF 生物多样性引擎（species 域） ────────────────────────────────────────

_GBIF_API = "https://api.gbif.org/v1"
_GBIF_RANK_ORDER = {"SPECIES": 3, "SUBSPECIES": 2, "GENUS": 1}


def _build_gbif_engine(spec: dict[str, Any]) -> Any:
    """GBIF 物种检索（api.gbif.org/v1/species/search，免认证）。

    学名/俗名搜索，优先学名包含查询词的 ACCEPTED 物种条目。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)"}

    def _jget(url: str, to: float) -> dict:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=to) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        # 剥离中文查询噪声词（学名/物种/俗名等）；GBIF 只对拉丁学名/英文俗名可靠，
        # 纯中文俗名（如「大熊猫」）搜出来的是无关属种，直接放弃
        q = re.sub(r"(学名|物种|俗名|拉丁名|生物|分类|是什么|有哪些|查询|搜索)", "", q).strip()
        if not re.search(r"[A-Za-z]{2,}", q):
            return []
        to = _timeout or timeout
        ql = q.lower()
        url = f"{_GBIF_API}/species/search?q={urllib.parse.quote(q)}&limit={min(n * 3, 20)}"
        try:
            d = _jget(url, to)
        except Exception as e:
            logger.warning(f"GBIF 失败: {e}")
            return []

        def _key(r: dict) -> tuple:
            sn = (r.get("scientificName") or "").lower()
            name_hit = ql in sn  # 学名包含查询词优先（俗名匹配的噪声大）
            status_ok = (r.get("taxonomicStatus") or "") == "ACCEPTED"
            rank = _GBIF_RANK_ORDER.get(r.get("rank"), 0)
            return (name_hit, status_ok, rank)

        items = sorted((r for r in (d.get("results") or []) if r.get("scientificName")), key=_key, reverse=True)
        results = []
        for r in items[:n]:
            sci = r.get("scientificName", "")
            rank = r.get("rank", "")
            kingdom = r.get("kingdom") or r.get("kingdomKey") or ""
            status = r.get("taxonomicStatus") or ""
            key = r.get("nubKey") or r.get("key")
            title = f"{sci} ({rank})"
            if status == "ACCEPTED":
                title = f"{sci}（有效名）"
            snip = f"界 {kingdom} | 分类 {rank}"
            if status:
                snip += f" | 状态 {status}"
            if r.get("vernacularName"):
                snip += f" | 俗名 {r['vernacularName']}"
            results.append({
                "title": title,
                "url": f"https://www.gbif.org/species/{key}" if key else "https://www.gbif.org/",
                "snippet": snip,
                "source": "gbif",
                "score": 0.92,
            })
        return results[: max(n, 3)]

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
        with urllib.request.urlopen(req, timeout=to) as resp:
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



# ── UniProt 蛋白质/基因组引擎 ────────────────────────────────────────────────

_UNIPROT_API = "https://rest.uniprot.org/uniprotkb/search"

# 高频蛋白中文名 → 英文检索词（UniProt 不索引中文）
_UNIPROT_CN = {
    "胰岛素": "insulin", "血红蛋白": "hemoglobin", "肌红蛋白": "myoglobin",
    "白蛋白": "albumin", "胶原蛋白": "collagen", "角蛋白": "keratin",
    "胰蛋白酶": "trypsin", "溶菌酶": "lysozyme", "细胞色素c": "cytochrome c",
    "细胞色素": "cytochrome", "泛素": "ubiquitin", "免疫球蛋白": "immunoglobulin",
    "抗体": "antibody", "转铁蛋白": "transferrin", "纤维蛋白原": "fibrinogen",
    "表皮生长因子受体": "EGFR", "生长激素": "growth hormone",
    "血管内皮生长因子": "VEGF", "肿瘤坏死因子": "TNF", "白介素": "interleukin",
    "干扰素": "interferon", "淀粉酶": "amylase", "胃蛋白酶": "pepsin",
    "弹性蛋白": "elastin", "肌动蛋白": "actin", "肌球蛋白": "myosin",
    "微管蛋白": "tubulin", "组蛋白": "histone", "核糖体蛋白": "ribosomal protein",
    "谷胱甘肽过氧化物酶": "glutathione peroxidase", "超氧化物歧化酶": "superoxide dismutase",
    "载脂蛋白": "apolipoprotein", "脂蛋白": "lipoprotein", "受体蛋白": "receptor protein",
    "蛋白激酶": "protein kinase", "磷酸酶": "phosphatase", "转录因子": "transcription factor",
    "癌基因": "oncogene", "抑癌基因": "tumor suppressor", "p53蛋白": "p53",
}


def _ascii_tokens(q: str) -> str:
    """提取查询中的 ASCII 字母数字 token（UniProt/PDB 只索引英文）。"""
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-\.\*]*", q)
    return " ".join(toks).strip()


def _build_uniprot_engine(spec: dict[str, Any]) -> Any:
    """UniProt 蛋白质检索（rest.uniprot.org/uniprotkb/search，免认证）。

    基因名/蛋白名/物种查询，返回 accession、推荐名、基因名与物种。
    中英混合查询只取 ASCII token（UniProt 不索引中文），纯中文查高频蛋白名映射表。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q_raw = query.strip()
        if not q_raw:
            return []
        q = _ascii_tokens(q_raw)
        if not q:
            q = _UNIPROT_CN.get(q_raw, "")
            if not q:
                return []
        to = _timeout or timeout
        url = f"{_UNIPROT_API}?" + urllib.parse.urlencode({
            "query": q, "format": "json", "size": min(n, 25),
        })
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"UniProt 失败: {e}")
            return []
        results = []
        for x in (data.get("results") or [])[:n]:
            acc = x.get("primaryAccession", "")
            pd = x.get("proteinDescription") or {}
            rn = ((pd.get("recommendedName") or {}).get("fullName") or {}).get("value", "")
            genes = x.get("genes") or []
            gene = ""
            if genes and isinstance(genes[0], dict):
                gene = ((genes[0].get("geneName") or {}).get("value", ""))
            org = ((x.get("organism") or {}).get("scientificName") or "")
            title = rn or (f"{acc}" if acc else q)
            snip = " · ".join(p for p in (org, gene) if p)
            results.append({
                "title": f"{title} ({acc})" if acc else title,
                "url": f"https://www.uniprot.org/uniprotkb/{acc}" if acc else "",
                "snippet": snip[:300],
                "source": "uniprot",
                "score": 0.9,
            })
        return results
    return _engine


# ── RCSB PDB 蛋白质结构引擎 ──────────────────────────────────────────────────

_PDB_SEARCH_API = "https://search.rcsb.org/rcsbsearch/v2/query"


def _build_rcsb_pdb_engine(spec: dict[str, Any]) -> Any:
    """RCSB PDB 蛋白质结构检索（search.rcsb.org v2，免认证）。

    全文搜索返回 PDB ID 列表，链接到 rcsb.org/structure/{id}。
    """
    timeout = spec.get("timeout", 15)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)",
                "Accept": "application/json", "Content-Type": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = _ascii_tokens(query)
        if not q:
            return []
        to = _timeout or timeout
        body = json.dumps({
            "query": {"type": "terminal", "service": "full_text", "parameters": {"value": q}},
            "return_type": "entry",
            "request_options": {"return_all_hits": True},
        }).encode("utf-8")
        try:
            req = urllib.request.Request(_PDB_SEARCH_API, data=body, headers=_HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"RCSB PDB 失败: {e}")
            return []
        results = []
        for x in (data.get("result_set") or [])[:n]:
            pid = x.get("identifier", "")
            if not pid:
                continue
            results.append({
                "title": f"PDB 结构 {pid}（{q}）",
                "url": f"https://www.rcsb.org/structure/{pid}",
                "snippet": f"RCSB PDB 实验结构 {pid}，分辨率 {x.get('score', '')}"[:300],
                "source": "rcsb_pdb",
                "score": 0.85,
            })
        return results
    return _engine


# ── CourtListener 美国判例引擎 ───────────────────────────────────────────────

_COURTLISTENER_API = "https://www.courtlistener.com/api/rest/v4/search/"


def _build_courtlistener_engine(spec: dict[str, Any]) -> Any:
    """CourtListener 美国判例检索（courtlistener.com/api/rest/v4，匿名可用）。

    注意 2026-05 起限额骤降（免费档约 5 次/分、125 次/天），qps 已压到 1。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        url = _COURTLISTENER_API + "?" + urllib.parse.urlencode({
            "q": q, "format": "json", "page_size": min(n, 10), "order_by": "score desc",
        })
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"CourtListener 失败: {e}")
            return []
        results = []
        for x in (data.get("results") or [])[:n]:
            name = x.get("caseName") or q
            rel = x.get("absolute_url") or ""
            date = (x.get("dateFiled") or "")[:10]
            snip = str(x.get("snippet") or "")[:280]
            body_txt = str(x.get("plain_text") or "")[:280]
            snip = snip or body_txt
            parts = [p for p in (date, snip) if p]
            results.append({
                "title": name,
                "url": f"https://www.courtlistener.com{rel}" if rel else "",
                "snippet": " · ".join(parts)[:300],
                "source": "courtlistener",
                "score": 0.9,
            })
        return results
    return _engine


# ── Project Gutenberg 公版书引擎（Gutendex） ─────────────────────────────────

_GUTENDEX_API = "https://gutendex.com/books"


def _build_gutenberg_engine(spec: dict[str, Any]) -> Any:
    """Project Gutenberg 公版书检索（gutendex.com，免认证）。

    返回书名/作者/语言与 Gutenberg 书目页链接；Gutenberg 提供结构化全文。
    """
    timeout = spec.get("timeout", 15)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        url = _GUTENDEX_API + "?" + urllib.parse.urlencode({"search": q, "page_size": min(n, 10)})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"Gutendex 失败: {e}")
            return []
        results = []
        for x in (data.get("results") or [])[:n]:
            title = x.get("title") or q
            gid = x.get("id") or ""
            authors = ", ".join((a.get("name") or "") for a in (x.get("authors") or [])[:2])
            langs = ",".join(x.get("languages") or [])
            fmt = x.get("formats") or {}
            txt = fmt.get("text/plain; charset=us-ascii") or fmt.get("text/html")
            snip = " · ".join(p for p in (authors, langs, "公版书全文") if p)
            results.append({
                "title": title,
                "url": f"https://www.gutenberg.org/ebooks/{gid}" if gid else txt or "",
                "snippet": snip[:300],
                "source": "gutenberg",
                "score": 0.85,
            })
        return results
    return _engine


# ── Wayback CDX 已删除内容检索引擎 ───────────────────────────────────────────

_CDX_API = "https://web.archive.org/cdx/search/cdx"


def _build_wayback_cdx_engine(spec: dict[str, Any]) -> Any:
    """Wayback Machine CDX 快照检索（web.archive.org/cdx，免认证）。

    输入域名/URL，返回历史快照时间戳与状态码，是查已删除页面的唯一路径。
    """
    timeout = spec.get("timeout", 20)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip().lower()
        if not q:
            return []
        # 剥离常见协议前缀，CDX 用裸域名/host 查询
        q = re.sub(r"^https?://", "", q).strip()
        if not q:
            return []
        to = _timeout or timeout
        url = _CDX_API + "?" + urllib.parse.urlencode({
            "url": q, "output": "json", "limit": min(n, 10),
            "fl": "timestamp,original,statuscode",
        })
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"Wayback CDX 失败: {e}")
            return []
        rows = data if isinstance(data, list) else []
        # 带 fl 时首行是列名
        if rows and rows[0] and rows[0][0] == "timestamp":
            rows = rows[1:]
        results = []
        for r in rows[:n]:
            if len(r) < 3:
                continue
            ts, orig, status = r[0], r[1], r[2]
            # CDX 时间戳 YYYYMMDDHHMMSS → 标准 published_at（最早快照语义）
            published_at = ""
            try:
                published_at = datetime.strptime(ts, "%Y%m%d%H%M%S").isoformat(timespec="seconds")
            except ValueError:
                pass
            item = {
                "title": f"快照 {orig}（{ts[:8]}）",
                "url": f"https://web.archive.org/web/{ts}/{orig}",
                "snippet": f"Wayback 快照 {ts} · HTTP {status}"[:300],
                "source": "wayback_cdx",
                "score": 0.8,
            }
            if published_at:
                item["published_at"] = published_at
            results.append(item)
        return results
    return _engine


# ── USGS 地震引擎 ────────────────────────────────────────────────────────────

_USGS_EQ_API = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# 中文地点词 → (place 匹配正则, bbox 地理框或 None)
# USGS place 是英文；美国国内震 place 用「城市, 州缩写」格式（"6 km NW of Pinnacles, CA"）。
# bbox 可让「台湾地震」这类查询不依赖全球 top-50，直接框选该地区全部地震。
_USGS_PLACE_CN = {
    "日本": ("japan", dict(minlatitude=24, maxlatitude=47, minlongitude=122, maxlongitude=147)),
    "台湾": ("taiwan", dict(minlatitude=20, maxlatitude=26, minlongitude=119, maxlongitude=124)),
    "加州": (r"california|, ca\b", dict(minlatitude=32, maxlatitude=42, minlongitude=-125, maxlongitude=-114)),
    "中国": ("china", None), "香港": ("hong kong", None),
    "美国": (r", (ca|nv|ak|or|wa|hi|az|ut|mt|id|nm|tx|ok|ar|mo|ky|tn|sc|nc|ga)\b", None),
    "阿拉斯加": ("alaska", dict(minlatitude=48, maxlatitude=72, minlongitude=-170, maxlongitude=-130)),
    "夏威夷": ("hawaii", None),
    "印尼": ("indonesia", None), "菲律宾": ("philippines", None), "新西兰": ("new zealand", None),
    "智利": ("chile", None), "秘鲁": ("peru", None), "墨西哥": ("mexico", None), "土耳其": ("turkey", None),
    "伊朗": ("iran", None), "印度": ("india", None), "尼泊尔": ("nepal", None), "意大利": ("italy", None),
    "希腊": ("greece", None), "冰岛": ("iceland", None), "阿富汗": ("afghanistan", None), "缅甸": ("myanmar", None),
    "巴布亚": ("papua", None), "所罗门": ("solomon", None), "汤加": ("tonga", None), "瓦努阿图": ("vanuatu", None),
    "斐济": ("fiji", None), "关岛": ("guam", None), "千岛群岛": ("kuril", None),
}


def _build_usgs_engine(spec: dict[str, Any]) -> Any:
    """USGS 地震检索（earthquake.usgs.gov FDSNWS，免认证）。

    返回最近 30 天 M2.5+ 地震；查询含地点词（如「日本」「加州」）时客户端侧
    按 place 字段过滤，命中地点相关地震。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        # 提取地点词：去掉「地震/earthquake/震级」等噪声
        place_kw = re.sub(r"(?i)(地震|earthquake|seismic|震级|magnitude|quakes?|最近|最新)", " ", q)
        place_kw = re.sub(r"\s+", " ", place_kw).strip()
        params = {
            "format": "geojson", "limit": 50,
            "starttime": time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400)),
            "minmagnitude": "2.5",
        }
        if place_kw:
            kw = place_kw.lower()
            bbox = None
            # 中文地点词查映射表（正则 + 可选 bbox）；英文词直接按原文匹配 place
            if not re.search(r"[a-z]", kw):
                entry = _USGS_PLACE_CN.get(place_kw)
                if entry:
                    kw, bbox = entry
            if bbox:
                params.update(bbox)
                # bbox 精确框选时无需放大样本，直接按时间取
                params["limit"] = min(n * 3, 30)
            else:
                # 无 bbox 时按震级降序取样本，避免目标地点被全球密集地震挤出 top
                params["orderby"] = "magnitude"
        url = _USGS_EQ_API + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"USGS 失败: {e}")
            return []
        feats = data.get("features") or []
        if place_kw:
            if kw:
                feats = [f for f in feats if re.search(kw, str(f.get("properties", {}).get("place", "")).lower())]
        results = []
        for f in feats[:n]:
            p = f.get("properties", {})
            mag = p.get("mag")
            place = p.get("place", "")
            ts = (p.get("time") or 0) // 1000
            url_ = p.get("url", "")
            results.append({
                "title": f"M{mag} {place}",
                "url": url_ or "",
                "snippet": f"USGS 地震 · {ts and time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ts))}"[:300],
                "source": "usgs",
                "score": 0.85,
            })
        return results
    return _engine


# ── NASA CMR 地球科学数据目录引擎 ────────────────────────────────────────────

_CMR_API = "https://cmr.earthdata.nasa.gov/search/collections.json"


def _build_nasa_cmr_engine(spec: dict[str, Any]) -> Any:
    """NASA CMR 地球科学数据目录检索（cmr.earthdata.nasa.gov，免认证）。

    MODIS/卫星/遥感等关键词 → 数据集集合（含概念 ID，链接 Earthdata Search）。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = _ascii_tokens(query)
        if not q:
            return []
        to = _timeout or timeout
        url = _CMR_API + "?" + urllib.parse.urlencode({"keyword": q, "page_size": min(n, 10)})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"NASA CMR 失败: {e}")
            return []
        results = []
        for x in ((data.get("feed") or {}).get("entry") or [])[:n]:
            title = x.get("title") or q
            cid = x.get("id") or ""
            summary = (x.get("summary") or "")[:200]
            results.append({
                "title": title,
                "url": f"https://search.earthdata.nasa.gov/search/granules?p={cid}" if cid else "",
                "snippet": summary[:300],
                "source": "nasa_cmr",
                "score": 0.8,
            })
        return results
    return _engine


# ── IMDb 影视 suggestion API（免认证） ────────────────────────────────────────

_IMDB_SUGGEST = "https://v2.sg.media-imdb.com/suggestion/{first}/{q}.json"
_IMDB_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


# 影视问句噪声词：整句进 suggestion 常 0 命中（如 "Interstellar film cast"）
_IMDB_STOP = re.compile(
    r"(?i)\b(movie|film|tv\s*show|tv\s*series|cinema|cast|starring|director|directed\s+by|"
    r"screenwriter|written\s+by|actor|actress|producer|box\s*office|trailer|review|"
    r"imdb|豆瓣|电影|电视剧|剧集|影视|影片|导演|主演|演员|编剧|制片|上映|首映|票房|"
    r"影评|奥斯卡|金像奖|金马奖|网剧|美剧|韩剧|日剧|英剧)\b"
)


def _imdb_clean_query(query: str) -> str:
    """去掉影视角色/事实词，保留片名/人名主体。"""
    q = (query or "").strip()
    if not q:
        return ""
    cleaned = _IMDB_STOP.sub(" ", q)
    cleaned = re.sub(r"[?？!！,，.。:：;；]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # 清洗后过短则回退原 query（避免只剩空）
    if len(cleaned) < 2:
        return q
    return cleaned


def _build_imdb_engine(spec: dict[str, Any]) -> Any:
    """IMDb 自动补全 suggestion API（v2.sg.media-imdb.com，免认证）。

    路径：/suggestion/{首字符}/{query}.json
    返回电影/剧集/人物，带 tt/nm ID，可拼正式 IMDb URL。
    """
    timeout = spec.get("timeout", 8)
    source_name = spec.get("_name", "imdb")

    def _fetch_suggest(q: str, to: float) -> list[dict[str, Any]]:
        if not q:
            return []
        first = q[0].lower()
        if not (("a" <= first <= "z") or ("0" <= first <= "9")):
            first = "_"
        path_q = urllib.parse.quote(q[:80], safe="")
        url = _IMDB_SUGGEST.format(first=first, q=path_q)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _IMDB_UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"IMDb 失败: {e}")
            return []
        return [x for x in (data.get("d") or []) if isinstance(x, dict)]

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        raw = (query or "").strip()
        if not raw:
            return []
        to = _timeout or timeout
        cleaned = _imdb_clean_query(raw)
        # 先试清洗词，空则回退原句
        items = _fetch_suggest(cleaned, to)
        if not items and cleaned != raw:
            items = _fetch_suggest(raw, to)

        # 按标题与查询 token 重叠 + feature 优先排序
        q_tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", cleaned or raw)}
        ranked: list[tuple[float, dict[str, Any]]] = []
        for item in items:
            title = item.get("l") or ""
            iid = item.get("id") or ""
            if not title and not iid:
                continue
            t_low = title.lower()
            t_tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", title)}
            overlap = len(q_tokens & t_tokens) / max(len(q_tokens), 1)
            # 精确/前缀加分；tt 影片优先于 nm 人名（片名查询时）
            exact = 1.0 if t_low == (cleaned or raw).lower() else 0.0
            prefix = 0.3 if t_low.startswith((cleaned or raw).lower()[: min(12, len(cleaned or raw))]) else 0.0
            kind_bonus = 0.15 if str(iid).startswith("tt") else (0.05 if str(iid).startswith("nm") else 0.0)
            year = item.get("y") or 0
            try:
                year_n = int(year) if year else 0
            except (TypeError, ValueError):
                year_n = 0
            # 近年作品轻微加分（同名旧片靠后）
            recency = min(max(year_n - 1980, 0), 50) / 500.0
            score = 0.5 + overlap + exact + prefix + kind_bonus + recency
            ranked.append((score, item))
        ranked.sort(key=lambda x: -x[0])

        results = []
        for score, item in ranked[: max(n, 1)]:
            title = item.get("l") or ""
            iid = item.get("id") or ""
            year = item.get("y")
            kind = item.get("q") or item.get("qid") or ""
            cast = item.get("s") or ""
            parts = [p for p in (str(year) if year else "", kind, cast) if p]
            if str(iid).startswith("tt"):
                href = f"https://www.imdb.com/title/{iid}/"
            elif str(iid).startswith("nm"):
                href = f"https://www.imdb.com/name/{iid}/"
            else:
                href = f"https://www.imdb.com/find/?q={urllib.parse.quote(title)}"
            results.append({
                "title": title,
                "url": href,
                "snippet": " · ".join(parts)[:300],
                "source": source_name,
                "score": min(0.95, 0.7 + score * 0.15),
            })
            if len(results) >= n:
                break
        return results
    return _engine


# ── iTunes 媒体库（中文需 country=cn；专辑问 entity=album）────────────────────

def _build_itunes_engine(spec: dict[str, Any]) -> Any:
    """iTunes Search API：按语言/意图调 country 与 entity，避免中文专辑落到脏曲目。"""
    timeout = spec.get("timeout", 8)
    source_name = spec.get("_name", "itunes")
    base = spec.get("url") or "https://itunes.apple.com/search"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        to = _timeout or timeout
        want_album = bool(re.search(r"(?i)专辑|album|discography|唱片", q))
        # 去掉意图词，保留艺人名
        term = re.sub(
            r"(?i)(专辑|新专辑|音乐|歌曲|单曲|album|music|song|songs|single|discography|podcast)\b",
            " ", q,
        )
        term = re.sub(r"\s+", " ", term).strip() or q
        has_zh = bool(re.search(r"[\u4e00-\u9fff]", term))
        params: dict[str, str] = {
            "term": term,
            "media": "music",
            "limit": str(min(max(n, 1), 25)),
        }
        if want_album:
            params["entity"] = "album"
        if has_zh:
            params["country"] = "cn"
            params["lang"] = "zh_cn"
        url = f"{base}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "argo-search/2.6 (itunes)", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"iTunes 失败: {e}")
            return []
        items = data.get("results") or []
        # 相关度：艺人名/专辑名与 term 重叠
        t_keys = {t.lower() for t in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", term)}
        out: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if want_album:
                title = it.get("collectionName") or it.get("trackName") or ""
                href = it.get("collectionViewUrl") or it.get("trackViewUrl") or ""
            else:
                title = it.get("trackName") or it.get("collectionName") or ""
                href = it.get("trackViewUrl") or it.get("collectionViewUrl") or ""
            artist = it.get("artistName") or ""
            if not title:
                continue
            blob = f"{title} {artist}".lower()
            if t_keys and not any(k in blob for k in t_keys):
                continue
            out.append({
                "title": title,
                "url": href,
                "snippet": artist[:300],
                "source": source_name,
                "score": 0.88 if want_album else 0.85,
            })
            if len(out) >= n:
                break
        return out
    return _engine


# ── TheSportsDB 体育（免认证 test key=3） ────────────────────────────────────

_SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"
_SPORTSDB_UA = "argo-search/2.6 (unified-search@local)"

# 高频中文球员/球队 → TheSportsDB 英文检索名（公开 API 基本只认拉丁文）
_SPORTS_ZH_ALIASES: dict[str, str] = {
    "梅西": "Messi",
    "C罗": "Ronaldo",
    "C羅": "Ronaldo",
    "罗纳尔多": "Ronaldo",
    "Cristiano": "Ronaldo",
    "内马尔": "Neymar",
    "姆巴佩": "Mbappe",
    "哈兰德": "Haaland",
    # 单姓 Curry 会命中 Eddy Curry；中文「库里」默认指 Stephen Curry
    "库里": "Stephen Curry",
    "斯蒂芬库里": "Stephen Curry",
    "史蒂芬库里": "Stephen Curry",
    "詹姆斯": "LeBron James",
    "勒布朗": "LeBron",
    "杜兰特": "Durant",
    "科比": "Kobe",
    "乔丹": "Jordan",
    "姚明": "Yao Ming",
    "易建联": "Yi Jianlian",
    "皇马": "Real Madrid",
    "巴萨": "Barcelona",
    "曼联": "Manchester United",
    "曼城": "Manchester City",
    "湖人": "Lakers",
    "勇士": "Warriors",
    "凯尔特人": "Celtics",
}


def _sports_expand_aliases(query: str) -> list[str]:
    """中文别名展开为英文检索变体。"""
    out: list[str] = []
    for zh, en in _SPORTS_ZH_ALIASES.items():
        if zh in query:
            if en not in out:
                out.append(en)
    return out


def _build_thesportsdb_engine(spec: dict[str, Any]) -> Any:
    """TheSportsDB 球员/球队/赛事搜索（api key=3 公开测试钥，免认证）。

    并行查 searchplayers / searchteams / searchevents，合并去重。
    """
    timeout = spec.get("timeout", 10)
    source_name = spec.get("_name", "thesportsdb")

    def _fetch(path: str, params: dict[str, str], to: float) -> Any:
        url = f"{_SPORTSDB_BASE}/{path}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"User-Agent": _SPORTSDB_UA, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=to) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        to = _timeout or timeout
        # 多词查询（如 "NBA finals MVP 2024"）整句常 0 命中 → 短语 + token 变体
        stop = {
            "finals", "final", "mvp", "vs", "the", "and", "of", "in", "for", "a", "an",
            "winner", "winners", "who", "won", "what", "which", "year", "years",
            "team", "club", "player", "coach", "manager", "roster", "squad",
            "决赛", "半决赛", "总冠军", "联赛", "球员", "俱乐部", "进球", "冠军",
            "球队", "球星", "效力", "夺冠", "卫冕", "助攻", "得分王",
        }
        tokens = re.findall(r"[A-Za-z\u4e00-\u9fff]{2,}", q)
        # 纯数字年号单独收集，用于 event 年份对齐，不当主检索词
        # 注意：必须整年捕获；r"(19|20)\d{2}" 的 findall 只会返回 "20"，
        # 导致 "20" in "2016-11-24" 恒真、年份过滤失效。
        years = re.findall(r"\b((?:19|20)\d{2})\b", q)
        meaningful = [t for t in tokens if t.lower() not in stop and not re.fullmatch(r"\d{4}", t)]

        variants: list[str] = []
        # 中文别名优先（梅西→Messi）
        for alias in _sports_expand_aliases(q):
            if alias not in variants:
                variants.append(alias)
        # 双词短语优先（World Cup / LeBron James）
        for i in range(len(meaningful) - 1):
            phrase = f"{meaningful[i]} {meaningful[i + 1]}"
            if phrase not in variants:
                variants.append(phrase)
        # 整句（短查询，非纯中文）
        if len(q) <= 40 and q not in variants and re.search(r"[A-Za-z]", q):
            variants.insert(0, q)
        for t in meaningful:
            # 纯中文 token 对英文 API 无效，跳过（已由别名覆盖）
            if re.fullmatch(r"[\u4e00-\u9fff]+", t):
                continue
            if t not in variants:
                variants.append(t)
            if len(variants) >= 5:
                break
        if not variants:
            variants = [q]

        # 意图：club/team/球队 → 优先 team/player；event 词 → 保留 event
        q_low = q.lower()
        prefer_entity = bool(re.search(
            r"(?i)(team|club|player|coach|球队|球星|俱乐部|效力|球员|roster|squad)", q,
        ))
        prefer_event = bool(re.search(
            r"(?i)(finals?|mvp|cup|championship|winner|won|决赛|总冠军|世界杯|欧冠|季后赛)", q,
        )) and not prefer_entity

        bucket: list[dict[str, Any]] = []
        seen: set[str] = set()

        endpoints = (
            ("searchplayers.php", "p", "player", "strPlayer", "idPlayer",
             lambda x: " · ".join(p for p in (
                 x.get("strTeam") or "",
                 x.get("strSport") or "",
                 x.get("strPosition") or "",
                 x.get("strNationality") or "",
             ) if p)),
            ("searchteams.php", "t", "team", "strTeam", "idTeam",
             lambda x: " · ".join(p for p in (
                 x.get("strSport") or "",
                 x.get("strLeague") or "",
                 x.get("strCountry") or "",
             ) if p)),
            ("searchevents.php", "e", "event", "strEvent", "idEvent",
             lambda x: " · ".join(p for p in (
                 x.get("strLeague") or "",
                 x.get("strSport") or "",
                 x.get("dateEvent") or x.get("strTimestamp") or "",
             ) if p)),
        )
        list_keys = {
            "searchplayers.php": "player",
            "searchteams.php": "teams",
            "searchevents.php": "event",
        }
        url_tpl = {
            "player": "https://www.thesportsdb.com/player/{id}",
            "team": "https://www.thesportsdb.com/team/{id}",
            "event": "https://www.thesportsdb.com/event/{id}",
        }
        score_of = {"player": 0.86, "team": 0.84, "event": 0.82}
        # 实体问优先 player/team；赛事问放宽 event
        if prefer_entity:
            endpoints = endpoints[:2] + endpoints[2:]  # 顺序已是 p,t,e
        elif prefer_event:
            endpoints = (endpoints[2], endpoints[0], endpoints[1])

        q_token_set = {t.lower() for t in meaningful}

        for v in variants:
            for path, param_k, kind, title_k, id_k, snip_fn in endpoints:
                try:
                    data = _fetch(path, {param_k: v}, to)
                except Exception as e:
                    logger.warning(f"TheSportsDB {path} 失败: {e}")
                    continue
                items = data.get(list_keys[path]) if isinstance(data, dict) else None
                if not items:
                    continue
                for x in items[: max(n, 5)]:
                    if not isinstance(x, dict):
                        continue
                    title = x.get(title_k) or ""
                    iid = str(x.get(id_k) or "")
                    key = f"{kind}:{iid or title}"
                    if not title or key in seen:
                        continue
                    # 相关度：标题/摘要 token 与查询重叠；event 尤其严格
                    snip = snip_fn(x) or ""
                    sport = str(x.get("strSport") or "")
                    league = str(x.get("strLeague") or "")
                    blob = f"{title} {snip} {sport} {league}".lower()
                    t_tokens = set(re.findall(r"[a-z0-9\u4e00-\u9fff]{2,}", blob))
                    overlap = len(q_token_set & t_tokens) if q_token_set else 1
                    # 「世界杯/World Cup」默认足球；公开 API 常塞高尔夫/篮网等噪声 → 非足球一律丢
                    world_cup_q = bool(re.search(r"(?i)world\s*cup|世界杯|fifa", q))
                    if world_cup_q:
                        if not re.search(r"(?i)soccer|football|fifa", f"{title} {sport} {league} {snip}"):
                            continue
                    if kind == "event":
                        # 无实质重叠 → 丢弃噪声 event（如 World Cup → World Sand Greens）
                        if q_token_set and overlap == 0:
                            continue
                        # 有年份且 API 给了日期：年份不对直接丢（避免 2022 问到 2016 Golf）
                        date_s = str(x.get("dateEvent") or x.get("strTimestamp") or "")
                        if years and date_s and not any(y in date_s for y in years):
                            continue
                        # 双词短语（如 World Cup）若在标题中，要求更高相关；单 token 过宽
                        phrase_hit = any(
                            len(p.split()) >= 2 and p.lower() in title.lower()
                            for p in variants[:3]
                        )
                        if q_token_set and overlap < 2 and not phrase_hit:
                            continue
                        rel = 0.35 + 0.15 * overlap + (0.2 if phrase_hit else 0)
                        if years and date_s and any(y in date_s for y in years):
                            rel += 0.2
                        if world_cup_q and re.search(r"(?i)soccer|football|fifa", blob):
                            rel += 0.25
                    else:
                        # 赛事问勿用弱 token 沾上球员（Metta World Peace）
                        if prefer_event and overlap < 2 and v.lower() not in title.lower():
                            continue
                        if world_cup_q and kind == "player" and not re.search(
                            r"(?i)soccer|football", f"{sport} {snip}",
                        ):
                            continue
                        # player/team：变体本身已是名，重叠可放宽
                        rel = 0.35 + 0.25 * overlap
                        if v.lower() in title.lower():
                            rel += 0.25
                        # 全名变体（Stephen Curry）精确命中加权
                        if " " in v and v.lower() == title.lower():
                            rel += 0.3
                    seen.add(key)
                    href = url_tpl[kind].format(id=iid) if iid else ""
                    base = score_of.get(kind, 0.8)
                    bucket.append({
                        "title": f"[{kind}] {title}",
                        "url": href,
                        "snippet": (snip_fn(x) or kind)[:300],
                        "source": source_name,
                        "score": min(0.95, base * 0.5 + rel),
                        "_kind": kind,
                        "_rel": rel,
                    })
            # 已有高相关实体则早停
            if sum(1 for r in bucket if r.get("_rel", 0) >= 0.5) >= n:
                break

        kind_rank = {"player": 0, "team": 1, "event": 2}
        if prefer_entity:
            kind_rank = {"player": 0, "team": 0, "event": 3}
        elif prefer_event:
            kind_rank = {"event": 0, "player": 1, "team": 2}

        bucket.sort(key=lambda r: (
            kind_rank.get(r.get("_kind", ""), 9),
            -float(r.get("_rel", 0)),
            -float(r.get("score", 0)),
        ))
        out = []
        for r in bucket[:n]:
            r.pop("_kind", None)
            r.pop("_rel", None)
            out.append(r)
        return out
    return _engine


# ── GDELT 全球事件数据库 ─────────────────────────────────────────────────────

def _build_gdelt_engine(spec: dict[str, Any]) -> Any:
    """GDELT 全球新闻事件数据库（api.gdeltproject.org/api/v2/doc/doc，免认证）。

    全球事件/舆情查询：返回事件描述、来源、时间、国家标签。中文查询走
    GEO JSON 全词搜索，英文走 phrase 搜索。独特价值：全球事件图谱，
    覆盖 argo 现有新闻引擎（财联社/东财/Google News）的地理与事件维度。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", query))
        mode = "geojson" if has_cjk else "phraselist"
        url = (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={urllib.parse.quote(query)}&mode={mode}"
            "&format=json&maxrecords=25&sort=hybridrel"
            "&timespan=30d"
        )
        headers = {"User-Agent": "argo-search/2.6 (unified-search@local)"}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []
        articles = data.get("articles") or []
        results: list[dict[str, Any]] = []
        for a in articles[:n]:
            title = a.get("title") or ""
            if not title:
                continue
            src = a.get("sourcecountry") or a.get("domain") or "gdelt"
            url_a = a.get("url") or ""
            seendate = a.get("seendate") or ""
            date_s = f"{seendate[:4]}-{seendate[4:6]}-{seendate[6:8]}" if len(seendate) >= 8 else ""
            results.append({
                "title": title[:200],
                "url": url_a,
                "snippet": (a.get("seentext") or "")[:300],
                "source": "gdelt",
                "score": 0.8,
                "published": date_s,
                "country": src,
                "language": a.get("lang") or "",
                "domain_label": "事件",
            })
        return results
    return _engine


# ── OpenCorporates 全球公司注册 ──────────────────────────────────────────────

def _build_opencorporates_engine(spec: dict[str, Any]) -> Any:
    """OpenCorporates 全球公司注册（api.opencorporates.com/v0.4/companies/search，免认证）。

    公司尽调/反欺诈：公司名、注册号、司法辖区、成立日期、状态。
    argo 现有引擎无公司注册数据源，这是尽调类查询的核心空白。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = query.strip()
        # 去常见修饰语，取核心公司名
        q2 = re.sub(r"(?i)\b(company|corporation|inc|limited|ltd|llc|公司|集团|有限|注册|查一下|查询)\b", " ", q)
        q2 = re.sub(r"\s+", " ", q2).strip() or q
        url = (
            "https://api.opencorporates.com/v0.4/companies/search"
            f"?q={urllib.parse.quote(q2)}&per_page={min(n, 10)}"
        )
        headers = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []
        companies = ((data.get("results") or {}).get("companies")) or []
        results: list[dict[str, Any]] = []
        for c in companies[:n]:
            comp = c.get("company") or {}
            name = comp.get("name") or ""
            if not name:
                continue
            num = comp.get("company_number") or ""
            juris = comp.get("jurisdiction_code") or ""
            inc_date = (comp.get("incorporation_date") or "")[:10]
            status = comp.get("current_status") or ""
            cc = (comp.get("registered_address") or {}).get("country") or ""
            results.append({
                "title": name[:200],
                "url": comp.get("opencorporates_url") or f"https://opencorporates.com/companies/{juris}/{num}",
                "snippet": (
                    f"{juris}{'/'+num if num else ''}"
                    f"{' · 成立 '+inc_date if inc_date else ''}"
                    f"{' · 状态 '+status if status else ''}"
                    f"{' · '+cc if cc else ''}"
                )[:300],
                "source": "opencorporates",
                "score": 0.8,
                "company_number": num,
                "jurisdiction": juris,
                "incorporation_date": inc_date,
                "status": status,
            })
        return results
    return _engine


# ── Google Patents 专利搜索 ──────────────────────────────────────────────────

def _build_google_patents_engine(spec: dict[str, Any]) -> Any:
    """Google Patents 专利搜索（patents.google.com/xhr/query，免认证）。

    技术尽调/竞品分析：专利标题、公开号、公开日期、申请人。
    用 patents.google.com 的公开 XHR 接口，免 key、返回 JSON。
    argo 现有引擎无专利数据源，这是技术尽调的核心空白。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = query.strip()
        # 去常见修饰语
        q2 = re.sub(r"(?i)\b(patent|专利|查询|搜索|检索)\b", " ", q)
        q2 = re.sub(r"\s+", " ", q2).strip() or q
        url = "https://patents.google.com/xhr/query"
        params = {
            "url": f"q={urllib.parse.quote(q2)}",
            "exp": "",
        }
        full_url = url + "?" + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "application/json",
        }
        try:
            with urllib.request.urlopen(urllib.request.Request(full_url, headers=headers), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []
        results: list[dict[str, Any]] = []
        try:
            cluster = (data.get("results") or {}).get("cluster") or []
            for c in cluster:
                for r in (c.get("result") or []):
                    if len(results) >= n:
                        break
                    pat = r.get("patent") or {}
                    title = pat.get("title") or ""
                    if not title:
                        continue
                    pub_date = pat.get("publication_date") or ""
                    pub_num = pat.get("publication_number") or ""
                    assignee = (pat.get("assignee") or [{}])[0].get("name") if isinstance(pat.get("assignee"), list) else ""
                    pub_id = pat.get("publication_number") or pub_num or ""
                    results.append({
                        "title": title[:200],
                        "url": f"https://patents.google.com/patent/{pub_id}" if pub_id else "",
                        "snippet": (r.get("snippet") or "")[:300],
                        "source": "google_patents",
                        "score": 0.8,
                        "publication_date": pub_date,
                        "publication_number": pub_num,
                        "assignee": assignee,
                    })
        except Exception:
            pass
        return results
    return _engine

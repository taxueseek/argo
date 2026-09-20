#!/usr/bin/env python3
"""多语言与开放数据专用构建器：日/韩/欧/全球开放源。

覆盖：
  cnii            CiNii Research（日本国立情报学研究所学术总库，JSON-LD）
  ndl             NDL Search（国立国会图书馆书目，OpenSearch RSS）
  kor_law         law.go.kr 韩国法令/判例（官方公开 demo 账号 OC=test，韩文 XML）
  hatena_bookmark Hatena Bookmark（日本技术圈书签，RDF RSS）
  dnb             DNB 德国国家图书馆（SRU/RDF XML）
  doaj            DOAJ 开放获取期刊（JSON，bibjson 嵌套）
  europeana       Europeana 欧洲文化遗产（JSON，title 为数组）
  hal             HAL 法国科研开放仓储（Solr JSON，字段为数组）
  eu_opendata     EU Open Data Portal（JSON，title 嵌套 label）
  open_meteo      全球天气（geocoding + forecast，免认证）
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from engines_base import safe_search, _http_get_raw, http_open, rank_score

logger = logging.getLogger("unified_search.engines")

_UA = "argo-search/2.4 (unified-search@local)"

# 结构字符保留集：既有 %XX 编码、协议/路径/参数分隔符不重复转义
_URL_SAFE = "/:?&=%,._~#+-"


def _encode_url(url: str) -> str:
    """URL 非 ASCII 安全编码：中文查询（如 open_meteo geocode name=北京）
    直接拼接会让 urllib 报 ascii 编码错误；已有 %XX 与结构字符不动。"""
    return urllib.parse.quote(url, safe=_URL_SAFE)


# 地理编码类查询的自然语言噪声：open_meteo 的 geocoding 只吃地名，
# 「今日北京天气」「北京今天多少度」这类完整问句会让 name= 带上时间词与
# 疑问词，上游 0 候选 → 引擎静默返回空（wx.py / weather_cn 各有一份同型
# 正则，漏掉任一处该查询在对应源就全军覆没）。
# 只剥「查询表达」不碰地名本身：坐标串（lat,lon）、纯地名原样通过。
_GEO_COORD_RE = re.compile(r"-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?")
_GEO_NOISE_RE = re.compile(
    r"(天气|气温|温度|多少度|预报|怎么样|如何|下雨|降水|雪|晴|阴|"
    r"今[天日晚]|明[天日晚]|后[天日]|昨[天日]|现在|实时|当前|查询|"
    r"的|了|呢|吗|\?|？|，|,|。|\s)+",
)


def _geo_core(query: str) -> str:
    """剥掉天气问句的时间/疑问词，留地名；空则回退原查询（不猜）。"""
    raw = (query or "").strip()
    # 坐标串（lat,lon）不是问句，原样透传——剥了逗号反而毁掉它
    if _GEO_COORD_RE.fullmatch(raw):
        return raw
    q = _GEO_NOISE_RE.sub(" ", raw).strip()
    return q or raw


def _http_json(url: str, timeout: float, engine: str = "") -> Any:
    req = urllib.request.Request(_encode_url(url), headers={"User-Agent": _UA, "Accept-Encoding": "gzip"})
    with http_open(req, timeout=timeout, engine=engine) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace"))


def _http_text(url: str, timeout: float, engine: str = "") -> str:
    req = urllib.request.Request(_encode_url(url), headers={"User-Agent": _UA})
    with http_open(req, timeout=timeout, engine=engine) as resp:
        return resp.read().decode("utf-8", "replace")


# ── CiNii Research ────────────────────────────────────────────────────────────

def _build_cnii_engine(spec: dict[str, Any]) -> Any:
    """CiNii Research 学术总库（cir.nii.ac.jp，OpenSearch JSON-LD，免认证）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = f"https://cir.nii.ac.jp/opensearch/all?q={urllib.parse.quote(query)}&format=json&count={min(n, 10)}"
        try:
            data = _http_json(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"CiNii 失败: {e}")
            return []
        results = []
        for _rk, it in enumerate((data.get("items") or [])[:n]):
            if not isinstance(it, dict):
                continue
            title = it.get("title", "")
            link = it.get("link")
            url_ = link.get("@id", "") if isinstance(link, dict) else str(link or "")
            creators = it.get("dc:creator") or []
            if isinstance(creators, str):
                creators = [creators]
            year = it.get("prism:publicationDate", "")
            parts = [p for p in (", ".join(str(c) for c in creators[:3]), str(year)) if p]
            if not title and not url_:
                continue
            results.append({
                "title": str(title)[:200],
                "url": url_,
                "snippet": (" · ".join(parts) + " · " + str(it.get("description", "")))[:300],
                "source": "cnii",
                "score": rank_score(0.7, _rk),
            })
        return results
    return _engine


# ── NDL Search（OpenSearch RSS）───────────────────────────────────────────────

def _build_ndl_engine(spec: dict[str, Any]) -> Any:
    """NDL Search 国立国会图书馆书目（ndlsearch.ndl.go.jp OpenSearch RSS）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = f"https://ndlsearch.ndl.go.jp/api/opensearch?title={urllib.parse.quote(query)}&cnt={min(n, 10)}"
        try:
            raw = _http_text(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"NDL 失败: {e}")
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            logger.warning(f"NDL XML 解析失败: {e}")
            return []
        results = []
        RSS = "http://purl.org/rss/1.0/"
        DC = "http://purl.org/dc/elements/1.1/"
        # NDL 的 <item> 无默认 namespace（rss 根只声明了带前缀的 xmlns）
        for _rk1, item in enumerate(root.iter("item")):
            def _txt(ns: str, tag: str) -> str:
                el = item.find(f"{{{ns}}}{tag}") if ns else item.find(tag)
                return el.text.strip() if el is not None and el.text else ""
            title = _txt("", "title")
            link = _txt("", "link")
            description = _txt(DC, "description")
            if not title and not link:
                continue
            results.append({
                "title": title[:200],
                "url": link,
                "snippet": description[:300],
                "source": "ndl",
                "score": rank_score(0.7, _rk1),
            })
            if len(results) >= n:
                break
        return results
    return _engine


# ── law.go.kr 韩国法令/判例 ───────────────────────────────────────────────────

def _build_kor_law_engine(spec: dict[str, Any]) -> Any:
    """law.go.kr 国家法令信息中心（官方公开 demo 账号 OC=test，韩文 XML）。

    target=prec 判例（总库最扎实，实测「계약」3004 条）；查询词命中判例要点。
    """
    timeout = spec.get("timeout", 12)
    target = spec.get("extra_params", {}).get("target", "prec")

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = (f"https://law.go.kr/DRF/lawSearch.do?OC=test&target={target}&type=XML"
               f"&query={urllib.parse.quote(query)}&display={min(n, 10)}")
        try:
            raw = _http_text(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"law.go.kr 失败: {e}")
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            logger.warning(f"law.go.kr XML 解析失败: {e}")
            return []
        # 判例条目标签 <prec>，法令条目 <law>
        results = []
        for tag in ("prec", "law"):
            for item in root.iter(tag):
                def _field(name: str) -> str:
                    el = item.find(name)
                    return el.text.strip() if el is not None and el.text else ""
                title = _field("사건명") or _field("법령명한글")
                link = _field("판례상세링크") or _field("법령상세링크")
                if link and link.startswith("/"):
                    link = "https://law.go.kr" + link
                meta = " · ".join(p for p in (
                    _field("사건번호") or _field("법령일련번호"),
                    _field("선고일자"),
                    _field("사건종류명"),
                ) if p)
                if not title and not link:
                    continue
                results.append({
                    "title": title[:200],
                    "url": link,
                    "snippet": meta[:300],
                    "source": "kor_law",
                    "score": rank_score(0.7, len(results)),
                })
                if len(results) >= n:
                    return results
        return results
    return _engine


# ── Hatena Bookmark（RDF RSS）─────────────────────────────────────────────────

def _build_hatena_bookmark_engine(spec: dict[str, Any]) -> Any:
    """Hatena Bookmark 书签搜索（b.hatena.ne.jp RDF RSS，带收藏热度）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = f"https://b.hatena.ne.jp/search/text?q={urllib.parse.quote(query)}&mode=rss"
        try:
            raw = _http_text(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Hatena 失败: {e}")
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            logger.warning(f"Hatena XML 解析失败: {e}")
            return []
        results = []
        RSS = "http://purl.org/rss/1.0/"
        DC = "http://purl.org/dc/elements/1.1/"
        for _rk3, item in enumerate(root.iter(f"{{{RSS}}}item")):
            def _txt(ns: str, tag: str) -> str:
                el = item.find(f"{{{ns}}}{tag}")
                return el.text.strip() if el is not None and el.text else ""
            title = _txt(RSS, "title")
            link = _txt(RSS, "link")
            date = _txt(DC, "date")
            if not title and not link:
                continue
            results.append({
                "title": title[:200],
                "url": link,
                "snippet": (f"bookmarked: {date[:10]}" if date else "")[:300],
                "source": "hatena_bookmark",
                "score": rank_score(0.7, _rk3),
            })
            if len(results) >= n:
                break
        return results
    return _engine


# ── DNB 德国国家图书馆（SRU）─────────────────────────────────────────────────

def _build_dnb_engine(spec: dict[str, Any]) -> Any:
    """DNB 德国国家图书馆书目（services.dnb.de SRU，RDF XML 记录）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = ("https://services.dnb.de/sru/dnb?operation=searchRetrieve&version=1.1"
               f"&query={urllib.parse.quote(query)}&maximumRecords={min(n, 10)}")
        try:
            raw = _http_text(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"DNB 失败: {e}")
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            logger.warning(f"DNB XML 解析失败: {e}")
            return []
        results = []
        SRW = "http://www.loc.gov/zing/srw/"
        for _rk4, rec in enumerate(root.iter(f"{{{SRW}}}record")):
            title = ""
            link = ""
            # 记录内 RDF/DC 元数据：取 dc:title / dc:identifier 或链接
            for el in rec.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if not el.text or not el.text.strip():
                    continue
                if tag in ("title", "Title") and not title:
                    title = el.text.strip()
                elif tag in ("identifier", "Identifier") and (
                        "d-nb.info" in el.text or "dnb.de" in el.text):
                    link = el.text.strip()
            if not title:
                continue
            results.append({
                "title": title[:200],
                "url": link or f"https://portal.dnb.de/opac/simpleSearch?query={urllib.parse.quote(title)}",
                "snippet": "",
                "source": "dnb",
                "score": rank_score(0.7, _rk4),
            })
            if len(results) >= n:
                break
        return results
    return _engine


# ── DOAJ 开放获取期刊 ─────────────────────────────────────────────────────────

def _build_doaj_engine(spec: dict[str, Any]) -> Any:
    """DOAJ 开放获取期刊全球总库（doaj.org/api，bibjson 嵌套结构）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = f"https://doaj.org/api/search/articles/{urllib.parse.quote(query)}?pageSize={min(n, 10)}"
        try:
            data = _http_json(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"DOAJ 失败: {e}")
            return []
        results = []
        for _rk5, r in enumerate((data.get("results") or [])[:n]):
            bib = r.get("bibjson") or {}
            title = bib.get("title", "")
            # link 是 [{url, type}] 数组，取 fulltext/doi 优先
            link = ""
            for lk in (bib.get("link") or []):
                if isinstance(lk, dict):
                    u = lk.get("url", "")
                    if u and (lk.get("type") in ("fulltext", "doi") or not link):
                        link = u
            authors = bib.get("author") or []
            if isinstance(authors, list):
                names = [a.get("name", "") for a in authors[:3] if isinstance(a, dict)]
            else:
                names = []
            year = bib.get("year", "")
            parts = [p for p in (", ".join(names), str(year), bib.get("journal", {}).get("title", "") if isinstance(bib.get("journal"), dict) else "") if p]
            if not title and not link:
                continue
            results.append({
                "title": str(title)[:200],
                "url": link,
                "snippet": (" · ".join(parts) + " · " + str(bib.get("abstract", "")))[:300],
                "source": "doaj",
                "score": rank_score(0.7, _rk5),
            })
        return results
    return _engine


# ── Europeana 欧洲文化遗产 ────────────────────────────────────────────────────

def _build_europeana_engine(spec: dict[str, Any]) -> Any:
    """Europeana 欧洲文化遗产聚合（api.europeana.eu，官方公开 demo key api2demo）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = (f"https://api.europeana.eu/record/v2/search.json?wskey=api2demo"
               f"&query={urllib.parse.quote(query)}&rows={min(n, 10)}")
        try:
            data = _http_json(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Europeana 失败: {e}")
            return []
        results = []
        for _rk6, it in enumerate((data.get("items") or [])[:n]):
            if not isinstance(it, dict):
                continue
            title = it.get("title")
            if isinstance(title, list):
                title = title[0] if title else ""
            link = it.get("link", "")
            provider = it.get("dataProvider")
            if isinstance(provider, list):
                provider = provider[0] if provider else ""
            if not title and not link:
                continue
            results.append({
                "title": str(title)[:200],
                "url": link,
                "snippet": (f"provider: {provider}" if provider else "")[:300],
                "source": "europeana",
                "score": rank_score(0.7, _rk6),
            })
        return results
    return _engine


# ── HAL 法国科研开放仓储 ──────────────────────────────────────────────────────

def _build_hal_engine(spec: dict[str, Any]) -> Any:
    """HAL 法国全国科研机构开放仓储（api.archives-ouvertes.fr Solr JSON）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = ("https://api.archives-ouvertes.fr/search/?wt=json"
               f"&q={urllib.parse.quote(query)}&rows={min(n, 10)}"
               "&fl=title_s,uri_s,abstract_s,authFullName_s,producedDate_s")
        try:
            data = _http_json(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"HAL 失败: {e}")
            return []
        results = []
        for _rk7, doc in enumerate((data.get("response", {}).get("docs") or [])[:n]):
            title = doc.get("title_s")
            if isinstance(title, list):
                title = title[0] if title else ""
            link = doc.get("uri_s", "")
            authors = doc.get("authFullName_s") or []
            date = doc.get("producedDate_s") or ""
            parts = [p for p in (", ".join(str(a) for a in authors[:3]), str(date)) if p]
            if not title and not link:
                continue
            results.append({
                "title": str(title)[:200],
                "url": link,
                "snippet": (" · ".join(parts) + " · " + str(doc.get("abstract_s", "")))[:300],
                "source": "hal",
                "score": rank_score(0.7, _rk7),
            })
        return results
    return _engine


# ── EU Open Data Portal ───────────────────────────────────────────────────────

def _build_eu_opendata_engine(spec: dict[str, Any]) -> Any:
    """EU Open Data Portal（data.europa.eu/api/hub，24 语言元数据）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = f"https://data.europa.eu/api/hub/search/search?q={urllib.parse.quote(query)}&pageSize={min(n, 10)}"
        try:
            data = _http_json(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"EU ODP 失败: {e}")
            return []
        results = []
        for _rk8, it in enumerate((data.get("result", {}).get("results") or [])[:n]):
            if not isinstance(it, dict):
                continue
            # title 是 {语言code: 标题} 映射；优先 en/zh，否则取首个
            title = ""
            tl = it.get("title") or {}
            if isinstance(tl, dict):
                for lang in ("en", "zh", "fr", "de"):
                    if tl.get(lang):
                        title = tl[lang]
                        break
                if not title and tl:
                    title = next(iter(tl.values()))
            url_ = (it.get("landing_page") or it.get("resource")
                    or it.get("id") or "")
            if isinstance(url_, list):
                # landing_page 是 [{resource: url}] 数组
                url_ = url_[0].get("resource", "") if url_ and isinstance(url_[0], dict) else ""
            if not title and not url_:
                continue
            results.append({
                "title": str(title)[:200],
                "url": str(url_),
                "snippet": "",
                "source": "eu_opendata",
                "score": rank_score(0.7, _rk8),
            })
        return results
    return _engine


# ── Open-Meteo 全球天气 ───────────────────────────────────────────────────────

def _build_open_meteo_engine(spec: dict[str, Any]) -> Any:
    """Open-Meteo 全球天气（geocoding + forecast，免认证）。

    查询词视为城市名/地名，先 geocode 取坐标再取当前天气。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        # geocoding 只吃地名：完整天气问句（「今日北京天气」）先剥时间/疑问词，
        # 否则上游 0 候选、引擎静默返回空
        place_q = _geo_core(query)
        try:
            geo = _http_json(
                f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(place_q)}&count=3&language=zh",
                to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Open-Meteo geocode 失败: {e}")
            return []
        results = []
        for _rk9, place in enumerate((geo.get("results") or [])[:n]):
            lat, lon = place.get("latitude"), place.get("longitude")
            if lat is None or lon is None:
                continue
            try:
                wx = _http_json(
                    f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true",
                    to, engine=spec.get("_name", ""))
            except Exception:
                continue
            cw = wx.get("current_weather") or {}
            name = place.get("name", "")
            country = place.get("country", "")
            temp = cw.get("temperature")
            windspeed = cw.get("windspeed")
            wcode = cw.get("weathercode")
            wmo = {0: "晴", 1: "基本晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
                   51: "毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨", 71: "小雪",
                   73: "中雪", 75: "大雪", 80: "阵雨", 95: "雷暴"}.get(wcode, f"code{wcode}")
            results.append({
                "title": f"{name} · {country} 天气",
                "url": f"https://open-meteo.com/en/docs?latitude={lat}&longitude={lon}",
                "snippet": (f"当前 {temp}°C · {wmo} · 风速 {windspeed}km/h"
                            if temp is not None else "暂无数据")[:300],
                "source": "open_meteo",
                "score": rank_score(0.7, _rk9),
            })
        return results
    return _engine


# ── SearchMySite 小型个人站索引（HTML）───────────────────────────────────────

def _build_searchmysite_engine(spec: dict[str, Any]) -> Any:
    """SearchMySite 人工审核个人站索引（searchmysite.net，HTML 解析）。

    只收非商业独立博客，与 marginalia/wiby 同类；结果含标题与正文摘要。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = f"https://searchmysite.net/search/?q={urllib.parse.quote(query)}"
        try:
            raw = _http_text(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"SearchMySite 失败: {e}")
            return []
        results = []
        # 结果容器 class="search-result"，按容器切段后取标题与链接
        # （容器内 href 先于 class="result-link"，正则需兼容两种顺序）
        for _rk10, m in enumerate(re.finditer(r'<div class="search-result[^"]*">(.*?)(?=<div class="search-result|$)', raw, re.S)):
            seg = m.group(1)
            tm = re.search(r'result-title-txt[^>]*>(.*?)</span>', seg, re.S)
            lm = re.search(r'href="(https?://[^"]+)"[^>]*class="result-link"', seg) or \
                 re.search(r'class="result-link"[^>]*href="(https?://[^"]+)"', seg)
            title = re.sub(r"<[^>]+>", "", tm.group(1)).strip() if tm else ""
            link = lm.group(1) if lm else ""
            if not title and not link:
                continue
            results.append({
                "title": title[:200] or link[:200],
                "url": link,
                "snippet": "",
                "source": "searchmysite",
                "score": rank_score(0.7, _rk10),
            })
            if len(results) >= n:
                break
        return results
    return _engine


# ── Lieu webring 搜索引擎（HTML）──────────────────────────────────────────────

def _build_lieu_engine(spec: dict[str, Any]) -> Any:
    """Lieu webring 专用搜索（lieu.cblgh.org，HTML）。

    只索引加入 webring 的站点，比 wiby 更小众；结果即站点内链接。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = f"https://lieu.cblgh.org/?q={urllib.parse.quote(query)}"
        try:
            raw = _http_text(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Lieu 失败: {e}")
            return []
        results = []
        # 结果项 <li class="result|entry">，内含 <a href="https://...">文本</a>
        for _rk11, m in enumerate(re.finditer(
                r'<li class="(?:result|entry)[^"]*">.*?<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                raw, re.S)):
            link = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if not title and not link:
                continue
            results.append({
                "title": title[:200] or link[:200],
                "url": link,
                "snippet": "",
                "source": "lieu",
                "score": rank_score(0.7, _rk11),
            })
            if len(results) >= n:
                break
        return results
    return _engine


# ── OpenSky 实时航班 ──────────────────────────────────────────────────────────

def _build_opensky_engine(spec: dict[str, Any]) -> Any:
    """OpenSky Network 全球实时航班（ADS-B，免 key）。

    查询词含城市/地区名时按 bounding box 返回该区域当前航班。
    """
    timeout = spec.get("timeout", 12)
    # 常见都会区 bbox（lamin,lomin,lamax,lomax）
    _BBOX = {
        "北京": ("39.4", "115.4", "41.1", "117.5"), "上海": ("30.6", "120.8", "31.9", "122.1"),
        "东京": ("35.4", "139.4", "36.1", "140.2"), "纽约": ("40.4", "-74.3", "41.0", "-73.7"),
        "伦敦": ("51.2", "-0.7", "51.7", "0.4"), "巴黎": ("48.6", "2.1", "49.1", "2.6"),
        "新加坡": ("1.1", "103.6", "1.5", "104.1"), "香港": ("22.1", "113.8", "22.6", "114.4"),
    }

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        box = None
        for name, b in _BBOX.items():
            if name in query:
                box = b
                break
        if box is None:
            return []
        lamin, lomin, lamax, lomax = box
        url = (f"https://opensky-network.org/api/states/all?lamin={lamin}&lomin={lomin}"
               f"&lamax={lamax}&lomax={lomax}")
        try:
            data = _http_json(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"OpenSky 失败: {e}")
            return []
        results = []
        for _rk12, st in enumerate((data.get("states") or [])[:n]):
            if not isinstance(st, list) or len(st) < 8:
                continue
            callsign = (st[1] or "").strip()
            country = st[2] or ""
            lat, lon = st[6], st[5]
            alt, vel = st[7], st[9]
            if not callsign and not lat:
                continue
            results.append({
                "title": f"{callsign or '未知航班'} · {country}",
                "url": f"https://globe.adsbexchange.com/?lat={lat}&lon={lon}&zoom=9",
                "snippet": (f"高度 {alt}m · 地速 {vel}m/s · 位置 {lat:.2f},{lon:.2f}"
                            if alt else f"位置 {lat:.2f},{lon:.2f}")[:300],
                "source": "opensky",
                "score": rank_score(0.7, _rk12),
            })
        return results
    return _engine


# ── Electricity Maps 电网碳强度 ───────────────────────────────────────────────

def _build_electricity_maps_engine(spec: dict[str, Any]) -> Any:
    """Electricity Maps 全球电网分区（api.electricitymap.org/v3/zones，免 key）。

    zones 列表为静态目录；查询词匹配国家/区域名时返回分区信息。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = "https://api.electricitymap.org/v3/zones"
        try:
            data = _http_json(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Electricity Maps 失败: {e}")
            return []
        if not isinstance(data, dict):
            return []
        q = query.lower()
        results = []
        for _rk13, (key, zone) in enumerate(data.items()):
            name = zone.get("zoneName", "")
            if q in key.lower() or q in name.lower() or q in (zone.get("countryCode") or "").lower():
                results.append({
                    "title": f"{name} 电网分区",
                    "url": f"https://app.electricitymaps.com/zone/{key}",
                    "snippet": (f"分区键 {key} · 商业可用: {zone.get('isCommerciallyAvailable')}"
                                f" · 层级 {zone.get('tier')}")[:300],
                    "source": "electricity_maps",
                    "score": rank_score(0.7, _rk13),
                })
                if len(results) >= n:
                    break
        return results
    return _engine


# ── USDA FoodData Central 营养成分 ────────────────────────────────────────────

def _build_usda_engine(spec: dict[str, Any]) -> Any:
    """USDA FoodData Central 营养成分（api.nal.usda.gov，官方 DEMO_KEY）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = (f"https://api.nal.usda.gov/fdc/v1/foods/search?api_key=DEMO_KEY"
               f"&query={urllib.parse.quote(query)}&pageSize={min(n, 10)}")
        try:
            data = _http_json(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"USDA 失败: {e}")
            return []
        results = []
        for _rk14, food in enumerate((data.get("foods") or [])[:n]):
            if not isinstance(food, dict):
                continue
            desc = food.get("description", "")
            fdc = food.get("fdcId", "")
            brand = food.get("brandOwner") or ""
            parts = [p for p in (food.get("foodCategory", ""), brand) if p]
            if not desc:
                continue
            results.append({
                "title": desc[:200],
                "url": f"https://fdc.nal.usda.gov/food-details/{fdc}/nutrients" if fdc else "",
                "snippet": (" · ".join(parts) + f" · FDC {fdc}")[:300] if fdc else " · ".join(parts)[:300],
                "source": "usda",
                "score": rank_score(0.7, _rk14),
            })
        return results
    return _engine


# ── Tatoeba 双语例句 ──────────────────────────────────────────────────────────

def _build_tatoeba_engine(spec: dict[str, Any]) -> Any:
    """Tatoeba 多语言例句库（tatoeba.org/api_v0，400+ 语言对）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = (f"https://tatoeba.org/en/api_v0/search?query={urllib.parse.quote(query)}"
               f"&limit={min(n, 10)}")
        try:
            data = _http_json(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Tatoeba 失败: {e}")
            return []
        results = []
        sentences = data.get("results")
        if isinstance(sentences, dict):
            sentences = sentences.get("Sentences") or []
        for _rk15, s in enumerate((sentences or [])[:n]):
            if not isinstance(s, dict):
                continue
            text = s.get("text", "")
            lang = s.get("lang", "")
            trans = s.get("translations") or []
            t_texts = []
            # translations 是 list of lists（每层一个目标语言数组）
            for t in trans[:3]:
                arr = t if isinstance(t, list) else []
                t_texts.extend(str(x.get("text", "")) for x in arr[:2] if isinstance(x, dict))
            snippet = " ⇄ ".join(t for t in t_texts if t)[:200] or f"({lang})"
            if not text:
                continue
            results.append({
                "title": text[:200],
                "url": f"https://tatoeba.org/en/sentences/show/{s.get('id', '')}" if s.get("id") else "",
                "snippet": snippet,
                "source": "tatoeba",
                "score": rank_score(0.7, _rk15),
            })
        return results
    return _engine


# ── Figshare 科研数据集 ───────────────────────────────────────────────────────

def _build_figshare_engine(spec: dict[str, Any]) -> Any:
    """Figshare 科研数据集检索（api.figshare.com/v2/articles）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = (f"https://api.figshare.com/v2/articles?search_for={urllib.parse.quote(query)}"
               f"&page_size={min(n, 10)}")
        try:
            data = _http_json(url, to, engine=spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Figshare 失败: {e}")
            return []
        results = []
        for _rk16, art in enumerate((data if isinstance(data, list) else [])[:n]):
            if not isinstance(art, dict):
                continue
            title = art.get("title", "")
            if not title:
                continue
            results.append({
                "title": str(title)[:200],
                "url": art.get("url_public_api") or art.get("url_public_html") or "",
                "snippet": f"DOI: {art.get('doi', '')}"[:300] if art.get("doi") else "",
                "source": "figshare",
                "score": rank_score(0.7, _rk16),
            })
        return results
    return _engine


# ── 腾讯 K 线（前复权日 K）────────────────────────────────────────────────────

def _resolve_tencent_symbol(q: str, to: float, headers: dict, engine: str = "") -> str:
    """腾讯代码解析（与行情引擎同源逻辑：smartbox 建议接口）。"""
    _STOP = ("股价", "行情", "股票", "价格", "走势", "最新", "今日", "报价", "查询",
             "怎么样", "多少", "怎么", "了", "吗", "的", "a股", "港股", "美股",
             "K线", "k线", "走势图")
    cands = [q]
    for token in re.split(r"[\s,，、/]+", q):
        t = token.strip()
        if not t:
            continue
        for stop in _STOP:
            t = t.replace(stop, "")
        t = t.strip()
        if 2 <= len(t) <= 8 and t not in cands:
            cands.append(t)
    for c in cands:
        try:
            req = urllib.request.Request(
                f"https://smartbox.gtimg.cn/s3/?v=2&t=all&q={urllib.parse.quote(c)}",
                headers=headers)
            with http_open(req, timeout=to, engine=engine) as resp:
                text = resp.read().decode("gbk", "replace")
        except Exception as e:
            logger.warning(f"腾讯代码解析失败: {e}")
            continue
        for m in re.finditer(r'v_hint="([^"]+)"', text):
            parts = m.group(1).split("~")
            if len(parts) >= 3 and parts[2]:
                return parts[0] + parts[1]
    return ""


def _build_tencent_kline_engine(spec: dict[str, Any]) -> Any:
    """腾讯财经前复权日 K 线（web.ifzq.gtimg.cn，免认证 JSON）。

    查询「茅台 K线」类意图：代码解析 + 最近 N 日 OHLCV。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                   "Referer": "https://gu.qq.com/"}
        symbol = _resolve_tencent_symbol(query, to, headers, engine=spec.get("_name", ""))
        if not symbol:
            return []
        days = min(max(n * 20, 20), 120)
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={symbol},day,,,{days},qfq")
        try:
            req = urllib.request.Request(url, headers=headers)
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"腾讯 K 线失败: {e}")
            return []
        node = ((data.get("data") or {}).get(symbol) or {})
        klines = node.get("qfqday") or node.get("day") or []
        if not klines:
            return []
        results = []
        # 最近 n 根：每根一行
        for _rk17, row in enumerate(klines[-n:]):
            if len(row) < 6:
                continue
            date, opn, close, high, low, vol = row[0], row[1], row[2], row[3], row[4], row[5]
            results.append({
                "title": f"{symbol} {date} K线",
                "url": f"https://gu.qq.com/{symbol}/kline",
                "snippet": (f"开 {opn} · 收 {close} · 高 {high} · 低 {low} · 量 {vol}")[:300],
                "source": "tencent_kline",
                "score": rank_score(0.7, _rk17),
            })
        return results
    return _engine


# ── QQ 音乐搜索 ───────────────────────────────────────────────────────────────

def _build_qq_music_engine(spec: dict[str, Any]) -> Any:
    """QQ 音乐搜索（c.y.qq.com，需 Referer，免认证 JSON）。"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = (f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?w={urllib.parse.quote(query)}"
               f"&format=json&n={min(n, 10)}&p=1")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                              "Referer": "https://y.qq.com/"})
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"QQ 音乐失败: {e}")
            return []
        songs = ((data.get("data") or {}).get("song") or {}).get("list") or []
        results = []
        for _rk18, s in enumerate(songs[:n]):
            if not isinstance(s, dict):
                continue
            title = s.get("songname", "")
            singer = " / ".join(str(a.get("name", "")) for a in (s.get("singer") or []) if isinstance(a, dict))
            album = s.get("albumname", "")
            mid = s.get("songmid", "")
            if not title:
                continue
            parts = [p for p in (singer, album) if p]
            results.append({
                "title": f"{title} · {singer}"[:200] if singer else title[:200],
                "url": f"https://y.qq.com/n/ryqq/songDetail/{mid}" if mid else "",
                "snippet": (" · ".join(parts))[:300],
                "source": "qq_music",
                "score": rank_score(0.7, _rk18),
            })
        return results
    return _engine


# ── X/Twitter 单条推文（syndication 免登录通道）─────────────────────────────

# 推文 URL / 裸 ID 提取。链接形态覆盖 x.com 与 twitter.com 两代域名，
# 以及 /statuses/ 老路径与移动端分享链接的 query 参数形态。
_TWEET_URL_RE = re.compile(
    r"(?:x\.com|twitter\.com)/[^/\s]+/(?:status|statuses)/(\d{10,20})", re.I)
_TWEET_BARE_ID_RE = re.compile(r"^\d{10,20}$")
_TWEET_QUERY_ID_RE = re.compile(r"[?&](?:tweet_id|status_id|id)=(\d{10,20})")


def extract_tweet_id(text: str) -> str | None:
    """从查询词提取推文 ID：完整 URL、分享链接参数、裸 ID 均可。"""
    if not text:
        return None
    m = _TWEET_URL_RE.search(text)
    if m:
        return m.group(1)
    m = _TWEET_QUERY_ID_RE.search(text)
    if m:
        return m.group(1)
    text = text.strip()
    if _TWEET_BARE_ID_RE.match(text):
        return text
    return None


# syndication 端点要求 token 参数存在，但值不参与校验。
# 实测 2026-09-12（推文 1585841080431321088）：token=a、token=0、
# token=aaaaaaaaaaaa 三者都返回完整推文，省略 token 参数则返回空响应体。
# 因此这里用固定值——按 id 推导 base36 没有意义：那段手写浮点实现既与服务端
# 计算方式无关（服务端不校验），也与它自己声称的公式不一致（固定 12 位小数 vs
# 最短往返表示；只去首尾 0 vs 去所有 0），测试还把它锁成了金标。
# 若将来服务端真的开始校验，再按公开公式
#   ((id / 1e15) * π).toString(36) 去掉所有 '0' 与 '.'
# 重做推导，并在此处补真实向量测试。
_SYNDICATION_TOKEN = "argo"


def syndication_token(tweet_id: str) -> str:
    """syndication 展示参数（服务端不校验值，见上方常量注释）。

    保留函数签名：调用方与测试按「请求必须带非空 token」这一契约依赖它。
    """
    return _SYNDICATION_TOKEN


def _build_twitter_syndication_engine(spec: dict[str, Any]) -> Any:
    """X/Twitter 单条推文（syndication 通道，免登录、零 key）。

    为什么需要第二条通道：X 的网页与官方 API 对未登录请求一律拒绝，
    唯一常年开放的入口是面向第三方网页嵌入的 syndication 接口——它
    本来就是给外部站点引用单条推文用的，天然公开且带完整正文与媒体。

    语义是「按推文取单条」的抽取型引擎，不是搜索：查询词接受推文 URL
    （x.com / twitter.com / 带 tweet_id 参数的分享链接）或裸 ID。拿它搜
    关键词时诚实返回空列表，不回落到伪造结果——syndication 没有搜索端点。
    """
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        tweet_id = extract_tweet_id(query)
        if not tweet_id:
            return []
        token = syndication_token(tweet_id)
        url = (f"https://cdn.syndication.twimg.com/tweet-result"
               f"?id={tweet_id}&token={token}")
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/150.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://platform.twitter.com/",
        }
        from engines_base import _http_get_raw
        raw = _http_get_raw(url, headers, to, engine="twitter_syndication")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        # 正常推文 __typename="Tweet"；墓碑（已删除/受限）返回 TweetTombstone
        # 等其他类型或空对象，一律按无结果处理，不伪造占位
        if not isinstance(data, dict) or not data:
            return []
        if data.get("__typename") and data["__typename"] != "Tweet":
            return []
        text = str(data.get("text") or "")
        # 作者字段：现行响应在 user 键（author 为历史兼容候选）
        author = data.get("user") or data.get("author") or {}
        screen_name = str(author.get("screen_name") or "")
        display_name = str(author.get("name") or "")
        created = str(data.get("created_at") or "")
        media = data.get("mediaDetails") or (data.get("entities") or {}).get("media") or []
        media_note = f"（含 {len(media)} 项媒体）" if media else ""
        canonical_url = (f"https://x.com/{screen_name}/status/{tweet_id}"
                         if screen_name else
                         f"https://x.com/i/web/status/{tweet_id}")
        title = f"@{screen_name}: {text[:80]}" if screen_name else text[:80]
        snippet_parts = []
        if created:
            snippet_parts.append(created)
        if display_name:
            snippet_parts.append(display_name)
        snippet_parts.append(text[:280])
        if media_note:
            snippet_parts.append(media_note)
        return [{
            "title": title[:100],
            "url": canonical_url,
            "snippet": " | ".join(p for p in snippet_parts if p),
            "source": "twitter_syndication",
            "score": 0.9,
        }]
    return _engine


# ── 批次八：数据源扩展（2026-09-12）──────────────────────────────────────────

# Google News RSS 的 hl/gl/ceid 三参数必须成组（ceid 是 "地区:语言" 复合值），
# 不能走 engines_base 的单参数 _lang_param 动态化——这里按查询主语言整组切换。
_NEWS_LOCALES = {
    "zh": ("zh-CN", "CN", "CN:zh-Hans"),
    "ja": ("ja", "JP", "JP:ja"),
    "ko": ("ko", "KR", "KR:ko"),
    "en": ("en-US", "US", "US:en"),
}


def _news_locale(query: str) -> tuple[str, str, str]:
    """按查询主语言选 (hl, gl, ceid)；检测不了默认 en-US。"""
    try:
        from lang_detect import detect_language
        lang = (detect_language(query) or "en").split("-")[0]
    except ImportError:
        lang = "en"
    return _NEWS_LOCALES.get(lang, _NEWS_LOCALES["en"])


def _build_google_news_engine(spec: dict[str, Any]) -> Any:
    """Google News RSS（多语言新闻，免认证，结构化 RSS 2.0）。

    engines_base._parse_xml 只解析 Atom（arXiv 型），Google News 是 RSS
    channel/item 结构且标题带「 - 媒体名」尾巴，这里单独解析并剥短尾巴。
    查询里的 when:1d / site: 等运算符原样透传给 Google。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        hl, gl, ceid = _news_locale(q)
        url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}"
               f"&hl={hl}&gl={gl}&ceid={ceid}")
        raw = _http_get_raw(url, {"User-Agent": "argo-search/1.0 (+google_news)", "Accept": "application/rss+xml"},
                            to, engine=spec.get("_name", "google_news"))
        if raw is None:
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []
        channel = root.find("channel")
        if channel is None:
            return []
        out = []
        for item in channel.findall("item")[:max(int(n), 1)]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            # 「标题 - 媒体名」尾巴只在水线以下剥：尾巴过长说明是标题本体的一部分
            if " - " in title:
                head, tail = title.rsplit(" - ", 1)
                if 0 < len(tail) <= 25:
                    title = head.strip()
            pub = (item.findtext("pubDate") or "").strip()
            out.append({
                "title": title[:200],
                "url": link,
                "snippet": "",
                "source": "google_news",
                "published_at": pub,
            })
        return out
    return _engine


def _build_met_museum_engine(spec: dict[str, Any]) -> Any:
    """Met Museum 藏品库（艺术/博物馆藏品，两跳：search → objects/{id}）。

    search 只回 objectIDs 数组，详情逐个取（上限 6 条控二跳延迟）；artist 为空
    是常态（大量藏品无署名），标题用 title 保底。
    """
    timeout = spec.get("timeout", 20)
    _BASE = "https://collectionapi.metmuseum.org/public/collection/v1"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        engine_name = spec.get("_name", "met_museum")
        headers = {"User-Agent": "argo-search/1.0 (+met_museum)", "Accept": "application/json"}
        search_url = f"{_BASE}/search?q={urllib.parse.quote(q)}&hasImages=true"
        raw = _http_get_raw(search_url, headers, to, engine=engine_name)
        if raw is None:
            return []
        try:
            ids = (json.loads(raw) or {}).get("objectIDs") or []
        except (json.JSONDecodeError, ValueError):
            return []
        out = []
        for oid in ids[:min(max(int(n), 1), 6)]:
            obj_raw = _http_get_raw(f"{_BASE}/objects/{oid}", headers, to, engine=engine_name)
            if obj_raw is None:
                continue
            try:
                d = json.loads(obj_raw)
            except (json.JSONDecodeError, ValueError):
                continue
            title = str(d.get("title") or "").strip()
            if not title:
                continue
            artist = str(d.get("artistDisplayName") or "").strip()
            bits = [b for b in (artist, str(d.get("objectDate") or "").strip(),
                                str(d.get("medium") or "").strip(),
                                str(d.get("department") or "").strip()) if b]
            _row: dict[str, Any] = {
                "title": title,
                "url": str(d.get("objectURL") or "").strip(),
                "snippet": " · ".join(bits)[:300],
                "source": "met_museum",
            }
            # 图片字段：search 已带 hasImages=true（只筛有图的），但详情里
            # primaryImage/primaryImageSmall 仍可能是空串，回落到 Small；
            # 两者都空则不加字段，不发空链接。
            #
            # 上游权利策略是本字段的主要变量（实测 2026-09-13）：Met 对
            # `isPublicDomain: false` 的藏品**一律不给图片链接**（"monet" 前
            # 四个 objectID 全空，仅公版件有值）。所以这里取不到图不代表
            # 解析坏了——Met 只开放公有领域影像，受版权保护的只给著录信息。
            _img = (str(d.get("primaryImage") or "").strip()
                    or str(d.get("primaryImageSmall") or "").strip())
            if _img:
                _row["image_url"] = _img
                _row["image_license"] = (
                    "公版（Met Open Access）" if d.get("isPublicDomain")
                    else "受版权保护，仅供检索定位（Met）")
            out.append(_row)
        return out
    return _engine

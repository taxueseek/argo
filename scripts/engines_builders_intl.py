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

from engines_base import safe_search

logger = logging.getLogger("unified_search.engines")

_UA = "argo-search/2.4 (unified-search@local)"


def _http_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace"))


def _http_text(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
            data = _http_json(url, to)
        except Exception as e:
            logger.warning(f"CiNii 失败: {e}")
            return []
        results = []
        for it in (data.get("items") or [])[:n]:
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
                "score": 0.7,
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
            raw = _http_text(url, to)
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
        for item in root.iter("item"):
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
                "score": 0.7,
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
            raw = _http_text(url, to)
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
                    "score": 0.7,
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
            raw = _http_text(url, to)
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
        for item in root.iter(f"{{{RSS}}}item"):
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
                "score": 0.7,
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
            raw = _http_text(url, to)
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
        for rec in root.iter(f"{{{SRW}}}record"):
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
                "score": 0.7,
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
            data = _http_json(url, to)
        except Exception as e:
            logger.warning(f"DOAJ 失败: {e}")
            return []
        results = []
        for r in (data.get("results") or [])[:n]:
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
                "score": 0.7,
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
            data = _http_json(url, to)
        except Exception as e:
            logger.warning(f"Europeana 失败: {e}")
            return []
        results = []
        for it in (data.get("items") or [])[:n]:
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
                "score": 0.7,
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
            data = _http_json(url, to)
        except Exception as e:
            logger.warning(f"HAL 失败: {e}")
            return []
        results = []
        for doc in (data.get("response", {}).get("docs") or [])[:n]:
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
                "score": 0.7,
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
            data = _http_json(url, to)
        except Exception as e:
            logger.warning(f"EU ODP 失败: {e}")
            return []
        results = []
        for it in (data.get("result", {}).get("results") or [])[:n]:
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
                "score": 0.7,
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
        try:
            geo = _http_json(
                f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(query)}&count=3",
                to)
        except Exception as e:
            logger.warning(f"Open-Meteo geocode 失败: {e}")
            return []
        results = []
        for place in (geo.get("results") or [])[:n]:
            lat, lon = place.get("latitude"), place.get("longitude")
            if lat is None or lon is None:
                continue
            try:
                wx = _http_json(
                    f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true",
                    to)
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
                "score": 0.7,
            })
        return results
    return _engine

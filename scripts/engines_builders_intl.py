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

from engines_http import UA
from engines_base import safe_search, _http_get_raw, http_open, rank_score
from bounded_run import run_bounded
from engines_http import (
    UA as _UA,
    encode_url as _encode_url,
    http_json as _http_json,
    http_text as _shared_http_text,
)

logger = logging.getLogger("unified_search.engines")


# 天气查询的地名/跨度解析（未来N天）与 Open-Meteo 构建器已拆至
# engines_builders_weather.py（2026-11-11 拆分，本文件越过 1000 行硬上限）。
# 此处保留同名再导出，既有 `from engines_builders_intl import _geo_core`
# 的调用面（tests/plan.py/batch_probe.py）不受影响。
from engines_builders_weather import (  # noqa: E402
    forecast_span as _forecast_span,
    geo_core as _geo_core,
    build_open_meteo_engine as _build_open_meteo_engine,
)


def _http_text(url: str, timeout: float, engine: str = "") -> str:
    return _shared_http_text(url, timeout, engine=engine, opener=http_open)


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

_TWEET_PATH = re.compile(r"/(?:[\w]+|i/web)/status(?:es)?/([0-9]{1,25})(?:/.*)?$")


def extract_tweet_id(text: str) -> str | None:
    """接受 X/Twitter 的状态链接、分享参数或 10–20 位裸 ID。"""
    text = (text or "").strip()
    if re.fullmatch(r"[0-9]{10,20}", text):
        return text
    try:
        url = urllib.parse.urlsplit(text if "://" in text else "https://" + text)
        if url.scheme not in ("http", "https") or url.hostname not in {
                "x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}:
            return None
    except ValueError:
        return None
    m = _TWEET_PATH.fullmatch(url.path)
    if m:
        return m.group(1)
    for key, value in urllib.parse.parse_qsl(url.query):
        if key in ("tweet_id", "status_id", "id") and re.fullmatch(r"[0-9]{1,25}", value):
            return value
    return None


def syndication_token(tweet_id: str) -> str:
    """公开嵌入端点要求非空 token 参数。"""
    return "argo"


def _build_twitter_syndication_engine(spec: dict[str, Any]) -> Any:
    from twitter_single_read import read_tweet

    timeout = spec.get("timeout", 8)

    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        tweet_id = extract_tweet_id(query)
        if not tweet_id:
            return []
        return read_tweet(tweet_id, syndication_token(tweet_id), _timeout or timeout)

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
        raw = _http_get_raw(url, {"User-Agent": UA, "Accept": "application/rss+xml"},
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
    # /search 已于 2026-10-01 退役（410，Solr 下线）迁 v1.1（Elastic，响应不变）；
    # objects/{id} 详情端点 v1.1 没有（404），只能留在 v1。
    _BASE = "https://collectionapi.metmuseum.org/public/collection/v1"
    _SEARCH_BASE = "https://collectionapi.metmuseum.org/public/collection/v1.1"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        engine_name = spec.get("_name", "met_museum")
        headers = {"User-Agent": UA, "Accept": "application/json"}
        search_url = f"{_SEARCH_BASE}/search?q={urllib.parse.quote(q)}&hasImages=true"
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

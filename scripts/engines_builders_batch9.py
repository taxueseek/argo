#!/usr/bin/env python3
"""批次九构建器：免密钥垂直数据源扩展（2026-09-13）。

收录原则与批次七/八一致：免密钥开箱可用、填真空白、过收录检查。
本批聚焦 argo 此前的七个真实空白域：

  who_don         公共卫生事件（WHO 官方 OData）
  who_gho         全球卫生指标（WHO GHO OData）
  gdacs           多灾种预警（洪水/台风/野火，补 usgs 只有地震）
  nebula           —
  osv              —
  obis            海洋物种观测（2.29 亿条，补 gbif 的陆生视角）
  worms           海洋分类学权威命名
  energy_charts   欧洲发电结构/可再生占比
  openf1          F1 赛车
  jolpica         F1 积分榜（Ergast 继任）
  openligadb      德甲足球赛程比分
  artic           芝加哥艺术博物馆（补 art_museum 域）
  cleveland       克利夫兰艺术博物馆（CC0 图像）
  tvmaze          电视剧元数据（补 film_search 的剧集维度）
  jikan           动漫库（MyAnimeList 数据）
  deezer          音乐艺人/专辑（补 media_search 的西语圈）
  listenbrainz    收听趋势（社区收听数据）
  egov_law        日本法令（e-Gov 官方全文，补 kor_law/flk 之外的日韩）
  k10plus         德国最大联合目录（SRU 标准，补 dnb 之外的馆藏）
  ror             研究机构标识（补 org_entity 的学术机构维度）
  nhtsa_vpic      车辆厂商本体
  soilgrids       全球土壤属性（逐点栅格）
  noaa_swpc       空间天气（Kp 指数/太阳活动区）
  satnogs         卫星目录（配合 TLE 镜像）
  tle_mirror      TLE 两行根数（Celestrak 不可达时的替代）
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from engines_base import (
    rank_score,
    safe_search,
    http_open,
)

logger = logging.getLogger("unified_search.engines")

_UA = "argo-search/2.8 (+batch9)"

# e-Gov 法令列表进程内缓存（体积大、变动低频）
_EGOV_ENTRIES: list[str] | None = None
_EGOV_FETCHED_AT: float = 0.0
def _latin_query(q: str) -> str:
    """从中英混排查询里抽出拉丁学名部分。

    「Gadus morhua 海洋物种」->「Gadus morhua」；纯中文查询返回空串
    （交回百科类引擎，OBIS/WoRMS 无中文名索引）。
    """
    if not q:
        return ""
    stripped = re.sub(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", " ", q)
    stripped = re.sub(r"[^A-Za-z0-9\-\s]", " ", stripped)
    return " ".join(stripped.split())


def _terms(q: str) -> list[str]:
    """切检索词：兼顾半角/全角空格（U+3000 不被 \\s 匹配，中文输入常见）。"""
    return [t for t in re.split(r"[\s\u3000]+", (q or "").strip()) if t]



def _read(resp, engine: str = "") -> bytes:
    """读取响应体，容忍 IncompleteRead。

    大体积响应（e-Gov 法令列表实测 2.7MB）在 http.client 上偶发
    `IncompleteRead` —— 此时已读到的部分数据仍然可用（我们先拿头部/整体结构），
    所以接受 partial 而不是直接失败。
    """
    try:
        return resp.read()
    except Exception as e:  # http.client.IncompleteRead 等
        partial = getattr(e, "partial", None)
        if partial:
            logger.warning(f"{engine} 响应不完整，使用已读部分 {len(partial)}B")
            return partial
        raise


def _json(url: str, to: float, engine: str = "") -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with http_open(req, timeout=to, engine=engine) as resp:
        return json.loads(_read(resp, engine).decode("utf-8", "replace"))


def _text(url: str, to: float, engine: str = "", accept: str = "*/*") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": accept})
    with http_open(req, timeout=to, engine=engine) as resp:
        return _read(resp, engine).decode("utf-8", "replace")


# ── WHO DON 疫情暴发通报 ──────────────────────────────────────────────────────

def _build_who_don_engine(spec: dict[str, Any]) -> Any:
    """WHO Disease Outbreak News（官方 OData，$filter contains 语法）。"""
    timeout = spec.get("timeout", 15)
    base = "https://www.who.int/api/news/diseaseoutbreaknews"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        # OData：contains(Title,'x') 对中文无效（WHO 通报为英文），
        # 取最近 n 条并按查询词做本地过滤，兼顾「查询无关时仍给官方事件流」。
        # 不传 $top/$orderby：未编码的 $ 参数在此网关返回 400，改为取回后本地按日期排序
        url = base
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"WHO DON 失败: {e}")
            return []
        # 该网关把 OData 的 value 展平为顶层 list；兼容两种形态
        if isinstance(data, list):
            items = data
        else:
            items = data.get("value") or []
        if not isinstance(items, list):
            return []
        # 本地按发布日期倒序（新事件优先）
        items = sorted(
            [i for i in items if isinstance(i, dict)],
            key=lambda i: str(i.get("PublicationDate") or ""), reverse=True)
        # 只保留真正的暴发通报条目（该端点混有其它新闻类型）
        items = [i for i in items if (i.get("Title") or i.get("OverrideTitle"))]
        terms = [t for t in _terms(q.lower()) if len(t) > 1]
        hits, rest = [], []
        for it in items:
            if not isinstance(it, dict):
                continue
            blob = " ".join(str(it.get(k) or "") for k in
                            ("Title", "OverrideTitle", "Overview")).lower()
            (hits if (terms and any(t in blob for t in terms)) else rest).append(it)
        out = (hits + rest)[:n]
        results = []
        for _rk, it in enumerate(out):
            title = str(it.get("Title") or it.get("OverrideTitle") or "").strip()
            if not title:
                continue
            results.append({
                "title": title[:200],
                "url": f"https://www.who.int/emergencies/disease-outbreak-news/item/{it.get('Id','')}",
                "snippet": str(it.get("Overview") or it.get("Summary") or "").strip()[:300],
                "source": "who_don",
                "score": rank_score(0.9, _rk),
                "published_at": it.get("PublicationDate") or "",
            })
        return results
    return _engine


# ── WHO GHO 全球卫生指标 ──────────────────────────────────────────────────────

def _build_who_gho_engine(spec: dict[str, Any]) -> Any:
    """WHO Global Health Observatory（官方 OData，指标目录检索）。"""
    timeout = spec.get("timeout", 15)
    base = "https://ghoapi.azureedge.net/api/Indicator"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip().lower()
        if not q:
            return []
        # 指标目录不大（数千条），一次取回本地过滤，避免 OData contains 的引号转义坑
        try:
            # 不传 $top：部分网关对未编码的 $ 参数返回 400；目录本身只有数千条
            data = _json(base, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"WHO GHO 失败: {e}")
            return []
        vals = data.get("value") or []
        terms = [t for t in _terms(q) if len(t) > 2]
        scored = []
        for it in vals:
            if not isinstance(it, dict):
                continue
            name = str(it.get("IndicatorName") or "")
            code = str(it.get("IndicatorCode") or "")
            low = f"{name} {code}".lower()
            hits = sum(1 for t in terms if t in low)
            if hits:
                scored.append((hits, name, code))
        scored.sort(key=lambda x: -x[0])
        results = []
        for _rk, (_h, name, code) in enumerate(scored[:n]):
            results.append({
                "title": name[:200],
                "url": f"https://www.who.int/data/gho/data/indicators/indicator-details/GHO/{code}",
                "snippet": f"WHO GHO 指标 · {code}"[:300],
                "source": "who_gho",
                "score": rank_score(0.85, _rk),
            })
        return results
    return _engine


# ── GDACS 多灾种预警 ──────────────────────────────────────────────────────────

def _build_gdacs_engine(spec: dict[str, Any]) -> Any:
    """GDACS 全球灾害预警（EU/JRC 官方 GeoJSON，多灾种）。"""
    timeout = spec.get("timeout", 20)
    base = ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
            "?eventlist=EQ,TC,FL,WF,DR,VO")

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip().lower()
        try:
            data = _json(base, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"GDACS 失败: {e}")
            return []
        feats = data.get("features") or []
        terms = [t for t in _terms(q) if len(t) > 2]
        hits, rest = [], []
        for f in feats:
            if not isinstance(f, dict):
                continue
            p = f.get("properties") or {}
            blob = f"{p.get('name','')} {p.get('eventtype','')} {p.get('htmldescription','')}".lower()
            (hits if (terms and any(t in blob for t in terms)) else rest).append(p)
        out = (hits + rest)[:n]
        results = []
        for _rk, p in enumerate(out):
            name = (p.get("name") or p.get("htmldescription") or "").strip()
            if not name:
                continue
            alert = (p.get("alertlevel") or "").lower()
            base_score = 0.9 if alert in ("red",) else (0.85 if alert == "orange" else 0.8)
            # GDACS 的 properties.url 是 dict（{report, geometry, ...}），
            # 不是字符串——直接透传会在下游 url.lower() 处抛 AttributeError。
            u = p.get("url")
            if isinstance(u, dict):
                link = u.get("report") or u.get("geometry") or ""
            else:
                link = u or ""
            if not link:
                link = "https://www.gdacs.org/"
            results.append({
                "title": f"[{(p.get('eventtype') or '').upper()}] {name}"[:200],
                "url": link,
                "snippet": (p.get("htmldescription") or "")[:300],
                "source": "gdacs",
                "score": rank_score(base_score, _rk),
                "published_at": (p.get("fromdate") or "")[:10],
            })
        return results
    return _engine


# ── OBIS 海洋生物观测 ─────────────────────────────────────────────────────────

def _build_obis_engine(spec: dict[str, Any]) -> Any:
    """OBIS 海洋物种观测（IOC/UNESCO，2.29 亿条记录）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        # OBIS 的 scientificname 过滤对拉丁学名准确；混排查询抽出拉丁部分再查
        q = _latin_query(q)
        if not q:
            return []
        url = ("https://api.obis.org/v3/occurrence?scientificname="
               f"{urllib.parse.quote(q)}&size={n}")
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"OBIS 失败: {e}")
            return []
        total = data.get("total")
        results = []
        seen = set()
        for _rk, r in enumerate((data.get("results") or [])[:n]):
            if not isinstance(r, dict):
                continue
            sci = r.get("scientificName") or r.get("acceptedNameUsage") or ""
            if not sci or sci in seen:
                continue
            seen.add(sci)
            results.append({
                "title": f"{sci}（海洋观测记录）"[:200],
                "url": "https://obis.org/",
                "snippet": (f"OBIS 海洋生物多样性 · 记录总数 {total} · "
                            f"深度 {r.get('depth','-')} m")[:300],
                "source": "obis",
                "score": rank_score(0.85, _rk),
            })
        return results
    return _engine


def _build_worms_engine(spec: dict[str, Any]) -> Any:
    """WoRMS 海洋物种权威命名（分类学标准）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        q = _latin_query(q)
        if not q:
            return []
        url = f"https://www.marinespecies.org/rest/AphiaRecordsByName/{urllib.parse.quote(q)}?like=true"
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"WoRMS 失败: {e}")
            return []
        if not isinstance(data, list):
            return []
        results = []
        for _rk, r in enumerate(data[:n]):
            if not isinstance(r, dict):
                continue
            name = r.get("scientificname") or ""
            if not name:
                continue
            results.append({
                "title": f"{name}（{'已接受名' if r.get('status') == 'accepted' else r.get('status') or ''}）"[:200],
                "url": f"https://www.marinespecies.org/aphia.php?p=taxdetails&id={r.get('AphiaID','')}",
                "snippet": (f"WoRMS AphiaID {r.get('AphiaID','')} · {r.get('rank','')} · "
                            f"{r.get('kingdom','')}")[:300],
                "source": "worms",
                "score": rank_score(0.85, _rk),
            })
        return results
    return _engine


# ── Energy-Charts 欧洲发电结构 ────────────────────────────────────────────────

_EC_COUNTRY = {
    "germany": "de", "德国": "de", "france": "fr", "法国": "fr",
    "spain": "es", "西班牙": "es", "italy": "it", "意大利": "it",
    "poland": "pl", "波兰": "pl", "austria": "at", "奥地利": "at",
    "netherlands": "nl", "荷兰": "nl", "belgium": "be", "比利时": "be",
    "switzerland": "ch", "瑞士": "ch", "denmark": "dk", "丹麦": "dk",
    "sweden": "se", "瑞典": "se", "norway": "no", "挪威": "no",
    "portugal": "pt", "葡萄牙": "pt", "czech": "cz", "捷克": "cz",
}
_EC_LABEL = {
    "de": "德国", "fr": "法国", "es": "西班牙", "it": "意大利", "pl": "波兰",
    "at": "奥地利", "nl": "荷兰", "be": "比利时", "ch": "瑞士", "dk": "丹麦",
    "se": "瑞典", "no": "挪威", "pt": "葡萄牙", "cz": "捷克",
}


def _build_energy_charts_engine(spec: dict[str, Any]) -> Any:
    """Energy-Charts 发电结构（Fraunhofer ISE，免认证，需署名）。"""
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        low = q.lower()
        cc = None
        for k, v in _EC_COUNTRY.items():
            if k in low:
                cc = v
                break
        if cc is None:
            cc = "de"  # 默认德国（数据集覆盖最全）
        # 取最近 2 天的逐时发电结构
        from datetime import date, timedelta
        end = date.today()
        start = end - timedelta(days=1)
        url = ("https://api.energy-charts.info/public_power?country="
               f"{cc}&start={start.isoformat()}&end={end.isoformat()}")
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Energy-Charts 失败: {e}")
            return []
        types = data.get("production_types") or []
        if not isinstance(types, list):
            return []
        label = _EC_LABEL.get(cc, cc.upper())
        results = []
        # 按累计发电量排序，给该国的发电结构快照
        ranked = []
        for t in types:
            if not isinstance(t, dict):
                continue
            name = t.get("name") or ""
            series = t.get("data") or []
            total = sum(v for v in series if isinstance(v, (int, float)))
            ranked.append((total, name))
        ranked.sort(key=lambda x: -x[0])
        for _rk, (total, name) in enumerate(ranked[:n]):
            if not name:
                continue
            results.append({
                "title": f"{label} 发电结构 · {name}"[:200],
                "url": "https://www.energy-charts.info/",
                "snippet": (f"{label} 近 24h {name} 累计发电 {round(total/1000, 1)} GWh "
                            f"（数据来源 Fraunhofer ISE Energy-Charts）")[:300],
                "source": "energy_charts",
                "score": rank_score(0.85, _rk),
            })
        return results
    return _engine


# ── F1：OpenF1 + Jolpica ─────────────────────────────────────────────────────

def _build_jolpica_engine(spec: dict[str, Any]) -> Any:
    """Jolpica F1（Ergast 继任 API，积分榜/赛程）。"""
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip().lower()
        year = None
        m = re.search(r"\b(19|20)\d{2}\b", q)
        if m:
            year = m.group(0)
        else:
            from datetime import date
            year = str(date.today().year)
        url = f"https://api.jolpi.ca/ergast/f1/{year}/driverstandings/"
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Jolpica 失败: {e}")
            return []
        try:
            lists = data["MRData"]["StandingsTable"]["StandingsLists"][0]["DriverStandings"]
        except (KeyError, IndexError, TypeError):
            return []
        results = []
        terms = [t for t in _terms(q) if len(t) > 2 and not t.isdigit()]
        filtered = []
        for d in lists:
            if not isinstance(d, dict):
                continue
            drv = d.get("Driver") or {}
            nm = f"{drv.get('givenName','')} {drv.get('familyName','')}".strip()
            cons = ", ".join((c.get("Constructor") or {}).get("name", "")
                             for c in (d.get("Constructors") or []) if isinstance(c, dict))
            blob = f"{nm} {cons}".lower()
            filtered.append((1 if (terms and any(t in blob for t in terms)) else 0, d, nm, cons))
        filtered.sort(key=lambda x: -x[0])
        for _rk, (_h, d, nm, cons) in enumerate(filtered[:n]):
            if not nm:
                continue
            results.append({
                "title": f"F1 {year} 车手积分榜 · P{d.get('position','')} {nm}"[:200],
                "url": (d.get("Driver") or {}).get("url") or "https://www.formula1.com/",
                "snippet": (f"{nm} · {cons} · 积分 {d.get('points','')} · 胜场 {d.get('wins','')}"
                            f"（{year} 赛季）")[:300],
                "source": "jolpica",
                "score": rank_score(0.85, _rk),
            })
        return results
    return _engine


def _build_openf1_engine(spec: dict[str, Any]) -> Any:
    """OpenF1 车手/车队（两级：sessions → drivers）。"""
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip().lower()
        try:
            sessions = _json("https://api.openf1.org/v1/sessions?year=2026", to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"OpenF1 sessions 失败: {e}")
            return []
        if not isinstance(sessions, list) or not sessions:
            return []
        # 取最近一场有 session_key 的场次
        sk = None
        for s in reversed(sessions):
            if isinstance(s, dict) and s.get("session_key"):
                sk = s["session_key"]
                break
        if sk is None:
            return []
        try:
            drivers = _json(f"https://api.openf1.org/v1/drivers?session_key={sk}", to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"OpenF1 drivers 失败: {e}")
            return []
        if not isinstance(drivers, list):
            return []
        terms = [t for t in _terms(q) if len(t) > 2]
        rows = []
        for d in drivers:
            if not isinstance(d, dict):
                continue
            nm = d.get("full_name") or d.get("broadcast_name") or ""
            team = d.get("team_name") or ""
            blob = f"{nm} {team}".lower()
            rows.append((1 if (terms and any(t in blob for t in terms)) else 0, d, nm, team))
        rows.sort(key=lambda x: -x[0])
        results = []
        for _rk, (_h, d, nm, team) in enumerate(rows[:n]):
            if not nm:
                continue
            results.append({
                "title": f"F1 {nm} · {team}"[:200],
                "url": "https://openf1.org/",
                "snippet": (f"车号 {d.get('driver_number','')} · {team} · "
                            f"{d.get('country_code','')}（OpenF1 遥测数据）")[:300],
                "source": "openf1",
                "score": rank_score(0.8, _rk),
            })
        return results
    return _engine


def _build_openligadb_engine(spec: dict[str, Any]) -> Any:
    """OpenLigaDB 德甲/欧洲联赛赛程比分（免认证）。"""
    timeout = spec.get("timeout", 15)
    LEAGUES = {"bundesliga": "bl1", "德甲": "bl1", "bl1": "bl1",
               "bl2": "bl2", "德乙": "bl2", "premier": "bl1"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        low = q.lower()
        league = "bl1"
        for k, v in LEAGUES.items():
            if k in low:
                league = v
                break
        from datetime import date
        season = date.today().year
        url = f"https://api.openligadb.de/getmatchdata/{league}/{season}"
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"OpenLigaDB 失败: {e}")
            return []
        if not isinstance(data, list):
            return []
        terms = [t for t in _terms(low) if len(t) > 2 and not t.isdigit()]
        rows = []
        for m in data:
            if not isinstance(m, dict):
                continue
            t1 = ((m.get("team1") or {}).get("teamName") or "")
            t2 = ((m.get("team2") or {}).get("teamName") or "")
            blob = f"{t1} {t2}".lower()
            rows.append((1 if (terms and any(t in blob for t in terms)) else 0, m, t1, t2))
        # 无命中时给最近场次（按时间倒序取尾部）
        rows.sort(key=lambda x: x[0])
        hits = [r for r in rows if r[0] == 1]
        pool = (hits if hits else rows)[:n]
        results = []
        for _rk, (_h, m, t1, t2) in enumerate(pool):
            if not t1 and not t2:
                continue
            res = m.get("matchResults") or []
            score_txt = ""
            if res and isinstance(res[-1], dict):
                score_txt = f" 比分 {res[-1].get('pointsTeam1','')}-{res[-1].get('pointsTeam2','')}"
            results.append({
                "title": f"{t1} vs {t2}{score_txt}"[:200],
                "url": "https://www.openligadb.de/",
                "snippet": (f"{m.get('leagueName','')} · 第 {m.get('groupName') or ''} 轮 · "
                            f"{str(m.get('matchDateTime',''))[:16]}")[:300],
                "source": "openligadb",
                "score": rank_score(0.8, _rk),
                "published_at": str(m.get("matchDateTime") or "")[:10],
            })
        return results
    return _engine


# ── 博物馆：AIC + Cleveland（补 art_museum 域）────────────────────────────────

def _build_artic_engine(spec: dict[str, Any]) -> Any:
    """Art Institute of Chicago 馆藏（免认证，含 IIIF 图像）。"""
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        url = ("https://api.artic.edu/api/v1/artworks/search?"
               f"q={urllib.parse.quote(q)}&limit={n}"
               "&fields=id,title,artist_display,date_display,image_id")
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"AIC 失败: {e}")
            return []
        # IIIF 基址取响应自带的 config.iiif_url，不硬编码——上游换域时硬编码会
        # 静默失效。该图床要求带 AIC-User-Agent 头才放行直连（否则 403 HEAD）。
        iiif = str(((data.get("config") or {}).get("iiif_url")) or "").rstrip("/")
        results = []
        for _rk, it in enumerate((data.get("data") or [])[:n]):
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or "").strip()
            if not title:
                continue
            _row = {
                "title": title[:200],
                "url": f"https://www.artic.edu/artworks/{it.get('id','')}",
                "snippet": (f"{it.get('artist_display','')} · {it.get('date_display','')}"
                            "（芝加哥艺术博物馆）")[:300],
                "source": "artic",
                "score": rank_score(0.85, _rk),
            }
            # 图片字段：请求里本就取了 image_id（此前取到即丢），这里按 AIC
            # 官方 IIIF 模板拼出可直接打开的图；无 image_id 的藏品不加字段。
            _iid = str(it.get("image_id") or "").strip()
            if _iid and iiif:
                _row["image_url"] = f"{iiif}/{_iid}/full/843,/0/default.jpg"
                _row["image_license"] = "AIC 公开接口（权利状态见作品页）"
            results.append(_row)
        return results
    return _engine


def _build_cleveland_engine(spec: dict[str, Any]) -> Any:
    """克利夫兰艺术博物馆（CC0 开放图像）。"""
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        url = ("https://openaccess-api.clevelandart.org/api/artworks/?"
               f"q={urllib.parse.quote(q)}&limit={n}&has_image=1")
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Cleveland 失败: {e}")
            return []
        results = []
        for _rk, it in enumerate((data.get("data") or [])[:n]):
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or "").strip()
            if not title:
                continue
            _lic = str(it.get("share_license_status") or "").strip()
            _row = {
                "title": title[:200],
                "url": it.get("url") or "https://www.clevelandart.org/",
                "snippet": (f"{it.get('tombstone','')} · 授权 {_lic}"
                            "（克利夫兰艺术博物馆）")[:300],
                "source": "cleveland",
                "score": rank_score(0.85, _rk),
            }
            # 图片字段：请求已带 has_image=1，这里把 images.web.url 一并带上
            # （web 尺寸适合直接展示，full/print 留给需要原图的场景）。
            _img = str((((it.get("images") or {}).get("web") or {}).get("url")) or "").strip()
            if _img:
                _row["image_url"] = _img
                if _lic:
                    _row["image_license"] = _lic
            results.append(_row)
        return results
    return _engine


# ── 影视/动漫/音乐 ────────────────────────────────────────────────────────────

def _build_tvmaze_engine(spec: dict[str, Any]) -> Any:
    """TVMaze 电视剧元数据（免认证，补 film_search 的剧集维度）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        url = f"https://api.tvmaze.com/search/shows?q={urllib.parse.quote(q)}"
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"TVMaze 失败: {e}")
            return []
        results = []
        for _rk, row in enumerate((data or [])[:n]):
            if not isinstance(row, dict):
                continue
            s = row.get("show") or {}
            name = (s.get("name") or "").strip()
            if not name:
                continue
            genres = ", ".join(s.get("genres") or [])
            results.append({
                "title": f"{name}（{s.get('premiered','')[:4]}）"[:200],
                "url": s.get("url") or "",
                "snippet": (f"{s.get('language','')} · {genres} · "
                            f"评分 {((s.get('rating') or {}).get('average')) or '-'}"
                            "（TVMaze）")[:300],
                "source": "tvmaze",
                "score": rank_score(0.8, _rk),
                "published_at": s.get("premiered") or "",
            })
        return results
    return _engine


def _build_jikan_engine(spec: dict[str, Any]) -> Any:
    """Jikan（MyAnimeList 非官方 API）动漫元数据。"""
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        url = f"https://api.jikan.moe/v4/anime?q={urllib.parse.quote(q)}&limit={n}"
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Jikan 失败: {e}")
            return []
        results = []
        for _rk, a in enumerate((data.get("data") or [])[:n]):
            if not isinstance(a, dict):
                continue
            title = a.get("title") or a.get("title_japanese") or ""
            if not title:
                continue
            results.append({
                "title": title[:200],
                "url": a.get("url") or "",
                "snippet": (f"{a.get('type','')} · {a.get('episodes','') or '-'} 集 · "
                            f"评分 {a.get('score') or '-'} · {a.get('status','')}"
                            "（MyAnimeList）")[:300],
                "source": "jikan",
                "score": rank_score(0.8, _rk),
            })
        return results
    return _engine


def _build_deezer_engine(spec: dict[str, Any]) -> Any:
    """Deezer 音乐艺人/专辑（免认证）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        url = f"https://api.deezer.com/search/artist?q={urllib.parse.quote(q)}&limit={n}"
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"Deezer 失败: {e}")
            return []
        results = []
        for _rk, a in enumerate((data.get("data") or [])[:n]):
            if not isinstance(a, dict):
                continue
            name = (a.get("name") or "").strip()
            if not name:
                continue
            results.append({
                "title": f"{name}（音乐人）"[:200],
                "url": a.get("link") or "",
                "snippet": (f"Deezer 音乐人 · 专辑 {a.get('nb_album','')} · "
                            f"粉丝 {a.get('nb_fan','')}")[:300],
                "source": "deezer",
                "score": rank_score(0.8, _rk),
            })
        return results
    return _engine


def _build_listenbrainz_engine(spec: dict[str, Any]) -> Any:
    """ListenBrainz 收听趋势（社区收听统计，补 MusicBrainz 元数据的动态维度）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip().lower()
        url = "https://api.listenbrainz.org/1/stats/sitewide/artists?range=week"
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"ListenBrainz 失败: {e}")
            return []
        arts = ((data.get("payload") or {}).get("artists")) or []
        terms = [t for t in _terms(q) if len(t) > 2]
        rows = []
        for a in arts:
            if not isinstance(a, dict):
                continue
            nm = a.get("artist_name") or ""
            rows.append((1 if (terms and any(t in nm.lower() for t in terms)) else 0, a, nm))
        rows.sort(key=lambda x: -x[0])
        results = []
        for _rk, (_h, a, nm) in enumerate(rows[:n]):
            if not nm:
                continue
            results.append({
                "title": f"{nm}（本周收听榜）"[:200],
                "url": f"https://listenbrainz.org/artist/{a.get('artist_mbid','')}",
                "snippet": f"ListenBrainz 全站本周收听 {a.get('listen_count','')} 次"[:300],
                "source": "listenbrainz",
                "score": rank_score(0.75, _rk),
            })
        return results
    return _engine


# ── 法律/图书馆/机构 ──────────────────────────────────────────────────────────

def _build_egov_law_engine(spec: dict[str, Any]) -> Any:
    """日本 e-Gov 法令（官方全文，免认证 XML）。"""
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        # lawlists 全量返回法令名列表 XML（~2.7MB），本地按关键词过滤。
        # 该端点无服务端检索参数且体积大，加进程内缓存（TTL 1h）避免每次查询
        # 都重新拉取——实测单次 3.5s，缓存后 <10ms。
        global _EGOV_ENTRIES, _EGOV_FETCHED_AT
        now = time.time()
        if _EGOV_ENTRIES is None or (now - _EGOV_FETCHED_AT) > 3600:
            try:
                xml = _text("https://laws.e-gov.go.jp/api/1/lawlists/1", to,
                            spec.get("_name", ""), "application/xml")
            except Exception as e:
                logger.warning(f"e-Gov 失败: {e}")
                return []
            _EGOV_ENTRIES = re.findall(
                r"<LawNameListInfo>(.*?)</LawNameListInfo>", xml, re.S)
            _EGOV_FETCHED_AT = now
        entries = _EGOV_ENTRIES
        # 检索词做 OR + 打分：多词查询（如「日本民法 条文」）里「条文」
        # 不会出现在法令名中，若要求每个词都命中会全数落空（实测踩过）。
        # 同时把「日本/日语」等修饰词剔除——它们指语言而非法令名内容。
        _STOP = {"日本", "日语", "日文", "条文", "法律", "法令", "法条", "japan", "japanese"}
        terms = [t for t in _terms(q) if t and t.lower() not in _STOP]
        # 中日文无空格分词：整串「日本民法」在法令名里查不到（法令名形如
        # 「民法」「民法施行法」）。故对每个词补出 **2 字以上子串** 作为候选，
        # 并对前缀「日本」做剥离，让「日本民法」也能命中「民法」。
        expanded = []
        for t in terms:
            if not t:
                continue
            if t.startswith(("日本", "日本国")):
                t = t[2:] if t.startswith("日本国") else t[2:]
            if t:
                expanded.append(t)
            # 长词切成 2-gram 片段（中文/日文检索的常用退化策略）
            if len(t) >= 4 and not t.isascii():
                for i in range(len(t) - 1):
                    expanded.append(t[i:i + 2])
        terms = list(dict.fromkeys(expanded)) or [q]
        hits = []
        for e in entries:
            mid = re.search(r"<LawId>(.*?)</LawId>", e, re.S)
            mnm = re.search(r"<LawName>(.*?)</LawName>", e, re.S)
            mno = re.search(r"<LawNo>(.*?)</LawNo>", e, re.S)
            if not mnm:
                continue
            name = mnm.group(1).strip()
            blob = f"{name} {mno.group(1) if mno else ''}"
            score = sum(1 for t in terms if t in blob)
            # 命中任一检索词即入选，完全没命中则只保留含「法」的条目做保底
            if score or any(t in blob for t in terms):
                hits.append((score, name, mid.group(1).strip() if mid else "",
                             mno.group(1).strip() if mno else ""))
        hits.sort(key=lambda x: -x[0])
        results = []
        for _rk, (_s, name, lid, lno) in enumerate(hits[:n]):
            _row = {
                "title": name[:200],
                "url": f"https://laws.e-gov.go.jp/law/{lid}" if lid else "https://laws.e-gov.go.jp/",
                "snippet": f"日本法令 · {lno}（e-Gov 法令検索）"[:300],
                "source": "egov_law",
                "score": rank_score(0.85, _rk),
            }
            # 源内全文直出：`/law/{id}` 是 JS 空壳页（实测去标签后只剩
            # 「e-Gov 法令検索」几个字，拿不到条文），而 lawdata 端点返回
            # 该法令的官方全文 XML。所以把**可确定性取正文**的 URL 单独带上，
            # 交给下游取数建议优先使用，避免为了正文去走浏览器渲染那几级。
            if lid:
                _row["full_text_url"] = f"https://laws.e-gov.go.jp/api/1/lawdata/{lid}"
            results.append(_row)
        return results
    return _engine


def _build_k10plus_engine(spec: dict[str, Any]) -> Any:
    """K10plus SRU 联合目录（德国最大，SRU 标准协议）。

    SRU 检索在 k10plus 上较慢（实测常 5-15s），超时给足。
    """
    timeout = spec.get("timeout", 30)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        url = ("https://sru.k10plus.de/gvk?version=1.1&operation=searchRetrieve"
               f"&query={urllib.parse.quote('pica.all=' + q)}"
               f"&maximumRecords={max(n, 1)}&recordSchema=dc")
        try:
            xml = _text(url, to, spec.get("_name", ""), "application/xml")
        except Exception as e:
            logger.warning(f"K10plus 失败: {e}")
            return []
        # SRU 响应的 record 可能带命名空间前缀（zs:record），两种都收
        recs = re.findall(r"<(?:\w+:)?record[^>]*>(.*?)</(?:\w+:)?record>", xml, re.S)
        if not recs:
            # 退一步：按 numberOfRecords 判空，避免把「确实无结果」误报为失败
            recs = []
        results = []
        for _rk, r in enumerate(recs[:n]):
            # OAI-DC 的元素带 inline xmlns 属性（<dc:title xmlns:dc="...">），
            # 正则必须容忍标签属性，否则全部匹配失败（实测踩过）。
            t = re.search(r"<dc:title\b[^>]*>(.*?)</dc:title>", r, re.S)
            c = re.search(r"<dc:(?:creator|contributor)\b[^>]*>(.*?)</dc:(?:creator|contributor)>", r, re.S)
            d = re.search(r"<dc:date\b[^>]*>(.*?)</dc:date>", r, re.S)
            i = re.search(r"<dc:identifier\b[^>]*>(.*?)</dc:identifier>", r, re.S)
            if not t:
                continue
            title = re.sub(r"<[^>]+>", "", t.group(1)).strip()
            if not title:
                continue
            results.append({
                "title": title[:200],
                "url": (re.sub(r"<[^>]+>", "", i.group(1)).strip() if i else "https://k10plus.de/"),
                "snippet": (f"{re.sub(r'<[^>]+>','',c.group(1)).strip() if c else ''} · "
                            f"{re.sub(r'<[^>]+>','',d.group(1)).strip() if d else ''}"
                            "（K10plus 联合目录）")[:300],
                "source": "k10plus",
                "score": rank_score(0.8, _rk),
            })
        return results
    return _engine


def _build_ror_engine(spec: dict[str, Any]) -> Any:
    """ROR 研究机构标识（免认证，机构 ID 与域名映射）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        url = f"https://api.ror.org/organizations?query={urllib.parse.quote(q)}"
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"ROR 失败: {e}")
            return []
        results = []
        items = data.get("items")
        if not isinstance(items, list):
            items = []
        for _rk, o in enumerate(items[:n]):
            if not isinstance(o, dict):
                continue
            # ROR v2 的机构名在 names[] 里，主名标 types 含 ror_display；
            # 顶层 name 字段已不再返回（实测为 None）。
            name = ""
            names = o.get("names") or []
            for nm in names:
                if not isinstance(nm, dict):
                    continue
                types = nm.get("types") or []
                if "ror_display" in types:
                    name = str(nm.get("value") or "").strip()
                    break
            if not name:
                for nm in names:
                    if isinstance(nm, dict) and nm.get("value"):
                        name = str(nm["value"]).strip()
                        break
            if not name:
                continue
            # 国家在 locations[].geonames_details.country_name
            country = ""
            for loc in (o.get("locations") or []):
                if isinstance(loc, dict):
                    gd = loc.get("geonames_details") or {}
                    if gd.get("country_name"):
                        country = str(gd["country_name"])
                        break
            domains = ", ".join((o.get("domains") or [])[:2])
            results.append({
                "title": name[:200],
                "url": o.get("id") or "",
                "snippet": (f"ROR {str(o.get('id','')).rsplit('/',1)[-1]} · "
                            f"{country} · {domains}（研究机构唯一标识）")[:300],
                "source": "ror",
                "score": rank_score(0.8, _rk),
            })
        return results
    return _engine


# ── 科学：土壤 / 空间天气 / 卫星 ──────────────────────────────────────────────

def _build_soilgrids_engine(spec: dict[str, Any]) -> Any:
    """ISRIC SoilGrids 全球土壤属性（逐点栅格，免认证）。"""
    timeout = spec.get("timeout", 25)
    PROPS = ["clay", "sand", "silt", "soc", "phh2o", "nitrogen", "cec", "bdod"]

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        # 先地理编码拿坐标（复用 Open-Meteo geocoding，免认证）
        try:
            geo = _json("https://geocoding-api.open-meteo.com/v1/search?name="
                        f"{urllib.parse.quote(q)}&count=1&language=zh", to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"SoilGrids 地理编码失败: {e}")
            return []
        places = geo.get("results") or []
        if not places:
            return []
        lat, lon = places[0].get("latitude"), places[0].get("longitude")
        place = places[0].get("name", "")
        if lat is None or lon is None:
            return []
        results = []
        for _rk, prop in enumerate(PROPS[:n]):
            try:
                d = _json("https://rest.isric.org/soilgrids/v2.0/properties/query?"
                          f"lon={lon}&lat={lat}&property={prop}&depth=0-5cm&value=mean",
                          to, spec.get("_name", ""))
            except Exception:
                continue
            try:
                val = d["properties"]["layers"][0]["depths"][0]["values"]["mean"]
            except (KeyError, IndexError, TypeError):
                continue
            if val is None:
                continue
            results.append({
                "title": f"{place} 土壤属性 · {prop}"[:200],
                "url": f"https://soilgrids.org/?lat={lat}&lon={lon}",
                "snippet": (f"{place}（{round(lat,3)},{round(lon,3)}）0-5cm {prop} 均值 "
                            f"{val}（ISRIC SoilGrids）")[:300],
                "source": "soilgrids",
                "score": rank_score(0.8, _rk),
            })
        return results
    return _engine


def _build_noaa_swpc_engine(spec: dict[str, Any]) -> Any:
    """NOAA SWPC 空间天气（Kp 指数 + 太阳活动区，官方免认证）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip().lower()
        results = []
        # 1) 行星 Kp 指数（最近若干条）
        try:
            kp = _json("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
                       to, spec.get("_name", ""))
            if isinstance(kp, list) and len(kp) > 1:
                for _rk, row in enumerate(list(reversed(kp[1:]))[:max(1, n // 2)]):
                    if not isinstance(row, list) or len(row) < 3:
                        continue
                    results.append({
                        "title": f"行星 Kp 指数 {row[1]}（{str(row[0])[:16]}）"[:200],
                        "url": "https://www.swpc.noaa.gov/products/planetary-k-index",
                        "snippet": f"NOAA SWPC 地磁活动指数 Kp={row[1]} · a_running={row[2]}"[:300],
                        "source": "noaa_swpc",
                        "score": rank_score(0.85, len(results)),
                    })
        except Exception as e:
            logger.warning(f"SWPC Kp 失败: {e}")
        # 2) 太阳活动区（黑子/耀斑）
        try:
            regs = _json("https://services.swpc.noaa.gov/json/solar_regions.json",
                         to, spec.get("_name", ""))
            if isinstance(regs, list):
                for reg in list(reversed(regs))[:max(1, n - len(results))]:
                    if not isinstance(reg, dict):
                        continue
                    results.append({
                        "title": f"太阳活动区 {reg.get('region','')}（{reg.get('location','')}）"[:200],
                        "url": "https://www.swpc.noaa.gov/products/solar-regions",
                        "snippet": (f"NOAA SWPC 太阳活动区 · X 级耀斑 {reg.get('x_xray_events',0)} · "
                                    f"M 级 {reg.get('m_flares',0)}")[:300],
                        "source": "noaa_swpc",
                        "score": rank_score(0.8, len(results)),
                    })
        except Exception as e:
            logger.warning(f"SWPC regions 失败: {e}")
        return results[:n]
    return _engine


def _build_satnogs_engine(spec: dict[str, Any]) -> Any:
    """SatNOGS 卫星目录（免认证，NORAD ID 与发射信息）。"""
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        url = f"https://db.satnogs.org/api/satellites/?format=json&search={urllib.parse.quote(q)}&limit={n}"
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"SatNOGS 失败: {e}")
            return []
        if not isinstance(data, list):
            return []
        results = []
        for _rk, s in enumerate(data[:n]):
            if not isinstance(s, dict):
                continue
            name = (s.get("name") or "").strip()
            if not name:
                continue
            results.append({
                "title": f"{name}（NORAD {s.get('norad_cat_id','')}）"[:200],
                "url": f"https://db.satnogs.org/satellite/{s.get('norad_cat_id','')}",
                "snippet": (f"卫星状态 {s.get('status','')} · 发射 {s.get('launched','')}"
                            "（SatNOGS DB）")[:300],
                "source": "satnogs",
                "score": rank_score(0.8, _rk),
                "published_at": s.get("launched") or "",
            })
        return results
    return _engine


def _build_tle_mirror_engine(spec: dict[str, Any]) -> Any:
    """TLE 两行根数镜像（Celestrak 不可达时的替代，第三方非官方）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        url = f"https://tle.ivanstanojevic.me/api/tle?search={urllib.parse.quote(q)}"
        try:
            data = _json(url, to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"TLE 镜像失败: {e}")
            return []
        members = data.get("member") or []
        results = []
        for _rk, m in enumerate(members[:n]):
            if not isinstance(m, dict):
                continue
            name = (m.get("name") or "").strip()
            if not name:
                continue
            line1 = m.get("line1") or ""
            results.append({
                "title": f"{name}（TLE 轨道根数）"[:200],
                "url": f"https://tle.ivanstanojevic.me/api/tle/{m.get('satelliteId','')}",
                "snippet": (f"NORAD {m.get('satelliteId','')} · {str(line1)[:60]}"
                            "（第三方 TLE 镜像，非官方 SLA）")[:300],
                "source": "tle_mirror",
                "score": rank_score(0.75, _rk),
            })
        return results
    return _engine


# 常见车企中文名 → vPIC 英文厂商名（vPIC 无中文索引，中文问法需先归一）
_VPIC_ZH = {
    "特斯拉": "TESLA", "比亚迪": "BYD", "蔚来": "NIO", "小鹏": "XPENG",
    "理想": "LI AUTO", "吉利": "GEELY", "长城": "GREAT WALL", "奇瑞": "CHERY",
    "长安": "CHANGAN", "上汽": "SAIC", "一汽": "FAW", "广汽": "GAC",
    "东风": "DONGFENG", "北汽": "BAIC", "五菱": "WULING", "红旗": "HONGQI",
    "丰田": "TOYOTA", "本田": "HONDA", "日产": "NISSAN", "马自达": "MAZDA",
    "大众": "VOLKSWAGEN", "奥迪": "AUDI", "宝马": "BMW", "奔驰": "MERCEDES-BENZ",
    "保时捷": "PORSCHE", "福特": "FORD", "通用": "GENERAL MOTORS",
    "别克": "BUICK", "雪佛兰": "CHEVROLET", "凯迪拉克": "CADILLAC",
    "现代": "HYUNDAI", "起亚": "KIA", "沃尔沃": "VOLVO", "路虎": "LAND ROVER",
    "捷豹": "JAGUAR", "标致": "PEUGEOT", "雪铁龙": "CITROEN", "雷诺": "RENAULT",
    "菲亚特": "FIAT", "法拉利": "FERRARI", "兰博基尼": "LAMBORGHINI",
    "玛莎拉蒂": "MASERATI", "宾利": "BENTLEY", "劳斯莱斯": "ROLLS-ROYCE",
    "阿斯顿马丁": "ASTON MARTIN", "三菱": "MITSUBISHI", "铃木": "SUZUKI",
    "斯巴鲁": "SUBARU", "雷克萨斯": "LEXUS", "英菲尼迪": "INFINITI",
    "讴歌": "ACURA", "吉普": "JEEP", "道奇": "DODGE", "克莱斯勒": "CHRYSLER",
    "斯柯达": "SKODA", "西雅特": "SEAT", "欧宝": "OPEL", "依维柯": "IVECO",
}


def _build_nhtsa_vpic_engine(spec: dict[str, Any]) -> Any:
    """NHTSA vPIC 车辆厂商/车型本体（免认证）。

    vPIC 只有英文厂商名，中文问法先过 _VPIC_ZH 归一（否则「特斯拉 车型」恒空）。
    """
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        try:
            data = _json("https://vpic.nhtsa.dot.gov/api/vehicles/getallmakes?format=json",
                         to, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"NHTSA vPIC 失败: {e}")
            return []
        makes = (data.get("Results") or [])
        # 中文厂商名归一为英文；再去掉「车型/厂商/汽车」这类检索噪声词
        _NOISE = {"车型", "厂商", "汽车", "牌子", "品牌", "哪个", "什么", "make", "model"}
        norm = q
        for zh, en in _VPIC_ZH.items():
            if zh in norm:
                norm = norm.replace(zh, en)
                break
        terms = [t for t in _terms(norm.lower()) if len(t) > 1 and t not in _NOISE]
        hits = []
        for m in makes:
            if not isinstance(m, dict):
                continue
            nm = m.get("Make_Name") or ""
            if terms and any(t in nm.lower() for t in terms):
                hits.append((m.get("Make_ID"), nm))
        results = []
        for _rk, (mid, nm) in enumerate(hits[:n]):
            results.append({
                "title": f"{nm}（汽车厂商）"[:200],
                "url": f"https://vpic.nhtsa.dot.gov/api/vehicles/getmodelsformake/{urllib.parse.quote(nm)}?format=json",
                "snippet": f"NHTSA vPIC 厂商 ID {mid} · 含该厂商全部车型"[:300],
                "source": "nhtsa_vpic",
                "score": rank_score(0.8, _rk),
            })
        return results
    return _engine


# ── PubMed E-utilities（两段式：esearch 取 ID → esummary 取详情）──────────────

def _build_pubmed_engine(spec: dict[str, Any]) -> Any:
    """PubMed 生物医学文献（NCBI E-utilities，免 key）。

    ## 为什么需要自定义 builder 而不是声明式 spec

    旧实现是纯声明式：一次 esearch 拿 `idlist`，再靠 output_map
    `item_title/item_url/item_summary: pmid` 取字段。它有两处结构性错误，
    合起来让这个标着 ready 的引擎**静默返回 0 条**：

    ① **请求被注入 `format=json` 而 400**。E-utilities 的 `format` 是输出
       格式参数且不接受 json；同一条 URL 加 `&format=json` 实测 400、去掉
       200。根因在 engines_base 把解析提示当查询参数发（已加
       `format_is_query_param` 开关，本引擎声明 false）。
    ② **idlist 是字符串数组**。`_make_field_parser` 对非 dict 条目
       `continue`，所以哪怕请求通了也一条都解析不出来；退一步说，
       esearch 只给 PMID，本就拿不到标题与摘要。

    故本 builder 做两段：esearch 取 PMID → esummary 取 title/pubdate/
    source/DOI。第二段失败时**降级为带 PMID 的最小条目**而非整体返回空
    ——至少让用户能拿到可点的 PubMed 链接。
    """
    timeout = spec.get("timeout", 12)
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    # NCBI 免 key 档限 3 req/s；声明在 spec 上由 http_client 统一节流
    tool = "argo-search"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        eng = spec.get("_name", "local_pubmed")
        # 两段式要打两次网（实测 esearch≈1.4s、esummary≈1.1s，NCBI 本身不慢）。
        # 关键：**第二段必须分到独立预算**。若两段共用同一个 to，esearch 用掉
        # 大半后 esummary 只剩零头，并发场景下必然 timeout，整体降级成「无结果」
        # ——用户看到的是「这个源坏了」，而实际只是预算没切分。按 6:4 切，
        # 每段不低于 3s（低于 3s 时 NCBI 首包都可能收不完）。
        if to > 6.0:
            t1 = max(3.0, min(to * 0.6, to - 3.0))
            t2 = max(3.0, to - t1)
        else:
            t1 = t2 = to
        # 医学语料多为英文；中文查询直接进 esearch 会命中极少，交由
        # recovery/通用源补位（此处不做静默翻译，避免伪造查询语义）。
        want = max(1, min(int(n or 5), 20))
        url1 = (f"{base}/esearch.fcgi?db=pubmed&retmode=json&sort=relevance"
                f"&retmax={want}&tool={tool}&term={urllib.parse.quote(q)}")
        try:
            d1 = _json(url1, t1, eng)
        except Exception as e:
            logger.warning(f"PubMed esearch 失败: {e}")
            return []
        ids = (((d1 or {}).get("esearchresult") or {}).get("idlist")) or []
        ids = [str(i) for i in ids if str(i).strip()][:want]
        if not ids:
            return []

        # 第二段：取详情。失败则降级为 PMID 最小条目（可点链接仍可用）。
        details: dict[str, dict] = {}
        try:
            url2 = (f"{base}/esummary.fcgi?db=pubmed&retmode=json"
                    f"&tool={tool}&id={','.join(ids)}")
            d2 = _json(url2, t2, eng)
            res = (d2 or {}).get("result") or {}
            for pid in ids:
                it = res.get(pid)
                if isinstance(it, dict):
                    details[pid] = it
        except Exception as e:
            logger.warning(f"PubMed esummary 失败（降级为 PMID 条目）: {e}")

        results: list[dict[str, Any]] = []
        for _rk, pid in enumerate(ids):
            it = details.get(pid) or {}
            title = str(it.get("title") or "").strip()
            src = str(it.get("source") or "").strip()
            pubdate = str(it.get("pubdate") or it.get("epubdate") or "").strip()
            # elocationid 形如 "doi: 10.xxxx/yyy"；抽出后可给可验证的出处
            eloc = str(it.get("elocationid") or "")
            doi = ""
            m = re.search(r"10\.\d{4,9}/\S+", eloc)
            if m:
                doi = m.group(0).rstrip(".")
            # 摘要不在 esummary 里（需 efetch），如实给期刊/日期/DOI 而不是编造
            bits = [b for b in (src, pubdate, f"DOI {doi}" if doi else "") if b]
            snippet = " · ".join(bits) or "PubMed 记录（详情见原文）"
            results.append({
                "title": (title or f"PubMed {pid}")[:200],
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                "snippet": snippet[:300],
                "source": eng,
                "score": rank_score(0.9, _rk),
                "published_at": pubdate,
            })
        return results
    return _engine


# ── CORE：开放获取论文全文聚合（core.ac.uk v3，免 key）─────────────────────

_CORE_ERROR_KEYS = ("message", "error", "detail")


def _build_core_engine(spec: dict[str, Any]) -> Any:
    """CORE 开放获取全文（api.core.ac.uk v3，免 key）。

    ## 两个必须写在这里的理由

    ① **限流是 HTTP 200 + 错误封套**。CORE 后端是 Azure Search，被限流时
       返回 `{"message": "Azure search failed with status code: 503 ..."}`
       且 HTTP 状态是 **200**。通用 `_envelope_error` 要求 `Code` 与
       `message` 同时在场（火山/知乎那种封套），这里只有 `message`，
       识别不出来 → 会被 pipeline 当成「这个词没结果」静默吞掉。
       本 builder 显式检查：**响应同时缺 `results` 又带 message/error 时报错**，
       让熔断器拿到失败信号而不是假空结果。
    ② **端点必须带尾斜杠**。`/v3/search/works?q=` 会 301 到
       `/v3/search/works/?q=`，多一次往返且部分客户端不跟随。

    无 `downloadUrl` 的条目退回 CORE 详情页；仍无则用 DOI 兜底。
    """
    timeout = spec.get("timeout", 20)
    endpoint = "https://api.core.ac.uk/v3/search/works/"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        eng = spec.get("_name", "core")
        want = max(1, min(int(n or 5), 20))
        url = f"{endpoint}?q={urllib.parse.quote(q)}&limit={want}"
        try:
            data = _json(url, to, eng)
        except Exception as e:
            logger.warning(f"CORE 请求失败: {e}")
            raise
        # 显式限流/错误封套识别（见文档串①）
        if isinstance(data, dict) and not data.get("results"):
            for k in _CORE_ERROR_KEYS:
                if data.get(k):
                    msg = str(data[k])[:200]
                    logger.warning(f"CORE 上游错误封套: {msg}")
                    raise RuntimeError(f"core upstream error: {msg}")
        items = (data or {}).get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []

        results: list[dict[str, Any]] = []
        for _rk, it in enumerate(items[:want]):
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            abstract = str(it.get("abstract") or "").strip()
            doi = str(it.get("doi") or "").strip()
            dl = str(it.get("downloadUrl") or "").strip()
            year = str(it.get("yearPublished") or "").strip()
            pub = str(it.get("publisher") or "").strip()
            # 出处优先级：可下载全文 > DOI > CORE 详情页（全部可验证）
            if dl:
                url_out = dl
            elif doi and doi.lower() != "none":
                url_out = f"https://doi.org/{doi}"
            else:
                url_out = f"https://core.ac.uk/works/{it.get('id','')}"
            bits = [b for b in (pub, year, f"DOI {doi}" if doi and doi.lower() != 'none' else "") if b]
            if abstract:
                snippet = abstract
            else:
                snippet = " · ".join(bits) or "CORE 开放获取记录"
            results.append({
                "title": title[:200],
                "url": url_out,
                "snippet": snippet[:300],
                "source": eng,
                "score": rank_score(0.85, _rk),
                "published_at": year,
            })
        return results
    return _engine

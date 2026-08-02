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
from typing import Any

from engines_base import safe_search, _run, _resolve, _get_path, _coerce_field

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


# ── FRED 宏观数据引擎 ────────────────────────────────────────────────────────

# 常用宏观指标 → FRED series_id（关键词前缀匹配，命中即拉取）
_FRED_SERIES: dict[str, str] = {
    # 物价
    "cpi": "CPIAUCSL", "通胀": "CPIAUCSL", "消费者物价": "CPIAUCSL", "物价指数": "CPIAUCSL",
    "核心cpi": "CPILFESL", "ppi": "PPIACO", "生产者价格": "PPIACO", "pce": "PCEPI",
    "个人消费支出": "PCEPI",
    # 就业
    "失业率": "UNRATE", "非农": "PAYEMS", "就业": "PAYEMS", "初请": "ICSA", "申请失业金": "ICSA",
    # 利率
    "联邦基金": "FEDFUNDS", "联邦基金利率": "FEDFUNDS", "美联储利率": "FEDFUNDS",
    "十年期": "DGS10", "10年期": "DGS10", "国债收益率": "DGS10", "美债收益率": "DGS10",
    "三十年期": "DGS30", "30年期": "DGS30", "两年期": "DGS2", "2年期": "DGS2",
    "三个月国债": "TB3MS", "3个月国债": "TB3MS", "国库券": "TB3MS",
    "sofr": "SOFR", "隔夜回购": "SOFR",
    # 增长 / 货币
    "gdp": "GDP", "国内生产总值": "GDP",
    "m2": "M2SL", "货币供应": "M2SL", "m1": "M1SL",
    "零售": "RSAFS", "零售销售": "RSAFS",
    "美元指数": "DTWEXBGS", "贸易加权美元": "DTWEXBGS",
}
_FRED_SERIES_FALLBACK = ["CPIAUCSL", "UNRATE", "FEDFUNDS", "DGS10", "GDP", "M2SL"]

# series_id → 展示标签（标题用中文指标名）
_FRED_LABELS: dict[str, str] = {
    "CPIAUCSL": "美国CPI（消费者物价指数）",
    "CPILFESL": "美国核心CPI",
    "PPIACO": "美国PPI（生产者价格指数）",
    "PCEPI": "美国PCE（个人消费支出物价）",
    "UNRATE": "美国失业率",
    "PAYEMS": "美国非农就业",
    "ICSA": "美国初请失业金",
    "FEDFUNDS": "美国联邦基金利率",
    "DGS10": "美国10年期国债收益率",
    "DGS30": "美国30年期国债收益率",
    "DGS2": "美国2年期国债收益率",
    "TB3MS": "美国3个月国库券收益率",
    "SOFR": "SOFR（隔夜融资利率）",
    "GDP": "美国GDP（国内生产总值）",
    "M2SL": "美国M2货币供应",
    "M1SL": "美国M1货币供应",
    "RSAFS": "美国零售销售",
    "DTWEXBGS": "美元指数（贸易加权）",
}


def _build_fred_engine(spec: dict[str, Any]) -> Any:
    """FRED 宏观数据（fredgraph.csv 免认证）。

    按关键词映射 series_id，拉取近 N 个观测值，展示最新值 + 趋势方向。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        # 国家词守卫：FRED 序列均为美国或全球口径，明确指向其他国家时放弃，
        # 避免「中国GDP」被美国序列冒充（由 worldbank 按国家参数接管）
        if is_foreign_macro_query(query):
            return []
        q = query.lower()
        series_id = None
        for kw, sid in _FRED_SERIES.items():
            if kw in q:
                series_id = sid
                break
        if not series_id:
            return []
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "argo-search/2.4 (unified-search@local)"})
            with urllib.request.urlopen(req, timeout=to) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception as e:
            logger.warning(f"FRED 拉取失败 ({series_id}): {e}")
            return []
        rows = []
        for line in text.strip().splitlines()[1:]:
            if "," not in line:
                continue
            date, _, val = line.partition(",")
            val = val.strip()
            if val in ("", "."):
                continue
            try:
                rows.append((date, float(val)))
            except ValueError:
                continue
        if not rows:
            return []
        # 近 N 期各为一条结果（url 带日期锚点防去重合并），RRF 分数累计上浮
        label = _FRED_LABELS.get(series_id, f"FRED {series_id}")
        results = []
        for date, val in rows[-min(n, 5):]:
            results.append({
                "title": f"{label} · {date} = {val:g}",
                "url": f"https://fred.stlouisfed.org/series/{series_id}?obs={date}",
                "snippet": f"FRED {series_id} 最新值 {val:g}（截至 {date}）",
                "source": "fred",
                "score": 0.9,
            })
        # 首条附趋势方向
        if len(results) > 1:
            prev = rows[-2]
            diff = rows[-1][1] - prev[1]
            arrow = "↑" if diff > 0 else ("↓" if diff < 0 else "→")
            results[0]["snippet"] = (f"FRED {series_id} 最新值 {rows[-1][1]:g}（{rows[-1][0]}）"
                                     f" {arrow}{abs(diff):.2f}")
        return results
    return _engine


# ── 汇率引擎（open.er-api.com 免认证） ───────────────────────────────────────

_CURRENCY_ALIASES: dict[str, str] = {
    "人民币": "CNY", "美元": "USD", "欧元": "EUR", "英镑": "GBP", "日元": "JPY",
    "港币": "HKD", "澳元": "AUD", "加元": "CAD", "瑞郎": "CHF", "新西兰元": "NZD",
    "新加坡元": "SGD", "韩元": "KRW", "卢布": "RUB", "印度卢比": "INR",
    "泰铢": "THB", "林吉特": "MYR", "印尼盾": "IDR", "越南盾": "VND",
}

def _build_fx_rate_engine(spec: dict[str, Any]) -> Any:
    """实时汇率（open.er-api.com/v6/latest，免认证）。

    命中"美元兑人民币"类查询：解析两种货币代码，返回最新汇率快照。
    """
    timeout = spec.get("timeout", 10)
    api_url = "https://open.er-api.com/v6/latest/"

    @safe_search
    def _engine(query: str, n: int = 3, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        base, target = _parse_pair(query)
        if not base or not target:
            return []
        try:
            req = urllib.request.Request(api_url + base, headers={"User-Agent": "argo-search/2.4 (unified-search@local)"})
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"汇率接口失败 ({base}): {e}")
            return []
        if data.get("result") != "success":
            return []
        rates = data.get("rates") or {}
        if target not in rates:
            return []
        rate = rates[target]
        date = data.get("time_last_update_utc") or data.get("time_last_update_unix") or ""
        results = [{
            "title": f"1 {base} = {rate:g} {target}",
            "url": f"https://www.exchangerate-api.com/{base.lower()}-{target.lower()}",
            "snippet": f"实时汇率 {base}/{target} = {rate:g}（{date}，open.er-api.com）",
            "source": "fx_rate",
            "score": 0.9,
        }]
        # 反向汇率补充一条
        if rate:
            results.append({
                "title": f"1 {target} = {1 / rate:.4g} {base}",
                "url": f"https://www.exchangerate-api.com/{target.lower()}-{base.lower()}",
                "snippet": f"反向汇率 {target}/{base} = {1 / rate:.4g}（open.er-api.com）",
                "source": "fx_rate",
                "score": 0.8,
            })
        return results

    def _parse_pair(q: str) -> tuple[str, str]:
        import re as _re

        def _match_currency(token: str) -> str:
            """精确匹配别名或代码；失败时前缀匹配（人民币汇率→人民币）。"""
            token = token.strip()
            if not token:
                return ""
            if token in _CURRENCY_ALIASES:
                return _CURRENCY_ALIASES[token]
            if _re.fullmatch(r"[A-Z]{3}", token):
                return token
            # 前缀匹配：token 以某别名开头，或某别名以 token 开头
            for name, code in _CURRENCY_ALIASES.items():
                if token.startswith(name) or name.startswith(token):
                    return code
            return ""

        codes = _re.findall(r"\b([A-Z]{3})\b", q.upper())
        aliases = [c for c in _CURRENCY_ALIASES if c in q]
        base, target = "", ""
        if len(codes) >= 2:
            base, target = codes[0], codes[1]
        elif len(codes) == 1 and aliases:
            if codes[0] in ("CNY", "USD", "EUR"):
                base, target = codes[0], _CURRENCY_ALIASES[aliases[0]]
            else:
                base, target = "CNY", codes[0]
        elif len(aliases) >= 2:
            base, target = _CURRENCY_ALIASES[aliases[0]], _CURRENCY_ALIASES[aliases[1]]
        elif len(aliases) == 1:
            base, target = "CNY", _CURRENCY_ALIASES[aliases[0]]
        # 中文"兑"字方向修正：X兑Y → base=X, target=Y
        if "兑" in q:
            seg = q.split("兑")
            if len(seg) == 2:
                lc = _match_currency(seg[0])
                rc = _match_currency(seg[1])
                if lc and rc:
                    base, target = lc, rc
        return base, target
    return _engine


# ── 世界银行开放数据引擎（宏观指标，免认证） ─────────────────────────────────

# 模块级国家表：worldbank 引擎解析国家、fred 引擎国家词守卫、route 层分流共用
WORLDBANK_COUNTRIES: dict[str, str] = {
    "中国": "CHN", "美国": "USA", "日本": "JPN", "德国": "DEU", "英国": "GBR",
    "法国": "FRA", "印度": "IND", "巴西": "BRA", "俄罗斯": "RUS", "韩国": "KOR",
    "加拿大": "CAN", "澳大利亚": "AUS", "意大利": "ITA", "西班牙": "ESP",
    "世界": "WLD", "欧元区": "EMU", "香港": "HKG", "台湾": "TWN", "新加坡": "SGP",
}


def is_foreign_macro_query(query: str) -> bool:
    """查询是否明确指向非美国国家/地区。

    FRED 序列均为美国或全球口径，遇到「中国GDP」「日本通胀」这类查询时
    应放弃响应（返回 True），由 worldbank 按国家参数接管，避免美国数据冒充。
    """
    for name in WORLDBANK_COUNTRIES:
        if name == "美国":
            continue
        if name in query:
            return True
    return False


def _build_worldbank_engine(spec: dict[str, Any]) -> Any:
    """世界银行开放数据（api.worldbank.org，免认证，300+ 指标）。

    覆盖 GDP/人均GDP/通胀/失业/贸易/人口/利率等国家宏观指标，
    macro_data 域与 FRED 互补：FRED 偏美国，worldbank 覆盖全球。
    """
    timeout = spec.get("timeout", 12)
    # 默认按时间倒序返回最新数据，per_page 直取最近年份；mrnev 参数实测易超时
    api = "https://api.worldbank.org/v2/country/{cc}/indicator/{ind}?format=json&per_page=6"

    _INDICATORS: list[tuple[tuple[str, ...], str, str]] = [
        (("gdp", "国内生产总值", "生产总值", "经济总量"), "NY.GDP.MKTP.CD", "GDP（现价美元）"),
        (("人均gdp", "人均国内生产总值"), "NY.GDP.PCAP.CD", "人均GDP（现价美元）"),
        (("gdp增速", "gdp增长", "经济增长率"), "NY.GDP.MKTP.KD.ZG", "GDP年增长率"),
        (("cpi", "通胀", "通货膨胀", "物价"), "FP.CPI.TOTL.ZG", "CPI通胀率"),
        (("失业", "失业率"), "SL.UEM.TOTL.ZS", "失业率"),
        (("人口",), "SP.POP.TOTL", "总人口"),
        (("贸易", "进出口", "对外贸易"), "NE.TRD.GNFS.ZS", "贸易占GDP比重"),
        (("利率", "贷款利率"), "FR.INR.LEND", "贷款利率"),
        (("经常账户", "经常项目"), "BN.CAB.XOKA.GD.ZS", "经常账户占GDP比重"),
    ]

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        headers = {"User-Agent": "argo-search/2.4 (unified-search@local)"}
        cc, ind, ind_label = _parse(query)
        if not cc or not ind:
            return []
        data = None
        for _a in range(2):
            try:
                req = urllib.request.Request(api.format(cc=cc, ind=ind), headers=headers)
                with urllib.request.urlopen(req, timeout=to) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                break
            except Exception as e:
                logger.warning(f"世界银行数据失败(重试): {e}")
                time.sleep(0.3)
        if data is None:
            return []
        if not isinstance(data, list) or len(data) < 2:
            return []
        rows = data[1]
        if not rows:
            return []
        cname = rows[0].get("country", {}).get("value", cc)
        results = []
        for it in rows:
            year = it.get("date", "")
            val = it.get("value")
            if val is None or year is None:
                continue
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue
            title = _fmt_title(cname, ind_label, year, f, ind)
            results.append({
                "title": title,
                "url": f"https://data.worldbank.org/indicator/{ind}?locations={cc}",
                "snippet": f"{ind_label} | {cname} | 世界银行开放数据 | 数据截至 {year}".strip(),
                "source": "worldbank",
                "score": 0.9,
            })
            if len(results) >= min(n, 5):
                break
        return results

    def _parse(q: str) -> tuple[str, str, str]:
        low = q.lower()
        cc = ""
        for name, code in WORLDBANK_COUNTRIES.items():
            if name in q:
                cc = code
                break
        if not cc:
            return "", "", ""
        ind, ind_label = "", ""
        for keys, code, label in _INDICATORS:
            if any(k in low for k in keys):
                ind, ind_label = code, label
                break
        return cc, ind, ind_label

    def _fmt_title(cname: str, label: str, year: str, val: float, ind: str) -> str:
        if ind == "SP.POP.TOTL":
            return f"{cname} 总人口（{year}）：{val / 1e8:.2f} 亿人"
        if ind == "NY.GDP.MKTP.CD":
            return f"{cname} {label}（{year}）：{val / 1e12:.2f} 万亿美元"
        if ind == "NY.GDP.PCAP.CD":
            return f"{cname} 人均GDP（{year}）：{val / 1e4:.2f} 万美元"
        if ind in ("FP.CPI.TOTL.ZG", "NY.GDP.MKTP.KD.ZG"):
            return f"{cname} {label}（{year}）：{val:+.2f}%"
        if ind == "SL.UEM.TOTL.ZS":
            return f"{cname} 失业率（{year}）：{val:.2f}%"
        if ind in ("NE.TRD.GNFS.ZS", "BN.CAB.XOKA.GD.ZS"):
            return f"{cname} {label}（{year}）：{val:.2f}%"
        if ind == "FR.INR.LEND":
            return f"{cname} 贷款利率（{year}）：{val:.2f}%"
        return f"{cname} {label}（{year}）：{val:,.2f}"
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


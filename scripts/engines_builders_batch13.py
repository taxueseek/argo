#!/usr/bin/env python3
"""批次十三构建器：美股/汇率数据源（2026-10-06）。

  yahoo_finance      Yahoo Finance 代码解析 + 实时行情（search/chart 免认证）
  sec_companyfacts   SEC XBRL companyfacts 年度结构化财务（免认证）
  frankfurter        ECB 官方参考汇率（免认证）

为什么放 batch 而不是 engines_builders_data：data 是登记上限 2923 的
祖父文件，新引擎的下一站按既有惯例是 batch 模块。三源均经实测验证
（health + quality 准入通过）后入库；frankfurter 与 sec_companyfacts
的探针 UA 必须是合法邮箱域（SEC 对 @local 返 403）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any

from engines_base import http_open, safe_search


# ── Yahoo Finance 美股代码解析 + 实时行情（免认证）──────────────────────────────

def _build_yahoo_finance_engine(spec: dict[str, Any]) -> Any:
    """Yahoo Finance：query → 代码/公司解析（search API）→ 实时行情（chart API）。

    2026-10-06 接入。两个端点均免认证免 crumb（实测本机 search ~0.5s、
    chart ~0.3s）。search 解决「这家公司美股代码是什么/哪个交易所」，
    chart 补实时价与涨跌幅——finviz 是筛选器不带实时价，本引擎补上。
    """
    timeout = spec.get("timeout", 10)
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

    def _get_json(url: str, to: float) -> Any:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua, "Accept": "application/json"})
        with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        try:
            data = _get_json("https://query2.finance.yahoo.com/v1/finance/search?"
                             + up.urlencode({"q": q, "quotesCount": 10, "newsCount": 0}), to)
        except Exception:
            return []
        quotes = [x for x in (data.get("quotes") or [])
                  if isinstance(x, dict) and x.get("symbol")]
        # 只留 EQUITY/ETF：指数/货币/期货对「查行情」意图无用，留着只会
        # 挤占结果位（2026-10-06 单测抓到：INDEX 排第 3 位混进输出）。
        # EQUITY 优先于 ETF，组内按 Yahoo 相关度分（score）降序。
        quotes = [x for x in quotes if x.get("quoteType") in ("EQUITY", "ETF")]
        quotes.sort(key=lambda x: (0 if x.get("quoteType") == "EQUITY" else 1,
                                   -float(x.get("score") or 0)))
        out: list[dict[str, Any]] = []
        for item in quotes[:max(1, min(n, 5))]:
            symbol = str(item.get("symbol", ""))
            name = (item.get("longname") or item.get("shortname") or symbol).strip()
            exch = (item.get("exchDisp") or item.get("fullExchange") or "").strip()
            sector = (item.get("sectorDisp") or "").strip()
            qtype = (item.get("typeDisp") or item.get("quoteType") or "").strip()
            price = item.get("regularMarketPrice")
            if price in (None, ""):
                # search 响应不带价时补一次 chart（crumb-free）
                try:
                    ch = _get_json(f"https://query2.finance.yahoo.com/v8/finance/chart/"
                                   f"{up.quote(symbol)}?range=1d&interval=1d", to)
                    meta = ((ch.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
                    price = meta.get("regularMarketPrice")
                    if price not in (None, "") and not exch:
                        exch = (meta.get("fullExchangeName") or "").strip()
                except Exception:
                    price = None
            snippet_bits = []
            if price not in (None, ""):
                snippet_bits.append(f"最新价 {price}")
            if exch:
                snippet_bits.append(exch)
            if sector:
                snippet_bits.append(sector)
            if qtype:
                snippet_bits.append(qtype)
            out.append({
                "title": f"{name} ({symbol})",
                "url": f"https://finance.yahoo.com/quote/{symbol}/",
                "snippet": " | ".join(snippet_bits)[:300],
                "source": "yahoo_finance",
                "facts": {"ticker": symbol, "price": price, "exchange": exch,
                          "sector": sector},
            })
        return out
    return _engine


# ── SEC EDGAR 结构化财务（XBRL companyfacts，免认证）───────────────────────────

# ticker→CIK 映射表缓存（进程内 + 磁盘 24h）：company_tickers.json ~1MB，
# 每次调用重取是纯浪费；SEC 该文件一天一更，24h TTL 足够。
_SEC_CF_MEM: dict[str, Any] = {"ts": 0.0, "map": {}}
_SEC_CF_TTL_S = 86400.0

# 关键指标 → XBRL 概念名（按优先级；不同公司用的概念不同，逐个回退）
_SEC_CF_METRICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("营收", ("RevenueFromContractWithCustomerExcludingAssessedTax",
              "Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax",
              "SalesRevenueNet")),
    ("净利润", ("NetIncomeLoss", "ProfitLoss")),
    ("经营利润", ("OperatingIncomeLoss",)),
    ("总资产", ("Assets",)),
    ("总负债", ("Liabilities", "LiabilitiesAndStockholdersEquity")),
    ("摊薄EPS", ("EarningsPerShareDiluted",)),
    ("现金及等价物", ("CashAndCashEquivalentsAtCarryingValue",)),
)


def _sec_cf_ticker_map(to: float, state_dir: str = "") -> dict[str, str]:
    """加载 ticker→CIK 映射（内存 → 磁盘 → 网络）。失败返回空 dict。"""
    import os as _os
    now = time.time()
    if _SEC_CF_MEM["map"] and now - _SEC_CF_MEM["ts"] < _SEC_CF_TTL_S:
        return _SEC_CF_MEM["map"]
    disk = _os.path.join(state_dir or "/tmp", "sec_company_tickers.json")
    try:
        if _os.path.exists(disk) and now - _os.path.getmtime(disk) < _SEC_CF_TTL_S:
            raw = json.loads(open(disk, encoding="utf-8").read())
            m = {str(v.get("ticker", "")).upper(): str(v.get("cik_str", ""))
                 for v in raw.values() if isinstance(v, dict)}
            if m:
                _SEC_CF_MEM.update({"ts": now, "map": m})
                return m
    except Exception:
        pass
    try:
        req = urllib.request.Request("https://www.sec.gov/files/company_tickers.json",
                                     headers={"User-Agent": "argo-search/2.9 (research contact@example.com)",
                                              "Accept": "application/json"})
        with http_open(req, timeout=max(to, 10), engine="sec_companyfacts") as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        m = {str(v.get("ticker", "")).upper(): str(v.get("cik_str", ""))
             for v in raw.values() if isinstance(v, dict)}
        if m:
            _SEC_CF_MEM.update({"ts": now, "map": m})
            try:
                _os.makedirs(_os.path.dirname(disk), exist_ok=True)
                with open(disk, "w", encoding="utf-8") as f:
                    json.dump(raw, f)
            except Exception:
                pass
        return m
    except Exception:
        return {}


def _sec_cf_latest_annual(entries: list[dict[str, Any]], unit_key: str) -> tuple[Any, int]:
    """取最近一个 10-K 年度值（fy 优先，其次按 end 日期）。"""
    if not isinstance(entries, list):
        return None, 0
    annual = [e for e in entries
              if isinstance(e, dict) and e.get("form") == "10-K" and e.get("val") is not None]
    if not annual:
        annual = [e for e in entries
                  if isinstance(e, dict) and e.get("val") is not None]
    if not annual:
        return None, 0
    def _fy(e: dict[str, Any]) -> int:
        try:
            return int(e.get("fy") or 0)
        except (TypeError, ValueError):
            return 0
    best = max(annual, key=lambda e: (_fy(e), str(e.get("end") or "")))
    return best.get("val"), _fy(best)


def _build_sec_companyfacts_engine(spec: dict[str, Any]) -> Any:
    """SEC XBRL companyfacts：ticker/公司名 → 年度结构化财务（营收/净利/资产/EPS）。

    2026-10-06 接入。data.sec.gov 免认证。与 sec_edgar（全文检索）互补：
    全文回答「哪些文件提到 X」，本引擎回答「这家公司历年财务数字」。
    """
    timeout = spec.get("timeout", 15)
    ua = "argo-search/2.9 (research contact@example.com)"

    def _ticker_candidates(query: str) -> list[str]:
        q = (query or "").strip()
        # 修饰语剥离子（营收/利润类词不是 ticker）
        q2 = re.sub(r"(?i)\b(us|stock|ticker|financials?|fundamentals?|annual|"
                    r"revenue|earnings|income|eps|balance|cash\s*flow|"
                    r"营收|净利润|利润|财务|财务数据|年报|美股|股票|公司)\b", " ", q)
        cands: list[str] = []
        for raw in re.findall(r"[A-Za-z][A-Za-z.]{0,9}", q2):
            t = raw.upper()
            if 1 <= len(t) <= 6 and t not in cands:
                cands.append(t)
        for raw in re.findall(r"[A-Za-z][A-Za-z.]{0,9}", q):
            t = raw.upper()
            if 1 <= len(t) <= 6 and t not in cands:
                cands.append(t)
        return cands[:8]

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        cands = _ticker_candidates(query)
        if not cands:
            return []  # 纯中文公司名无 ASCII 线索：诚实空，fallback 接手
        tmap = _sec_cf_ticker_map(to)
        cik = next((tmap[t] for t in cands if t in tmap), "")
        if not cik:
            return []
        try:
            req = urllib.request.Request(
                f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json",
                headers={"User-Agent": ua, "Accept": "application/json"})
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []
        name = str(data.get("entityName") or cands[0])
        us_gaap = (data.get("facts") or {}).get("us-gaap") or {}
        parts: list[str] = []
        facts: dict[str, Any] = {}
        fy_seen = 0
        for label, concepts in _SEC_CF_METRICS:
            for concept in concepts:
                node = us_gaap.get(concept)
                if not isinstance(node, dict):
                    continue
                units = node.get("units") or {}
                unit_key = "USD" if "USD" in units else ("USD/shares" if "USD/shares" in units
                                                        else next(iter(units), ""))
                if not unit_key:
                    continue
                val, fy = _sec_cf_latest_annual(units.get(unit_key) or [], unit_key)
                if val is None:
                    continue
                fy_seen = fy_seen or fy
                unit_label = "美元" if unit_key == "USD" else ("美元/股" if unit_key == "USD/shares" else unit_key)
                human = f"{val/1e8:.1f}亿" if unit_key == "USD" and abs(val) >= 1e8 else str(val)
                parts.append(f"{label} {human}{unit_label if unit_key != 'USD' else ''}")
                facts[label] = val
                break
        if not parts:
            return []
        ticker = next((t for t in cands if t in tmap), cands[0])
        url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
               f"&CIK={int(cik):010d}&type=10-K&dateb=&owner=include&count=40")
        return [{
            "title": f"{name} ({ticker}) 年度财务 FY{fy_seen}" if fy_seen else f"{name} ({ticker}) 年度财务",
            "url": url,
            "snippet": (" | ".join(parts))[:300],
            "source": "sec_companyfacts",
            "facts": {"ticker": ticker, "cik": cik, "fy": fy_seen, **facts},
        }]
    return _engine


# ── Frankfurter（ECB 官方参考汇率，免认证）────────────────────────────────────

# 查询词 → ISO 代码（覆盖主要币种中英文）
_FRANKFURTER_CURRENCIES: dict[str, str] = {
    "美元": "USD", "美金": "USD", "usd": "USD", "dollar": "USD",
    "人民币": "CNY", "rmb": "CNY", "cny": "CNY", "yuan": "CNY", "元": "CNY",
    "欧元": "EUR", "eur": "EUR", "euro": "EUR",
    "日元": "JPY", "日圆": "JPY", "jpy": "JPY", "yen": "JPY",
    "英镑": "GBP", "gbp": "GBP", "pound": "GBP",
    "港币": "HKD", "港元": "HKD", "hkd": "HKD",
    "韩元": "KRW", "krw": "KRW", "won": "KRW",
    "加元": "CAD", "cad": "CAD", "澳元": "AUD", "aud": "AUD",
    "瑞士法郎": "CHF", "瑞郎": "CHF", "chf": "CHF",
    "新加坡元": "SGD", "sgd": "SGD", "新币": "SGD",
    "泰铢": "THB", "thb": "THB", "卢布": "RUB", "rub": "RUB",
    "印度卢比": "INR", "inr": "INR", "台币": "TWD", "新台币": "TWD", "twd": "TWD",
    "巴西雷亚尔": "BRL", "brl": "BRL", "墨西哥比索": "MXN", "mxn": "MXN",
}


def _build_frankfurter_engine(spec: dict[str, Any]) -> Any:
    """Frankfurter：查询内币种解析 → ECB 官方参考汇率（api.frankfurter.dev）。

    2026-10-06 接入。与 fx_rate（open.er-api.com 聚合源）互补：ECB 官方
    参考价，用于宏观/汇率类查询的权威口径。查询里没有两个币种时诚实空
    （不猜 base），由 fx_rate/anysearch 接手。
    """
    timeout = spec.get("timeout", 8)

    def _currencies(query: str) -> list[str]:
        q = (query or "").lower()
        found: list[str] = []
        for key, code in _FRANKFURTER_CURRENCIES.items():
            if key in q and code not in found:
                found.append(code)
        for code in re.findall(r"\b([A-Z]{3})\b", query or ""):
            if code in _FRANKFURTER_CURRENCIES.values() and code not in found:
                found.append(code)
        return found

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        codes = _currencies(query)
        if len(codes) < 2:
            return []
        base, symbols = codes[0], codes[1:1 + max(1, min(n, 5))]
        url = ("https://api.frankfurter.dev/v1/latest?"
               + up.urlencode({"base": base, "symbols": ",".join(symbols)}))
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "argo-search/2.9 (unified-search@local)",
                "Accept": "application/json"})
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []
        rates = data.get("rates") or {}
        date = str(data.get("date") or "")
        out: list[dict[str, Any]] = []
        for sym, val in list(rates.items())[:max(1, n)]:
            try:
                rate = float(val)
            except (TypeError, ValueError):
                continue
            out.append({
                "title": f"1 {base} = {rate:g} {sym}（ECB 参考价 {date}）",
                "url": "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/"
                       "euro_reference_exchange_rates/html/index.en.html",
                "snippet": f"欧洲央行参考汇率 | 基准 {base} | 报价 {sym} | 日期 {date}",
                "source": "frankfurter",
                "facts": {"base": base, "symbol": sym, "rate": rate, "date": date},
            })
        return out
    return _engine

#!/usr/bin/env python3
"""批次十四构建器：港股披露 / 商品期货 / 人民币中间价 / 链上 TVL（2026-10-21）。

  hkex        港交所披露易（HKEXnews）公告检索：代码/公司名解析 + 标题关键词
  shfe        上海期货交易所日行情（铜/铝/黄金/螺纹钢等，按品种过滤）
  cfets       中国外汇交易中心人民币中间价（25 个币种，官方口径）
  defillama   DeFiLlama 协议 TVL（链上资金规模/24h 与 7d 变化）

四个源都是免 key 的官方/一手接口，补的是既有覆盖的结构性空白：
港股公告（A 股有巨潮、美股有 SEC，港股此前没有）、商品期货（此前零源）、
人民币中间价（此前只有 ECB/聚合汇率，没有在岸官方价）、链上 TVL（此前只有
CoinGecko 币价，没有协议资金规模）。

共同约束：
  - 上游都不是「关键词全文检索」，而是「按标的枚举」——查询先做标的解析
    （代码/品种/币种/协议名），解析不出来就诚实返回空，不猜、不拿全量
    数据冒充结果。
  - 全部走 http_open（失败现场归因），异常由 safe_search 兜底成空结果。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from engines_http import UA
from engines_base import http_open, safe_search

_UA = UA
_CST = timezone(timedelta(hours=8))   # 三个中国市场源都按北京时间取「今天」


def _today_cst() -> datetime:
    return datetime.now(_CST)


def _json_open(url: str, timeout: float, engine: str,
               headers: dict[str, str] | None = None) -> Any:
    hdr = {"User-Agent": _UA, "Accept": "application/json"}
    hdr.update(headers or {})
    req = urllib.request.Request(url, headers=hdr)
    with http_open(req, timeout=timeout, engine=engine) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ── 港交所披露易（HKEXnews）──────────────────────────────────────────────────

# 披露易文档标题是繁体中文， mainland 用户习惯输入简体；不做整表转换
# （引入 OpenCC 太重），只覆盖港股公告里的高频词。未命中的简体词仍可通过
# 代码/公司名解析路径命中，不会整源失效。
_HKEX_S2T_CHARS = str.maketrans({
    "腾": "騰", "讯": "訊", "汇": "匯", "丰": "豐", "团": "團", "亚": "亞",
    "银": "銀", "证": "證", "产": "產", "业": "業", "务": "務", "国": "國",
    "际": "際", "华": "華", "东": "東", "电": "電", "车": "車", "险": "險",
    "药": "藥", "医": "醫", "疗": "療", "网": "網", "络": "絡", "软": "軟",
    "数": "數", "据": "據", "显": "顯", "传": "傳", "铁": "鐵", "钢": "鋼",
    "铝": "鋁", "铜": "銅", "矿": "礦", "黄": "黃", "锂": "鋰", "风": "風",
    "环": "環", "气": "氣", "农": "農", "养": "養", "饲": "飼", "纺": "紡",
    "织": "織", "装": "裝", "递": "遞", "线": "線", "导": "導", "体": "體",
    "制": "製", "筑": "築", "饰": "飾", "轮": "輪", "纸": "紙", "贸": "貿",
    "资": "資", "权": "權", "兴": "興", "发": "發", "长": "長", "龙": "龍",
    "马": "馬", "湾": "灣", "门": "門", "开": "開", "关": "關", "报": "報",
    "总": "總", "结": "結", "构": "構", "变": "變", "调": "調", "库": "庫",
    "检": "檢", "测": "測", "试": "試", "验": "驗", "认": "認", "书": "書",
    "许": "許", "营": "營", "销": "銷", "价": "價", "额": "額", "页": "頁",
    "码": "碼", "视": "視", "频": "頻", "图": "圖", "买": "買", "卖": "賣",
    "货": "貨", "币": "幣", "债": "債", "万": "萬", "亿": "億", "广": "廣",
    "宁": "寧", "辽": "遼", "苏": "蘇", "无": "無", "与": "與", "为": "為",
    "会": "會", "员": "員", "计": "計", "划": "劃", "实": "實", "现": "現",
    "场": "場", "区": "區", "儿": "兒", "妇": "婦", "学": "學", "术": "術",
    "艺": "藝", "乐": "樂", "闻": "聞", "题": "題", "问": "問", "见": "見",
    "观": "觀", "识": "識", "说": "說", "请": "請", "谢": "謝", "让": "讓",
    "对": "對", "应": "應", "该": "該", "个": "個", "这": "這", "时": "時",
    "间": "間", "户": "戶", "决": "決", "议": "議", "注": "註",
})

# 常见公告主题词（简体→繁体）。字表之外的整词映射，覆盖「回购/业绩」这类
# 高频查询；未覆盖的简体生僻词仍可用代码查询命中。
_HKEX_TERMS = {
    "回购": "購回", "回購": "購回", "回购注销": "購回註銷",
    "业绩": "業績", "年报": "年報", "月报表": "月報表", "中期报告": "中期報告",
    "分红": "分紅", "股东": "股東", "股东大会": "股東大會",
    "董事会": "董事會", "收购": "收購", "合并": "合併",
    "私有化": "私有化", "供股": "供股", "配股": "配股",
    "停牌": "停牌", "复牌": "復牌", "招股": "招股",
    "股权": "股權", "债券": "債券", "增持": "增持", "减持": "減持",
}

# 查询里描述「要什么」而非「哪家公司」的词，解析公司名前先剥掉。
# 主题词（回购/业绩/停牌…）不在此列——它们是过滤条件，剥掉就退化成
# 「该公司最新公告」，用户要的特定公告会被淹掉。
_HKEX_NOISE = re.compile(
    r"(?i)(公告|通告|披露|文件|最新|查询|查詢|全部|港股|港交所|披露易|hkex|H股|红筹|"
    r"恒生指数|announcements?|filing|filings?)"
)


def _hkex_s2t(text: str) -> str:
    out = str(text or "")
    # 最长词优先替换：防止「回购」先把「回购注销」前半截换掉而漏掉「注销→註銷」
    for s, t in sorted(_HKEX_TERMS.items(), key=lambda kv: len(kv[0]), reverse=True):
        out = out.replace(s, t)
    return out.translate(_HKEX_S2T_CHARS)


_HKEX_PREFIX_CACHE: dict[str, list[dict[str, Any]]] = {}


def _hkex_prefix(probe: str) -> list[dict[str, Any]]:
    hit = _HKEX_PREFIX_CACHE.get(probe)
    if hit is not None:
        return hit
    url = ("https://www1.hkexnews.hk/search/prefix.do?"
           + urllib.parse.urlencode({"callback": "callback", "lang": "ZH",
                                     "type": "A", "name": probe,
                                     "market": "SEHK"}))
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml",
    })
    with http_open(req, timeout=10, engine="hkex") as resp:
        body = resp.read().decode("utf-8", "replace")
    m = re.search(r"callback\((.*)\)\s*;?\s*$", body, re.S)
    info: list[dict[str, Any]] = []
    if m:
        try:
            info = (json.loads(m.group(1)) or {}).get("stockInfo") or []
        except (json.JSONDecodeError, ValueError):
            info = []
    if len(_HKEX_PREFIX_CACHE) > 200:
        _HKEX_PREFIX_CACHE.clear()
    _HKEX_PREFIX_CACHE[probe] = info
    return info


_HKEX_NON_EQUITY = re.compile(r"(購|沽|牛|熊|證|ETF|基金|債券|票據|優先|票據)")


from concurrent.futures import ThreadPoolExecutor

_TOPIC_TERMS = tuple(sorted(
    frozenset(_HKEX_TERMS) | frozenset(_HKEX_TERMS.values()),
    key=len, reverse=True,
))


def _hkex_resolve(query: str) -> tuple[list[tuple[str, str, str]], str] | None:
    """查询 → ([(stockId, code, name)…], core)。

    code 优先；公司名先剥主题词（「腾讯 回购」→「騰訊」）再做简→繁，然后按
    「原名 → 加控股/股份/集團 → 截短」探测，前 6 个候选并发发（单次 2~3s，
    串行会把整次查询顶到 10s 预算外）。

    加后缀是必需的：前缀接口对两字简称只回窝轮（实测「騰訊」前 6 条全是
    「騰訊中銀…購」），「騰訊控股」才回正股 00700。返回的 core 是命中的
    那截，调用方据此把主题词从查询里扣出来。
    """
    q = (query or "").strip()
    if not q:
        return None
    m = re.search(r"(?<!\d)(\d{5})(?!\d)", q)
    if not m:
        m4 = re.search(r"(?<!\d)(\d{4})(?!\d)", q)
        code = m4.group(1).zfill(5) if m4 else ""
    else:
        code = m.group(1)
    if code:
        rows = [(str(i.get("stockId") or "").strip(), str(i.get("code") or ""),
                 str(i.get("name") or "")) for i in _hkex_prefix(code)]
        rows = [r for r in rows if r[0]]
        return (rows[:2], code) if rows else None

    raw = _HKEX_NOISE.sub(" ", q)
    raw = re.sub(r"\s+", " ", raw).strip()
    full = _hkex_s2t(raw)
    if not full:
        return None
    name_only = full
    for term in _TOPIC_TERMS:
        if term and term in name_only:
            name_only = name_only.replace(term, " ")
    name_only = re.sub(r"\s+", " ", name_only).strip() or full

    candidates: list[tuple[str, str]] = []   # (probe, core)

    def _add(probe: str, core: str) -> None:
        probe = probe.strip()
        if probe and probe not in [c[0] for c in candidates]:
            candidates.append((probe, core))

    for base in (name_only, full):
        for suffix in ("控股", "股份", "集團", "集团", ""):
            _add(base + suffix, base)
    parts = name_only.split()
    for i in range(len(parts) - 1, 0, -1):
        _add(" ".join(parts[:i]), " ".join(parts[:i]))
    for size in (4, 3, 2):
        if len(name_only) > size:
            _add(name_only[:size], name_only[:size])
    candidates = candidates[:4]
    if not candidates:
        return None

    def _probe(item: tuple[str, str]) -> list[tuple[str, str, str]]:
        probe, _core = item
        info = _hkex_prefix(probe)
        rows = [(str(i.get("stockId") or "").strip(), str(i.get("code") or ""),
                 str(i.get("name") or "")) for i in info
                if not _HKEX_NON_EQUITY.search(str(i.get("name") or ""))]
        return [r for r in rows if r[0]]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_probe, candidates))
    for (probe, core), rows in zip(candidates, results):
        if rows:
            rows.sort(key=lambda r: abs(len(r[2]) - len(core)))
            return rows[:2], core
    return None


def _hkex_query(params: dict[str, Any], kw_tokens: list[str], filt: bool,
                to: float, n: int, seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按一组参数拉公告并转成统一结果；filt=True 时按主题词过滤标题。"""
    url = ("https://www1.hkexnews.hk/search/titleSearchServlet.do?"
           + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml",
    })
    with http_open(req, timeout=to, engine="hkex") as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    raw = payload.get("result") or "[]"
    rows = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(rows, list):
        return []
    have = {str(r.get("url") or "") for r in seen}
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = re.sub(r"<[^>]+>", "", str(row.get("TITLE") or "")).strip()
        long_text = re.sub(r"<[^>]+>", "", str(row.get("LONG_TEXT") or "")).strip()
        body = long_text or title
        if filt and kw_tokens and not all(t in body for t in kw_tokens):
            continue
        code = re.sub(r"<[^>]+>", " ", str(row.get("STOCK_CODE") or "")).split()
        name = re.sub(r"<[^>]+>", " ", str(row.get("STOCK_NAME") or "")).split()
        link = str(row.get("FILE_LINK") or "").strip()
        if not link:
            continue
        full = "https://www1.hkexnews.hk" + link
        if full in have:
            continue
        have.add(full)
        code_s = code[0] if code else ""
        name_s = name[0] if name else ""
        when = str(row.get("DATE_TIME") or "").strip()
        published = ""
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})", when)
        if m:
            d, mo, y, hh, mm = m.groups()
            published = f"{y}-{mo}-{d}T{hh}:{mm}:00+08:00"
        out.append({
            "title": f"{code_s} {name_s}｜{body}"[:160],
            "url": full,
            "snippet": (f"{body} · {when} · {row.get('FILE_TYPE') or ''} "
                        f"{row.get('FILE_INFO') or ''}").strip(" ·"),
            "source": "hkex",
            "published_at": published,
            "facts": {"stock_code": code_s, "stock_name": name_s,
                      "news_id": str(row.get("NEWS_ID") or "")},
        })
        if len(seen) + len(out) >= max(1, n):
            break
    return out


def _build_hkex_engine(spec: dict[str, Any]) -> Any:
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []

        hit = None
        try:
            hit = _hkex_resolve(q)
        except Exception:
            hit = None

        # 剩余主题词：公司解析命中时扣掉 probe，否则用整条查询
        base = _hkex_s2t(_HKEX_NOISE.sub(" ", q))
        base = re.sub(r"(?<!\d)\d{4,5}(?!\d)", " ", base)
        base = re.sub(r"\s+", " ", base).strip()
        topic = base
        if hit and hit[1] and hit[1] in base:
            topic = base.replace(hit[1], " ", 1).strip()
        topic = re.sub(r"\s+", " ", topic).strip()

        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", topic))
        targets: list[tuple[str, str, str]] = []
        if hit:
            targets = hit[0]
        elif topic and has_cjk:
            targets = [("-1", "", "")]
        else:
            # 既没有可解析的标的、也没有中文主题词：不猜，交回上游路由
            return []

        kw_tokens = [t for t in re.split(r"\s+", topic) if t] if (has_cjk and hit) else []
        # 公司+主题时只查最匹配的一家（前缀接口对简称会返回多家关联主体，
        # 逐家拉 60 条会把单次查询推到 10s 预算之上）；纯代码/纯公司名查 2 家。
        targets = targets[:1] if kw_tokens else targets[:2]
        out: list[dict[str, Any]] = []
        for sid, _code, _name in targets:
            if len(out) >= max(1, n):
                break
            params: dict[str, Any] = {
                "sortDir": "0", "sortByOptions": "DateTime", "category": "0",
                "market": "SEHK", "documentType": "-1",
                # 公司+主题词查的是「历史上哪份公告讲了这个」，45 天窗口
                # 会漏掉半年报/年报（业绩类公告的间隔本来就长），放宽到 180 天。
                "fromDate": (_today_cst() - timedelta(
                    days=180 if kw_tokens else 45)).strftime("%Y%m%d"),
                "toDate": _today_cst().strftime("%Y%m%d"),
                "title": "", "searchType": "0",
                "t1code": "-2", "t2Gcode": "-2", "t2code": "-2",
                "rowRange": str(max(5, min(int(n) * 3, 30))), "lang": "ZH",
            }
            if sid == "-1":
                params["stockId"] = "-1"
                params["title"] = topic
                params["searchType"] = "1"
            else:
                params["stockId"] = sid
                if kw_tokens:
                    params["rowRange"] = "60"   # 公司+主题：多拉再本地筛
            out.extend(_hkex_query(params, kw_tokens, filt=bool(hit), to=to,
                                    n=n, seen=out))
        return out[:max(1, n)]

    return _engine


# ── 上海期货交易所日行情 ─────────────────────────────────────────────────────

_SHFE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SHFE_TTL = 600.0


def _shfe_daily(engine: str, timeout: float) -> tuple[str, dict[str, Any]] | None:
    """取最近一个可用交易日的全量日行情（周末/节假日自动往前找）。"""
    today = _today_cst()
    for back in range(0, 12):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        key = day.strftime("%Y%m%d")
        hit = _SHFE_CACHE.get(key)
        if hit and time.time() - hit[0] < _SHFE_TTL:
            return key, hit[1]
        url = ("https://www.shfe.com.cn/data/tradedata/future/dailydata/"
               f"kx{key}.dat")
        try:
            data = _json_open(url, timeout, engine)
        except Exception:
            continue
        rows = (data or {}).get("o_curinstrument") if isinstance(data, dict) else None
        # 当日收盘前文件已生成但价格为空的快照（实测 2026-10-21 14:00 的
        # kx20261021.dat：331 行、CLOSEPRICE 全空）不能当结果用，继续往前找。
        if not rows or not any(str(r.get("CLOSEPRICE") or "").strip() for r in rows
                               if isinstance(r, dict)):
            continue
        _SHFE_CACHE[key] = (time.time(), data)
        if len(_SHFE_CACHE) > 8:
            oldest = min(_SHFE_CACHE, key=lambda k: _SHFE_CACHE[k][0])
            _SHFE_CACHE.pop(oldest, None)
        return key, data
    return None


def _fmt_num(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.2f}"


def _build_shfe_engine(spec: dict[str, Any]) -> Any:
    timeout = spec.get("timeout", 20)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        # 「沪铜主力」「螺纹钢 2610」这类查询：剥掉交易所/意图噪声，留品种名
        core = re.sub(
            r"(?i)(上期所|上海期货交易所|期货|主力|连续|合约|行情|价格|报价|今日|最新|"
            r"收盘|结算价|shfe|futures?)", " ", q)
        core = core.replace("沪", " ")
        core = re.sub(r"(?<!\d)\d{4}(?!\d)", " ", core)
        core = re.sub(r"\s+", " ", core).strip().lower()
        if not core:
            return []
        got = _shfe_daily("shfe", to)
        if not got:
            return []
        day, data = got
        rows = data.get("o_curinstrument") or []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            pname = str(row.get("PRODUCTNAME") or "").strip()
            pgroup = str(row.get("PRODUCTGROUPID") or "").strip().lower()
            pid = str(row.get("PRODUCTID") or "").strip().lower()
            if not pname:
                continue
            hay = f"{pname} {pgroup} {pid}".lower()
            if core not in hay and not any(tok in hay for tok in core.split()):
                continue
            close = _fmt_num(row.get("CLOSEPRICE"))
            settle = _fmt_num(row.get("SETTLEMENTPRICE"))
            chg = _fmt_num(row.get("ZD1_CHG"))
            month = str(row.get("DELIVERYMONTH") or "").strip()
            out.append({
                "title": f"{pname} {month} 收 {close} 结算 {settle}（{day}）",
                "url": f"https://www.shfe.com.cn/products/future/{pgroup}/",
                "snippet": (f"上海期货交易所日行情 · {pname} {month} · "
                            f"收盘 {close} · 结算 {settle} · 涨跌 {chg} · "
                            f"成交 {_fmt_num(row.get('VOLUME'))} 手 · "
                            f"持仓 {_fmt_num(row.get('OPENINTEREST'))} 手"),
                "source": "shfe",
                "published_at": f"{day[:4]}-{day[4:6]}-{day[6:]}T15:00:00+08:00",
                "facts": {"product": pname, "month": month, "date": day,
                          "close": close, "settlement": settle},
            })
            if len(out) >= max(1, n):
                break
        return out

    return _engine


# ── 中国外汇交易中心人民币中间价 ─────────────────────────────────────────────

# 币种词 → 披露易口径的货币对（100JPY 表示每 100 日元报价，是 CFETS 惯例）
_CFETS_CCY = {
    "美元": "USD/CNY", "美金": "USD/CNY", "usd": "USD/CNY",
    "欧元": "EUR/CNY", "歐元": "EUR/CNY", "eur": "EUR/CNY",
    "日元": "100JPY/CNY", "日币": "100JPY/CNY", "jpy": "100JPY/CNY",
    "港元": "HKD/CNY", "港币": "HKD/CNY", "港幣": "HKD/CNY", "hkd": "HKD/CNY",
    "英镑": "GBP/CNY", "英鎊": "GBP/CNY", "gbp": "GBP/CNY",
    "澳元": "AUD/CNY", "aud": "AUD/CNY",
    "新西兰元": "NZD/CNY", "nzd": "NZD/CNY",
    "新加坡元": "SGD/CNY", "sgd": "SGD/CNY",
    "瑞士法郎": "CHF/CNY", "chf": "CHF/CNY",
    "加元": "CAD/CNY", "加拿大元": "CAD/CNY", "cad": "CAD/CNY",
    "澳门元": "CNY/MOP", "mop": "CNY/MOP",
    "林吉特": "CNY/MYR", "myr": "CNY/MYR",
    "卢布": "CNY/RUB", "rub": "CNY/RUB",
    "兰特": "CNY/ZAR", "zar": "CNY/ZAR",
    "韩元": "CNY/KRW", "韩币": "CNY/KRW", "krw": "CNY/KRW",
    "迪拉姆": "CNY/AED", "aed": "CNY/AED",
    "里亚尔": "CNY/SAR", "sar": "CNY/SAR",
    "福林": "CNY/HUF", "huf": "CNY/HUF",
    "兹罗提": "CNY/PLN", "pln": "CNY/PLN",
    "丹麦克朗": "CNY/DKK", "dkk": "CNY/DKK",
    "瑞典克朗": "CNY/SEK", "sek": "CNY/SEK",
    "挪威克朗": "CNY/NOK", "nok": "CNY/NOK",
    "里拉": "CNY/TRY", "try": "CNY/TRY",
    "比索": "CNY/MXN", "mxn": "CNY/MXN",
    "泰铢": "CNY/THB", "thb": "CNY/THB",
}


def _cfets_pair(query: str) -> str:
    q = (query or "").lower()
    for word, pair in _CFETS_CCY.items():
        if word in q:
            return pair
    m = re.search(r"\b([a-z]{3})\s*[/\-]\s*(cny|rmb)\b", q)
    if m:
        code = m.group(1).upper()
        for pair in _CFETS_CCY.values():
            if pair.startswith(code + "/"):
                return pair
    m = re.search(r"\b(cny|rmb)\s*[/\-]\s*([a-z]{3})\b", q)
    if m:
        code = m.group(2).upper()
        for pair in _CFETS_CCY.values():
            if pair.endswith("/" + code):
                return pair
    return "USD/CNY"   # 只说「人民币中间价」时按最常用的美元对


def _build_cfets_engine(spec: dict[str, Any]) -> Any:
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        if not (query or "").strip():
            return []
        pair = _cfets_pair(query)
        end = _today_cst()
        start = end - timedelta(days=14)
        url = ("https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew?"
               + urllib.parse.urlencode({
                   "startDate": start.strftime("%Y-%m-%d"),
                   "endDate": end.strftime("%Y-%m-%d"),
                   "currency": pair,
                   "pageNum": 1,
                   "pageSize": max(1, min(int(n), 20)),
               }))
        data = _json_open(url, to, "cfets", headers={
            "Referer": "https://www.chinamoney.com.cn/",
        })
        head = ((data or {}).get("data") or {})
        searchlist = head.get("searchlist") or [pair]
        out: list[dict[str, Any]] = []
        for rec in (data or {}).get("records") or []:
            if not isinstance(rec, dict):
                continue
            vals = rec.get("values") or []
            if not vals:
                continue
            day = str(rec.get("date") or "")
            value = str(vals[0]).strip()
            label = str(searchlist[0]) if searchlist else pair
            out.append({
                "title": f"{label} 人民币中间价 {value}（{day}）",
                "url": "https://www.chinamoney.com.cn/chinese/bkccpr/",
                "snippet": (f"中国外汇交易中心受权公布 · {label} · {day} · "
                            f"中间价 {value}"),
                "source": "cfets",
                "published_at": f"{day}T09:15:00+08:00" if day else "",
                "facts": {"pair": label, "date": day, "mid_price": value},
            })
        return out

    return _engine


# ── DeFiLlama 协议 TVL ─────────────────────────────────────────────────────

_DLLAMA_CACHE: dict[str, Any] = {"ts": 0.0, "rows": []}
_DLLAMA_TTL = 300.0


def _dllama_rows(timeout: float) -> list[dict[str, Any]]:
    now = time.time()
    if _DLLAMA_CACHE["rows"] and now - float(_DLLAMA_CACHE["ts"]) < _DLLAMA_TTL:
        return _DLLAMA_CACHE["rows"]
    data = _json_open("https://api.llama.fi/protocols", max(timeout, 20), "defillama")
    rows = data if isinstance(data, list) else []
    _DLLAMA_CACHE["rows"] = rows
    _DLLAMA_CACHE["ts"] = now
    return rows


def _usd(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    for unit, scale in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(f) >= scale:
            return f"${f / scale:.2f}{unit}"
    return f"${f:.0f}"


def _build_defillama_engine(spec: dict[str, Any]) -> Any:
    timeout = spec.get("timeout", 30)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip().lower()
        if not q:
            return []
        noise = re.compile(
            r"(?i)(defillama|tvl|总锁仓|liquidity|protocol|协议|链上|资金|规模|排名|"
            r"排名前|top|多少|查询|最新)")
        core = noise.sub(" ", q)
        tokens = [t for t in re.split(r"\s+", core) if len(t) >= 2]
        if not tokens:
            return []
        rows = _dllama_rows(to)
        if not rows:
            return []
        scored: list[tuple[int, float, dict[str, Any]]] = []
        for p in rows:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "")
            symbol = str(p.get("symbol") or "")
            category = str(p.get("category") or "")
            chains = p.get("chains") or []
            hay = f"{name} {symbol} {category} {' '.join(map(str, chains[:8]))}".lower()
            score = sum(1 for t in tokens if t in hay)
            if score == 0:
                continue
            try:
                tvl = float(p.get("tvl") or 0)
            except (TypeError, ValueError):
                tvl = 0.0
            scored.append((score, tvl, p))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out: list[dict[str, Any]] = []
        for score, tvl, p in scored[:max(1, n)]:
            name = str(p.get("name") or "")
            symbol = str(p.get("symbol") or "")
            slug = str(p.get("slug") or "")
            cat = str(p.get("category") or "")
            chg1 = p.get("change_1d")
            chg7 = p.get("change_7d")
            chg_txt = ""
            try:
                chg_txt = f"24h {float(chg1):+.2f}%"
            except (TypeError, ValueError):
                pass
            try:
                chg_txt += f" · 7d {float(chg7):+.2f}%"
            except (TypeError, ValueError):
                pass
            desc = re.sub(r"\s+", " ", str(p.get("description") or "")).strip()
            out.append({
                "title": f"{name}（{symbol or '-'}）TVL {_usd(tvl)}"
                         + (f" · {cat}" if cat else ""),
                "url": (f"https://defillama.com/protocol/{slug}" if slug
                        else str(p.get("url") or "https://defillama.com/protocols")),
                "snippet": " · ".join(x for x in (chg_txt, desc[:180]) if x),
                "source": "defillama",
                "facts": {"protocol": name, "tvl_usd": tvl,
                          "change_1d": chg1, "change_7d": chg7},
            })
        return out

    return _engine

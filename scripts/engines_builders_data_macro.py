#!/usr/bin/env python3
"""专用构建器：宏观数据引擎（FRED / 汇率 / 世界银行 / 国家统计局 / Eurostat）

自 engines_builders_data.py 拆分，减少单文件体积；数据表随各自 builder 内聚。
"""

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

from engines_base import (
    safe_search, _run, _resolve, _get_path, _coerce_field, _detect_anti_bot,
)

logger = logging.getLogger("unified_search.engines")

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



# ── 国家统计局数据引擎（data.stats.gov.cn V2 API，免认证） ────────────────────

_NBS_PROVINCES: dict[str, str] = {
    "北京": "110000000000", "天津": "120000000000", "河北": "130000000000",
    "山西": "140000000000", "内蒙古": "150000000000", "辽宁": "210000000000",
    "吉林": "220000000000", "黑龙江": "230000000000", "上海": "310000000000",
    "江苏": "320000000000", "浙江": "330000000000", "安徽": "340000000000",
    "福建": "350000000000", "江西": "360000000000", "山东": "370000000000",
    "河南": "410000000000", "湖北": "420000000000", "湖南": "430000000000",
    "广东": "440000000000", "广西": "450000000000", "海南": "460000000000",
    "重庆": "500000000000", "四川": "510000000000", "贵州": "520000000000",
    "云南": "530000000000", "西藏": "540000000000", "陕西": "610000000000",
    "甘肃": "620000000000", "青海": "630000000000", "宁夏": "640000000000",
    "新疆": "650000000000",
}
_NBS_PROVINCE_NAMES = tuple(_NBS_PROVINCES.keys())

# 指标关键词 → (搜索后缀, 全国搜索词, 显示名匹配子串)。顺序重要：人均类必须先于总量类匹配
_NBS_INDICATORS: list[tuple[tuple[str, ...], str, str, tuple[str, ...]]] = [
    (("人均gdp", "人均生产总值", "人均国内生产总值"), "人均gdp", "人均gdp",
     ("人均地区生产总值", "人均国内生产总值")),
    (("gdp", "生产总值", "国内生产总值", "经济总量"), "gdp", "国内生产总值",
     ("地区生产总值", "国内生产总值")),
    (("cpi", "物价", "居民消费价格", "通货膨胀", "通胀"), "居民消费价格", "居民消费价格",
     ("居民消费价格指数",)),
    (("ppi", "工业生产者", "生产者价格"), "工业生产者价格", "工业生产者价格",
     ("工业生产者出厂价格指数",)),
    (("失业",), "失业", "失业", ("城镇登记失业", "城镇调查失业")),
    (("就业",), "就业", "就业", ("就业人员",)),
    (("人口",), "人口", "人口", ("人口数",)),
    (("固定资产投资", "投资"), "固定资产投资", "固定资产投资", ("固定资产投资",)),
    (("社会消费品零售", "零售"), "社会消费品零售", "社会消费品零售", ("社会消费品零售总额",)),
    (("财政收入",), "财政收入", "财政收入", ("财政收入",)),
    (("进出口", "出口", "外贸"), "进出口", "进出口", ("进出口",)),
    (("m2", "货币供应"), "货币供应", "货币供应", ("货币供应",)),
    (("pmi", "采购经理"), "采购经理", "采购经理", ("采购经理指数",)),
    (("人均可支配", "可支配收入"), "可支配收入", "可支配收入", ("人均可支配收入",)),
    (("出生", "生育"), "出生", "出生", ("出生人口",)),
    (("死亡",), "死亡", "死亡", ("死亡人口",)),
]

# 显示名中的噪声词：指数/拉动/贡献率/环比/季度累计/不变价/构成等派生指标一律降级
_NBS_NOISE = ("指数", "拉动", "贡献", "环比", "当季", "累计", "不变价",
              "支出法", "收入法", "构成", "1978", "1985", "定基", "增速", "增长")

_NBS_ROOT_ID = "69c574ab128a44e595cc0b24502b771b"
_NBS_API = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"


def _build_nbs_stats_engine(spec: dict[str, Any]) -> Any:
    """国家统计局数据（data.stats.gov.cn V2 API，免认证）。

    三步走：搜索定位 cid（含省名才触发分省数据）→ esData 批量取数。
    支持全国/单省/31省排行三类查询，GDP/CPI/PPI/人口/就业/投资等指标。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://data.stats.gov.cn/",
    }

    def _search(kw: str, to: float) -> list[dict]:
        """搜索指标元数据，返回原始条目列表。"""
        url = f"{_NBS_API}/query?search={urllib.parse.quote(kw)}&pagenum=1&pageSize=15"
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"统计局搜索失败: {e}")
            return []
        items = (d.get("data") or {}).get("data") or []
        return [it for it in items if isinstance(it, dict) and it.get("cid")]

    def _fetch(cid: str, indicator_ids: list[str], das: list[dict],
               dts: list[str], to: float) -> list[dict]:
        """esData 批量取数，返回 [{code,name,values:[...]}]。"""
        body = {
            "cid": cid, "indicatorIds": indicator_ids, "das": das,
            "dts": dts, "showType": "2", "rootId": _NBS_ROOT_ID,
        }
        try:
            req = urllib.request.Request(
                f"{_NBS_API}/stream/esData",
                data=json.dumps(body).encode(),
                headers={**_HEADERS, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"统计局取数失败: {e}")
            return []
        data = d.get("data")
        return data if isinstance(data, list) else []

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import datetime as _dt
        to = _timeout or timeout
        q = query.strip()
        low = q.lower()
        if not low:
            return []

        # 1. 省份解析：单省 / 全部31省（各省、排名）/ 全国
        province = ""
        for name in _NBS_PROVINCE_NAMES:
            if name in q:
                province = name
                break
        all_prov = bool(province) and any(k in q for k in ("各省", "31省", "省份", "排名", "排行", "对比", "全部"))
        if not province and any(k in q for k in ("各省", "31省", "省份", "全国各省")):
            all_prov = True

        # 2. 指标解析
        suffix = national = ""
        matches: tuple[str, ...] = ()
        for keys, s, ns, ms in _NBS_INDICATORS:
            if any(k in low for k in keys):
                suffix, national, matches = s, ns, ms
                break
        if not suffix:
            return []

        # 3. 年份解析：指定年或默认最新（用搜索命中条目的年份）
        ym = re.search(r"(20\d{2})\s*年?", q)
        year = ym.group(1) if ym else ""

        # 4. 搜索定位 cid：分省查询必须带省名才能触发分省数据
        if province:
            search_kw = f"{province}{suffix}"
        elif all_prov:
            search_kw = f"北京{suffix}"
        else:
            search_kw = national
        items = _search(search_kw, to)
        if not items:
            return []

        # 5. 从搜索结果选最佳条目：指标名命中 > 无噪声派生词 > 年度 > 地域匹配
        def _score(it: dict) -> int:
            sn = (it.get("show_name") or "").strip()
            s = 0
            if any(m in sn for m in matches):
                s += 100
                if any(m in sn[:8] for m in matches):
                    s += 30
            if any(w in sn for w in _NBS_NOISE):
                s -= 60
            if "(上年=100)" in sn or "(上年同期=100)" in sn:
                s += 20
            if "分省" in (it.get("type_text") or ""):
                s += 15
            if "年度" in (it.get("type_text") or ""):
                s += 20
            elif "季度" in (it.get("type_text") or ""):
                s -= 30
            if province and not all_prov:
                if (it.get("da_name") or "").startswith(province):
                    s += 40
            elif not province and not all_prov:
                if (it.get("da_name") or "") == "全国":
                    s += 40
            return s
        items.sort(key=_score, reverse=True)
        hit = items[0]
        cid = hit["cid"]
        indicator_id = hit["indic_id"]
        show_name = (hit.get("show_name") or "").strip()
        hit_year = (hit.get("dt") or "")[:4]
        if not year:
            year = hit_year

        # 6. 构造 das / dts
        if all_prov:
            das = [{"text": name, "value": code} for name, code in _NBS_PROVINCES.items()]
        elif province:
            das = [{"text": province, "value": _NBS_PROVINCES[province]}]
        else:
            das = [{"text": "全国", "value": "000000000000"}]

        rows = _fetch(cid, [indicator_id], das, [f"{year}YY-{year}YY"], to)
        if not rows and year != hit_year:
            # 指定年份数据未发布时回退到命中条目的年份
            year = hit_year
            rows = _fetch(cid, [indicator_id], das, [f"{year}YY-{year}YY"], to)
        if not rows:
            return []

        # 7. 格式化：31省按值排序逐条输出，单省/全国输出一条
        if all_prov:
            label = f"{year}年各省{show_name}"
        elif province:
            label = f"{province} {year}年 {show_name}"
        else:
            label = f"全国 {year}年 {show_name}"
        results = []
        if all_prov:
            parsed = []
            for it in rows:
                vals = it.get("values") or []
                if not vals:
                    continue
                v = vals[0]
                try:
                    fv = float(v.get("value"))
                except (TypeError, ValueError):
                    continue
                parsed.append((fv, it.get("name", "")))
            parsed.sort(reverse=True)
            du = (rows[0].get("values") or [{}])[0].get("du_name") or ""
            if du == "无":
                du = ""
            for i, (fv, pname) in enumerate(parsed[: min(n, 31)], 1):
                results.append({
                    "title": f"{i}. {pname} {show_name} {fv:,.1f} {du}",
                    "url": "https://data.stats.gov.cn/easyquery.htm",
                    "snippet": f"{label} 排行第{i}名 | 数据来源：国家统计局",
                    "source": "nbs_stats",
                    "score": 0.95,
                })
        else:
            for it in rows:
                vals = it.get("values") or []
                if not vals:
                    continue
                v = vals[0]
                val = v.get("value")
                du = v.get("du_name") or ""
                if du == "无":
                    du = ""
                dt_name = v.get("dt_name") or f"{year}年"
                results.append({
                    "title": f"{label}: {val} {du}".strip(),
                    "url": "https://data.stats.gov.cn/easyquery.htm",
                    "snippet": f"{label} | 数据来源：国家统计局 | 统计周期 {dt_name}",
                    "source": "nbs_stats",
                    "score": 0.95,
                })
        return results[: max(n, 5)]

    return _engine



# ── Eurostat 欧盟统计引擎（macro_data 域） ───────────────────────────────────

_EUROSTAT_GEO: dict[str, str] = {
    "德国": "DE", "法国": "FR", "英国": "UK", "意大利": "IT", "西班牙": "ES",
    "荷兰": "NL", "比利时": "BE", "奥地利": "AT", "爱尔兰": "IE", "葡萄牙": "PT",
    "希腊": "EL", "芬兰": "FI", "瑞典": "SE", "丹麦": "DK", "波兰": "PL",
    "捷克": "CZ", "匈牙利": "HU", "罗马尼亚": "RO", "保加利亚": "BG", "斯洛伐克": "SK",
    "斯洛文尼亚": "SI", "克罗地亚": "HR", "立陶宛": "LT", "拉脱维亚": "LV",
    "爱沙尼亚": "EE", "塞浦路斯": "CY", "卢森堡": "LU", "马耳他": "MT",
    "欧盟": "EU27_2020", "欧元区": "EA20",
}
# 指标关键词 → (数据集, 维度参数, 指标名)。维度均已实测验证
_EUROSTAT_INDICATORS: list[tuple[tuple[str, ...], str, str, str]] = [
    (("人均gdp", "人均生产总值", "人均国内生产总值"), "nama_10_pc", "na_item=B1GQ&unit=CP_EUR_HAB", "人均GDP"),
    (("gdp", "生产总值", "国内生产总值", "经济总量"), "nama_10_gdp", "na_item=B1GQ&unit=CP_MEUR", "GDP"),
    (("失业",), "une_rt_a", "age=Y15-74&sex=T&unit=PC_ACT", "失业率"),
    (("人口",), "demo_pjan", "age=TOTAL&sex=T", "人口"),
]
# unit 编码 → 简短单位标签（未命中时回退 API 原文）
_EUROSTAT_UNIT: dict[str, str] = {
    "CP_MEUR": "百万欧元", "CP_EUR_HAB": "欧元/人", "PC_ACT": "%", "PERSON": "人",
}


def _build_eurostat_engine(spec: dict[str, Any]) -> Any:
    """欧盟统计局 Eurostat 数据（ec.europa.eu SDMX 2.1，免认证）。

    支持 EU 国家/欧盟/欧元区的 GDP、人均GDP、失业率、人口年度查询。
    """
    timeout = spec.get("timeout", 15)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}
    _BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"

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
        low = q.lower()

        country = ""
        for name, code in _EUROSTAT_GEO.items():
            if name in q:
                country = code
                break
        if not country:
            return []

        ds = dims = label = ""
        for keys, d, dm, lb in _EUROSTAT_INDICATORS:
            if any(k in low for k in keys):
                ds, dims, label = d, dm, lb
                break
        if not ds:
            return []

        ym = re.search(r"(20\d{2})", q)
        year = ym.group(1) if ym else ""

        url = f"{_BASE}/{ds}?format=JSON&lang=en&geo={country}&{dims}"
        if year:
            url += f"&time={year}"
        try:
            d = _jget(url, to)
        except Exception as e:
            logger.warning(f"Eurostat 取数失败: {e}")
            return []
        vals = d.get("value") or {}
        if not vals:
            return []
        # 最新观测
        v = list(vals.values())[0]
        dims_meta = d.get("dimension", {})
        if not year:
            # 无年份时取最新观测：value 键为观测索引（按时间升序），映射回时间标签
            t_idx = max(vals.keys(), key=lambda k: int(k) if str(k).isdigit() else 0)
            t_cat = (dims_meta.get("time") or {}).get("category", {})
            t_code = next((c for c, i in (t_cat.get("index") or {}).items()
                           if str(i) == str(t_idx)), "")
            year = (t_cat.get("label") or {}).get(t_code, t_code)[:4]
        # 单位标签：unit 维度 category 的 key 是编码（如 CP_MEUR），先查短标签表再回退 API 标签
        du = ""
        try:
            unit_cat = (dims_meta.get("unit") or {}).get("category", {})
            ucode = next(iter((unit_cat.get("index") or {}).keys()), "")
            du = _EUROSTAT_UNIT.get(ucode) or (unit_cat.get("label") or {}).get(ucode, "")
        except Exception:
            pass
        cname = next((k for k, v in _EUROSTAT_GEO.items() if v == country), country)
        try:
            fv = float(v)
            val_s = f"{fv:,.1f}"
        except (TypeError, ValueError):
            val_s = str(v)
        title = f"{cname} {year}年 {label}: {val_s} {du}".strip()
        results = [{
            "title": title,
            "url": f"https://ec.europa.eu/eurostat/databrowser/view/{ds}",
            "snippet": f"{cname} {label}（{year}）| 数据来源：Eurostat | 数据集 {ds}",
            "source": "eurostat",
            "score": 0.93,
        }]
        return results[: max(n, 3)]

    return _engine




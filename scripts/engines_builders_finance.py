#!/usr/bin/env python3
"""专用构建器：财经、贸易与公司注册（Finviz / Seeking Alpha / 金十 / UN Comtrade / OpenCorporates）

自 engines_builders_data.py 按主题拆出（2026-11-08）。
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

from engine_env import get_env
from engines_base import (
    rank_score,
    safe_search, _detect_anti_bot,
    http_open,
)

logger = logging.getLogger("unified_search.engines")

# ── Finviz 美股筛选引擎（HTML 抓取）─────────────────────────────────────────────

def _build_finviz_engine(spec: dict[str, Any]) -> Any:
    """Finviz 个股快照（finviz.com/stock?t=TICKER）

    从 snapshot 表格提取：公司名、行业、市值、PE、价格、涨跌幅。
    query 视为 ticker（大写）；非纯字母时取首个字母 token 作 ticker。
    （2026-10-28：旧地址 /quote.ashx 全站 301 到 /stock，http_open 不跟随重定向，
    端点随之切换；新页 snapshot 表结构与解析器兼容，实测 AAPL 全字段命中。）
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        m = re.search(r"[A-Za-z]{1,6}", query or "")
        if not m:
            return []
        ticker = m.group(0).upper()
        url = f"https://finviz.com/stock?t={ticker}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        req = urllib.request.Request(url, headers=headers)
        with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
        with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
        with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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

# ── OpenCorporates 全球公司注册 ──────────────────────────────────────────────

def _build_opencorporates_engine(spec: dict[str, Any]) -> Any:
    """OpenCorporates 全球公司注册（api.opencorporates.com/v0.4/companies/search）。

    公司尽调/反欺诈：公司名、注册号、司法辖区、成立日期、状态。
    argo 现有引擎无公司注册数据源，这是尽调类查询的核心空白。
    （2026-10-28：匿名请求已被上游全面拒 401，API 需 api_token；直连实测确认。
    支持 ARGO_OPENCORPORATES_API_TOKEN / OPENCORPORATES_API_TOKEN 双别名，
    spec required_env 已声明，无 token 时被 env 门自动跳过而非报错。）
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        _token = get_env(["ARGO_OPENCORPORATES_API_TOKEN", "OPENCORPORATES_API_TOKEN"]) or ""
        q = query.strip()
        # 去常见修饰语，取核心公司名
        q2 = re.sub(r"(?i)\b(company|corporation|inc|limited|ltd|llc|公司|集团|有限|注册|查一下|查询)\b", " ", q)
        q2 = re.sub(r"\s+", " ", q2).strip() or q
        url = (
            "https://api.opencorporates.com/v0.4/companies/search"
            f"?q={urllib.parse.quote(q2)}&per_page={min(n, 10)}"
            + (f"&api_token={urllib.parse.quote(_token)}" if _token else "")
        )
        headers = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}
        try:
            with http_open(urllib.request.Request(url, headers=headers), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []
        companies = ((data.get("results") or {}).get("companies")) or []
        results: list[dict[str, Any]] = []
        for _rk19, c in enumerate(companies[:n]):
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
                "score": rank_score(0.8, _rk19),
                "company_number": num,
                "jurisdiction": juris,
                "incorporation_date": inc_date,
                "status": status,
            })
        return results
    return _engine


# ── UN Comtrade 双边贸易引擎 ─────────────────────────────────────────────────

# 报告国词表：查询里的国家名 → Comtrade reporterCode。
# 取主要经济体 30+；未收录国家诚实返回空（代码表不硬猜）。
_COMTRADE_REPORTERS = {
    "中国": 156, "美国": 842, "日本": 392, "德国": 276, "韩国": 410,
    "印度": 699, "英国": 826, "法国": 251, "意大利": 380, "荷兰": 528,
    "俄罗斯": 643, "巴西": 76, "澳大利亚": 36, "加拿大": 124, "墨西哥": 484,
    "印尼": 360, "印度尼西亚": 360, "泰国": 764, "越南": 704, "马来西亚": 458,
    "新加坡": 702, "土耳其": 792, "西班牙": 724, "波兰": 616, "瑞士": 756,
    "瑞典": 752, "阿联酋": 784, "沙特": 682, "南非": 710, "阿根廷": 32,
    "智利": 152, "菲律宾": 608, "香港": 344, "挪威": 578, "丹麦": 208,
}


def _build_un_comtrade_engine(spec: dict[str, Any]) -> Any:
    """UN Comtrade 双边商品贸易引擎（preview 端点免 key）。

    查询形态：「<国家> <HS 码> [年份] [出口]」，如「中国 8541」「美国 8542
    2024 出口」。未提供国家或 HS 码时诚实返回空——参数解析不做猜测。
    preview 端点返回代码与数值，不含描述字典（reporter/partner/cmd 的
    Desc 字段为 null），故标题用查询词原文回显、金额取 primaryValue。
    每条结果附 comtradeplus 网页链接可核验同一数据。
    """
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        q = (query or "").strip()
        reporter = None
        reporter_name = ""
        for name, code in _COMTRADE_REPORTERS.items():
            if name in q:
                reporter, reporter_name = code, name
                break
        if reporter is None:
            return []
        year_m = re.search(r"\b(20[0-2]\d)\b", q)
        period = year_m.group(1) if year_m else "2023"
        flow = "X" if ("出口" in q or "export" in q.lower()) else "M"
        flow_name = "出口" if flow == "X" else "进口"
        cmd = ""
        for tok in re.findall(r"\d{4,6}", q):
            if tok == period:
                continue
            cmd = tok
            break
        if not cmd:
            return []
        params = {
            "reporterCode": reporter,
            "period": period,
            "cmdCode": cmd,
            "flowCode": flow,
        }
        url = ("https://comtradeapi.un.org/public/v1/preview/C/A/HS?"
               + up.urlencode(params))
        from engines_base import _http_get_raw
        raw = _http_get_raw(url, {"Accept": "application/json"}, to,
                            engine="un_comtrade")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        rows = data.get("data") or []
        if not rows:
            return []
        rows.sort(key=lambda r: float(r.get("primaryValue") or 0), reverse=True)
        results = []
        for r in rows[:n]:
            partner = r.get("partnerCode")
            value = r.get("primaryValue")
            value_str = f"{float(value):,.0f} USD" if value else "金额未披露"
            # title/url 均带伙伴维度：融合层按 title+url 去重，同一伙伴维度
            # 才算重复；URL 带 PartnerAreas 使每条结果可独立核验
            partner_url = (
                "https://comtradeplus.un.org/TradeFlow?Frequency=A&Classification=HS"
                f"&Commodities={cmd}&Flow={flow}&Reporters={reporter}"
                f"&PartnerAreas={partner}&Period={period}")
            label = "全球合计" if partner in (0, 1) else f"伙伴 {partner}"
            results.append({
                "title": f"{reporter_name} {flow_name} HS{cmd} {period} → {label}",
                "url": partner_url,
                "snippet": value_str + (f" | 净重 {r.get('netWgt')}" if r.get('netWgt') else ""),
                "source": "un_comtrade",
                "score": 0.95 if partner in (0, 1) else 0.75,
                "published_at": f"{period}-12-31",
            })
        return results
    return _engine

#!/usr/bin/env python3
"""专用构建器：中文财经 / 热榜 / 百科"""

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

# ── 同花顺热点引擎 ─────────────────────────────────────────────────────────────

def _build_ths_hot_engine(spec: dict[str, Any]) -> Any:
    """同花顺当日强势股 + 题材归因（独家能力）

    不只告诉你"哪些走强"，还告诉你"为什么走强"——同花顺编辑部人工运营的题材标签。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        from datetime import date as _date
        trade_date = _date.today().strftime("%Y-%m-%d")

        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{trade_date}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "Chrome/117.0.0.0 Safari/537.36"
            )
        }
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read())
            if data.get("errocode", 0) != 0:
                return []
            rows = data.get("data") or []
            results = []
            for r in rows[:n]:
                results.append({
                    "title": f"{r.get('name', '')}({r.get('code', '')}) +{r.get('zhangfu', 0)}%",
                    "url": f"https://quote.eastmoney.com/{r.get('code', '')}.html",
                    "snippet": f"题材: {r.get('reason', '未知')} | 换手{r.get('huanshou', 0)}% | 成交额{r.get('chengjiaoe', 0)/1e8:.1f}亿",
                    "source": "ths_hot",
                })
            return results
        except Exception as e:
            logger.warning(f"同花顺热点引擎失败: {e}")
            return []
    return _engine


# ── 财联社电报引擎 ─────────────────────────────────────────────────────────────

def _build_cls_telegraph_engine(spec: dict[str, Any]) -> Any:
    """财联社电报（全市场实时快讯，v1 API + 本地签名，零 key）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import hashlib
        from datetime import datetime
        to = _timeout or timeout
        params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
                  "last_time": "", "refresh_type": "1", "rn": str(n)}
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
        url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("roll_data", []) or []:
                ts = item.get("ctime")
                t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
                title = item.get("title", "") or item.get("brief", "")
                # 关键词过滤
                if query and query.strip():
                    keywords = query.strip().split()
                    if not any(kw.lower() in (title + item.get("content", "")).lower() for kw in keywords):
                        continue
                results.append({
                    "title": title[:80],
                    "url": "https://www.cls.cn/",
                    "snippet": f"{t} | {(item.get('content', '') or item.get('brief', ''))[:150]}",
                    "source": "cls_telegraph",
                })
            return results[:n]
        except Exception as e:
            logger.warning(f"财联社电报引擎失败: {e}")
            return []
    return _engine


# ── 东财全球资讯引擎 ─────────────────────────────────────────────────────────

def _build_em_global_news_engine(spec: dict[str, Any]) -> Any:
    """东财全球财经资讯（7×24 滚动）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import uuid
        to = _timeout or timeout
        url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        params = {
            "client": "web", "biz": "web_724",
            "fastColumn": "102", "sortEnd": "",
            "pageSize": str(n * 2),  # 多拉一些用于过滤
            "req_trace": str(uuid.uuid4()),
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://kuaixun.eastmoney.com/"}
        try:
            req = urllib.request.Request(url + "?" + "&".join(f"{k}={v}" for k, v in params.items()), headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("fastNewsList", []):
                title = item.get("title", "")
                # 关键词过滤
                if query and query.strip():
                    keywords = query.strip().split()
                    if not any(kw.lower() in (title + item.get("summary", "")).lower() for kw in keywords):
                        continue
                results.append({
                    "title": title[:80],
                    "url": "https://kuaixun.eastmoney.com/",
                    "snippet": f"{item.get('showTime', '')} | {(item.get('summary', '') or '')[:150]}",
                    "source": "em_global_news",
                })
            return results[:n]
        except Exception as e:
            logger.warning(f"东财全球资讯引擎失败: {e}")
            return []
    return _engine



# ── 东财妙想搜索（官方权威信源） ─────────────────────────────────────────────

def _build_em_miaoxiang_engine(spec: dict[str, Any]) -> Any:
    """东财妙想搜索（mkapi2.dfcfs.com/finskillshub，需 EASTMONEY_APIKEY）。

    官方金融信源智能筛选：研报/公告/政策/解读，authorityLevel 权威分级，
    比公开 search-api-web 接口更适合金融资讯场景。
    """
    timeout = spec.get("timeout", 12)
    url = "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import json as _json
        to = _timeout or timeout
        key = os.environ.get("EASTMONEY_APIKEY") or os.environ.get("ARGO_EASTMONEY_APIKEY")
        if not key:
            logger.warning("妙想搜索缺 EASTMONEY_APIKEY")
            return []
        body = _json.dumps({"query": query, "size": min(n + 3, 15)}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "apikey": key,
                "Content-Type": "application/json",
                "User-Agent": "argo-search/2.4 (unified-search@local)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"妙想搜索失败: {e}")
            return []
        items = []
        try:
            items = data["data"]["data"]["llmSearchResponse"]["data"] or []
        except (KeyError, TypeError):
            logger.warning("妙想搜索返回结构异常")
            return []
        results = []
        seen: set[str] = set()
        for it in items:
            title = (it.get("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            content = (it.get("content") or "").strip().replace("\n", " ")
            date = it.get("date") or ""
            authority = it.get("authorityLevel") or ""
            info_type = it.get("informationType") or ""
            parts = [p for p in (date, authority, info_type, content[:200]) if p]
            results.append({
                "title": title[:120],
                "url": it.get("jumpUrl") or "https://eastmoney.com",
                "snippet": " | ".join(parts)[:280],
                "source": "em_miaoxiang",
                "score": 0.85,
            })
            if len(results) >= n:
                break
        return results
    return _engine


# ── 巨潮资讯公告引擎（官方公告） ──────────────────────────────────────────────

def _build_cninfo_engine(spec: dict[str, Any]) -> Any:
    """巨潮资讯网官方公告检索（www.cninfo.com.cn/new/hisAnnouncement/query）。

    A 股上市公司公告第一官方源，覆盖沪深京三所，免认证。
    查询词命中公司名/公告标题关键词，返回标题/日期/PDF 原文链接。
    """
    timeout = spec.get("timeout", 12)
    url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout

        def _search_once(keyword: str) -> list[dict[str, Any]]:
            body = up.urlencode({
                "pageNum": "1",
                "pageSize": str(min(n + 2, 15)),
                "column": "szse",
                "tabName": "fulltext",
                "plate": "", "stock": "", "searchkey": keyword,
                "secid": "", "category": "", "trade": "", "seDate": "",
                "sortName": "", "sortType": "", "isHLtitle": "true",
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=to) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as e:
                logger.warning(f"巨潮公告搜索失败: {e}")
                return []
            items = data.get("announcements") or []
            if not items:
                return []
            results = []
            seen: set[str] = set()
            for it in items:
                raw_title = (it.get("announcementTitle") or "").strip()
                if not raw_title or raw_title in seen:
                    continue
                seen.add(raw_title)
                title = re.sub(r"<[^>]+>", "", raw_title)[:100]
                sec = re.sub(r"<[^>]+>", "", it.get("secName") or "").strip()
                ts = it.get("announcementTime")
                date = time.strftime("%Y-%m-%d", time.localtime(ts / 1000)) if ts else ""
                adjunct = it.get("adjunctUrl") or ""
                pdf_url = "https://static.cninfo.com.cn/" + adjunct if adjunct else "https://www.cninfo.com.cn/"
                results.append({
                    "title": f"{sec} {title}" if sec and sec not in title else title,
                    "url": pdf_url,
                    "snippet": " | ".join(p for p in (date, "公告原文 PDF", "巨潮资讯网官方") if p)[:280],
                    "source": "cninfo",
                    "score": 0.9,
                })
                if len(results) >= n:
                    break
            return results

        # 候选词干：super_search 的查询改写会把原句拼上「贵州茅台 600519 白酒 股票行情」
        # 等扩展词，整句全文搜索命中率反而低。先试整句，0 结果时按分词去 STOP 词重试。
        _STOP = ("公告", "披露", "查询", "什么", "怎么样", "多少", "怎么", "了", "吗",
                 "今日", "最新", "股票", "股价", "行情", "走势", "报价", "分红方案",
                 "利润分配", "每股", "多少钱")
        cands = [query]
        for token in re.split(r"[\s,，、/]+", query):
            t = token.strip()
            if not t:
                continue
            for stop in _STOP:
                t = t.replace(stop, "")
            t = t.strip()
            if 2 <= len(t) <= 20 and t not in cands:
                cands.append(t)
        for c in cands:
            res = _search_once(c)
            if res:
                return res
        return []
    return _engine


# ── 新浪行情引擎（实时行情快照） ─────────────────────────────────────────────

def _build_sina_quote_engine(spec: dict[str, Any]) -> Any:
    """新浪实时行情快照（hq.sinajs.cn + suggest3 代码解析）。

    免认证直连：suggest3.sinajs.cn 把中文名/拼音/代码解析为证券代码，
    再拉 hq.sinajs.cn 实时快照（现价/涨跌/开高低/成交量），适合"茅台股价"类查询。
    """
    timeout = spec.get("timeout", 8)
    suggest_url = "https://suggest3.sinajs.cn/suggest/type=11,12,15,21,31,41&key="
    quote_url = "https://hq.sinajs.cn/list="

    _MARKET_PREFIX = {
        "sh": "沪", "sz": "深", "bj": "北",
        "hk": "港", "us": "美", "hf": "期货",
        "gb_": "外盘", "rt_hk": "港", "znb_": "银行",
    }

    @safe_search
    def _engine(query: str, n: int = 3, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                   "Referer": "https://finance.sina.com.cn/"}
        code = _resolve_code(query, to, headers)
        if not code:
            return []
        symbol = code.split(",")[0] if "," in code else code
        try:
            req = urllib.request.Request(quote_url + symbol, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                text = resp.read().decode("gbk", "replace").strip()
        except Exception as e:
            logger.warning(f"新浪行情失败: {e}")
            return []
        if not text or "=" not in text:
            return []
        var_part = text.split("=", 1)[1].strip().strip('"')
        fields = var_part.split(",")
        if not fields or len(fields) < 4:
            return []
        name = fields[0]
        # A 股字段：0名称 1今开 2昨收 3现价 4最高 5最低 6买一 7卖一 8成交量 9成交额
        if len(fields) >= 10:
            cur, prev = fields[3], fields[2]
            try:
                chg = float(cur) - float(prev) if cur and prev else 0.0
                pct = chg / float(prev) * 100 if prev and float(prev) else 0.0
            except ValueError:
                chg, pct = 0.0, 0.0
            arrow = "↑" if pct > 0 else ("↓" if pct < 0 else "→")
            title = f"{name} {cur} {arrow}{pct:+.2f}%" if cur else f"{name} 行情"
            snip_parts = [
                f"现价 {cur}", f"涨跌 {chg:+.2f} ({pct:+.2f}%)" if chg else "",
                f"今开 {fields[1]}", f"昨收 {prev}",
                f"最高 {fields[4]}", f"最低 {fields[5]}",
                f"成交量 {_fmt_vol(fields[8])}" if fields[8] else "",
            ]
        else:
            title = f"{name} 行情"
            snip_parts = [f"数据 {var_part[:60]}"]
        market = symbol[:2]
        prefix = _MARKET_PREFIX.get(market, "")
        quote_page = "https://finance.sina.com.cn/realstock/company/" + symbol + "/nc.shtml"
        return [{
            "title": title[:80],
            "url": quote_page,
            "snippet": " | ".join(p for p in snip_parts if p)[:200],
            "source": "sina_quote",
            "score": 0.9,
        }]

    def _resolve_code(q: str, to: float, headers: dict) -> str:
        import urllib.parse as up
        # 候选词干：先整句，再逐词试；去掉常见行情后缀词
        _STOP = ("股价", "行情", "股票", "价格", "走势", "最新", "今日", "报价", "查询",
                 "怎么样", "多少", "怎么", "了", "吗", "的", "a股", "港股", "美股")
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
                req = urllib.request.Request(suggest_url + up.quote(c), headers=headers)
                with urllib.request.urlopen(req, timeout=to) as resp:
                    text = resp.read().decode("gbk", "replace")
            except Exception as e:
                logger.warning(f"新浪代码解析失败: {e}")
                continue
            if "suggestvalue=" not in text:
                continue
            val = text.split("suggestvalue=", 1)[1].strip().strip('"')
            # 格式: 名称,类型,代码,符号,拼音,... 取第一个符号
            parts = val.split(",")
            if len(parts) >= 4 and parts[3]:
                return parts[3]
        return ""

    def _fmt_vol(v: str) -> str:
        try:
            f = float(v)
        except ValueError:
            return v
        if f >= 1e8:
            return f"{f / 1e8:.2f}亿手"
        if f >= 1e4:
            return f"{f / 1e4:.2f}万手"
        return v
    return _engine


# ── 腾讯行情引擎（实时行情快照，含换手率/市盈率/五档） ───────────────────────

def _build_tencent_quote_engine(spec: dict[str, Any]) -> Any:
    """腾讯实时行情快照（qt.gtimg.cn + smartbox 代码解析）。

    免费直连 GBK 接口，比新浪多换手率/市盈率/市净率/总市值等字段，
    与 sina_quote 互为交叉验证，适合「茅台股价」「上证指数」类查询。
    """
    timeout = spec.get("timeout", 8)
    suggest_url = "https://smartbox.gtimg.cn/s3/?v=2&t=all&q="
    quote_url = "https://qt.gtimg.cn/q="

    @safe_search
    def _engine(query: str, n: int = 3, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                   "Referer": "https://gu.qq.com/"}
        symbol = _resolve_symbol(query, to, headers)
        if not symbol:
            return []
        try:
            req = urllib.request.Request(quote_url + symbol, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                text = resp.read().decode("gbk", "replace").strip()
        except Exception as e:
            logger.warning(f"腾讯行情失败: {e}")
            return []
        if "=" not in text:
            return []
        var_part = text.split("=", 1)[1].strip().strip('"')
        fields = var_part.split("~")
        if len(fields) < 35:
            return []
        name, code = fields[1], fields[2]
        cur, prev, opn = fields[3], fields[4], fields[5]
        try:
            chg = float(fields[31]) if fields[31] else 0.0
            pct = float(fields[32]) if fields[32] else 0.0
        except ValueError:
            chg, pct = 0.0, 0.0
        arrow = "↑" if pct > 0 else ("↓" if pct < 0 else "→")
        title = f"{name} {cur} {arrow}{pct:+.2f}%" if cur else f"{name} 行情"
        parts = [
            f"现价 {cur}", f"涨跌 {chg:+.2f} ({pct:+.2f}%)",
            f"今开 {opn}", f"昨收 {prev}",
            f"最高 {fields[33]}", f"最低 {fields[34]}",
            f"成交量 {fields[36]}手" if len(fields) > 36 and fields[36] else "",
            f"成交额 {float(fields[37]) / 1e4:.2f}亿" if len(fields) > 37 and fields[37] else "",
            f"换手率 {fields[38]}%" if len(fields) > 38 and fields[38] else "",
            f"市盈率(动) {fields[39]}" if len(fields) > 39 and fields[39] else "",
        ]
        return [{
            "title": title[:80],
            "url": f"https://gu.qq.com/{symbol}/gp",
            "snippet": " | ".join(p for p in parts if p)[:220],
            "source": "tencent_quote",
            "score": 0.9,
        }]

    def _resolve_symbol(q: str, to: float, headers: dict) -> str:
        import urllib.parse as up
        _STOP = ("股价", "行情", "股票", "价格", "走势", "最新", "今日", "报价", "查询",
                 "怎么样", "多少", "怎么", "了", "吗", "的", "a股", "港股", "美股")
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
                req = urllib.request.Request(suggest_url + up.quote(c), headers=headers)
                with urllib.request.urlopen(req, timeout=to) as resp:
                    text = resp.read().decode("gbk", "replace")
            except Exception as e:
                logger.warning(f"腾讯代码解析失败: {e}")
                continue
            # v_hint="sh~600519~贵州茅台~600519~gp~A股~贵州茅台~GP-A"
            for m in re.finditer(r'v_hint="([^"]+)"', text):
                parts = m.group(1).split("~")
                if len(parts) >= 3 and parts[2]:
                    return parts[0] + parts[1]
        return ""
    return _engine


# ── 东财资金流引擎（个股主力资金流/北向资金/板块资金流） ─────────────────────

def _build_em_flow_engine(spec: dict[str, Any]) -> Any:
    """东方财富资金流向（push2delay.eastmoney.com，免认证直连）。

    三类数据：个股主力资金流（fflow/kline）、北向资金（kamt/get）、
    板块资金流排行（clist/get）。「资金流/主力/北向」类查询的答案源。
    """
    timeout = spec.get("timeout", 8)
    smartbox_url = "https://smartbox.gtimg.cn/s3/?v=2&t=all&q="
    fflow_url = "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get"
    kamt_url = "https://push2delay.eastmoney.com/api/qt/kamt/get"
    clist_url = "https://push2delay.eastmoney.com/api/qt/clist/get"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                   "Referer": "https://data.eastmoney.com/"}
        q = query.lower()
        if "北向" in q or "沪深港通" in q or ("外资" in q and "流入" in q):
            res = _northbound(to, headers)
            if res:
                return res
        if "板块" in q and ("资金" in q or "流入" in q or "净额" in q):
            res = _sector_flow(to, headers, n)
            if res:
                return res
        symbol = _resolve_symbol(query, to, headers)
        if symbol:
            res = _stock_flow(symbol, to, headers)
            if res:
                return res
        return _sector_flow(to, headers, n)

    def _northbound(to: float, headers: dict) -> list[dict[str, Any]]:
        data = None
        for _a in range(2):
            try:
                req = urllib.request.Request(
                    kamt_url + "?fields1=f1,f3&fields2=f51,f52,f53,f54,f55,f56",
                    headers=headers)
                with urllib.request.urlopen(req, timeout=to) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                break
            except Exception as e:
                logger.warning(f"北向资金失败(重试): {e}")
                time.sleep(0.3)
        if data is None:
            return []
        try:
            hk2sh = data["data"]["hk2sh"]
            hk2sz = data["data"]["hk2sz"]
        except (KeyError, TypeError):
            return []
        rows = []
        for name, leg in (("沪股通", hk2sh), ("深股通", hk2sz)):
            amt = leg.get("dayNetAmtIn")
            date = leg.get("date2") or leg.get("date") or ""
            rows.append((name, amt, date))
        results = []
        total = 0.0
        for name, amt, date in rows:
            try:
                f = float(amt) / 1e8
            except (TypeError, ValueError):
                continue
            total += f
            results.append({
                "title": f"北向资金-{name} 当日净流入 {f:+.2f} 亿元",
                "url": "https://data.eastmoney.com/hsgt/index.html",
                "snippet": f"日期 {date} | 沪深港通北向资金 | 东方财富数据中心".strip(),
                "source": "em_flow",
                "score": 0.9,
            })
        if total:
            results.append({
                "title": f"北向资金合计 当日净流入 {total:+.2f} 亿元",
                "url": "https://data.eastmoney.com/hsgt/index.html",
                "snippet": "沪股通 + 深股通合计 | 东方财富数据中心",
                "source": "em_flow",
                "score": 0.85,
            })
        return results

    def _sector_flow(to: float, headers: dict, n: int) -> list[dict[str, Any]]:
        url = (clist_url + "?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2&fid=f62"
               "&fs=m:90+t:2&fields=f12,f14,f2,f3,f62,f184" % min(max(n, 3), 10))
        data = None
        for _a in range(2):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=to) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                break
            except Exception as e:
                logger.warning(f"板块资金流失败(重试): {e}")
                time.sleep(0.3)
        if data is None:
            return []
        try:
            diff = data["data"]["diff"] or []
        except (KeyError, TypeError):
            return []
        results = []
        for it in diff:
            name = it.get("f14", "")
            chg = it.get("f3")
            flow = it.get("f62")
            pct = it.get("f184")
            if not name:
                continue
            try:
                flow_yi = float(flow) / 1e8
            except (TypeError, ValueError):
                flow_yi = 0.0
            try:
                chg_s = f"{float(chg):+.2f}%" if chg is not None else ""
            except (TypeError, ValueError):
                chg_s = ""
            try:
                pct_s = f"主力净占比 {float(pct):.2f}%"
            except (TypeError, ValueError):
                pct_s = ""
            results.append({
                "title": f"板块 {name} 主力净流入 {flow_yi:+.2f} 亿元",
                "url": "https://data.eastmoney.com/bkzj/hy.html",
                "snippet": " | ".join(x for x in (chg_s, pct_s, "东方财富板块资金流") if x)[:200],
                "source": "em_flow",
                "score": 0.9,
            })
        return results

    def _stock_flow(symbol: str, to: float, headers: dict) -> list[dict[str, Any]]:
        # symbol 形如 sh600519 / sz000858，secid 沪市=1.xxx 深市=0.xxx
        market = "1" if symbol.startswith("sh") else "0"
        code = symbol[2:]
        url = (fflow_url + "?lmt=0&klt=101&fields1=f1,f2,f3,f7"
               "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"
               "&secid=%s.%s" % (market, code))
        data = None
        for _a in range(2):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=to) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                break
            except Exception as e:
                logger.warning(f"个股资金流失败(重试): {e}")
                time.sleep(0.3)
        if data is None:
            return []
        try:
            klines = data["data"]["klines"] or []
            name = data["data"]["name"] or code
        except (KeyError, TypeError):
            return []
        if not klines:
            return []
        last = klines[-1].split(",")
        if len(last) < 6:
            return []
        # push2delay 返回 6 字段: 0日期 1主力 2小单 3中单 4大单 5超大单
        # push2 完整 13 字段: 6-10 净占比 11收盘 12涨跌幅（延迟源仅 6 字段）
        date, main, small, mid, big, xbig = last[0], last[1], last[2], last[3], last[4], last[5]
        try:
            main_yi = float(main) / 1e8
        except (TypeError, ValueError):
            main_yi = 0.0
        main_ratio, chg = 0.0, 0.0
        if len(last) >= 13:
            try:
                main_ratio = float(last[6]) if last[6] else 0.0
                chg = float(last[12]) if last[12] else 0.0
            except (TypeError, ValueError):
                pass
        try:
            detail = (f"超大单 {float(xbig) / 1e8:+.2f}亿 大单 {float(big) / 1e8:+.2f}亿"
                      f" 中单 {float(mid) / 1e8:+.2f}亿 小单 {float(small) / 1e8:+.2f}亿")
        except (TypeError, ValueError):
            detail = ""
        extra = " | ".join(x for x in (
            f"主力净占比 {main_ratio:+.2f}%" if main_ratio else "",
            f"涨跌幅 {chg:+.2f}%" if chg else "",
        ) if x)
        snippet = " | ".join(x for x in (f"日期 {date}", extra, detail, "东方财富资金流向") if x)
        return [{
            "title": f"{name} 主力资金净流入 {main_yi:+.2f} 亿元",
            "url": f"https://data.eastmoney.com/zjlx/{code}.html",
            "snippet": snippet[:220],
            "source": "em_flow",
            "score": 0.9,
        }]

    def _resolve_symbol(q: str, to: float, headers: dict) -> str:
        import urllib.parse as up
        _STOP = ("股价", "行情", "股票", "价格", "走势", "最新", "今日", "资金流", "资金",
                 "主力", "净流入", "净流出", "查询", "怎么样", "多少", "怎么", "了", "吗",
                 "的", "a股", "港股", "美股")
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
                req = urllib.request.Request(smartbox_url + up.quote(c), headers=headers)
                with urllib.request.urlopen(req, timeout=to) as resp:
                    text = resp.read().decode("gbk", "replace")
            except Exception as e:
                logger.warning(f"东财代码解析失败: {e}")
                continue
            for m in re.finditer(r'v_hint="([^"]+)"', text):
                parts = m.group(1).split("~")
                if len(parts) >= 3 and parts[2]:
                    return parts[0] + parts[1]
        return ""
    return _engine


# ── 东财财经搜索引擎 ─────────────────────────────────────────────────────────

def _build_eastmoney_engine(spec: dict[str, Any]) -> Any:
    """东财经搜搜索（纯 HTTP API，零外部依赖）

160→    支持：
    - 个股新闻搜索（按股票代码或关键词）
    - 东财全球资讯（7×24 财经快讯）
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        import re
        to = _timeout or timeout

        # 判断是股票代码（6位数字）还是关键词
        is_stock_code = re.match(r'^\d{6}$', query.strip())

        if is_stock_code:
            # 按股票代码搜新闻
            return _eastmoney_stock_news(query.strip(), n, to)
        else:
            # 按关键词搜全球资讯
            return _eastmoney_keyword_news(query, n, to)

    def _eastmoney_stock_news(code: str, n: int, to: float) -> list[dict[str, Any]]:
        """按股票代码搜新闻"""
        import json as _json
        import urllib.parse as up
        cb = "jQuery_news"
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        inner_params = _json.dumps({
            "uid": "", "keyword": code, "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                      "pageIndex": 1, "pageSize": n, "preTag": "", "postTag": ""}},
        }, separators=(',', ':'))
        params = {"cb": cb, "param": inner_params}
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com/"}
        try:
            full_url = url + "?" + "&".join(f"{k}={up.quote(str(v))}" for k, v in params.items())
            req = urllib.request.Request(full_url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                text = resp.read().decode("utf-8")
            json_str = text[text.index("(") + 1:text.rindex(")")]
            d = _json.loads(json_str)
            articles = d.get("result", {}).get("cmsArticleWebOld", []) or []
            results = []
            for a in articles[:n]:
                results.append({
                    "title": re.sub(r'<[^>]+>', '', a.get("title", ""))[:80],
                    "url": a.get("url", ""),
                    "snippet": re.sub(r'<[^>]+>', '', a.get("content", ""))[:200],
                    "source": "eastmoney",
                })
            return results
        except Exception as e:
            logger.warning(f"东财个股新闻搜索失败: {e}")
            return []

    def _eastmoney_keyword_news(query: str, n: int, to: float) -> list[dict[str, Any]]:
        """按关键词搜东财资讯（search-api-web 检索接口，按词真正检索）"""
        import urllib.parse as up
        cb = "jQuery_news"
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        inner_params = json.dumps({
            "uid": "", "keyword": query, "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                      "pageIndex": 1, "pageSize": n, "preTag": "", "postTag": ""}},
        }, separators=(',', ':'))
        params = {"cb": cb, "param": inner_params}
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com/"}
        try:
            full_url = url + "?" + "&".join(f"{k}={up.quote(str(v))}" for k, v in params.items())
            req = urllib.request.Request(full_url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                text = resp.read().decode("utf-8")
            json_str = text[text.index("(") + 1:text.rindex(")")]
            d = json.loads(json_str)
            articles = d.get("result", {}).get("cmsArticleWebOld", []) or []
            results = []
            for a in articles[:n]:
                results.append({
                    "title": re.sub(r'<[^>]+>', '', a.get("title", ""))[:80],
                    "url": a.get("url", "") or "https://so.eastmoney.com/",
                    "snippet": re.sub(r'<[^>]+>', '', a.get("content", ""))[:200],
                    "source": "eastmoney",
                })
            return results
        except Exception as e:
            logger.warning(f"东财关键词搜索失败: {e}")
            return []

    return _engine


# ── itotii 梗百科引擎 ────────────────────────────────────────────────────────

def _build_itotii_engine(spec: dict[str, Any]) -> Any:
    """itotii 梗百科（中文流行语/网络梗词条，WordPress REST API，免认证）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = (
            "https://geng.itotii.com/wp-json/wp/v2/posts?search="
            + up.quote(query) + f"&per_page={min(n, 10)}"
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            results = []
            for p in data:
                title = p.get("title", {})
                title = title.get("rendered", "") if isinstance(title, dict) else str(title)
                content = p.get("content", {})
                content = content.get("rendered", "") if isinstance(content, dict) else str(content)
                title = re.sub(r"<[^>]+>", "", title).strip()
                content = re.sub(r"<[^>]+>", "", content).strip()
                if not title:
                    continue
                results.append({
                    "title": title[:80],
                    "url": p.get("link", ""),
                    "snippet": f"{p.get('date', '')[:10]} | {content[:200]}",
                    "source": "itotii",
                    "score": max(1.0 - len(results) * 0.1, 0.1),
                })
            return results[:n]
        except Exception as e:
            logger.warning(f"itotii 梗百科失败: {e}")
            return []
    return _engine


# ── 百度热搜引擎 ─────────────────────────────────────────────────────────────

def _build_baidu_hot_engine(spec: dict[str, Any]) -> Any:
    """百度热搜（top.baidu.com 实时热搜榜，HTML 解析）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://top.baidu.com/board?tab=realtime"
        headers = {"User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                page = resp.read().decode("utf-8", "replace")
            words = re.findall(r'word":"([^"]+)"', page)
            results, seen = [], set()
            for w in words:
                if w in seen:
                    continue
                seen.add(w)
                results.append({
                    "title": w[:60],
                    "url": "https://www.baidu.com/s?wd=" + up.quote(w),
                    "snippet": "百度热搜",
                    "source": "baidu_hot",
                    "score": max(1.0 - len(results) * 0.05, 0.1),
                })
                if len(results) >= n:
                    break
            return results
        except Exception as e:
            logger.warning(f"百度热搜失败: {e}")
            return []
    return _engine


# ── 今日头条热榜引擎 ─────────────────────────────────────────────────────────

def _build_toutiao_hot_engine(spec: dict[str, Any]) -> Any:
    """今日头条热榜（hot-board JSON，免认证）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.toutiao.com/"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            results = []
            for i, item in enumerate(data.get("data", [])[:n]):
                hot = item.get("HotValue", "")
                results.append({
                    "title": item.get("Title", "")[:60],
                    "url": item.get("Url", ""),
                    "snippet": f"热度 {hot}" if hot else "今日头条热榜",
                    "source": "toutiao_hot",
                    "score": max(1.0 - i * 0.05, 0.1),
                })
            return results
        except Exception as e:
            logger.warning(f"今日头条热榜失败: {e}")
            return []
    return _engine


# ── B站热搜引擎 ──────────────────────────────────────────────────────────────

def _build_bilibili_hot_engine(spec: dict[str, Any]) -> Any:
    """B站热搜（search/square 热搜词，免认证）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://api.bilibili.com/x/web-interface/search/square?limit=20"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            items = data.get("data", {}).get("trending", {}).get("list", [])
            results = []
            for i, item in enumerate(items[:n]):
                kw = item.get("keyword", "")
                if not kw:
                    continue
                results.append({
                    "title": kw[:60],
                    "url": "https://search.bilibili.com/all?keyword=" + up.quote(kw),
                    "snippet": "B站热搜",
                    "source": "bilibili_hot",
                    "score": max(1.0 - i * 0.05, 0.1),
                })
            return results
        except Exception as e:
            logger.warning(f"B站热搜失败: {e}")
            return []
    return _engine


# ── 知乎全网搜索（global_search，需 ZHIHU_ACCESS_SECRET）─────────────────────

# site:/host: 站点限定语法 → Filter host=="..." 表达式。
# 取值吸收到首个空白或中文字符（值只能是域名，纯中文值视为无效 host）。
_SITE_FILTER_RE = re.compile(r"(?:site|host)\s*[:：]\s*([^\s\u4e00-\u9fff]+)")
# 残留的 site:/host: 词法片段（无有效 host 值也剥离，避免把语法词当搜索词）
_SITE_TOKEN_RE = re.compile(r"(?:site|host)\s*[:：]")


def _parse_site_filter(query: str) -> tuple[str, str]:
    """解析 site:/host: 站点限定语法 → (Filter 表达式, 剔除后的查询词)。

    只取第一处匹配；host 兼容裸域名 / 完整 URL / 带端口三种写法，
    统一剥离 scheme、路径、端口为裸域名（global_search 的 Filter host==
    要求裸域名，`site:https://blog.csdn.net/x` 若整串传入会取到 https 当 host）。

    Returns:
        (filter_expr, cleaned_query)；无有效 host 时 filter_expr 为空串，
        但残留的 site:/host: 词法片段仍会被剥离。
    """
    m = _SITE_FILTER_RE.search(query)
    if not m:
        t = _SITE_TOKEN_RE.search(query)
        if not t:
            return "", query.strip()
        cleaned = (query[: t.start()] + query[t.end():]).strip()
        return "", re.sub(r"\s{2,}", " ", cleaned)
    raw = m.group(1).strip().rstrip(".,;:，。；")
    host = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", raw)  # 去 scheme
    host = host.split("/", 1)[0].split(":", 1)[0].strip().lower()  # 去路径与端口
    cleaned = (query[: m.start()] + query[m.end():]).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return (f'host=="{host}"' if host else ""), cleaned


def _build_zhihu_global_engine(spec: dict[str, Any]) -> Any:
    """知乎开放平台全网搜索（developer.zhihu.com global_search）。

    第一性：zhihu_global 是「真全网搜索」——默认 SearchDB=all 搜全网索引，
    且支持 Filter host== 精确限定站点（byted/bocha 做不到的精确能力）。
    响应含 AuthorityLevel（1-4 权威等级）、VoteUpCount、EditTime 等结构化信号，
    全部提取进统一 schema，供 evidence 消费。

    查询语法：
      - `site:zhuanlan.zhihu.com 关键词` → Filter: host=="zhuanlan.zhihu.com"
      - `host:blog.csdn.net 关键词`     → 同上（兼容写法）
      - 普通查询 → SearchDB=all 全网
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        secret = os.environ.get("ZHIHU_ACCESS_SECRET") or os.environ.get("ARGO_ZHIHU_ACCESS_SECRET", "")
        if not secret:
            return []

        # 解析 site:/host: 站点限定语法 → Filter: host=="..."
        filter_expr, search_query = _parse_site_filter(query)

        params: dict[str, Any] = {
            "Query": search_query or query,
            "Count": str(min(n, 20)),
            "SearchDB": "all",
        }
        if filter_expr:
            params["Filter"] = filter_expr
        url = "https://developer.zhihu.com/api/v1/content/global_search?" + up.urlencode(params)
        headers = {
            "Authorization": f"Bearer {secret}",
            "X-Request-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
            "User-Agent": "argo-search/2.6 (unified-search@local)",
        }
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"zhihu_global 失败: {e}")
            return []
        if data.get("Code") not in (0, None):
            logger.warning(f"zhihu_global 返回码异常: {data.get('Code')} {data.get('Message')}")
            return []
        items = (data.get("Data") or {}).get("Items") or []
        results = []
        for item in items[:n]:
            title = (item.get("Title") or "").strip()
            url_ = item.get("Url") or ""
            snippet = item.get("ContentText") or ""
            # 去 <em> 高亮标签
            snippet = re.sub(r"<[^>]+>", "", snippet).strip()
            if not title and not url_:
                continue
            # 结构化信号：权威等级 / 互动 / 时效
            social_meta = {
                "author": item.get("AuthorName") or "",
                "content_type": item.get("ContentType") or "",
                "vote_up": item.get("VoteUpCount") or 0,
                "comment_count": item.get("CommentCount") or 0,
                "authority_level": item.get("AuthorityLevel") or "",
                "edit_time": item.get("EditTime") or 0,
            }
            results.append({
                "title": title[:200],
                "url": url_,
                "snippet": snippet[:300],
                "source": "zhihu_global",
                "score": 0.7,
                "authority_level": social_meta["authority_level"],
                "social_meta": social_meta,
            })
        return results
    return _engine


# ── 博查 Web Search 引擎（专用解析 + 动态时效）──────────────────────────────────

_BOCHA_FRESH_RE = re.compile(
    r"(本周|本月|最近一周|近一周|近一个月|recent|past\s*(week|month)|last\s*(week|month))",
    re.I,
)


def _bocha_freshness(query: str) -> str:
    """按查询时效敏感度动态选择 freshness 参数。

    周/月级窗口词（本周/本月/近一周等）→ oneWeek；
    日/时级时效词（今日/实时/最新/盘中/快讯等，复用缓存层的敏感检测）→ oneDay；
    其余放宽为 noLimit（全量）。
    """
    if re.search(r"(本周|本月|近一周|近一个月|recent|past\s*(week|month)|last\s*(week|month)|this\s*week)", query or "", re.I):
        return "oneWeek"
    try:
        from cache import is_freshness_sensitive_query
        if is_freshness_sensitive_query(query or ""):
            return "oneDay"
    except Exception:
        pass
    return "noLimit"


def _bocha_key() -> str:
    return os.environ.get("ARGO_BOCHA_API_KEY") or os.environ.get("BOCHA_API_KEY", "")


def _bocha_web_item(item: dict[str, Any]) -> dict[str, Any]:
    """webPages.value 单条 → 统一结果项（name/summary/datePublished/siteName 语义映射）。"""
    return {
        "title": str(item.get("name") or item.get("title") or "")[:200],
        "url": str(item.get("url") or ""),
        "snippet": str(item.get("summary") or item.get("snippet") or item.get("description") or "")[:300],
        "source": "bocha",
        "score": 0.7,
        "date": item.get("datePublished") or "",
        "site_name": item.get("siteName") or "",
    }


def _build_bocha_engine(spec: dict[str, Any]) -> Any:
    """博查 Web Search（中文全网搜索，AI 友好摘要）。

    专用解析修复通用 parser 对 `data.webPages.value` 嵌套路径的漏检；
    freshness 按查询时效敏感度动态化（替代静态 oneYear）。
    """
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        key = _bocha_key()
        if not key:
            return [{"error": "BOCHA_API_KEY 未设置", "source": "bocha"}]
        body = {
            "query": query or "",
            "summary": True,
            "freshness": _bocha_freshness(query),
            "count": max(1, min(int(n or 5), 50)),
        }
        req = urllib.request.Request(
            "https://api.bochaai.com/v1/web-search",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=to) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pages = (data.get("data") or {}).get("webPages") or {}
        return [_bocha_web_item(i) for i in (pages.get("value") or [])]
    return _engine


# ── 博查 AI Search 引擎（垂直结构化模态卡）──────────────────────────────────────

_BOCHA_CARD_NAMES: dict[str, str] = {
    "weather": "天气", "baike": "百科", "medical": "医疗", "almanac": "万年历",
    "train": "火车票", "constellation": "星座运势", "precious_metal": "贵金属",
    "exchange_rate": "汇率", "oil_price": "油价", "phone": "手机", "stock": "股票",
    "auto": "汽车", "calendar": "日历", "movie": "电影", "hotel": "酒店",
    "restaurant": "餐厅", "scenic": "景点", "company": "企业", "news": "新闻",
    "knowledge": "百科", "image": "图片",
}


def _flatten_card(data: Any, depth: int = 0) -> str:
    """模态卡结构化数据 → 可读单行（嵌套最多两层，长内容截断）。"""
    if not isinstance(data, dict):
        return str(data)
    if depth > 2:
        return json.dumps(data, ensure_ascii=False)[:500]
    parts = []
    for k, v in data.items():
        if isinstance(v, dict):
            parts.append(_flatten_card(v, depth + 1))
        elif isinstance(v, list):
            sub = [_flatten_card(x, depth + 1) if isinstance(x, dict) else str(x) for x in v[:3]]
            parts.append(f"{k}: {'; '.join(sub)}")
        elif v is not None and str(v) != "":
            parts.append(f"{k}: {v}")
    return " | ".join(parts)[:500]


def _build_bocha_ai_engine(spec: dict[str, Any]) -> Any:
    """博查 AI Search：统一语义识别 + 垂直结构化模态卡。

    在网页结果基础上，额外返回天气/股票/汇率/油价/火车/万年历/医疗等
    几十种垂直领域的结构化模态卡。card_type 标注模态类型，
    card_data 保留原始结构化 JSON（供精确消费），snippet 为可读扁平化摘要。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        key = _bocha_key()
        if not key:
            return [{"error": "BOCHA_API_KEY 未设置", "source": "bocha_ai"}]
        body = {
            "query": query or "",
            "freshness": _bocha_freshness(query),
            "count": max(1, min(int(n or 5), 50)),
            "answer": False,
            "stream": False,
        }
        req = urllib.request.Request(
            "https://api.bochaai.com/v1/ai-search",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=to) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results: list[dict[str, Any]] = []
        for message in data.get("messages") or []:
            ct = message.get("content_type") or ""
            raw = message.get("content") or "{}"
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (json.JSONDecodeError, ValueError):
                parsed = {}
            if ct == "webpage":
                for item in parsed.get("value") or []:
                    if not isinstance(item, dict):
                        continue
                    it = _bocha_web_item(item)
                    it["source"] = "bocha_ai"
                    results.append(it)
            elif ct == "image" or not parsed:
                continue
            else:
                card_name = _BOCHA_CARD_NAMES.get(ct, ct)
                flat = _flatten_card(parsed)
                results.append({
                    "title": f"{card_name}（结构化数据卡）" if _BOCHA_CARD_NAMES.get(ct) else f"[{ct}]",
                    "url": "",
                    "snippet": flat[:300],
                    "source": "bocha_ai",
                    "score": 1.0,
                    "card_type": ct,
                    "card_data": parsed,
                })
        return results
    return _engine




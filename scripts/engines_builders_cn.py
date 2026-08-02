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



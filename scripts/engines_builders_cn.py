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

from engine_env import get_env

from engines_base import safe_search, _run, _resolve, _get_path, _coerce_field, _http_get_raw, http_open, rank_score

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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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

# 低信息量触发词：快讯/热榜类引擎的查询词仅作路由触发，不参与内容过滤。
# 「快讯」「美股」这类查询若当关键词过滤会把全量榜单滤空。
_TRIGGER_WORDS = frozenset({
    "快讯", "电报", "资讯", "新闻", "热点", "实时", "美股", "行情",
    "财经", "股市", "港股", "A股", "盘面", "速递", "播报", "latest",
    "news", "flash", "market", "stock", "finance",
    # 复合触发短语：各域 pattern 里就是这么写的（jin10_flash / cls_telegraph_search），
    # 不补的话「财经快讯」会被当主题词去过滤，把全量榜单滤成 0 条
    "财经快讯", "市场快讯", "实时快讯", "7x24快讯", "全球快讯",
})


def _should_filter(query: str) -> bool:
    """查询是否值得做内容关键词过滤（含具体主题才过滤，纯触发词放行全量榜单）。"""
    q = (query or "").strip()
    if not q:
        return False
    tokens = [t for t in re.split(r"[\s,，。、]+|美股|港股|A股", q) if t]
    if not tokens:
        return False
    return not all(t in _TRIGGER_WORDS for t in tokens)


def _build_cls_telegraph_engine(spec: dict[str, Any]) -> Any:
    """财联社电报（全市场实时快讯，v1 API + 本地签名，零 key）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import hashlib
        from datetime import datetime
        to = _timeout or timeout
        # 关键词过滤时放大拉取量（3×），避免小样本过滤后 0 条
        fetch_n = max(int(n) * 3, 10)
        params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
                  "last_time": "", "refresh_type": "1", "rn": str(fetch_n)}
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
        url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("roll_data", []) or []:
                ts = item.get("ctime")
                t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
                title = item.get("title", "") or item.get("brief", "")
                # 关键词过滤：纯触发词（快讯/美股等）放行全量榜单
                if _should_filter(query):
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
            "pageSize": str(max(int(n) * 3, 10)),  # 放大拉取供关键词过滤
            "req_trace": str(uuid.uuid4()),
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://kuaixun.eastmoney.com/"}
        try:
            req = urllib.request.Request(url + "?" + "&".join(f"{k}={v}" for k, v in params.items()), headers=headers)
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("fastNewsList", []):
                title = item.get("title", "")
                # 关键词过滤：纯触发词（快讯/美股等）放行全量榜单
                if _should_filter(query):
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
        key = get_env(["ARGO_EASTMONEY_APIKEY", "EASTMONEY_APIKEY"])
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
        for _rk, it in enumerate(items):
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
                "score": rank_score(0.85, _rk),
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
                with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as e:
                logger.warning(f"巨潮公告搜索失败: {e}")
                return []
            items = data.get("announcements") or []
            if not items:
                return []
            results = []
            seen: set[str] = set()
            for _rk5, it in enumerate(items):
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
                    "score": rank_score(0.9, _rk5),
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
                with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
                with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
                with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
        for _rk1, name, amt, date in enumerate(rows):
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
                "score": rank_score(0.9, _rk1),
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
                with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
        for _rk2, it in enumerate(diff):
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
                "score": rank_score(0.9, _rk2),
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
                with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
                with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
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
        secret = get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
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
            with http_open(urllib.request.Request(url, headers=headers), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            # 401/403 等必须暴露为 error item 而非静默空——调用侧把
            # 「没配置」「鉴权失败」「没结果」区分开才可行动（保持一致
            # social_engines/zhihu_engine.py 同接口的计算方式）
            return [{"error": f"zhihu_global API HTTP {e.code}", "source": "zhihu_global"}]
        except Exception as e:
            logger.warning(f"zhihu_global 失败: {e}")
            return [{"error": f"zhihu_global {type(e).__name__}: {e}", "source": "zhihu_global"}]
        if data.get("Code") not in (0, None):
            # 30001=频率限制 30002=配额限制，显式暴露供配额状态机归类
            logger.warning(f"zhihu_global 返回码异常: {data.get('Code')} {data.get('Message')}")
            return [{"error": f"zhihu_global Code={data.get('Code')} "
                              f"{str(data.get('Message') or '')[:100]}",
                     "source": "zhihu_global"}]
        items = (data.get("Data") or {}).get("Items") or []
        results = []
        for _rk3, item in enumerate(items[:n]):
            title = (item.get("Title") or "").strip()
            # API 标题统一带「 - 知乎」尾巴：截断前剥掉（先剥再切，尾巴不占正文）
            if title.endswith(" - 知乎"):
                title = title[: -len(" - 知乎")].rstrip()
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
                "score": rank_score(0.7, _rk3),
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


# ── zhihu_user：知乎个人数据（本人创作/收藏/关注）─────────────────────────
# 站内搜索/全网搜覆盖「找内容」；本引擎覆盖「看自己」——个人创作运营
# （哪些回答点赞高、最近发了什么）与素材回溯（收藏夹）。查本人数据用
# Access Secret 直调（官方文档：不传 X-OAuth-Token 即本人），OAuth 仅在
# 查其他用户时才需要。/user/* 端点共享 user_data 日额度（10000/天）。

_ZHIHU_USER_API = "https://developer.zhihu.com/api/v1/user"

# 子意图 → 端点与参数（按序匹配，命中即停）
_ZHIHU_USER_INTENTS: list[tuple[str, str, dict[str, str]]] = [
    # (pattern, endpoint, extra query params)
    (r"我的收藏夹|收藏夹(列表|有哪些)", "favlists", {}),
    (r"我的收藏|收藏的内容", "collections", {}),
    (r"我关注(的)?(人|列表|谁)?(?!.*动态)", "followees", {}),
    (r"我的(回答|答主)", "contents", {"ContentType": "answer"}),
    (r"我的(文章|专栏)", "contents", {"ContentType": "article"}),
    (r"我的视频", "contents", {"ContentType": "zvideo"}),
    (r"我的(想法|动态|短内容)", "contents", {"ContentType": "pin"}),
    (r"我的(提问|问题)", "contents", {"ContentType": "question"}),
]


def _parse_zhihu_user_intent(query: str) -> tuple[str, dict[str, str], str]:
    """解析子意图，返回 (endpoint, extra_params, 剩余关键词)。"""
    q = (query or "").strip()
    for pat, endpoint, extra in _ZHIHU_USER_INTENTS:
        if re.search(pat, q):
            leftover = re.sub(pat, " ", q, count=1)
            return endpoint, extra, leftover
    # 默认：全部内容（时间倒序）
    leftover = re.sub(r"(我的知乎|知乎我的|我的(全部)?内容|个人(主页|数据)|我的创作)", " ", q)
    return "contents", {"ContentType": "all"}, leftover


def _build_zhihu_user_engine(spec: dict[str, Any]) -> Any:
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        secret = get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
        if not secret:
            return []

        endpoint, extra, leftover = _parse_zhihu_user_intent(query)
        params: dict[str, Any] = {"Limit": str(min(int(n), 50)), **extra}
        if endpoint == "contents":
            # 创作分析语义：点赞最多/最受欢迎 → 按赞排序（默认最新）
            if re.search(r"点赞(最)?(多|高)|最受欢迎|高赞|表现(最)?好", query):
                params["SortField"] = "like_count"
        url = f"{_ZHIHU_USER_API}/{endpoint}?" + up.urlencode(params)
        headers = {
            "Authorization": f"Bearer {secret}",
            "X-Request-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
            "User-Agent": "argo-search/2.6 (unified-search@local)",
        }
        try:
            with http_open(urllib.request.Request(url, headers=headers), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return [{"error": f"zhihu_user API HTTP {e.code}", "source": "zhihu_user"}]
        except Exception as e:
            logger.warning(f"zhihu_user 失败: {e}")
            return [{"error": f"zhihu_user {type(e).__name__}: {e}", "source": "zhihu_user"}]
        if data.get("Code") not in (0, None):
            logger.warning(f"zhihu_user 返回码异常: {data.get('Code')} {data.get('Message')}")
            return [{"error": f"zhihu_user Code={data.get('Code')} "
                              f"{str(data.get('Message') or '')[:100]}",
                     "source": "zhihu_user"}]

        items = (data.get("Data") or {}).get("Items") or []
        # 剩余关键词做标题/摘要本地过滤（「我的收藏 大模型」→ 只留含大模型的）
        leftover_kw = re.sub(r"[\s，,。？?的]+", " ", leftover).strip()
        keywords = [w for w in leftover_kw.split() if len(w) >= 2]

        results: list[dict[str, Any]] = []
        for item in items[:n]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("Title") or "").strip()
            url_ = item.get("Url") or ""
            if endpoint == "followees":
                name = str(item.get("Fullname") or "").strip()
                if not name:
                    continue
                results.append({
                    "title": name[:100],
                    "url": item.get("Url") or "",
                    "snippet": str(item.get("Headline") or "")[:300],
                    "source": "zhihu_user",
                    "score": max(1.0 - len(results) * 0.1, 0.1),
                    "social_meta": {"platform": "zhihu_user",
                                    "content_type": "followee",
                                    "followers": item.get("FollowerCount") or 0},
                })
                continue
            # 收藏夹列表：Title/Description 为收藏夹本身
            summary = str(item.get("Summary") or item.get("Description") or "")[:300]
            if keywords and not any(
                    k in title or k in summary for k in keywords):
                continue
            created = item.get("CreatedAt") or 0
            results.append({
                "title": (title[:100] + ("..." if len(title) > 100 else "")) if title else "(无标题)",
                "url": url_,
                "snippet": summary,
                "source": "zhihu_user",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created)) if created else None,
                "social_meta": {
                    "platform": "zhihu_user",
                    "content_type": str(item.get("ContentType") or endpoint),
                    "likes": item.get("LikeCount") or 0,
                    "comments": item.get("CommentCount") or 0,
                    "favorites": item.get("FavoriteCount") or 0,
                    "fav_time": item.get("FavTime") or None,
                },
            })
        return results

    return _engine


def _bocha_http_error(exc: Exception, source: str) -> list[dict[str, Any]]:
    """把博查的 HTTP 失败转成 error 记录（错误体一并带上）。

    为什么不能直接抛给 safe_search：safe_search 吞掉异常返回空列表，
    「接口 403」与「这个词真没结果」在下游看起来一模一样。博查 AI Search
    端点要单独套餐，本机 key 只有 web-search 权限，实测
    `HTTP 403 {"message":"You do not have enough money or package quota"}`——
    此前这条被吃成空结果，引擎一直显示 ready，结构化卡静默降级成普通网页结果。
    这里把状态码与错误体交出去，search.py 的 `_QUOTA_ERROR_KEYWORDS`（含
    "quota"）就能判成 quota-exhausted，配额状态机据此停用该源。
    """
    detail = ""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            detail = ""
        return [{"error": f"HTTP {exc.code}: {detail}" if detail else f"HTTP {exc.code}",
                 "source": source}]
    return [{"error": f"{type(exc).__name__}: {str(exc)[:160]}", "source": source}]


def _bocha_key() -> str:
    return get_env(["ARGO_BOCHA_API_KEY", "BOCHA_API_KEY"])


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
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            return _bocha_http_error(e, "bocha")
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
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            return _bocha_http_error(e, "bocha_ai")

        results: list[dict[str, Any]] = []
        for _rk4, message in enumerate(data.get("messages") or []):
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
                    "score": rank_score(1.0, _rk4),
                    "card_type": ct,
                    "card_data": parsed,
                })
        return results
    return _engine





# ── 批次八：数据源扩展（2026-09-12）──────────────────────────────────────────

# std.samr 的 C_C_NAME 与政府类接口正文常带高亮标记，进结果前一律剥掉
_SACINFO_TAG_RE = re.compile(r"</?sacinfo\s*>")
_HL_TAG_RE = re.compile(r"</?em[^>]*>")
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_hl(text: str) -> str:
    return re.sub(r"\s+", " ", _HL_TAG_RE.sub("", str(text or ""))).strip()


def _build_std_samr_engine(spec: dict[str, Any]) -> Any:
    """全国标准信息公共服务平台（国标检索，免认证）。

    C_C_NAME 带 <sacinfo> 高亮标签；id 即 openstd 的 hcno，直通全文预览页。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        url = ("https://std.samr.gov.cn/gb/search/gbQueryPage"
               f"?searchText={urllib.parse.quote(q)}&pageNumber=1&pageSize={min(max(int(n), 1), 50)}")
        raw = _http_get_raw(url, {"User-Agent": "argo-search/1.0 (+std_samr)", "Accept": "application/json"},
                            to, engine=spec.get("_name", "std_samr"))
        if raw is None:
            return []
        try:
            rows = (json.loads(raw) or {}).get("rows") or []
        except (json.JSONDecodeError, ValueError):
            return []
        out = []
        for r in rows:
            # C_STD_CODE 同样带 <sacinfo> 高亮：旧实现只剥了 C_C_NAME，
            # 把 XML 标签直接印进了结果标题（实测
            # '<sacinfo>GB</sacinfo>/<sacinfo>T</sacinfo> <sacinfo>45577</sacinfo>-2025 …'）。
            code = _SACINFO_TAG_RE.sub("", str(r.get("C_STD_CODE") or "")).strip()
            name = _SACINFO_TAG_RE.sub("", str(r.get("C_C_NAME") or "")).strip()
            if not code and not name:
                continue
            bits = [str(r.get(k) or "").strip() for k in ("STD_NATURE", "STATE")]
            if r.get("ISSUE_DATE"):
                bits.append(f"发布 {r['ISSUE_DATE']}")
            if r.get("ACT_DATE"):
                bits.append(f"实施 {r['ACT_DATE']}")
            hcno = str(r.get("id") or "").strip()
            out.append({
                "title": f"{code} {name}".strip(),
                "url": f"https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno={hcno}" if hcno else "",
                "snippet": " · ".join(b for b in bits if b),
                "source": "std_samr",
            })
        return out[:max(int(n), 1)]
    return _engine


_STD_TAIL_WORDS = ("全文", "标准", "最新", "下载")


def _build_openstd_engine(spec: dict[str, Any]) -> Any:
    """国家标准全文公开系统（GB 全文预览入口，HTML 行解析，免认证）。

    列表为服务端渲染表格，详情键在 onclick="showInfo('hcno')"；列位置随
    行型浮动，按内容特征定位（标准号模式 / 日期模式 / 状态词表），不按序号。
    """
    timeout = spec.get("timeout", 15)
    _std_code_re = re.compile(r"^[A-Z]{2,4}(?:/[A-Z]{1,2})?\s?\d+[-—]\d{4}")
    _date_re = re.compile(r"^\d{4}-\d{2}-\d{2}")
    _known_words = {"推标", "强标", "推荐性", "强制性", "现行", "即将实施", "废止", "作废", "被代替", "查看详细"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        for w in _STD_TAIL_WORDS:
            q = re.sub(rf"{w}$", "", q).strip()
        if not q:
            return []
        to = _timeout or timeout
        url = ("https://openstd.samr.gov.cn/bzgk/gb/std_list"
               f"?p.p1=0&p.p2={urllib.parse.quote(q)}&p.p90=circulation_date&p.p91=desc")
        raw = _http_get_raw(url, {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"},
                            to, engine=spec.get("_name", "openstd"))
        if raw is None:
            return []
        out = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S):
            m = re.search(r"showInfo\('([0-9A-Fa-f]+)'\)", tr)
            if not m:
                continue
            tds = [re.sub(r"\s+", " ", _TAG_RE.sub("", t)).strip()
                   for t in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            code_i = next((i for i, t in enumerate(tds) if _std_code_re.match(t)), None)
            if code_i is None:
                continue
            code = tds[code_i]
            name = next((t for t in tds[code_i + 1:]
                         if len(t) > 3 and not _date_re.match(t) and t not in _known_words), "")
            dates = [t for t in tds if _date_re.match(t)]
            words = [t for t in tds[code_i + 1:] if t in _known_words and t != "查看详细"]
            bits = words + [f"发布 {dates[0]}" for _ in [0] if dates]
            if len(dates) > 1:
                bits.append(f"实施 {dates[1]}")
            out.append({
                "title": f"{code} {name}".strip(),
                "url": f"https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno={m.group(1)}",
                "snippet": " · ".join(bits),
                "source": "openstd",
            })
            if len(out) >= max(int(n), 1):
                break
        return out
    return _engine


def _build_bangumi_engine(spec: dict[str, Any]) -> Any:
    """Bangumi 番剧仓库（动画/漫画/游戏条目元数据，官方开放 API，POST JSON）。

    name_cn 常缺省 → 回落 name；rating.score 可能为 0/null。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        req = urllib.request.Request(
            "https://api.bgm.tv/v0/search/subjects",
            data=json.dumps({"keyword": q}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "taxue/argo-search (+bangumi engine)",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "bangumi")) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"Bangumi 引擎失败: {e}")
            return []
        out = []
        for it in (data.get("data") or [])[:max(int(n), 1)]:
            title = str(it.get("name_cn") or it.get("name") or "").strip()
            if not title:
                continue
            bits = [str(it.get(k) or "").strip() for k in ("platform", "date")]
            rating = it.get("rating") if isinstance(it.get("rating"), dict) else {}
            if rating and rating.get("score"):
                bits.append(f"评分 {rating.get('score')}")
            summary = re.sub(r"\s+", " ", str(it.get("summary") or "")).strip()
            head = " · ".join(b for b in bits if b)
            out.append({
                "title": title,
                "url": f"https://bgm.tv/subject/{it.get('id')}",
                "snippet": (f"{head} {summary}"[:300] if head else summary[:300]),
                "source": "bangumi",
            })
        return out
    return _engine


_DOUBAN_TYPE_CN = {"movie": "电影", "tv": "剧集"}


def _build_douban_movie_engine(spec: dict[str, Any]) -> Any:
    """豆瓣电影 suggest 接口（中文片名/年份/类型，免认证）。

    suggest 无评分字段；type 为 movie/tv。豆瓣对无 cookie 请求逐步收紧，
    失败诚实空。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        url = f"https://movie.douban.com/j/subject_suggest?q={urllib.parse.quote(q)}"
        raw = _http_get_raw(url, {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://movie.douban.com/",
            "Accept": "application/json",
        }, to, engine=spec.get("_name", "douban_movie"))
        if raw is None:
            return []
        try:
            items = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(items, list):
            return []
        out = []
        for it in items[:max(int(n), 1)]:
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            year = str(it.get("year") or "").strip()
            t = _DOUBAN_TYPE_CN.get(str(it.get("type") or ""), str(it.get("type") or ""))
            out.append({
                "title": title,
                "url": str(it.get("url") or "").strip(),
                "snippet": " · ".join(b for b in (t, f"{year}年" if year else "") if b),
                "source": "douban_movie",
            })
        return out
    return _engine


def _build_zdic_engine(spec: dict[str, Any]) -> Any:
    """汉典（中文字词典，词条页 /hans/{词} 直达）。

    取「详细解释」前几条释义拼摘要；查无此字 404 → _http_get_raw None → 诚实空。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q or len(q) > 12:
            return []
        to = _timeout or timeout
        url = f"https://www.zdic.net/hans/{urllib.parse.quote(q)}"
        raw = _http_get_raw(url, {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"},
                            to, engine=spec.get("_name", "zdic"))
        if raw is None:
            return []
        defs = [_TAG_RE.sub("", d) for d in
                re.findall(r'<div class="xxjs-item__def">(.*?)</div>', raw, re.S)]
        defs = [re.sub(r"\s+", " ", d).strip() for d in defs]
        defs = [d for d in defs if d]
        if not defs:
            return []
        title_m = re.search(r"<title>([^<]+)</title>", raw)
        title = title_m.group(1).split(" - ")[0].strip() if title_m else q
        return [{
            "title": title,
            "url": url,
            "snippet": "释义：" + "；".join(defs[:3])[:280],
            "source": "zdic",
        }]
    return _engine


# ── iPlant 植物智（中文植物名 → 学名 + 分类）──────────────────────────────────

_IPLANT_HOST = "https://www.iplant.cn"
_IPLANT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://www.iplant.cn/",
}

# 服务端渲染的页面变量。可靠——与「物种保护」「分类信息」那些 AJAX 填充块不同。
_IPLANT_VAR_RE = re.compile(r"var\s+(spno|spcname|latin2|systype)\s*=\s*\"([^\"]*)\"")
# 「您是否要找」消歧块：别名/泛称页在此给出接受名（中文名 + 学名 + 带内部 id 的链接）
_IPLANT_SUGGEST_RE = re.compile(r"您是否要找[：:](.*?)</div>", re.S)
_IPLANT_ANCHOR_RE = re.compile(r"<a\s+href='([^']+)'[^>]*>([^<]+)</a>")
# 俗名块：<div>俗名：<a href='/info/苞米'>苞米</a>、…</div>
# 不取「异名」：那是拉丁→拉丁的映射，gbif 本来就管得好，不属于本源承诺的
# 「中文名/俗名 → 学名 + 分类」（它的块结构也不同，异名：后紧跟 </div>）
_IPLANT_VERN_RE = re.compile(r"俗名：(.{0,400}?)</div>", re.S)
# 用户常把「玉米 学名」这类整体丢进来，去掉修饰词后才是可查的名字
_IPLANT_NOISE_RE = re.compile(
    r"(学名|拉丁名|拉丁学名|俗名|别名|别称|植物|物种|分类|是什么|有哪些|查询|搜索)"
)


def _build_iplant_engine(spec: dict[str, Any]) -> Any:
    """iPlant 植物智——中文植物名（含俗名）→ 学名 + 分类（HTML 解析，免认证）。

    补的是 argo 一条硬盲区：此前没有任何从中文名进入生物数据库的路。gbif 对纯
    中文查询返回的是无关属种（实测「玉米」首条是 Frithia 属；补 qField=VERNACULAR
    仍是垃圾），本仓 gbif builder 也因此在中文查询上直接短路。iPlant 的 /info/{名}
    直接吃中文名、学名、属名、科名，实测时延 0.29–0.41s。

    **三级判据，少一级就错**（每级的实测依据见下）：
      1. `spno` 为空 → 未收录 → 诚实空。**不能用 HTTP 状态码**：不存在的名字同样
         返回 200（只是页面小 1.8KB）；**也不能用 latin2**：未收录时它把查询词本身
         填回去（查 zzzz → latin2=zzzznotexist），看着像命中。
      2. `spno` 非空且 `latin2` 非空 → 直接命中（玉米 / 牡丹 / 银杏）。
      3. `spno` 非空但 `latin2` 为空 → 别名或泛称，接受名在「您是否要找」块里。
         **这是俗名查询的主路径而非边缘情况**：玉米页列出的 5 个俗名（包谷 / 苞米 /
         玉蜀黍 / 珍珠米 / 麻蜀棒子）逐个反查，全部走这一级——只做前两级等于对
         中文俗名基本失效。

    `systype` 过滤非植物：该站带「名称校对」功能，查「霸王龙」返回恐龙条目、
    「蘑菇」返回菌物条目，对植物源是噪声。实测 1=植物 2=动物 3=菌物，只放行 1；
    字段缺失时不拦（判不出类群就不拦截，宁放过不误杀）。

    分类链来自 `/ashx/getspinfos.ashx?spid=&type=classsys`（免认证，实测可直调），
    返回 8 套分类系统的 HTML 片段，取第一套——站点的默认展示（种>属>科>目>纲）。
    植物志正文（frps/foc）、分布、标本都是 AJAX 填充且内部接口不可直调（参数空间
    已试开，除本接口外一律空返回），不做承诺。
    """
    timeout = spec.get("timeout", 12)
    engine_name = spec.get("_name", "iplant")

    def _fetch(path: str, to: float) -> str | None:
        return _http_get_raw(f"{_IPLANT_HOST}{path}", _IPLANT_HEADERS, to, engine=engine_name)

    def _entry(path: str, to: float) -> dict[str, Any] | None:
        """取一个物种条目。未收录、非植物、取不到都返回 None。"""
        html = _fetch(path, to)
        if not html:
            return None
        v = dict(_IPLANT_VAR_RE.findall(html))
        if not v.get("spno"):
            return None
        if v.get("systype") and v["systype"] != "1":
            return None
        return {"spno": v["spno"], "cname": v.get("spcname", ""),
                "latin": v.get("latin2", ""), "html": html}

    def _classsys(spno: str, to: float) -> list[str]:
        """分类链（第一套系统），从种到门；取不到返回空列表（分类是增益不是前提）。"""
        raw = _fetch(f"/ashx/getspinfos.ashx?spid={spno}&type=classsys", to)
        if not raw:
            return []
        try:
            chains = (json.loads(raw) or {}).get("classsys") or []
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(chains, list) or not chains:
            return []
        return re.findall(r"<a[^>]*>([^<]*)</a>", str(chains[0]))

    def _suggest(html: str) -> list[dict[str, str]]:
        """「您是否要找」里的接受名。href 带内部 id，需再取一次才有 spno 与分类。"""
        blk = _IPLANT_SUGGEST_RE.search(html)
        if not blk:
            return []
        out = []
        for href, text in _IPLANT_ANCHOR_RE.findall(blk.group(1)):
            label = re.sub(r"\s+", " ", text).strip()
            m = re.match(r"^(\S+)\s+([A-Z][A-Za-z.\s×x]*)$", label)
            out.append({"href": href,
                        "cname": m.group(1) if m else label,
                        "latin": m.group(2).strip() if m else ""})
        return out

    def _rel_path(href: str) -> str:
        """页面里的相对 href → 可直接 GET 的路径。名字部分要转义，?id= 原样保留。"""
        path, _, qs = href.partition("?")
        return f"{urllib.parse.quote(path)}?{qs}" if qs else urllib.parse.quote(path)

    def _names(html: str, pat: re.Pattern) -> list[str]:
        """从「俗名：」「异名：」块取锚文本。"""
        blk = pat.search(html or "")
        if not blk:
            return []
        return [re.sub(r"\s+", " ", t).strip()
                for t in re.findall(r"<a[^>]*>([^<]+)</a>", blk.group(1)) if t.strip()]

    def _result(e: dict[str, Any], ranks: list[str], q: str, idx: int) -> dict[str, Any]:
        cname = e.get("cname") or q
        latin = e.get("latin") or ""
        html = e.get("html") or ""
        bits = [f"学名 {latin}"] if latin else []
        # 分类链首段是种本身，取其后三段当属/科/目
        bits += [f"{lab} {val}" for lab, val in zip(("属", "科", "目"), ranks[1:4])]
        vern = _names(html, _IPLANT_VERN_RE)
        if vern:
            bits.append("俗名 " + "、".join(vern[:6]))
        return {
            "title": f"{cname} {latin}".strip(),
            "url": f"{_IPLANT_HOST}/info/{urllib.parse.quote(cname)}",
            "snippet": " | ".join(bits)[:300],
            "source": engine_name,
            "score": rank_score(0.95, idx),
        }

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = _IPLANT_NOISE_RE.sub("", query).strip()
        if not q or len(q) > 40:
            return []
        to = _timeout or timeout
        head = _entry(f"/info/{urllib.parse.quote(q)}", to)
        if head is None:
            return []
        if head["latin"]:
            return [_result(head, _classsys(head["spno"], to), q, 0)]
        # 第三级：别名/泛称 → 取「您是否要找」给的接受名
        out: list[dict[str, Any]] = []
        for s in _suggest(head["html"])[: max(int(n), 1)]:
            acc = _entry(_rel_path(s["href"]), to)
            if acc is None:
                # 接受名页取不到（改版/超时）时退一步用提示里的名字对，仍比空手强
                if not s["latin"]:
                    continue
                acc = {"spno": "", "cname": s["cname"], "latin": s["latin"], "html": ""}
            ranks = _classsys(acc["spno"], to) if acc["spno"] else []
            out.append(_result(acc, ranks, q, len(out)))
        return out

    return _engine


def _build_people_daily_engine(spec: dict[str, Any]) -> Any:
    """人民网搜索（权威综合中文新闻，官方接口，免认证）。

    走 http 而非 https（https 301 丢 body）；标题/正文带 <em> 高亮标签；
    displayTime 为毫秒时间戳。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        req = urllib.request.Request(
            "http://search.people.cn/search-platform/front/search",
            data=json.dumps({
                "key": q, "page": 1, "limit": min(max(int(n), 1), 20),
                "hasTitle": True, "hasContent": True, "isFuzzy": True,
                "type": 0, "sortType": 2, "startTime": 0, "endTime": 0,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "argo-search/1.0 (+people_daily)"},
            method="POST",
        )
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "people_daily")) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"人民网引擎失败: {e}")
            return []
        records = ((data.get("data") or {}).get("records")) or []
        out = []
        for r in records[:max(int(n), 1)]:
            title = _strip_hl(r.get("title"))
            if not title:
                continue
            ts = str(r.get("displayTime") or "").strip()
            published = ""
            if ts.isdigit():
                published = time.strftime("%Y-%m-%d", time.localtime(int(ts) / 1000))
            content = _strip_hl(r.get("content"))[:280]
            out.append({
                "title": title,
                "url": str(r.get("url") or "").strip(),
                "snippet": content,
                "source": "people_daily",
                "published_at": published,
            })
        return out
    return _engine


# flk 时效性枚举（官网 enumData/前端常量）：1 已废止 / 2 已修改 / 3 有效 / 4 尚未生效
_FLK_SXX = {1: "已废止", 2: "已修改", 3: "有效", 4: "尚未生效"}

# flk 的模糊检索对「第 N 条」内部带空格的写法返回 0 条，压掉空格后返回
# 8-10 条（实测 2026-09-12：「民法典 第 1062 条」→「民法典 第1062条」）。
# 只压「第…条」内部的空白，不动查询里其余空格——法名与并列检索词之间的
# 空格是有语义的分隔（「个人信息保护 数据安全」必须原样透传）。
_FLK_ARTICLE_SPACE_RE = re.compile(r"第\s*(\d{1,7})\s*条")


def _build_flk_law_engine(spec: dict[str, Any]) -> Any:
    """国家法律法规数据库（法律/行政法规/司法解释/地方性法规，权威法条源）。

    接口为 flk SPA 前端逆向：searchRange 1=标题 2=正文；searchType 1=精确
    2=模糊；orderByParam 必须是 {order, sort} 对象（扁平字符串后端 500）。
    无验证码无签名；失败诚实空，不做绕过。
    """
    timeout = spec.get("timeout", 25)
    try:
        from http_client import register_spec_limit
        register_spec_limit(spec.get("_name", "flk_law"), None, 2500)
    except ImportError:
        pass

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = _FLK_ARTICLE_SPACE_RE.sub(r"第\1条", query.strip())
        if not q:
            return []
        to = _timeout or timeout
        req = urllib.request.Request(
            "https://flk.npc.gov.cn/law-search/search/list",
            data=json.dumps({
                "searchRange": 1, "sxrq": [], "gbrq": [], "searchType": 2,
                "sxx": [], "gbrqYear": [], "flfgCodeId": [], "zdjgCodeId": [],
                "searchContent": q,
                "orderByParam": {"order": "-1", "sort": ""},
                "pageNum": 1, "pageSize": min(max(int(n), 1), 20),
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json;charset=utf-8",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Referer": "https://flk.npc.gov.cn/",
                "Accept": "application/json",
            },
            method="POST",
        )
        # 服务端间歇性掐断 TLS 握手（实测约半数首连失败），单次退避重试消化波动
        data = None
        for attempt in range(2):
            try:
                with http_open(req, timeout=to, engine=spec.get("_name", "flk_law")) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(4)
                    continue
                logger.warning(f"flk 引擎失败: {e}")
                return []
        if data is None:
            return []
        if not isinstance(data, dict) or data.get("code") != 200:
            return []
        out = []
        for r in (data.get("rows") or [])[:max(int(n), 1)]:
            title = _strip_hl(r.get("title"))
            if not title:
                continue
            sxx = _FLK_SXX.get(r.get("sxx"), "")
            bits = [str(r.get("flxz") or "").strip(), sxx,
                    str(r.get("zdjgName") or "").strip()]
            if r.get("gbrq"):
                bits.append(f"公布 {r['gbrq']}")
            if r.get("sxrq"):
                bits.append(f"施行 {r['sxrq']}")
            bbbs = str(r.get("bbbs") or "").strip()
            out.append({
                "title": title,
                "url": f"https://flk.npc.gov.cn/detail?id={bbbs}" if bbbs else "",
                "snippet": " · ".join(b for b in bits if b),
                "source": "flk_law",
                "published_at": str(r.get("gbrq") or ""),
            })
        return out
    return _engine


def _build_wikisource_engine(spec: dict[str, Any]) -> Any:
    """维基文库（古文/公版文献全文，MediaWiki API，免认证）。

    标题含空格需编码后拼 wiki 路径——声明式 url_template 不做编码，
    故走 builder。snippet 带 <span class="searchmatch"> 高亮，剥离保文本。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        url = ("https://zh.wikisource.org/w/api.php?action=query&format=json&list=search"
               f"&srsearch={urllib.parse.quote(q)}&srlimit={min(max(int(n), 1), 20)}")
        raw = _http_get_raw(url, {"User-Agent": "argo-search/1.0 (+wikisource)", "Accept": "application/json"},
                            to, engine=spec.get("_name", "wikisource"))
        if raw is None:
            return []
        try:
            items = ((json.loads(raw) or {}).get("query") or {}).get("search") or []
        except (json.JSONDecodeError, ValueError):
            return []
        out = []
        for it in items[:max(int(n), 1)]:
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            snippet = re.sub(r"\s+", " ", _TAG_RE.sub("", str(it.get("snippet") or ""))).strip()
            out.append({
                "title": title,
                "url": "https://zh.wikisource.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
                "snippet": snippet[:300],
                "source": "wikisource",
                "published_at": str(it.get("timestamp") or ""),
            })
        return out
    return _engine


# ── 微博热搜榜引擎 ─────────────────────────────────────────────────────────────

def _build_weibo_hot_engine(spec: dict[str, Any]) -> Any:
    """微博热搜榜（weibo.com/ajax/side/hotSearch，免登录）

    榜单型源：查询词只作路由触发，不参与内容过滤（见 config 的 relevance_check: false）。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = "https://weibo.com/ajax/side/hotSearch"
        headers = {
            "User-Agent": "Mozilla/5.0",
            # 缺 Referer 时上游直接返回 {"error":"Forbidden"}（实测）
            "Referer": "https://weibo.com/",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                d = json.loads(resp.read().decode("utf-8", "replace"))
            rows = (d.get("data") or {}).get("realtime") or []
            results = []
            for i, it in enumerate(rows):
                if len(results) >= n:
                    break
                word = str(it.get("word") or "").strip()
                if not word:
                    continue
                rank = it.get("realpos") or (i + 1)
                label = str(it.get("label_name") or "").strip()
                num = it.get("num") or 0
                results.append({
                    "title": word[:80],
                    "url": "https://s.weibo.com/weibo?q=" + urllib.parse.quote("#" + word + "#"),
                    "snippet": f"微博热搜 第{rank}位 · 热度 {num}" + (f" · {label}" if label else ""),
                    "source": "weibo_hot",
                    "score": max(1.0 - len(results) * 0.02, 0.2),
                })
            return results
        except Exception as e:
            logger.warning(f"微博热搜榜失败: {e}")
            return []
    return _engine


# ── 抖音热榜引擎 ─────────────────────────────────────────────────────────────

def _build_douyin_hot_engine(spec: dict[str, Any]) -> Any:
    """抖音热榜（iesdouyin web api，免认证，榜单型）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                d = json.loads(resp.read().decode("utf-8", "replace"))
            rows = d.get("word_list") or []
            results = []
            for i, it in enumerate(rows):
                if len(results) >= n:
                    break
                word = str(it.get("word") or "").strip()
                if not word:
                    continue
                hot = it.get("hot_value") or 0
                results.append({
                    "title": word[:80],
                    "url": "https://www.douyin.com/search/" + urllib.parse.quote(word),
                    "snippet": f"抖音热榜 第{i + 1}位 · 热度 {hot}",
                    "source": "douyin_hot",
                    "score": max(1.0 - len(results) * 0.02, 0.2),
                })
            return results
        except Exception as e:
            logger.warning(f"抖音热榜失败: {e}")
            return []
    return _engine


# ── CSDN 搜索 ────────────────────────────────────────────────────────────────

_EM_TAG_RE = re.compile(r"</?em>")


def _build_csdn_engine(spec: dict[str, Any]) -> Any:
    """CSDN 搜索（so.csdn.net v3 API，免认证）

    标题与摘要是带 <em> 高亮的片段，URL 带 utm/request_id 追踪参数——两者都在这里清掉，
    否则召回分会被标签噪声拉低、URL 也无法直接复用。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        params = urllib.parse.urlencode({"q": query, "t": "blog", "p": 1})
        url = f"https://so.csdn.net/api/v3/search?{params}"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://so.csdn.net/"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                d = json.loads(resp.read().decode("utf-8", "replace"))
            rows = d.get("result_vos") or []
            results = []
            for it in rows:
                if len(results) >= n:
                    break
                title = _EM_TAG_RE.sub("", str(it.get("title") or "")).strip()
                link = str(it.get("url") or "").split("?")[0]
                if not title or not link:
                    continue
                desc = _EM_TAG_RE.sub(
                    "", str(it.get("description") or it.get("digest") or "")
                ).strip()
                results.append({
                    "title": title[:120],
                    "url": link,
                    "snippet": re.sub(r"\s+", " ", desc)[:220],
                    "source": "csdn",
                    "author": str(it.get("author") or ""),
                    "published_at": str(it.get("created_at") or it.get("create_time_str") or ""),
                })
            return results
        except Exception as e:
            logger.warning(f"CSDN 搜索失败: {e}")
            return []
    return _engine


# ── 华尔街见闻快讯引擎 ─────────────────────────────────────────────────────────

def _build_wallstreetcn_engine(spec: dict[str, Any]) -> Any:
    """华尔街见闻快讯（lives 直播流，免认证）

    与财联社电报同为「全量快讯流 + 本地关键词过滤」形态：带具体主题的查询做过滤，
    纯触发词（快讯/财经等）放行全量榜单，避免过滤后空结果。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        from datetime import datetime
        to = _timeout or timeout
        # 关键词过滤时放大拉取量：上游 limit 上限就是 100，过滤窗口开满才不至于空手
        fetch_n = min(max(int(n) * 5, 60), 100)
        url = (
            "https://api-one.wallstcn.com/apiv1/content/lives"
            f"?channel=global-channel&limit={fetch_n}"
        )
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://wallstreetcn.com/"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                d = json.loads(resp.read().decode("utf-8", "replace"))
            rows = (d.get("data") or {}).get("items") or []
            keywords = query.strip().split() if _should_filter(query) else []
            results = []
            for it in rows:
                if len(results) >= n:
                    break
                title = str(it.get("title") or "").strip()
                content = str(it.get("content_text") or "")
                if keywords and not any(
                    k.lower() in (title + content).lower() for k in keywords
                ):
                    continue
                ts = it.get("display_time")
                t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
                results.append({
                    "title": (title or content[:60]).strip()[:100],
                    "url": str(it.get("uri") or "https://wallstreetcn.com/live/global"),
                    "snippet": (f"{t} | {content}" if t else content)[:220],
                    "source": "wallstreetcn",
                    "published_at": t,
                })
            return results
        except Exception as e:
            logger.warning(f"华尔街见闻快讯失败: {e}")
            return []
    return _engine


# ── 中国天气网引擎 ─────────────────────────────────────────────────────────────

# 查询里的自然语言噪声：城市名之外的部分全部剥掉（"上海今天天气怎么样" → "上海"）
# 「今日/明日/昨日」是口语高频形态（与「今天」等价），漏掉会让联想接口拿
# 「今日北京」查不到 cityid → 引擎静默返回 0 条。
_WEATHER_CN_NOISE_RE = re.compile(
    r"(今[天日晚]|明[天日晚]|后[天日]|昨[天日]|现在|实时|当前|查询|怎么样|如何|多少度|"
    r"气温|温度|天气|预报|阴晴|下雨|的|了|呢|吗|\?|？|，|,|。|\s)+"
)


def _build_weather_cn_engine(spec: dict[str, Any]) -> Any:
    """中国天气网（城市联想 → 实况，免认证，两步）

    第一步 toy1.weather.com.cn/search 把城市名换成 cityid，第二步 data/sk/<id>.html 取实况。
    联想接口返回 JSONP 外壳（`([...])` 或 `callback([...])`），这里把外层剥掉再解析。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        city = _WEATHER_CN_NOISE_RE.sub("", query or "").strip() or (query or "").strip()
        if not city:
            return []
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.weather.com.cn/",
        }
        try:
            u1 = "https://toy1.weather.com.cn/search?cityname=" + urllib.parse.quote(city)
            with http_open(urllib.request.Request(u1, headers=headers),
                           timeout=to, engine=spec.get("_name", "")) as resp:
                raw = resp.read().decode("utf-8", "replace").strip()
            if not raw.startswith("["):
                lo, hi = raw.find("("), raw.rfind(")")
                raw = raw[lo + 1:hi] if lo >= 0 and hi > lo else raw
            cand = json.loads(raw) or []
            if not cand:
                return []
            parts = str(cand[0].get("ref") or "").split("~")
            if not parts or not parts[0]:
                return []
            cid = parts[0]
            name = parts[2] if len(parts) > 2 else city

            # 注意：老的 /data/sk/<id>.html 已 301 到 HTML 页（不再吐 JSON），
            # 现役实况端点是 d1 的 sk_2d，响应为 JS 赋值 `var dataSK={...}`。
            u2 = f"https://d1.weather.com.cn/sk_2d/{cid}.html"
            with http_open(urllib.request.Request(u2, headers=headers),
                           timeout=to, engine=spec.get("_name", "")) as resp:
                body = resp.read().decode("utf-8", "replace").strip()
            brace = body.find("{")
            sk = json.loads(body[brace:]) if brace >= 0 else {}
            if not sk:
                return []
            return [{
                "title": (f"{sk.get('cityname') or name} 实况：{sk.get('temp', '')}℃ "
                          f"{sk.get('weather', '')} {sk.get('WD', '')}{sk.get('WS', '')}"),
                "url": f"https://www.weather.com.cn/weather/{cid}.shtml",
                "snippet": (f"湿度 {sk.get('SD', '')} · 气压 {sk.get('qy', '')}hPa · "
                            f"AQI {sk.get('aqi', '')} · 能见度 {sk.get('njd', '')} · "
                            f"观测 {sk.get('time', '')} · {sk.get('date', '')}"),
                "source": "weather_cn",
            }]
        except Exception as e:
            logger.warning(f"中国天气网失败: {e}")
            return []
    return _engine


# ── 360 搜索（so.com）───────────────────────────────────────────────────────

def _build_so_engine(spec: dict[str, Any]) -> Any:
    """360 搜索（so.com/s?q=，HTML 解析）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://www.so.com/s?q=" + up.quote(query)
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.so.com/"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                page = resp.read().decode("utf-8", "replace")
            results = []
            # 360 搜索结果块：<li class="res-list">...<h3><a href="...">title</a>
            for m in re.finditer(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
                url_m, title_m = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
                if not title_m or not url_m:
                    continue
                results.append({
                    "title": title_m[:80],
                    "url": url_m,
                    "snippet": "",
                    "source": "so",
                    "score": max(1.0 - len(results) * 0.1, 0.1),
                })
                if len(results) >= n:
                    break
            return results
        except Exception as e:
            logger.warning(f"360 搜索失败: {e}")
            return []
    return _engine


# ── 神马搜索（sm.cn，移动端）────────────────────────────────────────────────

def _build_shenma_engine(spec: dict[str, Any]) -> Any:
    """神马搜索（m.sm.cn/s?q=，移动端 HTML 解析）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = "https://m.sm.cn/s?q=" + up.quote(query)
        headers = {
            "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
            "Referer": "https://m.sm.cn/",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                page = resp.read().decode("utf-8", "replace")
            results = []
            # 神马结果块：<a href="..." class="...">title</a> + snippet
            for m in re.finditer(r'<a[^>]*href="(https?://[^"]+)"[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</a>', page, re.S):
                url_m, title_m = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
                if not title_m or not url_m or "sm.cn" in url_m:
                    continue
                results.append({
                    "title": title_m[:80],
                    "url": url_m,
                    "snippet": "",
                    "source": "shenma",
                    "score": max(1.0 - len(results) * 0.1, 0.1),
                })
                if len(results) >= n:
                    break
            return results
        except Exception as e:
            logger.warning(f"神马搜索失败: {e}")
            return []
    return _engine

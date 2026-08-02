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



# ── 东财财经搜索引擎 ─────────────────────────────────────────────────────────

def _build_eastmoney_engine(spec: dict[str, Any]) -> Any:
    """东财经搜搜索（纯 HTTP API，零外部依赖）

    支持：
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
        """按关键词搜全球资讯"""
        import uuid
        url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        params = {
            "client": "web", "biz": "web_724", "fastColumn": "102",
            "sortEnd": "", "pageSize": str(n * 2),
            "req_trace": str(uuid.uuid4()),
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://kuaixun.eastmoney.com/"}
        try:
            full_url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
            req = urllib.request.Request(full_url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("fastNewsList", [])[:n]:
                title = item.get("title", "")
                summary = (item.get("summary", "") or "")[:200]
                if query:
                    keywords = query.strip().split()
                    if not any(kw.lower() in (title + summary).lower() for kw in keywords):
                        continue
                results.append({
                    "title": title[:80],
                    "url": "https://kuaixun.eastmoney.com/",
                    "snippet": f"{item.get('showTime', '')} | {summary}",
                    "source": "eastmoney",
                })
            return results
        except Exception as e:
            logger.warning(f"东财资讯搜索失败: {e}")
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



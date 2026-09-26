#!/usr/bin/env python3
"""批次十构建器：头条全文搜索 / 购物族 ddg_site / zhihu_global 补强。

为什么不在 engines_builders_cn.py 里加（任务原指定位置）：该文件被
test_module_size_gate.py 祖父清单冻结在 2319 行（当前恰好 2319，只能减不能增），
且 tests/ 门禁只放行新建测试文件——门禁自身文档写明「想加功能，正解是拆模块，
不是把数字往上调」，故按 batch9 命名先例落新文件（新增文件天然合规）。

zhihu_global 说明：cn 里的旧实现与本文件同名实现并存，engines.py 注册表已
指向本文件版本（候选池取满 + 错误文案行动提示两处补强）；cn 旧版仅在
engines_builders 聚合层保留转出，属被取代代码。
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
    _detect_anti_bot,
    _http_get_raw,
    _load_parse_maps,
    http_open,
    note_failure,
    rank_score,
    safe_search,
)
from engines_builders_cn import _parse_site_filter

logger = logging.getLogger("unified_search.engines")


# ── 头条搜索（content API，与 toutiao_hot 热榜是两个引擎）─────────────────────

def _build_toutiao_engine(spec: dict[str, Any]) -> Any:
    """头条全文搜索（/api/search/content，免认证）。

    与 _build_toutiao_hot_engine（hot-board 热榜）端点不同：本引擎吃查询词
    返回站内文章/视频。配方来源 last30days-skill-cn/scripts/lib/toutiao.py
    （2026-09-26 实测口径）：缺 tt_webid Cookie 时 data 恒为 null，必须带上。

    诚实空约定：上游 shark 反爬（data=null + shark_decision=reject）、无结果、
    网络失败一律返回 []——不降级伪装成成功。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        url = (
            "https://www.toutiao.com/api/search/content/?keyword="
            + urllib.parse.quote(query or "")
            + f"&count={min(int(n or 10), 20)}&offset=0"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.toutiao.com/",
            "Cookie": "tt_webid=1",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"头条搜索失败: {e}")
            return []
        items = data.get("data")
        # data 为 null（shark reject / 无结果）或非列表 → 诚实空
        if not isinstance(items, list):
            return []
        results = []
        for it in items[:n]:
            if not isinstance(it, dict):
                continue
            # 标题/摘要带 <em> 高亮标签：先剥再截，尾巴不占正文
            title = re.sub(r"<[^>]+>", "", str(it.get("title") or "")).strip()
            url_ = str(it.get("article_url") or it.get("display_url") or "")
            if url_ and not url_.startswith("http"):
                url_ = "https://www.toutiao.com" + url_
            if not title and not url_:
                continue
            abstract = re.sub(r"<[^>]+>", "", str(it.get("abstract") or "")).strip()
            ts = it.get("publish_time") or it.get("behot_time")
            try:
                published_at = (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts))) if ts else None
                )
            except (TypeError, ValueError, OSError, OverflowError):
                published_at = None
            results.append({
                "title": title[:200],
                "url": url_,
                "snippet": abstract[:300],
                "source": "toutiao",
                "score": rank_score(0.7, len(results)),
                "published_at": published_at,
                # source/media_name 是稿源媒体名（如「人民日报」），与引擎名区分
                "site_name": str(it.get("source") or it.get("media_name") or ""),
            })
        return results
    return _engine


# ── 购物族 ddg_site：SAC 电商「直连」的 DDG site: 语法包装 ─────────────────────
# jd/taobao/pdd/dangdang/suning/kaola 的站内搜索无一例外登录墙/强反爬；
# SAC（SAC_search/dsh-tool-websearch）的电商适配器实为 DuckDuckGo site:
# 语法包装（site-search.ts / pdd.ts / taobao.ts）。一个工厂服务六个声明，
# spec.domain 区分站点，不逐源复制 builder。

_DDGSITE_URL = "https://html.duckduckgo.com/html/"
_DDGSITE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _unwrap_ddg_link(href: str) -> str:
    """DDG 结果链接 → 真实目标 URL；广告壳返回空串由调用方丢弃。

    html 端点的 result__a href 是 //duckduckgo.com/l/?uddg=<urlencode 目标>
    &rut=... 跳转壳：不拆壳则产物是随 rut 轮换、不可核验的代理地址（与
    xhs/zhihu 的「URL 可验证性」要求相悖）。拆出的目标若仍指向
    duckduckgo.com（y.js 广告跳转），返回空串——广告不进结果。
    """
    if "uddg=" not in (href or ""):
        return href or ""
    from urllib.parse import parse_qs, urljoin, urlsplit
    abs_url = urljoin("https://duckduckgo.com", href)
    target = (parse_qs(urlsplit(abs_url).query).get("uddg") or [""])[0]
    if not target:
        return ""
    if urlsplit(target).netloc.lower().endswith("duckduckgo.com"):
        return ""
    return target


def _build_ddg_site_engine(spec: dict[str, Any]) -> Any:
    """DuckDuckGo site: 语法站点限定搜索（购物族薄引擎工厂）。

    spec.domain 支持字符串或列表：列表按 SAC pdd.ts 的写法拼 OR
    （pdd: [pinduoduo.com, yangkeduo.com]）。选择器复用 parse_maps 的
    local_duckduckgo 段（同一上游同一布局，不另维护一份映射）。
    0 结果时去掉 site: 前缀重试一次（SAC searchSiteWithFallback 降级思路：
    电商子页收录稀疏，全 web 兜底比诚实空之外伪装成功好）；仍 0 就诚实空。
    注意重试仅在「取到页面但解析 0 条」时触发——网络失败/拦截页不重试，
    否则把最坏代价翻倍（同 _http_get_raw 不做连接级重试的同一笔账）。
    """
    timeout = spec.get("timeout", 8)
    raw_domain = spec.get("domain") or ""
    domains = [str(d).strip() for d in (raw_domain if isinstance(raw_domain, list) else [raw_domain])
               if d and str(d).strip()]
    if not domains:
        raise ValueError(f"ddg_site 引擎 {spec.get('_name') or '?'} 缺 domain 字段")
    site_clause = " OR ".join(f"site:{d}" for d in domains)

    def _ddg_pass(q: str, n: int, to: float, engine: str) -> tuple[bool, list[dict[str, Any]]]:
        """单次 DDG html 查询。返回 (是否取到正常页面, 解析结果)。

        ok=False 仅用于网络失败/拦截页——这两种状态不触发 site: 降级重试。
        """
        full_url = f"{_DDGSITE_URL}?q={urllib.parse.quote(q)}"
        html = _http_get_raw(full_url, _DDGSITE_HEADERS, to, engine=engine)
        if not html:
            return False, []
        if _detect_anti_bot(html):
            # 202 challenge 页状态码正常但正文无结果（见 test_antibot_ddg）：
            # 拦截页必须归因 blocked，静默空会让熔断层把封锁当「无结果」
            note_failure(engine, "blocked", "anti-bot-page", full_url)
            return False, []
        mapping = _load_parse_maps().get("html", {}).get("local_duckduckgo", {})
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
        except Exception as e:
            logger.warning(f"ddg_site 解析失败: {e}")
            return True, []
        title_sel = mapping.get("title", "a.result__a")
        url_sel = mapping.get("url", "a.result__a")
        snippet_sel = mapping.get("snippet", "a.result__snippet")
        base_score = mapping.get("score", 0.7)
        results: list[dict[str, Any]] = []
        for item in soup.select(mapping.get("container", ".result"))[: n * 2]:
            t_el = item.select_one(title_sel)
            u_el = item.select_one(url_sel)
            s_el = item.select_one(snippet_sel) if snippet_sel else None
            title = t_el.get_text(strip=True)[:200] if t_el else ""
            url = _unwrap_ddg_link(u_el.get("href") if u_el else "")
            snippet = s_el.get_text(strip=True)[:300] if s_el else ""
            if not title or not url:
                continue
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "source": engine or "ddg_site",
                "score": max(base_score - len(results) * 0.05, 0.1),
            })
        return True, results[:n]

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        if not (query or "").strip():
            return []
        to = _timeout or timeout
        engine = spec.get("_name", "")
        ok, results = _ddg_pass(f"{site_clause} {query.strip()}", n, to, engine)
        if ok and not results:
            _, results = _ddg_pass(query.strip(), n, to, engine)
        return results
    return _engine


# ── zhihu_global 补强版（取代 engines_builders_cn 同名实现）───────────────────

# 端点 Count 上限：官方 global_search 上限 20（cn 旧版 min(n, 20) 同源口径）
_ZHIHU_COUNT_MAX = 20

# 非成功响应 → 可行动提示。时钟偏差类按 Message 文案识别（错误码表未公开，
# 不猜码值）：签名带 X-Request-Timestamp，本机时钟漂移会让服务端判签名过期，
# 现象是「密钥没错却全挂」——提示必须指向校时，防止用户误判 key 失效反复换 key
_ZHIHU_CLOCK_HINT_RE = re.compile(r"timestamp|时间戳|签名|时钟|clock", re.I)
_ZHIHU_CLOCK_HINT = "本机时钟可能偏差导致签名过期：先校准系统时间再重试，不必反复换密钥"


def _zhihu_error_item(code: Any, message: Any) -> dict[str, Any]:
    """非成功响应的 error item：保留原始 code/message，时钟类追加行动提示。"""
    text = f"zhihu_global Code={code} {str(message or '')[:100]}"
    if _ZHIHU_CLOCK_HINT_RE.search(str(message or "")):
        text = f"{text}；hint: {_ZHIHU_CLOCK_HINT}"
    return {"error": text, "source": "zhihu_global"}


def _build_zhihu_global_engine(spec: dict[str, Any]) -> Any:
    """知乎开放平台全网搜索（developer.zhihu.com global_search）。

    在 cn 旧版语义之上的两处微机制补强（2026-09-26）：
      1. 候选池取满再截断：Filter host== 是服务端在候选池上后筛，只取 n 条
         会把「过滤后恰好命中」的候选一并筛掉，空结果会被模型读成「知乎没有
         相关内容」；带站点限定或时间下限（since）时取满端点 Count 上限，
         客户端再截断到 n。since 的时间 Filter 语法官方未文档化，先只承担
         「取满候选池」语义。
      2. 错误码 → 行动提示：时钟偏差类错误给「校时」提示而非裸错误。
    查询语法与 cn 版一致：site:/host: → Filter host==，普通查询 SearchDB=all。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        secret = get_env(["ARGO_ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET"])
        if not secret:
            return []

        # 解析 site:/host: 站点限定语法 → Filter: host=="..."（实现单点在 cn）
        filter_expr, search_query = _parse_site_filter(query)

        filtered = bool(filter_expr or kwargs.get("since"))
        params: dict[str, Any] = {
            "Query": search_query or query,
            "Count": str(_ZHIHU_COUNT_MAX if filtered else min(int(n or 5), _ZHIHU_COUNT_MAX)),
            "SearchDB": "all",
        }
        if filter_expr:
            params["Filter"] = filter_expr
        url = "https://developer.zhihu.com/api/v1/content/global_search?" + urllib.parse.urlencode(params)
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
            # 「没配置」「鉴权失败」「没结果」区分开才可行动
            return [{"error": f"zhihu_global API HTTP {e.code}", "source": "zhihu_global"}]
        except Exception as e:
            logger.warning(f"zhihu_global 失败: {e}")
            return [{"error": f"zhihu_global {type(e).__name__}: {e}", "source": "zhihu_global"}]
        if data.get("Code") not in (0, None):
            # 30001=频率限制 30002=配额限制，显式暴露供配额状态机归类
            logger.warning(f"zhihu_global 返回码异常: {data.get('Code')} {data.get('Message')}")
            return [_zhihu_error_item(data.get("Code"), data.get("Message"))]
        items = (data.get("Data") or {}).get("Items") or []
        results = []
        for _rk3, item in enumerate(items[:n]):
            if not isinstance(item, dict):
                continue
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

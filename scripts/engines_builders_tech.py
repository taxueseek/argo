#!/usr/bin/env python3
"""专用构建器：技术社区 / 文档 / AI 搜索"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from engines_base import (safe_search, _run, _resolve, _get_path, _coerce_field, rank_score,
                          _http_get_raw, mcp_error_of as _mcp_error_of, http_open)
from engine_env import get_env

logger = logging.getLogger("unified_search.engines")

# ── Exa 专用引擎 ──────────────────────────────────────────────────────────────

_EXA_MCP_URL = "https://mcp.exa.ai/mcp"

# Exa MCP 文本应答的字段行。Published 的实际标签是 "Published Date"，
# 归一成 published；Highlights 可能折行，由解析器把续行并入上一字段。
_EXA_BLOCK_LABEL_RE = re.compile(
    r"^(Title|URL|Published(?:\s+Date)?|Author|Highlights?):\s*(.*)$")


def _parse_exa_text_blocks(text: str) -> list[dict[str, str]]:
    """把 Exa MCP web_search_exa 的 text content 解析成字段 dict 列表。

    应答文本是逐条目的标签行（Title:/URL:/...），块以新的 Title: 行开始；
    不属于任何标签行的非空行是上一字段的续行（Highlights 摘要常被折行）。
    """
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    last_key = ""
    for line in (text or "").splitlines():
        m = _EXA_BLOCK_LABEL_RE.match(line.strip())
        if m:
            key = m.group(1).split()[0].lower()  # "Published Date" → published
            val = m.group(2).strip()
            if key == "title" and cur:
                blocks.append(cur)
                cur = {}
            if val:
                cur[key] = val
            else:
                cur.setdefault(key, "")
            last_key = key
        elif line.strip() and cur and last_key:
            cur[last_key] = (cur[last_key] + " " + line.strip()).strip()
    if cur:
        blocks.append(cur)
    return blocks


def _parse_mcp_wire(raw: str) -> dict | None:
    """把 MCP 应答解析成 JSON-RPC dict；SSE 与纯 JSON 两种形态都支持。

    MCP 的 HTTP 传输没有强约定：text/event-stream 时应答藏在 data: 行里，
    application/json 时整个 body 就是一个 JSON-RPC 响应。两者都必须吃下，
    否则同一通道会随服务端 content-type 切换而「时好时坏」。SSE 场景下取
    第一个含 result/error 的 data 行（MCP 单请求-单应答，不做多事件聚合）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
            return obj
    return None


def _exa_mcp_keyless(query: str, n: int, to: float,
                     spec: dict[str, Any]) -> list[dict[str, Any]]:
    """exa 免 key 匿名通道：POST Exa 托管 MCP，调 web_search_exa 工具。

    任何一层失败（网络 / 非 MCP 应答 / JSON-RPC 错误 / isError）都返回
    带 error 的记录——匿名通道是免费的兼容路径，坏了必须让调度层看见，
    静默返回 [] 会和「真没结果」混淆并掩盖通道失效。
    """
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "web_search_exa",
                   "arguments": {"query": query, "numResults": min(n, 10)}},
    }).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               # mcp.exa.ai 在 Cloudflare 之后：默认 Python-urllib UA 直接
               # 403（实测 2026-09-26），必须带浏览器 UA
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/120.0.0.0 Safari/537.36"}
    req = urllib.request.Request(_EXA_MCP_URL, data=body, headers=headers)
    try:
        with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as e:
        logger.warning(f"exa 匿名 MCP 通道失败: {e}")
        return [{"error": f"exa mcp: {type(e).__name__}: {e}", "source": "exa"}]
    data = _parse_mcp_wire(raw)
    if not isinstance(data, dict):
        return [{"error": "exa mcp: 响应不是可解析的 MCP 应答（SSE/JSON 均失败）",
                 "source": "exa"}]
    mcp_err = _mcp_error_of(data)
    if mcp_err:
        logger.warning(f"exa 匿名 MCP 上游错误: {mcp_err}")
        return [{"error": mcp_err, "source": "exa"}]
    result = data.get("result")
    if not isinstance(result, dict):
        return [{"error": "exa mcp: 响应缺少 result 字段（非 tools/call 应答）",
                 "source": "exa"}]
    text = "".join(
        str(c.get("text", "")) for c in (result.get("content") or [])
        if isinstance(c, dict))
    results = []
    for i, blk in enumerate(_parse_exa_text_blocks(text)[:max(1, n)]):
        url = blk.get("url", "")
        title = (blk.get("title") or url).strip()
        if not url and not title:
            continue
        r: dict[str, Any] = {
            "title": title[:200],
            "url": url,
            "snippet": (blk.get("highlights") or "").strip()[:400],
            "source": "exa",
            "score": rank_score(0.75, len(results)),
        }
        # 匿名通道无值时给字面 "N/A"（实测 2026-09-26），等于没有，不能
        # 当成真实的发布时间/作者写进结果
        published = blk.get("published", "").strip()
        if published and published.lower() != "n/a":
            r["published_at"] = published
        author = blk.get("author", "").strip()
        if author and author.lower() != "n/a":
            r["metadata"] = {"author": author}
        results.append(r)
    return results


def _build_exa_engine(spec: dict[str, Any]) -> Any:
    """Exa 语义搜索专用引擎（embedding 匹配 + 内容摘要）。

    按是否有 key 分两级通道：
      - 有 key（ARGO_EXA_API_KEY / EXA_API_KEY）→ 官方 REST /search（原路径，
        字段语义不变）。
      - 无 key → Exa 托管 MCP 匿名通道（_exa_mcp_keyless）：POST
        https://mcp.exa.ai/mcp 调 web_search_exa，免 key 匿名可用
        （2026-09-26 实测）。通道失效（改版/加鉴权/限流）时表现为带 error
        的记录而非空结果，调度层据此切换备选源。
    """
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        api_key = get_env(["ARGO_EXA_API_KEY", "EXA_API_KEY"])
        if not api_key:
            return _exa_mcp_keyless(query, n, to, spec)
        url = "https://api.exa.ai/search"
        body = json.dumps({
            "query": query,
            "type": "auto",
            "numResults": min(n, 10),
            "contents": {"text": {"maxCharacters": 400}},
        }).encode("utf-8")
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = []
                for r in data.get("results", []):
                    snippet = (r.get("text") or r.get("snippet") or "")[:300]
                    # 轻量清洗：去掉 YAML front-matter（--- 开头的内容块）与导航噪声
                    if snippet.startswith("---"):
                        idx = snippet.find("---", 3)
                        if idx != -1:
                            snippet = snippet[idx + 3:].strip()
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": snippet,
                        "source": "exa",
                        # type:auto 模式不返回 score 字段（恒 0 会被 RRF 埋没），
                        # 无 score 时给固定基线分；并叠加位次衰减
                        "score": rank_score(r.get("score") or 0.75, len(results)),
                    })
                return results
        except Exception as e:
            logger.warning(f"Exa 引擎失败: {e}")
            return []
    return _engine


# ── anysearch 通用搜索（JSON-RPC / MCP，进程内 builder 替代 subprocess）───────

def _build_anysearch_engine(spec: dict[str, Any]) -> Any:
    """anysearch 通用搜索主力：POST JSON-RPC 到 api.anysearch.com/mcp。

    替换原 `type: cli` 的 subprocess 调用（每次启动 python3 解释器 ~200-300ms），
    改为进程内 builder + HttpClient（UA 轮换/重试/退避/Retry-After）：
    省启动开销 + 降低 errors（限流/网络）导致的高失败。2026-08 优化。
    """
    timeout = spec.get("timeout", 8)
    url = "https://api.anysearch.com/mcp"

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                domain: str = "", sub_domain: str = "", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        args: dict[str, Any] = {"query": query, "max_results": min(n, 10)}
        if domain:
            args["domain"] = domain
        if sub_domain:
            args["sub_domain"] = sub_domain
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search", "arguments": args}}
        headers = {"Content-Type": "application/json"}
        api_key = get_env(["ARGO_ANYSEARCH_API_KEY", "ANYSEARCH_API_KEY"])
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            from http_client import HttpClient
            # max_retries=0：**不再做引擎内连接级重试**（2026-09-10 实测修正）。
            # 原因：重试会与调度层的引擎级重试叠乘。此前 max_retries=1 与
            # 外层 retry_count=1 组合出 2×2=4 次尝试 × 8s = 32s 的最坏耗时，
            # 用户侧表现为「查询卡半分钟」，且期间不切备选源。
            # 失败切换的职责在调度层（熔断降权 + hedged 补发备选引擎 +
            # search.py 的每引擎墙钟预算），引擎内重复尝试只会放大延迟。
            client = HttpClient(timeout=to, max_retries=0, jitter=False)
            resp = client.post(url, body=body, extra_headers=headers)
        except ImportError:
            return []
        if resp.get("status", 0) >= 400 or not resp.get("text"):
            return []
        try:
            data = json.loads(resp["text"])
        except (ValueError, TypeError):
            return []
        # ── 上游错误必须显式上报，不得静默返回空 ──────────────────────
        # 实测（2026-09-10）上游返回 HTTP 200 但 isError=true：
        #   {"result":{"content":[{"text":"Service temporarily unavailable."}],
        #              "isError":true}}
        # 旧实现忽略 isError，而该文本不含配额关键词、也没有 "### N." 结果块，
        # 于是静默返回 [] —— 用户看到「没结果」而非「上游不可用」，
        # 熔断器也拿不到失败信号。这是本仓第 N 次「失败伪装成成功」。
        mcp_err = _mcp_error_of(data)
        if mcp_err:
            logger.warning(f"anysearch 上游错误: {mcp_err}")
            return [{"error": mcp_err, "source": "anysearch"}]

        content = data.get("result", {}).get("content", []) or []
        records = [
            (i.get("text", "") if isinstance(i, dict) else str(i)) for i in content
        ]
        joined = "".join(records).lower()
        # 配额/限流：仅当无任何结果块（### N.）且文本含配额信号时判定，避免
        # 正常结果正文里出现 'quota/429/rate limit' 等词被误判为配额耗尽。
        #
        # **刻意返回 []（不是 error item）**——与上方 isError 分支处理相反，
        # 理由不同、不可混同：
        #   · isError=true 是上游**明确宣示失败**（MCP 协议级信号）→ 必须报错，
        #     让熔断器拿到信号、停止对已坏源的空转调用。
        #   · 配额耗尽是**临时**状态（按日/月重置），上游本身健康。
        #     返回 [] 让路由优雅 fallback 到其它源即可；若报 error 会驱动
        #     熔断 open、把「只是暂时没额度」的引擎禁用，反而有害。
        # 既有测试 test_quota_only_response_returns_empty 锁定此契约。
        has_result_blocks = any("### " in t for t in records)
        if (not has_result_blocks) and any(
                k in joined for k in ("quota", "exhausted", "recharge",
                                      "rate limit", "429", "daily_free_quota")):
            return []
        results = []
        for item in content:
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            # 结果块以行首「### N.」分隔。用 (?m)^ 锚定行首而非 \n 前缀：
            # 首个结果块顶格开头（无前导换行）时，\n 前缀版本会把第一块
            # 并进 blocks[0] 而整块丢失（首个结果静默消失）。
            blocks = re.split(r"(?m)^### \d+\.\s", text)
            for block in blocks[1:]:
                lines = block.strip().split("\n")
                title = lines[0].strip() if lines else ""
                item_url = ""
                snippet_lines = []
                for line in lines[1:]:
                    ls = line.strip()
                    if ls.startswith("- **URL**: "):
                        item_url = ls.replace("- **URL**: ", "")
                    elif ls.startswith("**URL**: "):
                        item_url = ls.replace("**URL**: ", "")
                    else:
                        snippet_lines.append(line)
                snippet = "\n".join(snippet_lines).strip()[:500]
                if title:
                    results.append({
                        "title": title[:200], "url": item_url, "snippet": snippet,
                        "source": "anysearch", "score": rank_score(0.7, len(results)),
                    })
        return results
    return _engine


# ── 搜狗微信搜索引擎 ─────────────────────────────────────────────────────────

# 搜狗中间链 → 微信真实链接（mp.weixin.qq.com）的解析预算与熔断参数：
# 搜狗 /link、/weixin 跳转链是 SERP 链（SKILL.md 纪律 3：不得作正文来源），
# 且分钟级过期，--verify 与 article 均无法消费；解析在引擎内就地完成。
_SOGOU_RESOLVE_TIMEOUT_CAP = 3.5    # 单条解析请求超时封顶（秒）
_SOGOU_RESOLVE_BUDGET_S = 4.0       # 单轮解析墙钟预算，超预算的余下结果回落中间链
_SOGOU_RESOLVE_BODY_CAP = 65536     # 反爬 JS 页很小，响应体读取上限
_SOGOU_RESOLVE_FAIL_LIMIT = 3       # 连续失败达阈值 → 冷却期内跳过解析
_SOGOU_RESOLVE_COOLDOWN_S = 300.0
_SOGOU_RESOLVE_MAX_PER_CALL = 5     # 单轮解析条数封顶：n>5 无收益，少发请求降风控暴露


def _sogou_is_intermediate(url: str) -> bool:
    """是否搜狗中间跳转链（/link?url= 或 /weixin?url=），待解析成真实链接。"""
    if "weixin.sogou.com" not in url:
        return False
    return "/link?" in url or "/weixin?" in url


def _resolve_sogou_link(url: str, to: float, engine_name: str,
                        cookie: str = "") -> str:
    """单条搜狗中间链 → mp.weixin.qq.com 真实文章链接，失败返回空串。

    两条恢复路径：
      a) 正常风控：302 直落真实链，urllib 自动跟随，geturl() 即终址；
      b) 反爬 JS 页（200 + `url += '片段'` 拼接跳转）：按序拼回真实地址。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://weixin.sogou.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cookie:
        headers["Cookie"] = cookie
    # 搜狗把原始查询词原样塞进 /link 的 query（如 query=AI agent 工作），空格
    # 不编码会被 urllib 拒收（InvalidURL: control characters）
    url = url.replace(" ", "%20")
    req = urllib.request.Request(url, headers=headers)
    with http_open(req, timeout=min(to, _SOGOU_RESOLVE_TIMEOUT_CAP),
                   engine=engine_name) as resp:
        final = resp.geturl() if hasattr(resp, "geturl") else ""
        if "mp.weixin.qq.com" in final:
            return final
        body = resp.read(_SOGOU_RESOLVE_BODY_CAP).decode("utf-8", errors="replace")
    candidate = "".join(re.findall(r"\burl\b\s*\+?=\s*['\"]([^'\"]*)['\"]", body))
    if "mp.weixin.qq.com" in candidate:
        return candidate
    return ""


def _build_wechat_sogou_engine(spec: dict[str, Any]) -> Any:
    """搜狗微信搜索引擎（weixin.sogou.com）

    抓取搜狗微信搜索结果页，提取公众号文章标题、链接、摘要、公众号名，
    并将搜狗中间跳转链解析为 mp.weixin.qq.com 真实文章链接
    （url_resolved 标注解析结果，失败回落中间链，连续失败熔断冷却）。
    无需登录，无需 API key，纯 HTML 解析。
    """
    timeout = spec.get("timeout", 10)
    engine_name = spec.get("_name", "")
    resolve_state = {"fails": 0, "cooldown_until": 0.0}

    def _resolve_results(results: list[dict[str, Any]], to: float,
                         cookie: str) -> None:
        """就地解析结果中的搜狗中间链；降级不丢结果，熔断不白耗请求。"""
        import random
        started = time.monotonic()
        pending = 0
        for r in results:
            if not _sogou_is_intermediate(r.get("url", "")):
                continue
            if (pending >= _SOGOU_RESOLVE_MAX_PER_CALL
                    or time.monotonic() - started > _SOGOU_RESOLVE_BUDGET_S
                    or time.monotonic() < resolve_state["cooldown_until"]):
                r["url_resolved"] = False
                continue
            pending += 1
            time.sleep(random.uniform(0.05, 0.15))  # 逐条限速，贴近引擎 qps 声明
            try:
                real = _resolve_sogou_link(r["url"], to, engine_name, cookie)
            except Exception as e:
                logger.warning(f"搜狗中间链解析失败: {e}")
                real = ""
            if real:
                resolve_state["fails"] = 0
                r["url"] = real
                r["url_resolved"] = True
            else:
                resolve_state["fails"] += 1
                r["url_resolved"] = False
                if resolve_state["fails"] >= _SOGOU_RESOLVE_FAIL_LIMIT:
                    resolve_state["cooldown_until"] = (
                        time.monotonic() + _SOGOU_RESOLVE_COOLDOWN_S)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://weixin.sogou.com/weixin?type=2&query={up.quote(query)}&ie=utf8"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                html = resp.read().decode("utf-8")
                # 复用搜索响应的 Set-Cookie（SNUID/SUV）：中间链跳转带上可降风控
                _set_cookies = (resp.headers.get_all("Set-Cookie")
                                if hasattr(resp, "headers") else None)
            cookie = "; ".join(c.split(";")[0] for c in _set_cookies) if _set_cookies else ""
            results = []
            li_pattern = re.compile(
                r'<li\s+id="sogou_vr_11002601_box_\d+"[^>]*>(.*?)</li>', re.DOTALL
            )
            for li in li_pattern.findall(html)[:n]:
                title_match = re.search(
                    r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', li, re.DOTALL
                )
                if not title_match:
                    continue
                href = title_match.group(1).replace("&amp;", "&")
                title = re.sub(r"<[^>]+>", "", title_match.group(2)).strip()
                title = title.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")

                summary_match = re.search(
                    r'<p[^>]*class="txt-info"[^>]*>(.*?)</p>', li, re.DOTALL
                )
                summary = re.sub(r"<[^>]+>", "", summary_match.group(1)).strip() if summary_match else ""
                summary = summary.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")

                account_match = re.search(
                    r'<span[^>]*class="all-time-y2"[^>]*>(.*?)</span>', li, re.DOTALL
                )
                account = re.sub(r"<[^>]+>", "", account_match.group(1)).strip() if account_match else ""

                # 发布时间：结果条内嵌 script 写入 document.write(timeConvert('10位unix秒'))
                time_match = re.search(r"timeConvert\('?(\d{10})'?\)", li)
                published_at = ""
                if time_match:
                    try:
                        published_at = datetime.fromtimestamp(
                            int(time_match.group(1))
                        ).astimezone().isoformat(timespec="seconds")
                    except (ValueError, OSError):
                        published_at = ""

                result = {
                    "title": title[:80],
                    "url": "https://weixin.sogou.com" + href if href.startswith("/") else href,
                    "snippet": summary[:200],
                    "account": account,
                    "source": "wechat_sogou",
                }
                if published_at:
                    result["published_at"] = published_at
                results.append(result)
            if results:
                if str(kwargs.get("mode", "auto")) == "fast":
                    # fast：省时延跳过解析，统一显式标注未解析
                    for r in results:
                        if _sogou_is_intermediate(r.get("url", "")):
                            r["url_resolved"] = False
                else:
                    _resolve_results(results, to, cookie)
            return results
        except Exception as e:
            logger.warning(f"搜狗微信搜索失败: {e}")
            return []
    return _engine


# ── Hacker News 搜索引擎 ──────────────────────────────────────────────────────

def _build_hackernews_engine(spec: dict[str, Any]) -> Any:
    """Hacker News 搜索（Algolia API）"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://hn.algolia.com/api/v1/search?query={up.quote(query)}&tags=story&hitsPerPage={min(n, 10)}"
        headers = {"User-Agent": "argo-search/1.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read())
            results = []
            for h in data.get("hits", []):
                results.append({
                    "title": h.get("title", ""),
                    "url": h.get("url", f"https://news.ycombinator.com/item?id={h.get('objectID', '')}"),
                    "snippet": f"score: {h.get('points', 0)} | comments: {h.get('num_comments', 0)} | by: {h.get('author', '')}",
                    "source": "hackernews",
                })
            return results
        except Exception as e:
            logger.warning(f"HackerNews 引擎失败: {e}")
            return []
    return _engine


# ── Stack Overflow 搜索引擎 ───────────────────────────────────────────────────

# Stack Exchange 站点族：同一 API 换 site 参数覆盖 180+ 站点，
# 「site:serverfault nginx 配置」把查询定向到指定站点，其余原样透传
_SE_SITE_RE = re.compile(r"(?i)^\s*site:([a-z0-9\-]+)\s+")


def _build_stackoverflow_engine(spec: dict[str, Any]) -> Any:
    """Stack Overflow 搜索（Stack Exchange API，site: 前缀切站点族）"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        site = "stackoverflow"
        q = query.strip()
        m = _SE_SITE_RE.match(q)
        if m:
            site = m.group(1).lower()
            q = q[m.end():].strip()
        if not q:
            return []
        url = f"https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&q={up.quote(q)}&site={site}&pagesize={min(n, 10)}"
        headers = {"User-Agent": "argo-search/1.0", "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                import gzip
                raw = resp.read()
                try:
                    data = json.loads(gzip.decompress(raw))
                except Exception:
                    data = json.loads(raw)
            results = []
            for item in data.get("items", []):
                tags = ", ".join(item.get("tags", [])[:3])
                prefix = f"[{site}] " if site != "stackoverflow" else ""
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": f"{prefix}score: {item.get('score', 0)} | answers: {item.get('answer_count', 0)} | tags: {tags}",
                    "source": "stackoverflow",
                })
            return results
        except Exception as e:
            logger.warning(f"StackOverflow 引擎失败: {e}")
            return []
    return _engine


# ── GitHub 搜索引擎（按结构化语法切端点）──────────────────────────────────────

# GitHub 搜索与 X 一样支持结构化字段，但不同字段属于不同端点（失配会拿到空结果或噪音）：
#   - 仓库搜索: user:/org:/lang:/in:name/in:description/stars:/topic:  → /search/repositories
#   - issue/PR: repo:/is:issue/is:pr/label:/author:/assignee:/comments:/created:/in:title/in:body
#                                                                       → /search/issues
#   - 代码搜索: in:file/filename:/extension:/path:（需认证）                → /search/code
_GH_REPO_SYNTAX = ("user:", "org:", "lang:", "in:name", "in:description", "stars:", "topic:", "size:", "pushed:")
_GH_ISSUE_SYNTAX = ("repo:", "is:issue", "is:pr", "is:open", "is:closed", "label:", "milestone:",
                    "author:", "assignee:", "comments:", "created:", "updated:", "in:title", "in:body")
_GH_CODE_SYNTAX = ("in:file", "filename:", "extension:", "path:", "in:readme", "in:path")


def _github_endpoint(query: str, has_token: bool) -> str:
    """按查询中的结构化语法选 GitHub 搜索端点。"""
    if any(s in query for s in _GH_CODE_SYNTAX):
        return "code" if has_token else "issues"  # code 需认证；无 token 尽力退到 issues
    if any(s in query for s in _GH_ISSUE_SYNTAX):
        return "issues"
    return "repositories"


def _github_url(endpoint: str, query: str, n: int) -> str:
    q = urllib.parse.quote(query)
    per = min(n, 30)
    base = {
        "repositories": "https://api.github.com/search/repositories",
        "issues": "https://api.github.com/search/issues",
        "code": "https://api.github.com/search/code",
    }[endpoint]
    return f"{base}?q={q}&per_page={per}"


def _gh_repo_result(item: dict) -> dict[str, Any] | None:
    name = item.get("full_name") or item.get("name") or ""
    url = item.get("html_url") or ""
    desc = (item.get("description") or "").strip()
    if not name and not url:
        return None
    stars = item.get("stargazers_count")
    snippet = desc or f"stars: {stars} | language: {item.get('language')}"
    return {
        "title": name or url,
        "url": url,
        "snippet": snippet[:300],
        "source": "github",
        "score": 0.7,
        "published_at": item.get("updated_at"),
        "metadata": {"stars": stars, "language": item.get("language"), "forks": item.get("forks_count")},
    }


def _gh_issue_result(item: dict) -> dict[str, Any] | None:
    title = item.get("title") or ""
    url = item.get("html_url") or ""
    repo_full = (item.get("repository_url") or "").replace("https://api.github.com/repos/", "")
    if not title and not url:
        return None
    state = item.get("state") or ""
    comments = item.get("comments")
    user = (item.get("user") or {}).get("login") or ""
    snippet = f"[{repo_full}] {state} | comments: {comments} | by @{user}" if repo_full else f"{state} | by @{user}"
    return {
        "title": title or url,
        "url": url,
        "snippet": snippet[:300],
        "source": "github",
        "score": 0.7,
        "published_at": item.get("created_at"),
        "metadata": {"repo": repo_full, "state": state, "comments": comments},
    }


def _gh_code_result(item: dict) -> dict[str, Any] | None:
    name = item.get("name") or ""
    url = item.get("html_url") or ""
    repo = (item.get("repository") or {}).get("full_name") or ""
    path = item.get("path") or ""
    if not name and not url:
        return None
    snippets = [(tm.get("fragment") or "").strip() for tm in (item.get("text_matches") or []) if tm.get("fragment")]
    snippet = " / ".join(snippets)[:300] or path
    return {
        "title": f"{repo}:{path or name}",
        "url": url,
        "snippet": snippet,
        "source": "github",
        "score": 0.7,
        "metadata": {"repo": repo, "path": path},
    }


def _build_github_engine(spec: dict[str, Any]) -> Any:
    """GitHub 搜索：按查询结构化语法自动切 repositories / issues / code 端点。

    未认证（无 GITHUB_TOKEN）时可用 repositories / issues；code 端点需认证。
    失配端点会拿到空结果或大段噪音，这里是按语法选对端点的关键修复。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        token = get_env(["ARGO_GITHUB_TOKEN", "GITHUB_TOKEN"]).strip()
        endpoint = _github_endpoint(query, bool(token))
        url = _github_url(endpoint, query, n)
        headers = {
            "User-Agent": "argo-search/1.0 (+github)",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"token {token}"
        if endpoint == "code":
            headers["Accept"] = "application/vnd.github.v3.text-match+json"
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.warning(f"GitHub {endpoint} 失败 HTTP {e.code}: {e.reason}")
            return []
        except Exception as e:
            logger.warning(f"GitHub {endpoint} 失败: {e}")
            return []

        results = []
        if endpoint == "repositories":
            for item in data.get("items", [])[:n]:
                r = _gh_repo_result(item)
                if r:
                    results.append(r)
        elif endpoint == "issues":
            for item in data.get("items", [])[:n]:
                r = _gh_issue_result(item)
                if r:
                    results.append(r)
        else:
            for item in data.get("items", [])[:n]:
                r = _gh_code_result(item)
                if r:
                    results.append(r)
        return [dict(r, score=rank_score(r.get("score", 0.7), i)) for i, r in enumerate(results)]
    return _engine


# ── Google Scholar 搜索引擎 ───────────────────────────────────────────────────

def _build_google_scholar_engine(spec: dict[str, Any]) -> Any:
    """Google Scholar 搜索（HTTP 页面解析）"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://scholar.google.com/scholar?q={up.quote(query)}&hl=en&as_sdt=0%2C5&num={min(n, 10)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                html = resp.read().decode("utf-8")
            results = []
            titles = re.findall(r'<h3[^>]*class="[^"]*gs_rt[^"]*"[^>]*>(.*?)</h3>', html, re.DOTALL)
            snippets = re.findall(r'<div[^>]*class="[^"]*gs_rs[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
            for i, t in enumerate(titles[:n]):
                title = re.sub(r'<[^>]+>', '', t).strip()
                snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
                if title:
                    results.append({
                        "title": title[:100],
                        "url": f"https://scholar.google.com/scholar?q={up.quote(title[:50])}",
                        "snippet": snippet[:200],
                        "source": "google_scholar",
                    })
            return results
        except Exception as e:
            logger.warning(f"Google Scholar 引擎失败: {e}")
            return []
    return _engine


# ── V2EX 搜索引擎 ─────────────────────────────────────────────────────────────

# 拉丁词最小长度：单字符不做匹配。
# 实测证据：查询 "a" 会命中 Apple/astar 等任意含该字母的主题。
# 注意门槛只到 2：词边界（见 _term_hits）已能挡住 "ab"→Avalonia/Wabou
# 这类组合匹配，若把门槛提到 3 会误伤 "ai" 这种有真实语义的双字符词
# （实测 "AI" 查询会返回 0 条）。CJK 词不受此门槛限制。
MIN_LATIN_TERM_LEN = 2

_SOV2EX_API = "https://www.sov2ex.com/api/search"


def _sov2ex_search(query: str, n: int, timeout: float,
                   engine: str) -> list[dict[str, Any]] | None:
    """第一级来源：sov2ex 社区全文搜索（社区维护的 V2EX 全文索引）。

    GET /api/search?q=&size=，返回 {total, hits:[{_id, title, content,
    created, _score, highlight?}]}（2026-09-26 实测）。命中产出带真实
    /t/<id> 链接的全文结果；**空结果 / 失败 / 响应不可解析一律返回 None**，
    由调用方落回官方 API 候选池路径——None 是「此级无产出」的信号，
    不是最终结果，最终诚实空由池路径给出。

    字段读取兼容 _source 包裹：sov2ex 底层是 Elasticsearch，部分部署形态
    会把字段收进 hits[]._source，两种形态都认，避免索引端调整即失效。
    """
    q = (query or "").strip()
    if not q:
        return None
    u = f"{_SOV2EX_API}?{urllib.parse.urlencode({'q': q, 'size': n})}"
    raw = _http_get_raw(u, {"Accept": "application/json"}, timeout, engine=engine)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    hits = data.get("hits")
    if not isinstance(hits, list) or not hits:
        return None
    results: list[dict[str, Any]] = []
    for i, hit in enumerate(hits[:max(1, n)]):
        if not isinstance(hit, dict):
            continue
        src = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
        tid = str(hit.get("_id") or src.get("id") or "").strip()
        if not tid:
            continue
        # snippet 优先吃 highlight（查询词命中片段，更相关），缺失再退整段正文
        hl = hit.get("highlight")
        snippet = ""
        if isinstance(hl, dict):
            frag = hl.get("content")
            if isinstance(frag, list) and frag:
                snippet = " … ".join(str(x) for x in frag)
            elif isinstance(frag, str):
                snippet = frag
        if not snippet:
            snippet = str(src.get("content") or "")
        snippet = re.sub(r"</?em>", "", snippet).strip()
        published = ""
        try:
            published = datetime.fromtimestamp(
                int(src.get("created"))
            ).astimezone().isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            published = ""
        r: dict[str, Any] = {
            "title": (str(src.get("title") or "").strip() or tid)[:120],
            "url": f"https://www.v2ex.com/t/{tid}",
            "snippet": snippet[:300],
            "source": "v2ex",
            # sov2ex 的 _score 是 BM25 分，量纲与 argo 的 0-1 分不可比，
            # 不透传；上游相关性顺序用序位衰减编码，原始分进 social_meta
            "score": rank_score(0.85, len(results)),
            "social_meta": {
                "platform": "v2ex",
                "content_type": "topic",
                "url_verifiable": True,
                "retrieval_mode": "sov2ex_fulltext",
                "sov2ex_score": hit.get("_score"),
            },
        }
        if published:
            r["published_at"] = published
        results.append(r)
    return results or None


def _build_v2ex_engine(spec: dict[str, Any]) -> Any:
    """V2EX 社区搜索（sov2ex 全文优先 + 官方 API 候选池降级）

    两级来源，按可用性互补：
      1. sov2ex 社区全文搜索（_sov2ex_search）——能答「全文关键词」问题，
         命中即返回，不再消耗官方 API 配额；
      2. 官方开放 API 候选池 + 本地相关性过滤——sov2ex 空结果/失败/超时
         时的降级路径，语义保持不变。

    为什么不是「爬 /search 页」：V2EX 的站内搜索需要登录态，未登录访问
    `/search?q=` 会 302 到 `/go/search`——那是「搜索引擎技术研究」**节点页**，
    不是搜索结果页。旧实现用 item_title 正则直接抓该页面，于是把节点热帖
    当成了搜索结果：10 条结果的 url 全部等于查询自身的搜索页地址、
    snippet 恒为硬编码常量，标题则与该节点无关（如查「V2EX 社区」返回
    「装机 配置 预算」）。coverage 仍报 status=ok/returned=10，
    失败伪装成成功，破坏 argo「结果可核验」的证据完整链路。

    降级路径走官方开放 API（只读、无鉴权、配额 600 次/[窗口]）：
      - /api/topics/hot.json      热门主题
      - /api/topics/latest.json   最新主题
      - /api/replies/show.json    主题回复（按 topic_id）
    API 没有搜索端点，因此策略是「拉候选池 → 在本地按查询词做相关性过滤
    → 按命中强度排序」。这样产出的每条结果都带真实 /t/<id> 链接与真正文，
    可被 fetch 复核。若两级来源都无任何条目与查询相关，**诚实返回空列表**
    （调用方据此标记该引擎无结果），而不是回落到伪造占位结果。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout

        # ── 第一级：sov2ex 全文搜索，命中即返回（省官方 API 配额）────
        sov2ex_hits = _sov2ex_search(query, n, to, spec.get("_name", "v2ex"))
        if sov2ex_hits:
            return sov2ex_hits

        def _fetch_json(path: str, params: dict | None = None):
            u = f"https://www.v2ex.com{path}"
            if params:
                u += "?" + urllib.parse.urlencode(params)
            # 走 argo 统一 GET 出口（HttpClient：UA 轮换 / 重定向跟随 / 429 尊重），
            # 不自造 urllib 请求头——与「新引擎复用 argo HttpClient」纪律一致。
            # engine 必须显式传：默认值 "?" 会把 V2EX 的失败记到伪引擎名下，
            # 聚合层 pop("v2ex") 永远取不到（归因写不进去等于没做）。
            raw = _http_get_raw(u, {"Accept": "application/json"}, to,
                                engine=spec.get("_name", "v2ex"))
            if not raw:
                return None
            data = json.loads(raw)
            # 官方 API 会返回 {"status":"error","message":...,"rate_limit":{...}}，
            # 例如配额耗尽或参数非法。这类响应不是列表，静默当空会掩盖真实故障。
            if isinstance(data, dict) and data.get("status") == "error":
                rl = data.get("rate_limit") or {}
                logger.warning(
                    "V2EX API 返回错误: %s (rate_limit used=%s quota=%s)",
                    data.get("message"), rl.get("used"), rl.get("quota"),
                )
                return None
            return data

        # ── 候选池构建：节点路由优先，hot/latest 保底 ──────────────────
        #
        # 第一批只用 hot+latest（20 条全站热帖），与查询无关，长尾查询
        # 必然落空。现引入节点路由（见 v2ex_nodes）：把「全站热帖过滤」
        # 升级为「相关节点内检索」。节点表本地缓存 24h，匹配阶段零 API。
        #
        # 取池策略（成本与召回权衡）：
        #   命中节点 → 并发取 Top-K 节点的帖（K=3，配额占用 3/600）
        #   未命中   → 回落 hot+latest，并如实标注 layer=none
        topics: dict[int, dict] = {}
        routed = {"nodes": [], "confidence": 0.0, "layer": "none", "scores": {}}
        try:
            from v2ex_nodes import pick_nodes
            routed = pick_nodes(query, top_k=3, fetcher=lambda u: _http_get_raw(
                u, {"Accept": "application/json"}, to,
                engine=spec.get("_name", "v2ex")))
        except Exception as e:
            logger.debug(f"V2EX 节点路由失败，回落 hot/latest: {e}")

        for node_name in routed.get("nodes") or []:
            try:
                for t in _fetch_json("/api/topics/show.json",
                                     {"node_name": node_name, "page": 1}) or []:
                    tid = t.get("id")
                    if tid and tid not in topics:
                        t["_v2ex_node_routed"] = node_name
                        topics[tid] = t
            except Exception as e:
                logger.debug(f"V2EX 节点 {node_name} 拉取失败: {e}")

        # 保底与补充：无论是否命中节点，都并入 hot/latest
        # （节点帖可能与查询无关的部分互补；去重由 topic id 保证）
        for path in ("/api/topics/hot.json", "/api/topics/latest.json"):
            try:
                for t in _fetch_json(path) or []:
                    tid = t.get("id")
                    if tid and tid not in topics:
                        topics[tid] = t
            except Exception as e:
                logger.debug(f"V2EX {path} 拉取失败: {e}")

        if not topics:
            return []

        q_norm = (query or "").strip().lower()
        terms = [w for w in re.split(r"[\s,，、/]+", q_norm) if w]

        def _term_hits(term: str, blob: str) -> bool:
            """词命中判断：拉丁词要求词边界，CJK 走子串。

            为什么必须区分：纯子串匹配下 "ab" 会命中 Avalonia/Wabou/Workbuddy
            （实测），"a" 会命中 Apple/astar。对拉丁词加 word boundary 后
            "ai" 仍能精确命中独立的 "ai"/"AI"，但不再命中 "Avalonia"；
            CJK 没有词边界概念，"程序员" 这类子串匹配是正确的。
            单字符拉丁词（< MIN_LATIN_TERM_LEN）无边界可依，直接不匹配。
            """
            if not term:
                return False
            if re.search(r"[\u4e00-\u9fff]", term):
                return term in blob           # CJK：子串匹配
            if len(term) < MIN_LATIN_TERM_LEN:
                return False                  # 单字符拉丁词：放弃
            return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", blob) is not None

        def _relevance(t: dict) -> float:
            """命中强度：完整查询命中 > 全部词命中 > 部分词命中。0 表示不相关。

            只在标题与正文里匹配，**不匹配节点名/节点简介**：节点名多为
            「分享发现」「推广」这类通用词，参与匹配会让无关主题靠节点名
            蹭进结果（实测查询「V2EX 社区」曾命中一条推广帖）。
            部分词命中要求覆盖 ≥50% 且查询至少 2 个词，避免单个通用词
            （「社区」「工具」）把大量无关主题拉进来。
            词命中计算方式见 _term_hits（拉丁词边界 + CJK 子串 + 短词门槛）。
            """
            if not q_norm:
                return 0.0
            blob = f"{(t.get('title') or '').lower()} {(t.get('content') or '').lower()}"
            if _term_hits(q_norm, blob):
                return 3.0
            if not terms:
                return 0.0
            hit = sum(1 for w in terms if _term_hits(w, blob))
            if hit == 0:
                return 0.0
            if hit == len(terms):
                return 2.0
            # 部分命中：查询词数 ≥2 且覆盖率 ≥50% 才算相关
            if len(terms) >= 2 and hit / len(terms) >= 0.5:
                return 1.0 * (hit / len(terms))
            return 0.0

        scored = []
        for t in topics.values():
            rel = _relevance(t)
            if rel <= 0:
                continue
            scored.append((rel, t))

        # 相关度优先，同级按回复数（社区热度）降序
        scored.sort(key=lambda x: (-x[0], -(x[1].get("replies") or 0)))

        results = []
        for rel, t in scored[:n]:
            node = t.get("node") or {}
            member = t.get("member") or {}
            snippet = (t.get("content") or "").strip()
            if not snippet:
                snippet = re.sub(r"<[^>]+>", "", t.get("content_rendered") or "").strip()
            results.append({
                "title": (t.get("title") or "")[:120],
                "url": t.get("url") or f"https://www.v2ex.com/t/{t.get('id')}",
                "snippet": snippet[:300],
                "source": "v2ex",
                "published_at": t.get("created"),
                "social_meta": {
                    "platform": "v2ex",
                    "content_type": "topic",
                    "node": node.get("title") or "",
                    "node_name": node.get("name") or "",
                    "author": member.get("username") or "",
                    "replies": t.get("replies"),
                    "url_verifiable": True,
                    # 计算方式透明：结果来自官方 API 的候选池 + 本地相关性过滤，
                    # 非站内全文搜索。冷门/长尾查询命中率天然偏低，
                    # 命中 0 条是「池内无相关主题」，不等于「V2EX 上没有」。
                    "retrieval_mode": "node_routed_pool" if (
                        routed.get("nodes")) else "candidate_pool_filter",
                    "pool_size": len(topics),
                    # 节点路由可观测：命中哪些节点、置信层、节点路由本身
                    # 的置信度。layer=none 表示无匹配节点，已回落 hot/latest。
                    "routed_nodes": routed.get("nodes") or [],
                    "route_layer": routed.get("layer"),
                    "route_confidence": routed.get("confidence"),
                    "from_routed_node": bool(t.get("_v2ex_node_routed")),
                },
            })
        return results
    return _engine




# ── deps.dev 包依赖引擎 ──────────────────────────────────────────────────────

# 生态别名 → deps.dev system 名。包管理生态的日常叫法远多于正式名，
# 词表同时是查询解析器（「npm express」「python requests」均合法）。
_DEPS_SYSTEM_ALIASES = {
    "npm": "npm", "node": "npm", "nodejs": "npm", "js": "npm", "javascript": "npm",
    "pypi": "pypi", "python": "pypi", "pip": "pypi",
    "go": "go", "golang": "go",
    "maven": "maven", "java": "maven", "jvm": "maven",
    "cargo": "cargo", "rust": "cargo", "crate": "cargo",
}


def _build_deps_dev_engine(spec: dict[str, Any]) -> Any:
    """deps.dev 包依赖引擎（npm/pypi/go/maven/cargo 五生态，免认证）。

    查询形态：「<生态> <包名>」（npm express / python requests）或裸包名
    （默认 npm）；maven 包名为 group:artifact。每条结果可核验
    （deps.dev 网页 URL 对应同一数据）。

    边界说明：包级漏洞聚合端点不存在，逐版本查询漏洞需 N 次请求放大
    配额，故本引擎只答「包存在性 / 最新版本 / 弃用状态 / 发布时间」；
    漏洞情报由 nvd 引擎按关键词补位，职责分离而非功能缺失。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        parts = (query or "").strip().split()
        if not parts:
            return []
        system = "npm"
        if parts[0].lower() in _DEPS_SYSTEM_ALIASES:
            system = _DEPS_SYSTEM_ALIASES[parts[0].lower()]
            parts = parts[1:]
        package = " ".join(parts).strip()
        if not package:
            return []
        pkg_encoded = up.quote(package, safe="")
        url = f"https://api.deps.dev/v3/systems/{system}/packages/{pkg_encoded}"
        from engines_base import _http_get_raw
        raw = _http_get_raw(url, {"Accept": "application/json"}, to,
                            engine="deps_dev")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        versions = data.get("versions") or []
        if not versions:
            return []
        versions.sort(key=lambda v: str(v.get("publishedAt") or ""), reverse=True)
        latest = next((v for v in versions if v.get("isDefault")), versions[0])
        dep_count = sum(1 for v in versions if v.get("isDeprecated"))
        web_url = f"https://deps.dev/{system}/{pkg_encoded}"
        overview = {
            "title": f"{package} ({system}) 最新 {latest.get('versionKey', {}).get('version', '?')}",
            "url": web_url,
            "snippet": (f"共 {len(versions)} 个版本"
                        f"（{dep_count} 个已弃用），"
                        f"最新发布 {str(latest.get('publishedAt') or '')[:10]}"),
            "source": "deps_dev",
            "score": 0.95,
            "published_at": str(latest.get("publishedAt") or "")[:10],
        }
        results = [overview]
        for _rk1, v in enumerate(versions[: max(1, n - 1)]):
            vname = v.get("versionKey", {}).get("version", "")
            if vname == latest.get("versionKey", {}).get("version"):
                continue
            results.append({
                "title": f"{package}@{vname}",
                "url": web_url,
                "snippet": (f"发布 {str(v.get('publishedAt') or '')[:10]}"
                            + ("（已弃用）" if v.get("isDeprecated") else "")),
                "source": "deps_dev",
                "score": rank_score(0.7, _rk1),
                "published_at": str(v.get("publishedAt") or "")[:10],
            })
        return results[:n]
    return _engine


# ── endoflife.date 产品生命周期引擎 ──────────────────────────────────────────

_EOL_TAIL_WORDS = ("生命周期", "支持到", "eol", "support", "lifecycle",
                   "什么时候", "停更")


def _build_endoflife_engine(spec: dict[str, Any]) -> Any:
    """endoflife.date 产品生命周期引擎（200+ 产品，免认证）。

    查询词 = 产品 slug（python / nodejs / go / kubernetes / ubuntu…）。
    API 无搜索端点，slug 提取取查询里的第一个 ASCII 词并剥离「生命周期 /
    EOL / 支持」类尾词；未识别产品 404 → 诚实返回空，slug 模糊匹配交给
    通用搜索源。每条结果附 endoflife.date 产品页，可核验。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        slug = ""
        for tok in re.split(r"[\s,，。/?]+", (query or "")):
            t = tok.strip().lower()
            if not t or not re.fullmatch(r"[a-z][a-z0-9.\-_]{1,30}", t):
                continue
            if any(w in t for w in _EOL_TAIL_WORDS):
                continue
            slug = t
            break
        if not slug:
            return []
        from engines_base import _http_get_raw
        raw = _http_get_raw(f"https://endoflife.date/api/{slug}.json",
                            {"Accept": "application/json"}, to,
                            engine="endoflife")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        web_url = f"https://endoflife.date/{slug}"
        results = []
        for i, cyc in enumerate(data[:n]):
            label = "（最新）" if i == 0 else ""
            snippet_parts = []
            if cyc.get("latest"):
                snippet_parts.append(f"最新 {cyc['latest']}")
            if cyc.get("releaseDate"):
                snippet_parts.append(f"发布 {cyc['releaseDate']}")
            if cyc.get("support"):
                snippet_parts.append(f"全支持至 {cyc['support']}")
            if cyc.get("eol"):
                snippet_parts.append(f"EOL {cyc['eol']}")
            results.append({
                "title": f"{slug} {cyc.get('cycle', '?')}{label}",
                "url": web_url,
                "snippet": " · ".join(snippet_parts),
                "source": "endoflife",
                "score": 0.9 if i == 0 else 0.7,
                "published_at": str(cyc.get("latestReleaseDate") or "")[:10],
            })
        return results
    return _engine


# ── OSV.dev 开源漏洞库（免 key，必须 POST）─────────────────────────────────

# 包名 → 生态的推断表。OSV 必须显式给 ecosystem，而用户只会打「requests」
# 这样的裸包名，所以需要一层启发式；判断不了的按 PyPI 兜底（OSV 的 PyPI 库
# 最全，且 Python 是 agent 场景里最常问的）。
#
# 形态约定：
#   - 带冒号前缀（pypi:requests / npm:lodash）→ 显式指定，最高优先
#   - 带斜杠（@scope/pkg、github.com/a/b）→ npm 或 Go
#   - 纯 CVE 编号 → 不是包名，走 nvd 更合适，这里诚实返回空
_OSV_ECOSYSTEM_PREFIX: dict[str, str] = {
    "pypi": "PyPI", "npm": "npm", "go": "Go", "crates": "crates.io",
    "cargo": "crates.io", "maven": "Maven", "nuget": "NuGet",
    "packagist": "Packagist", "composer": "Packagist",
    "rubygems": "RubyGems", "gem": "RubyGems", "hex": "Hex",
}

# 这些包名在 npm 生态里比 PyPI 更常见，优先按 npm 查（同日名包不同生态）
_OSV_NPM_HINTS: frozenset[str] = frozenset({
    "lodash", "react", "vue", "express", "axios", "next", "webpack",
    "typescript", "eslint", "vite", "jest", "moment", "jquery", "angular",
    "svelte", "nuxt", "rollup", "babel", "prettier", "cheerio", "socket.io",
})

_OSV_GO_HINTS: frozenset[str] = frozenset({
    "golang.org/x/net", "golang.org/x/crypto", "golang.org/x/text",
    "github.com/gin-gonic/gin", "github.com/gorilla/websocket",
})


def _osv_resolve_ecosystem(query: str) -> tuple[str, str]:
    """把查询词解析成 (包名, OSV 生态)。解析不出时按 PyPI 兜底。

    返回生态名而非空串：OSV 对缺失 ecosystem 的请求返回 400，兜底比报错好——
    但会在结果里注明按哪个生态查的，避免「查了但查的是别的生态」这种静默偏差。
    """
    q = (query or "").strip()
    if ":" in q:
        head, _, tail = q.partition(":")
        eco = _OSV_ECOSYSTEM_PREFIX.get(head.strip().lower())
        if eco and tail.strip():
            return tail.strip(), eco
    name = q
    if name.startswith("@") or "/" in name:
        # npm scoped（@scope/pkg）或 Go module 路径
        if name.startswith("@"):
            return name, "npm"
        if name.startswith(("github.com/", "gitlab.com/", "golang.org/")):
            return name, "Go"
        return name, "npm"
    if name.lower() in _OSV_NPM_HINTS:
        return name, "npm"
    if name.lower() in _OSV_GO_HINTS:
        return name, "Go"
    return name, "PyPI"


def _build_osv_engine(spec: dict[str, Any]) -> Any:
    """OSV.dev 漏洞查询（按包名，POST /v1/query；免 key、免限次）。"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        # CVE 编号不是包名：OSV 查不到，交给 nvd（避免给出误导性的空结果）
        if re.match(r"^CVE-\d{4}-\d{4,}$", q, re.I):
            logger.info("osv: %s 是 CVE 编号而非包名，建议用 nvd 引擎", q)
            return []
        pkg, eco = _osv_resolve_ecosystem(q)
        body = json.dumps({"package": {"name": pkg, "ecosystem": eco}}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.osv.dev/v1/query", data=body,
            headers={"Content-Type": "application/json"})
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"OSV 失败: {e}")
            return []
        results = []
        for i, v in enumerate((data.get("vulns") or [])[:n]):
            vid = str(v.get("id") or "").strip()
            if not vid:
                continue
            summary = str(v.get("summary") or "").strip()
            # 影响版本：OSV 的 affected[].ranges 是版本区间，摘要里给出区间
            # 比只给「有漏洞」有用得多（能判断自己是否受影响）
            affected_bits = []
            for aff in (v.get("affected") or [])[:2]:
                if not isinstance(aff, dict):
                    continue
                for rng in (aff.get("ranges") or [])[:2]:
                    if not isinstance(rng, dict):
                        continue
                    events = rng.get("events") or []
                    introduced = next((e.get("introduced") for e in events
                                       if isinstance(e, dict) and e.get("introduced")), "")
                    fixed = next((e.get("fixed") for e in events
                                  if isinstance(e, dict) and e.get("fixed")), "")
                    if introduced or fixed:
                        affected_bits.append(
                            f"{introduced or '?'} ≤ 受影响 < {fixed or '未修复'}")
            aliases = [a for a in (v.get("aliases") or []) if a][:3]
            snippet = " · ".join(p for p in (
                summary,
                f"生态 {eco}",
                affected_bits[0] if affected_bits else "",
                " ".join(aliases),
            ) if p)[:300]
            results.append({
                # 标题带生态：同名包在不同生态是不同软件，标清楚避免误读
                "title": f"{vid} [{eco}] {summary}"[:500] or vid,
                "url": f"https://osv.dev/vulnerability/{vid}",
                "snippet": snippet,
                "source": "osv",
                "published_at": str(v.get("published") or "")[:10],
                "score": rank_score(0.85, i),
            })
        return results
    return _engine


# ── TinEye 反向图片搜索（以图搜图，免 key）────────────────────────────────

def _build_tineye_engine(spec: dict[str, Any]) -> Any:
    """TinEye 反向图片搜索（公开 JSON 端点，免 key）。

    填补的空白：argo 此前没有任何反搜图能力——通用网页引擎只吃文字查询，
    「这张图出自哪里 / 谁在用这张图 / 图片最早出现在哪」一类问题无源可查。
    TinEye 以图片指纹检索其历史索引，返回出现过该图的页面。

    用法约束：**query 必须是公网图片 URL（http(s):// 开头）**，不是关键词——
    本引擎不做关键词搜索。喂文字查询返回带提示的 error 记录（模型能从
    提示里纠正用法），诚实失败优于空结果或瞎猜。

    端点 GET https://tineye.com/api/v1/result_json/?url={image_url}，
    响应 matches[]：{image_url, domain, score(0-100 相似度), width, height,
    backlinks[]: {url(图片地址), backlink(出现该图的页面), crawl_date}}
    （字段名与 SearXNG 适配器/SAC_search tineye.ts 一致）。
    """
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not re.match(r"(?i)^https?://", q):
            return [{"error": "tineye: 反搜图引擎要喂图片 URL（http(s):// 开头）"
                              f"作为查询，收到的是关键词「{q[:50]}」",
                     "source": "tineye"}]
        # 图片 URL 里的 ://?& 必须整体编码，否则会被当成 query 参数边界
        u = ("https://tineye.com/api/v1/result_json/?url="
             + urllib.parse.quote(q, safe=""))
        raw = _http_get_raw(u, {"Accept": "application/json"}, to,
                            engine=spec.get("_name", ""))
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        matches = data.get("matches") if isinstance(data, dict) else None
        if not isinstance(matches, list):
            return []
        results: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        for m in matches:
            if len(results) >= max(1, n):
                break
            if not isinstance(m, dict):
                continue
            # 一个 match 可带多个 backlink（同一图多次收录）：取第一个有
            # 页面地址的；同一页面被多个 match 命中时只保留首条
            bl = next((b for b in (m.get("backlinks") or [])
                       if isinstance(b, dict) and (b.get("backlink") or b.get("url"))), None)
            if not bl:
                continue
            page_url = str(bl.get("backlink") or bl.get("url") or "")
            if not page_url or page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            try:
                sim = float(m.get("score"))
            except (TypeError, ValueError):
                sim = 0.0
            domain = str(m.get("domain") or "").strip()
            try:
                w, h = int(m.get("width") or 0), int(m.get("height") or 0)
            except (TypeError, ValueError):
                w = h = 0
            crawl = str(bl.get("crawl_date") or "").strip()
            bits = [b for b in (
                f"相似度 {sim:g}%" if sim > 0 else "",
                f"来源 {domain}" if domain else "",
                f"{w}x{h}" if w and h else "",
                f"收录 {crawl[:10]}" if crawl else "",
            ) if b]
            # TinEye score 是 0-100 相似度：归一到 0-1 当档位分再做序位衰减；
            # 上游没给分（0）时退固定档位，避免 0 分被下游当无效分
            base = min(sim / 100.0, 1.0) if sim > 0 else 0.7
            results.append({
                "title": (domain or "TinEye 图片匹配")[:120],
                "url": page_url,
                "snippet": " · ".join(bits),
                "source": "tineye",
                "score": rank_score(base, len(results)),
                "metadata": {
                    "image_url": str(m.get("image_url") or ""),
                    "similarity": sim,
                    "domain": domain,
                    "width": w,
                    "height": h,
                    "crawl_date": crawl,
                },
            })
        return results
    return _engine


# ── Bing RSS 通用网页搜索（免 key，稳定备胎）─────────────────────────────

def _build_bing_rss_engine(spec: dict[str, Any]) -> Any:
    """Bing 网页搜索 RSS 出口（免 key、免 HTML 解析的稳定备胎）。

    与 local_bing 的分工：local_bing 解析结果页 HTML，Bing 一改版式就
    全军覆没（argo 历史上 local_bing 因改版多次返工），且 HTML 路径对
    反爬更敏感；本引擎走 Bing 官方 format=rss 出口——字段语义由 RSS 2.0
    规范保证，没有版式可变，作为 local_bing 被改版/风控打断时的兜底源。
    代价：单页结果少（≤20 条）、不支持 setlang/mkt 本地化参数，中文场景
    优先 local_bing，这里保底；两者可同跑互补。

    GET https://www.bing.com/search?q={q}&format=rss&count={n≤20}，
    RSS item 的 title/link/description/pubDate；description 里的 HTML
    标签剥掉。pubDate 是 RFC 822（如 Wed, 24 Sep 2026 08:00:00 GMT），
    解析失败原样透传，不丢时间信息。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        to = _timeout or timeout
        u = (f"https://www.bing.com/search?q={urllib.parse.quote(q)}"
             f"&format=rss&count={min(max(int(n), 1), 20)}")
        raw = _http_get_raw(u, {"Accept": "application/rss+xml, application/xml, text/xml"},
                            to, engine=spec.get("_name", ""))
        if not raw:
            return []
        # 编码已由统一 GET 出口按 utf-8 errors=replace 解码；声明行剥掉——
        # 带 encoding 声明的 str 会被 ET.fromstring 拒收，且声明里的编码
        # 与解码后的实际内容已无关
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", raw.strip())
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []
        # .//item 不依赖 channel 层级；item 标签本身无命名空间（rss 的
        # xmlns 声明只修饰带前缀的扩展元素，不影响普通子标签查找）
        out: list[dict[str, Any]] = []
        for i, item in enumerate(root.findall(".//item")[:max(1, int(n))]):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            desc = re.sub(r"<[^>]+>", "",
                          item.findtext("description") or "").strip()
            r: dict[str, Any] = {
                "title": title[:200],
                "url": link,
                "snippet": desc[:300],
                "source": "bing_rss",
                "score": rank_score(0.7, i),
            }
            pub = (item.findtext("pubDate") or "").strip()
            if pub:
                try:
                    r["published_at"] = parsedate_to_datetime(
                        pub).astimezone().isoformat(timespec="seconds")
                except (TypeError, ValueError):
                    r["published_at"] = pub
            out.append(r)
        return out
    return _engine


# ── CISA KEV 已知在野利用漏洞（免 key，全量文件 + 本地过滤）──────────────

# 全量目录的进程内缓存。KEV 全量 1.35MB / 1710 条，上游不支持查询参数，
# 每次调用都要整份下载——而同一会话里连问几个 CVE 是常态。
# 只在**同一进程内**缓存（MCP 常驻进程受益最大；CLI 单发场景自然不命中），
# TTL 6 小时：CISA 的新增节奏是「每周几次」，6 小时足够新鲜且能省下重复下载。
_KEV_CACHE: dict[str, Any] = {"at": 0.0, "items": None}
_KEV_TTL_S = 6 * 3600


def _kev_load(timeout: float) -> list[dict[str, Any]]:
    """取 KEV 全量条目（带进程内 TTL 缓存）；失败返回空列表。"""
    now = time.time()
    cached = _KEV_CACHE.get("items")
    if cached and (now - float(_KEV_CACHE.get("at") or 0)) < _KEV_TTL_S:
        return cached
    req = urllib.request.Request(
        "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        headers={"User-Agent": "argo-search/1.0 (+cisa-kev)", "Accept": "application/json"})
    try:
        with http_open(req, timeout=timeout, engine="cisa_kev") as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        logger.warning(f"CISA KEV 失败: {e}")
        return cached or []
    items = data.get("vulnerabilities") or []
    if not isinstance(items, list):
        return cached or []
    _KEV_CACHE["items"] = items
    _KEV_CACHE["at"] = now
    return items


def _build_cisa_kev_engine(spec: dict[str, Any]) -> Any:
    """CISA KEV 在野利用漏洞目录（本地过滤全量目录）。"""
    timeout = spec.get("timeout", 30)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        items = _kev_load(to)
        if not items:
            return []
        # 查询是 CVE 编号时**只认 ID 精确匹配**：KEV 条目之间会互相引用
        # （CVE-2021-45046 的 description 里就写着 CVE-2021-44228），
        # 用纯子串匹配会把「提到它」的条目也算命中，排序后甚至排在正主前面
        # ——实测查 CVE-2021-44228 首条返回的是 45046，属明确错误答案。
        cve_q = q.upper() if re.match(r"^CVE-\d{4}-\d{4,}$", q.strip(), re.I) else ""
        matched = []
        for v in items:
            if not isinstance(v, dict):
                continue
            if cve_q:
                if str(v.get("cveID") or "").strip().upper() == cve_q:
                    matched.append(v)
                continue
            # 多词查询按「全部词都命中」过滤（AND），比 OR 精确：
            # 「chrome type confusion」这类组合能直接定位到具体那条
            terms = [t for t in re.split(r"[\s,]+", q.lower()) if t]
            hay = " ".join(str(v.get(k) or "") for k in (
                "cveID", "vendorProject", "product", "vulnerabilityName",
                "shortDescription", "requiredAction", "cwes")).lower()
            if all(t in hay for t in terms):
                matched.append(v)
        # 最近的排在前面（dateAdded 是 ISO 日期，字符串比较即可）
        matched.sort(key=lambda x: str(x.get("dateAdded") or ""), reverse=True)
        results = []
        for i, v in enumerate(matched[:n]):
            cve = str(v.get("cveID") or "").strip()
            if not cve:
                continue
            vendor = str(v.get("vendorProject") or "").strip()
            product = str(v.get("product") or "").strip()
            name = str(v.get("vulnerabilityName") or "").strip()
            desc = str(v.get("shortDescription") or "").strip()
            due = str(v.get("dueDate") or "").strip()
            ransom = str(v.get("knownRansomwareCampaignUse") or "").strip()
            bits = [b for b in (
                f"{vendor} {product}".strip(),
                f"修复期限 {due}" if due else "",
                "已知勒索软件利用" if ransom.lower() == "known" else "",
                desc,
            ) if b]
            results.append({
                "title": f"{cve} — {name or product or vendor}"[:500],
                "url": f"https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
                "snippet": " · ".join(bits)[:300],
                "source": "cisa_kev",
                "published_at": str(v.get("dateAdded") or "")[:10],
                "score": rank_score(0.9, i),
            })
        return results
    return _engine

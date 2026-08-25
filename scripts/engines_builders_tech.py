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
from datetime import datetime
from typing import Any

from engines_base import safe_search, _run, _resolve, _get_path, _coerce_field

logger = logging.getLogger("unified_search.engines")

# ── Exa 专用引擎 ──────────────────────────────────────────────────────────────

def _build_exa_engine(spec: dict[str, Any]) -> Any:
    """Exa 语义搜索专用引擎（embedding 匹配 + 内容摘要）"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        api_key = os.environ.get("EXA_API_KEY", "")
        if not api_key:
            logger.warning("EXA_API_KEY 未设置")
            return []
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
            with urllib.request.urlopen(req, timeout=to) as resp:
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
                        # 无 score 时给固定基线分
                        "score": r.get("score") or 0.75,
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
        api_key = os.environ.get("ANYSEARCH_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            from http_client import HttpClient
            client = HttpClient(timeout=to, max_retries=1, jitter=False)
            resp = client.post(url, body=body, extra_headers=headers)
        except ImportError:
            return []
        if resp.get("status", 0) >= 400 or not resp.get("text"):
            return []
        try:
            data = json.loads(resp["text"])
        except (ValueError, TypeError):
            return []
        content = data.get("result", {}).get("content", []) or []
        records = [
            (i.get("text", "") if isinstance(i, dict) else str(i)) for i in content
        ]
        joined = "".join(records).lower()
        # 配额/限流：仅当无任何结果块（### N.）且文本含配额信号时判定，避免
        # 正常结果正文里出现 'quota/429/rate limit' 等词被误判为配额耗尽。
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
                        "source": "anysearch", "score": 0.7,
                    })
        return results
    return _engine


# ── 搜狗微信搜索引擎 ─────────────────────────────────────────────────────────

def _build_wechat_sogou_engine(spec: dict[str, Any]) -> Any:
    """搜狗微信搜索引擎（weixin.sogou.com）

    抓取搜狗微信搜索结果页，提取公众号文章标题、链接、摘要、公众号名。
    无需登录，无需 API key，纯 HTML 解析。
    """
    timeout = spec.get("timeout", 10)

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
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8")
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
            with urllib.request.urlopen(req, timeout=to) as resp:
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

def _build_stackoverflow_engine(spec: dict[str, Any]) -> Any:
    """Stack Overflow 搜索（Stack Exchange API）"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&q={up.quote(query)}&site=stackoverflow&pagesize={min(n, 10)}"
        headers = {"User-Agent": "argo-search/1.0", "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                import gzip
                raw = resp.read()
                try:
                    data = json.loads(gzip.decompress(raw))
                except Exception:
                    data = json.loads(raw)
            results = []
            for item in data.get("items", []):
                tags = ", ".join(item.get("tags", [])[:3])
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": f"score: {item.get('score', 0)} | answers: {item.get('answer_count', 0)} | tags: {tags}",
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
        token = os.environ.get("GITHUB_TOKEN", "").strip()
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
            with urllib.request.urlopen(req, timeout=to) as resp:
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
        return results
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
            with urllib.request.urlopen(req, timeout=to) as resp:
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

def _build_v2ex_engine(spec: dict[str, Any]) -> Any:
    """V2EX 社区搜索"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://www.v2ex.com/search?q={up.quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8")
            results = []
            titles = re.findall(r'<span[^>]*class="[^"]*item_title[^"]*"[^>]*>(.*?)</span>', html, re.DOTALL)
            for t in titles[:n]:
                title = re.sub(r'<[^>]+>', '', t).strip()
                if title:
                    results.append({
                        "title": title[:80],
                        "url": f"https://www.v2ex.com/search?q={up.quote(query)}",
                        "snippet": "V2EX 社区讨论",
                        "source": "v2ex",
                    })
            return results
        except Exception as e:
            logger.warning(f"V2EX 引擎失败: {e}")
            return []
    return _engine



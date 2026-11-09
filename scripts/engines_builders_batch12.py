#!/usr/bin/env python3
"""批次十二构建器：CN 通用搜索补源（2026-09-28，9044621）。

  so      360 搜索（so.com/s?q=，HTML 解析）
  shenma  神马搜索（m.sm.cn/s?q=，移动端 HTML 解析）

为什么放 batch 而不是 engines_builders_cn：cn 是登记上限 2319 的祖父
文件（中文源声明表，逐源一段），新引擎的下一站按既有惯例是 batch 模块
（engines_builders_tech.py 的登记理由明写了「新引擎的下一站是 batch 模块」）。
两源在 chinese_general 域尾部（位次 8/9）观察期，台账见
tests/test_new_source_reachability.py::_DORMANT_ALLOWLIST。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from typing import Any

from engines_base import http_open, safe_search

logger = logging.getLogger("unified_search.engines")


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


# ── Repology 跨发行版包生态版本与状态真相（免认证）────────────────────

def _build_repology_engine(spec: dict[str, Any]) -> Any:
    """Repology 全网 Linux/UNIX 跨包管理器生态版本真相（免认证）。

    覆盖 Debian、Arch、Nix、Alpine、Homebrew、Fedora 等数百个仓库的真实版本与生命周期状态。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        cleaned = re.sub(r"[，,。？！?\s]+", " ", query or "").strip()
        tokens = [t for t in cleaned.split(" ") if t and not any(w in t.lower() for w in ("包", "版本", "依赖", "软件", "package", "version"))]
        q = tokens[0] if tokens else (cleaned.split(" ")[0] if cleaned else "")
        if not q:
            return []

        import urllib.parse as up
        url = f"https://repology.org/api/v1/projects/?search={up.quote(q)}"
        from engines_base import _http_get_raw
        raw = _http_get_raw(url, {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}, to, engine="repology")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []

        results: list[dict[str, Any]] = []
        for proj_name, entries in list(data.items())[:n]:
            if not isinstance(entries, list) or not entries:
                continue
            newest_ver = ""
            repos_summary = []
            for item in entries:
                if not isinstance(item, dict):
                    continue
                v = item.get("version") or ""
                repo = item.get("repo") or ""
                status = item.get("status") or ""
                if status == "newest" and not newest_ver:
                    newest_ver = v
                if any(k in repo for k in ("debian", "arch", "nix", "homebrew", "alpine", "fedora", "ubuntu")):
                    if len(repos_summary) < 4:
                        repos_summary.append(f"{repo}: {v}")
            if not newest_ver and entries:
                newest_ver = entries[0].get("version", "?")

            repo_text = " · ".join(repos_summary) if repos_summary else f"共收录于 {len(entries)} 个仓库"
            snippet = f"最新版本: {newest_ver} | 主要发行版: {repo_text}"
            results.append({
                "title": f"{proj_name} (Repology 跨发行版包详情)",
                "url": f"https://repology.org/project/{proj_name}/versions",
                "snippet": snippet,
                "source": "repology",
            })
        return results
    return _engine


# ── Grep.app 全球百万 GitHub 核心代码正则检索（官方 MCP 传输免认证）─────

_GREP_APP_MCP_URL = "https://mcp.grep.app"


def _build_grep_app_engine(spec: dict[str, Any]) -> Any:
    """Grep.app 全网开源代码精准检索引擎（官方 MCP 通道，免认证）。

    通过官方 MCP 端点直接调用 searchGitHub，避开常规前端 WAF 挑战，
    支持字面量代码匹配与正则搜索，返回包含仓库、文件路径与行号的真实代码片段。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []

        from engines_builders_tech import _parse_mcp_wire, _mcp_error_of

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "searchGitHub",
                "arguments": {
                    "query": q,
                },
            },
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        req = urllib.request.Request(_GREP_APP_MCP_URL, data=body, headers=headers)
        try:
            with http_open(req, timeout=to, engine=spec.get("_name", "grep_app")) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception as e:
            logger.warning(f"grep_app MCP 请求失败: {e}")
            return []

        data = _parse_mcp_wire(raw)
        if not isinstance(data, dict):
            return []

        mcp_err = _mcp_error_of(data)
        if mcp_err:
            logger.warning(f"grep_app MCP 上游错误: {mcp_err}")
            return []

        result = data.get("result")
        if not isinstance(result, dict):
            return []

        content = result.get("content") or []
        results: list[dict[str, Any]] = []

        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or ""
            if not text:
                continue

            repo_m = re.search(r"Repository:\s*(\S+)", text)
            path_m = re.search(r"Path:\s*(\S+)", text)
            url_m = re.search(r"URL:\s*(\S+)", text)
            snip_m = re.search(r"Snippets:\s*(.*)", text, re.S)

            repo = repo_m.group(1) if repo_m else ""
            path = path_m.group(1) if path_m else ""
            url = url_m.group(1) if url_m else ""
            snippet = snip_m.group(1).strip() if snip_m else text[:250]

            if not url:
                continue

            title = f"{repo}: {path}" if repo and path else (path or repo or "code snippet")
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet[:350],
                "source": "grep_app",
            })
            if len(results) >= n:
                break

        return results

    return _engine



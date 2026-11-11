#!/usr/bin/env python3
"""tests/test_mcp_consolidated_tools.py — 极简 3+1 MCP 工具体系与向后兼容软别名验证。

基于奥卡姆剃刀、第一性原理与 MECE 原则：
1. 验证默认工具暴露面收敛至正交的 4 个工具（Search/Fetch/Research/Screenshot）；
2. 验证 argo_search 按 domain 路由（web, social, job, local）；
3. 验证 argo_fetch 对微信公众号与 PDF 的自适应解析；
4. 验证历史工具名（argo_social_search, argo_job, argo_article, argo_pdf, argo_extract）
   经由软别名（Soft Aliases）转发且 100% 保持向后兼容。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mcp_tools import CORE_TOOL_NAMES, TOOLS, listed_tools
from mcp_handlers import execute_tool


def test_core_tools_mece_and_minimal():
    """验证默认暴露集为 4 个正交核心工具，且 TOOLS 全量中仍包含所有工具以支持兼容。"""
    assert CORE_TOOL_NAMES == ("argo_search", "argo_fetch", "argo_research", "argo_screenshot")
    default_tools = listed_tools()
    default_names = [t["name"] for t in default_tools]
    assert default_names == list(CORE_TOOL_NAMES)

    # 验证 search 的 domain 枚举
    search_tool = next(t for t in TOOLS if t["name"] == "argo_search")
    assert "domain" in search_tool["inputSchema"]["properties"]
    assert search_tool["inputSchema"]["properties"]["domain"]["enum"] == ["web", "social", "job", "local"]

    # 验证 fetch 的 mode 枚举
    fetch_tool = next(t for t in TOOLS if t["name"] == "argo_fetch")
    assert "article" in fetch_tool["inputSchema"]["properties"]["mode"]["enum"]
    assert "pdf" in fetch_tool["inputSchema"]["properties"]["mode"]["enum"]


def test_argo_search_domain_routing():
    """验证 argo_search 的 domain 参数能正确分发至对应业务模块。"""
    # 1. 默认 web 路由
    with patch("mcp_handlers._lazy_cached") as mock_lazy:
        mock_search = MagicMock()
        mock_search.super_search.return_value = {"results": [{"title": "Web Title"}]}
        mock_lazy.return_value = mock_search

        resp = execute_tool("argo_search", {"query": "python tutorial"})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert "results" in data

    # 2. domain="social" 路由
    with patch("mcp_handlers._search_social_platforms") as mock_social:
        mock_social.return_value = ({"zhihu": [{"title": "Zhihu Post"}]}, ["zhihu"], [])
        resp = execute_tool("argo_search", {"query": "ai agent", "domain": "social"})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert data["count"] == 1
        assert data["results"][0]["title"] == "Zhihu Post"

    # 3. domain="job" 路由
    with patch("mcp_handlers._lazy_cached") as mock_lazy:
        mock_job = MagicMock()
        mock_job.search.return_value = {"jobs": [{"title": "Python Dev"}]}
        mock_lazy.return_value = mock_job
        resp = execute_tool("argo_search", {"query": "python", "domain": "job"})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert "jobs" in data

    # 4. domain="local" 路由
    with patch("mcp_handlers.handle_local_search") as mock_local:
        mock_local.return_value = {"content": [{"type": "text", "text": '{"local": true}'}]}
        resp = execute_tool("argo_search", {"query": "readme", "domain": "local"})
        assert not resp.get("isError")
        assert json.loads(resp["content"][0]["text"]) == {"local": True}


def test_argo_fetch_adaptive_routing():
    """验证 argo_fetch 对微信公众号与 PDF 的自适应解析。"""
    # 1. 微信公众号链接自动识别
    with patch("mcp_handlers._lazy_cached") as mock_lazy:
        mock_article = MagicMock()
        mock_article.fetch_article.return_value = {
            "ok": True,
            "title": "WeChat Title",
            "content": "Article Content",
        }
        mock_lazy.return_value = mock_article
        resp = execute_tool("argo_fetch", {"url": "https://mp.weixin.qq.com/s/abcdef123456"})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert data["title"] == "WeChat Title"
        mock_article.fetch_article.assert_called_once()

    # 2. PDF 链接自适应识别
    with patch("mcp_handlers._lazy_cached") as mock_lazy:
        mock_pdf = MagicMock()
        mock_pdf.extract_pdf.return_value = {
            "success": True,
            "markdown": "# PDF Content",
        }
        mock_lazy.return_value = mock_pdf
        resp = execute_tool("argo_fetch", {"url": "https://arxiv.org/pdf/2301.00001.pdf"})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert data["success"] is True
        mock_pdf.extract_pdf.assert_called_once()


def test_soft_aliases_backward_compatibility():
    """验证旧工具名通过软别名（Soft Aliases）可 100% 正常分发执行。"""
    # 1. argo_social_search 软别名映射到 argo_search(domain="social")
    with patch("mcp_handlers._search_social_platforms") as mock_social:
        mock_social.return_value = ({"bilibili": [{"title": "Bilibili Video"}]}, ["bilibili"], [])
        resp = execute_tool("argo_social_search", {"query": "agent swarm"})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert data["count"] == 1
        assert data["results"][0]["title"] == "Bilibili Video"

    # 2. argo_job 软别名映射到 argo_search(domain="job")
    with patch("mcp_handlers._lazy_cached") as mock_lazy:
        mock_job = MagicMock()
        mock_job.search.return_value = {"jobs": [{"title": "Rust Engineer"}]}
        mock_lazy.return_value = mock_job
        resp = execute_tool("argo_job", {"query": "rust"})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert "jobs" in data

    # 3. argo_article 软别名映射到 argo_fetch(mode="article")
    with patch("mcp_handlers._lazy_cached") as mock_lazy:
        mock_article = MagicMock()
        mock_article.fetch_article.return_value = {"ok": True, "title": "Article Alias Title"}
        mock_lazy.return_value = mock_article
        resp = execute_tool("argo_article", {"url": "https://mp.weixin.qq.com/s/xyz"})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert data["title"] == "Article Alias Title"

    # 4. argo_pdf 软别名映射到 argo_fetch(mode="pdf")
    with patch("mcp_handlers._lazy_cached") as mock_lazy:
        mock_pdf = MagicMock()
        mock_pdf.extract_pdf.return_value = {"success": True, "markdown": "# PDF Alias"}
        mock_lazy.return_value = mock_pdf
        resp = execute_tool("argo_pdf", {"url": "https://example.com/paper.pdf"})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert data["markdown"] == "# PDF Alias"

    # 5. argo_fetch(mode="extract") 结构化提取支持
    with patch("mcp_handlers._lazy_cached") as mock_lazy:
        mock_fetch = MagicMock()
        mock_fetch.fetch_page_v3.return_value = {"success": True, "html": "<table></table>", "url": "https://test.com"}
        mock_extract = MagicMock()
        mock_extract.extract_tables.return_value = [{"col": "val"}]
        mock_extract.extract_metadata.return_value = {}
        mock_extract.extract_jsonld.return_value = []
        def _get_mod(mod_name):
            if mod_name == "fetch_v3":
                return mock_fetch
            if mod_name == "extract":
                return mock_extract
            return MagicMock()
        mock_lazy.side_effect = _get_mod
        resp = execute_tool("argo_fetch", {"url": "https://test.com", "mode": "extract"})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert "tables" in data

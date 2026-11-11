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
import re
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


def test_instructions_matches_core():
    """initialize instructions 里的工具名必须与 CORE_TOOL_NAMES 一致。

    回归（2026-11-11 审计）：aedee08 把 CORE 从「search/fetch/local_search」
    改成 4 个正交集（含 screenshot、不含 local_search），但 mcp_transport 的
    instructions 文案没跟着改，仍在教模型「日常 argo_search、argo_fetch、
    argo_local_search」——而 local_search 已不在默认注入面，模型照文案去调
    一个没挂的工具。同批还有 mcp_tools 文件头、mcp_server 文件头、
    references/operations.md、docs/MCP_SETUP.md 四处同款漂移。

    判据分两段：① CORE 里每个工具名都出现在 instructions 里；② instructions
    里点名的 argo_* 工具不得含 CORE 之外的（防「文案列了一堆旧工具」）。
    """
    from mcp_transport import handle_rpc

    instructions = handle_rpc("initialize", {})["instructions"]
    for name in CORE_TOOL_NAMES:
        assert name in instructions, (
            f"CORE 工具 {name} 没写进 initialize instructions——"
            "模型不知道默认面里有它，文案与 CORE_TOOL_NAMES 必须同源")

    mentioned = set(re.findall(r"argo_[a-z_]+", instructions))
    unknown = mentioned - set(CORE_TOOL_NAMES) - {
        "argo_search", "argo_fetch", "argo_research", "argo_screenshot",
        # 文案允许点名「all 才打开」的旧名，它们是真存在的工具
        "argo_local_search", "argo_local_read", "argo_recompute",
        "argo_evidence", "argo_clarify", "argo_crawl", "argo_extract",
        "argo_preflight", "argo_answer", "argo_watch", "argo_cite",
    }
    assert not unknown, f"instructions 点名了不存在的工具：{unknown}"


def test_social_domain_keeps_mode_and_platforms():
    """domain=social 必须仍读 platforms 与 mode，且 sentiment 可达。

    回归（2026-11-11 审计）：软别名把 argo_social_search 映射成
    argo_search(domain=social)，但新 schema 的 mode enum 只有
    fast/auto/deep/budget——旧调用方传 mode=sentiment 会静默走 text 分支，
    返回帖子列表而不是舆情聚合，且没有任何信号。同时 max_results 之外
    旧名 num 与 platforms 也必须继续可读。
    """
    # 1. mode=sentiment 走聚合分支（不真调 research，只看分流）
    with patch("mcp_handlers._search_social_platforms") as mock_social, \
         patch("mcp_handlers._lazy_cached") as mock_lazy:
        mock_social.return_value = ({"zhihu": [{"title": "t"}]}, ["zhihu"], [])
        mock_research = MagicMock()
        mock_research.aggregate_social_sentiment.return_value = {
            "query": "q", "sentiment": "positive", "count": 1}
        mock_lazy.return_value = mock_research

        resp = execute_tool("argo_search", {
            "query": "q", "domain": "social", "mode": "sentiment",
            "platforms": "zhihu,v2ex", "max_results": 3})
        assert not resp.get("isError")
        data = json.loads(resp["content"][0]["text"])
        assert data["sentiment"] == "positive", "mode=sentiment 没走聚合分支"

    # 2. platforms 与 num 继续被读到（旧参数名不丢）
    with patch("mcp_handlers._search_social_platforms") as mock_social:
        mock_social.return_value = ({}, [], [])
        execute_tool("argo_search", {
            "query": "q", "domain": "social",
            "platforms": "twitter,reddit", "num": 3})
        platforms_arg = mock_social.call_args[0][0]
        n_arg = mock_social.call_args[0][2]
        assert platforms_arg == ["twitter", "reddit"], \
            f"platforms 被丢，实际传入 {platforms_arg}"
        assert n_arg == 3, f"旧参数名 num 被丢，实际传入 {n_arg}"

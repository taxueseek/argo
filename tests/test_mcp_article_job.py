#!/usr/bin/env python3
"""argo_article / argo_job — 共用主链（article.fetch_article / job.search）
与 MCP 工具的契约测试。

全部离线：article 打桩 fetch()，job 打桩后端字典；MCP 走 execute_tool 真分发。
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import article
import job

_FIXTURE_HTML = """<html><head>
<meta property="og:title" content="测试文章标题"/>
<meta property="og:article:author" content="测试作者"/>
</head><body>
<div id="js_name">公众号名</div>
<div id="publish_time">2026-08-30</div>
<div id="js_content">
<p>第一段正文内容。</p>
<img data-src="https://mmbiz.qpic.cn/img1.jpg"/>
<img data-src="https://mmbiz.qpic.cn/img2.jpg"/>
</div>
</body></html>"""

_WX_URL = "https://mp.weixin.qq.com/s/abc123"


# ── article.fetch_article 共用主链 ──────────────────────────────────────

def test_article_parse_success():
    with patch.object(article, "fetch", return_value=_FIXTURE_HTML):
        out = article.fetch_article(_WX_URL)
    assert out["ok"] is True
    assert out["title"] == "测试文章标题"
    assert out["author"] == "测试作者"
    assert "第一段正文内容。" in out["content"]
    assert out["image_count"] == 2
    assert out["char_count"] == len(out["content"])


def test_article_non_weixin_url_rejected():
    with pytest.raises(ValueError, match="仅支持"):
        article.fetch_article("https://example.com/a")


def test_article_fetch_failure_is_ok_false():
    with patch.object(article, "fetch", side_effect=OSError("timed out")):
        out = article.fetch_article(_WX_URL)
    assert out["ok"] is False
    assert "抓取失败" in out["error"]


def test_article_anti_crawl_is_ok_false():
    # 反爬页无 js_content 且含「环境异常」
    with patch.object(article, "fetch", return_value="环境异常请稍后重试"):
        out = article.fetch_article(_WX_URL)
    assert out["ok"] is False
    assert "反爬" in out["error"]


def test_article_cli_bad_url_preserved():
    """CLI 错误路径保持原行为：stderr 文本 + exit 1。"""
    p = subprocess.run([sys.executable, str(SCRIPTS / "article.py"),
                        "https://example.com/a"],
                       capture_output=True, text=True)
    assert p.returncode == 1
    assert "仅支持" in p.stderr


# ── job.search 共用主链 ─────────────────────────────────────────────────

def _fake_backends():
    def a(q, n):
        return [{"url": "https://www.zhipin.com/job/1001.html", "title": "会计",
                 "snippet": "薪资面议"},
                {"url": "https://www.liepin.com/job/2002.html", "title": "出纳",
                 "snippet": "双休"}]

    def b(q, n):
        # 与 a 的 zhipin 岗位同标题 → 同指纹 → 应被去重
        return [{"url": "https://www.zhipin.com/job/1001.html", "title": "会计",
                 "snippet": "薪资面议"}]

    def bad(q, n):
        raise RuntimeError("boom")

    return {"a": a, "b": b, "bad": bad}


@pytest.fixture()
def patched_job():
    with patch.object(job, "DEFAULT_ENGINES", ["a", "b", "bad"]), \
         patch.object(job, "ALL_BACKENDS", _fake_backends()):
        yield


def test_job_search_dedupe_and_errors(patched_job):
    out = job.search("会计", city="")
    assert out["query"] == "会计"
    assert out["backends"] == ["a", "b", "bad"]
    # a 出 2 条 + b 出 1 条重复 → 去重后 2 条
    assert out["total"] == 2
    fps = [r["fingerprint"] for r in out["results"]]
    assert len(fps) == len(set(fps))
    assert any("boom" in e for e in out["errors"])
    assert all(r["platform"] for r in out["results"])


def test_job_search_strict_region_filter(patched_job):
    def a_cn(q, n):
        return [{"url": "https://www.zhipin.com/job/3001.html",
                 "title": "成都 会计", "snippet": "五险一金"},
                {"url": "https://www.zhipin.com/job/3002.html",
                 "title": "上海 会计", "snippet": "五险一金"}]

    with patch.object(job, "ALL_BACKENDS", {"a": a_cn}):
        out = job.search("会计", city="成都")
    assert out["strict"] is True
    assert out["dropped_region"] == 1
    assert out["total"] == 1
    assert "成都" in out["results"][0]["title"]


def test_job_search_unknown_platform_raises():
    with pytest.raises(ValueError, match="未识别的平台"):
        job.search("会计", platforms="nope")


def test_job_search_overseas_adds_free_backends(patched_job):
    out = job.search("会计", city="", engine="free")
    assert out["backends"] == list(job.FREE_BACKENDS)


# ── MCP 工具（execute_tool 真分发）───────────────────────────────────────

def test_mcp_argo_article_success_and_truncate():
    from mcp_handlers import execute_tool
    with patch.object(article, "fetch", return_value=_FIXTURE_HTML):
        res = execute_tool("argo_article", {"url": _WX_URL})
    payload = json.loads(res["content"][0]["text"])
    assert payload["title"] == "测试文章标题"
    assert "truncated" not in payload

    with patch.object(article, "fetch", return_value=_FIXTURE_HTML):
        res = execute_tool("argo_article", {"url": _WX_URL, "max_chars": 2})
    payload = json.loads(res["content"][0]["text"])
    assert payload["truncated"] is True
    assert len(payload["content"]) == 2


def test_mcp_argo_article_error_is_actionable():
    from mcp_handlers import execute_tool
    res = execute_tool("argo_article", {"url": "https://example.com/a"})
    assert res.get("isError") is True
    assert "仅支持" in res["content"][0]["text"]


def test_mcp_argo_job_success(patched_job):
    from mcp_handlers import execute_tool
    res = execute_tool("argo_job", {"query": "会计"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["total"] == 2
    assert payload["errors"]


def test_mcp_argo_job_error_is_actionable():
    from mcp_handlers import execute_tool
    res = execute_tool("argo_job", {"query": "x", "platforms": "nope"})
    assert res.get("isError") is True
    assert "未识别的平台" in res["content"][0]["text"]


def test_mcp_tool_schemas_registered():
    from mcp_tools import TOOLS
    names = {t["name"] for t in TOOLS}
    assert {"argo_article", "argo_job"} <= names
    for t in TOOLS:
        if t["name"] == "argo_job":
            assert t["inputSchema"]["required"] == ["query"]
        if t["name"] == "argo_article":
            assert t["inputSchema"]["required"] == ["url"]

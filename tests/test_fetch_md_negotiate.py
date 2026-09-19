#!/usr/bin/env python3
"""
test_fetch_md_negotiate.py — 内容协商（Accept: text/markdown）回归门

协商复用主路径那一次 GET，故必须证明两件事：
  收益：支持协商的站点真的拿到 Markdown，且不经 HTML 提取、结构不被破坏
  安全：声称 Markdown 却是假货的响应一律被拦，且不支持协商的站点行为不变

假货样本全部来自 2026-09 实测（详见 tests 内注释与各断言名），
它们不是构造出来的极端值，是真实站点当时的真实响应。
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_v3  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_v3, "_IDENTITY_PATH",
                        str(tmp_path / "identity.json"))
    monkeypatch.setattr(fetch_v3, "_identity_mem", {})
    monkeypatch.setattr(fetch_v3, "_identity_loaded", True)


def _resp(text, ctype="text/markdown; charset=utf-8", status=200, url=""):
    return {"status": status, "headers": {"Content-Type": ctype},
            "text": text, "url": url or "https://x.com/page"}


MD = "# Guide\n\n" + "正文段落，足够长以通过长度地板。" * 20


# ─── 收益侧：真 Markdown 必须被认下来 ─────────────────────────────────────────

def test_accepts_markdown_ctype():
    assert fetch_v3._negotiated_markdown(_resp(MD)) == MD


def test_accepts_text_plain_markdown():
    """react.dev/learn 实测返回 text/plain，正文却是带 frontmatter 的真 Markdown。"""
    body = "---\ntitle: Quick Start\n---\n\n" + MD
    assert fetch_v3._negotiated_markdown(_resp(body, "text/plain; charset=utf-8")) == body


def test_rejects_html_response():
    """不支持协商的站点原样返回 HTML —— 必须走原链路，不能被当成 Markdown。"""
    html = "<!DOCTYPE html><html><head><title>x</title></head><body>hi</body></html>"
    assert fetch_v3._negotiated_markdown(_resp(html, "text/html")) is None


# ─── 假货侧：四类污染源，每类一个真实样本 ────────────────────────────────────

def test_rejects_stub_page_with_markdown_ctype():
    """docs.docker.com/get-started/ 实测：200 + text/markdown + 16 字节占位页。"""
    assert fetch_v3._negotiated_markdown(_resp("# Get started\n")) is None


def test_rejects_404_disguised_as_markdown():
    """supabase.com/docs.md 实测：404 + Content-Type: text/markdown + 错误提示。"""
    body = "# 404 Not Found\n\n/docs.md does not exist on supabase.com. " * 5
    assert fetch_v3._negotiated_markdown(_resp(body, status=404)) is None


def test_rejects_html_error_page_with_markdown_ctype():
    """docs.gitbook.com/ 实测：200 + text/markdown，正文却是完整 HTML 错误页。"""
    body = ("<html>\n<head><title>302 Found</title></head>\n<body>\n"
            "<center><h1>302 Found</h1></center>\n</body>\n</html>\n") * 3
    assert fetch_v3._negotiated_markdown(_resp(body)) is None


def test_rejects_error_page_prose():
    """错误页也可能不带 HTML 标签，靠正文特征兜底（含前置 # 标题号）。"""
    body = "# Redirecting...\n\n" + "跳转说明。" * 40
    assert fetch_v3._negotiated_markdown(_resp(body)) is None


# ─── 判据本身的反向保护：不能过度拒绝 ────────────────────────────────────────

def test_markdown_with_embedded_jsx_is_not_html():
    """react.dev 的真 Markdown 内嵌 <Intro>/JSX 代码示例。

    历史风险：拿任意标签（如 <div）判「这是 HTML」会把它误判成 HTML，
    造成假阴性、白丢整页正文。判据必须只看文档级标记。
    """  # noqa: D301 — 说明里含尖括号，仅为可读性
    body = ("---\ntitle: Quick Start\n---\n\n<Intro>\n\nWelcome" + " 正文。" * 40
            + "\n\n</Intro>\n\n<YouWillLearn>\n\n- a\n- b\n\n</YouWillLearn>\n\n"
            "```jsx\n<div className=\"x\"></div>\n```\n")
    assert fetch_v3._negotiated_markdown(_resp(body, "text/plain")) is not None


def test_markdown_rich_in_html_tags_is_not_a_document():
    """内嵌大量 HTML 标签的真 Markdown 不得被判为 HTML 文档。

    两个真实反例，都曾把判据打穿：
      - mintlify 的文档里是带超长 className 的 JSX 代码示例，
        24 个标签就占了全篇 51.9% 的字符
      - blog.cloudflare.com 的协商 Markdown 里有 50 个闭标签
    两者返回的都是百分之百的真 Markdown。按「标签个数」或「标签字符占比」
    判都会误拒，代价是协商静默失效、掉到外部阅读器（实测 1.2s → 8.8s）。
    所以判据只认文档级标记：真实网页响应一定在开头自报家门。
    """
    jsx = "\n\n".join(
        f'<a className="group cursor-pointer pb-8 hover:opacity-80" href={{href}}>\n'
        f'  <img src={{`/images/hero/${{name}}-{i}.png`}} className="block dark:hidden '
        f'pointer-events-none group-hover:scale-105 transition-all duration-100" />\n'
        f'  <h3 className="mt-5 text-gray-900 dark:text-zinc-50 font-medium">标题 {i}</h3>\n'
        f'</a>'
        for i in range(20))
    body = "> ## Documentation Index\n\n# Introduction\n\n" + jsx
    assert fetch_v3._looks_like_html_document(body) is False
    assert fetch_v3._negotiated_markdown(_resp(body)) is not None


def test_document_level_markers_still_rejected():
    for marker in ("<!doctype html>", "<html>", "<head>", "<body>"):
        body = marker + "\n" + MD
        assert fetch_v3._looks_like_html_document(body), marker


def test_negotiate_disabled_by_env(monkeypatch):
    monkeypatch.setenv("ARGO_FETCH_MD_NEGOTIATE", "0")
    assert fetch_v3._negotiate_enabled() is False
    monkeypatch.setenv("ARGO_FETCH_MD_NEGOTIATE", "1")
    assert fetch_v3._negotiate_enabled() is True


# ─── 接线侧：协商结果不得再经 HTML 提取 ──────────────────────────────────────

def test_markdown_result_preserves_structure():
    """协商命中的价值在于保留站点自己的结构，故不得走 extract_content。

    extract_content 会把 Markdown 当纯文本重新切块，`#`/`|`/``` 这些结构全丢。
    """
    md = "# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nx = 1\n```\n\n" + "正文。" * 40
    out = fetch_v3._markdown_result("https://x.com/p", md, 8000, "http_md")
    assert out["title"] == "标题"
    assert "| a | b |" in out["content"]
    assert "```python" in out["content"]
    assert out["html"] == ""


def test_markdown_result_reads_frontmatter_title():
    """Cloudflare 等站点在 YAML frontmatter 里给标题，没有 H1。"""
    md = "---\ntitle: Cloudflare Fundamentals\ndescription: x\n---\n\n" + "正文。" * 40
    out = fetch_v3._markdown_result("https://x.com/p", md, 8000, "http_md")
    assert out["title"] == "Cloudflare Fundamentals"


def test_markdown_result_records_final_url_on_redirect():
    """nextjs.org 实测：308 重定向后正文来自别的地址，必须如实标注。"""
    out = fetch_v3._markdown_result("https://x.com/a", MD, 8000, "http_md",
                                    final_url="https://x.com/b")
    assert out["final_url"] == "https://x.com/b"
    same = fetch_v3._markdown_result("https://x.com/a", MD, 8000, "http_md",
                                     final_url="https://x.com/a/")
    assert "final_url" not in same


# ─── 升级链豁免：Markdown 结果不应再触发任何升级动作 ─────────────────────────

def test_markdown_result_never_escalates():
    """正文里出现 Cloudflare 这类词是常态，不得因此判为反爬壳。

    _CF_MARKERS 含裸词 "cloudflare"，若不豁免，blog.cloudflare.com 的正文
    会因为提到自己而触发 mobile/TLS/jina/parallel/浏览器整条升级链。
    """
    for method in ("http_md", "md_variant", "llms_txt"):
        r = fetch_v3._markdown_result("https://x.com/p", MD, 8000, method)
        assert fetch_v3._needs_browser(r) is False, method

    cf_text = ("# Cloudflare Fundamentals\n\nCloudflare is a connectivity "
               "cloud network. Cloudflare powers millions of websites.\n\n"
               + "正文。" * 40)
    r = fetch_v3._markdown_result("https://x.com/p", cf_text, 8000, "http_md")
    assert r["success"] and fetch_v3._needs_browser(r) is False


def test_markdown_challenge_page_still_escalates():
    """豁免只放行弱特征；读者代理返回的挑战页必须仍然升级。

    读者代理（jina/Parallel 之类）失败时会回一段人机校验文本。若把 Markdown
    结果整类豁免，这段校验文本会被当成正文收下、且不再起浏览器——比不豁免更糟。
    强特征在任何来源里都只可能是挑战页，与格式无关。
    """
    challenge = ("Just a moment...\n\nChecking your browser before accessing "
                 "the site.\n\n" + "Please wait." * 20)
    r = fetch_v3._markdown_result("https://x.com/p", challenge, 8000, "jina")
    assert r["success"] is True
    assert fetch_v3._needs_browser(r) is True

    for marker in ("Ray ID: 8f2a1b", "challenge-platform", "cf_chl_opt",
                   "cf-browser-verification", "Please verify you are a human"):
        text = f"# Title\n\n{marker}\n\n" + "正文。" * 40
        r = fetch_v3._markdown_result("https://x.com/p", text, 8000, "http_md")
        assert fetch_v3._needs_browser(r) is True, marker


def test_plain_http_result_still_escalates_on_js_shell():
    """反向保护：普通 HTML 结果的升级判据不能被削弱。"""
    r = {"url": "https://x.com", "content": "x" * 50, "html": "",
         "title": "", "length": 50, "success": True, "error": None,
         "fetch_method": "http"}
    assert fetch_v3._needs_browser(r) is True


# ─── _http_fetch 端到端（打桩 HttpClient，不打真实网络）──────────────────────

def _install_http(monkeypatch, resp, captured):
    import http_client

    def fake_get(self, url, extra_headers=None, follow_redirects=True):
        captured["headers"] = extra_headers
        captured["url"] = url
        return resp

    monkeypatch.setattr(http_client.HttpClient, "get", fake_get)
    monkeypatch.setattr(fetch_v3, "_negotiate_enabled", lambda: True)


def test_http_fetch_adopts_markdown_and_sends_accept(monkeypatch):
    captured = {}
    _install_http(monkeypatch, _resp(MD, url="https://x.com/page"), captured)
    out = fetch_v3._http_fetch("https://x.com/page", 8000, 5.0)
    assert out["fetch_method"] == "http_md"
    assert out["content"] == MD
    assert "text/markdown" in captured["headers"]["Accept"]


def test_http_fetch_html_unchanged_keeps_original_method(monkeypatch):
    captured = {}
    html = ("<html><body><article><p>" + "真实正文。" * 60 + "</p></article>"
            "</body></html>")
    _install_http(monkeypatch, _resp(html, "text/html"), captured)
    out = fetch_v3._http_fetch("https://x.com/page", 8000, 5.0)
    assert out["fetch_method"] == "http"
    assert "text/markdown" in captured["headers"]["Accept"]


def test_http_fetch_without_markdown_permission_sends_no_accept(monkeypatch):
    """crawl/extract 要 raw HTML 取链接，此时绝不能协商。"""
    captured = {}
    _install_http(monkeypatch, _resp(MD, url="https://x.com/page"), captured)
    out = fetch_v3._http_fetch("https://x.com/page", 8000, 5.0,
                               allow_markdown=False)
    assert captured["headers"] is None
    assert out["fetch_method"] != "http_md"


# ─── need_html 契约：只产 markdown 的通道必须全部关闭 ────────────────────────
# need_html 的 docstring 承诺「跳过 tinyfish/jina/Parallel（仅产 markdown）、
# 停用内容协商」。`.md` 变体回探是同一类通道，2026-09-19 前漏在门外：
# extract 传 need_html=True 抓到站点提供的 .md 后，html 字段为空，
# extract.py 再拿 markdown 跑表格/Meta/JSON-LD 正则 → 三项全空却 success=True。

_HTML_DOC = ("<html><head><title>T</title></head><body>"
             "<table><tr><td>1</td></tr></table></body></html>")
_MD_DOC = "# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"


def _install_md_variant(monkeypatch, calls):
    def _fake_http(url, max_chars, timeout, allow_markdown=True):
        calls.append(allow_markdown)
        return {"url": url, "title": "T", "content": "", "html": _HTML_DOC,
                "length": len(_HTML_DOC), "success": True, "error": "",
                "fetch_method": "http"}

    monkeypatch.setattr(fetch_v3, "_http_fetch", _fake_http)
    monkeypatch.setattr(fetch_v3, "_md_variant_enabled", lambda: True)
    monkeypatch.setattr(fetch_v3, "_md_variant_wanted", lambda r: True)
    monkeypatch.setattr(fetch_v3, "_md_variant_fetch", lambda url, mc, to: {
        "url": url, "title": "T", "content": _MD_DOC[:mc], "html": "",
        "length": len(_MD_DOC), "success": True, "error": "",
        "fetch_method": "md_variant"})
    monkeypatch.setattr(fetch_v3, "_needs_browser", lambda r: False)


def test_need_html_blocks_md_variant(monkeypatch):
    """need_html=True：不得被 .md 变体替换，HTML 必须留在结果里。"""
    calls = []
    _install_md_variant(monkeypatch, calls)
    out = fetch_v3.fetch_v3("https://example.com/doc", need_html=True,
                            skip_cache=True)
    assert out["fetch_method"] != "md_variant", "need_html 被 .md 变体绕过"
    assert out["html"], "need_html=True 却拿不到 HTML（extract 会静默全空）"


def test_without_need_html_md_variant_still_used(monkeypatch):
    """对照面：默认路径必须照旧享受 .md 变体（能力不得被这次修复砍掉）。"""
    calls = []
    _install_md_variant(monkeypatch, calls)
    out = fetch_v3.fetch_v3("https://example.com/doc", need_html=False,
                            skip_cache=True)
    assert out["fetch_method"] == "md_variant"

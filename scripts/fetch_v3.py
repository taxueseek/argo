#!/usr/bin/env python3
"""
fetch_v3.py — 抓取降级链（零外部依赖，纯 stdlib + 系统 Chrome）

吸收 Hound 的页面交互能力，但不引入 Playwright/Patchright 依赖：
  第零级：AI 友好变体探测（{url}.md 直出 + 站点根 /llms.txt，诚实身份；
          ARGO_FETCH_MD_VARIANT=0 关闭）
  第一级：增强 HTTP（UA 轮换 + Cookie 积累 + 重试弹性）
  第一级A2：移动端 UA 分支（客户端形态分流型反爬；门控站单次直连 +
            per-host 身份记忆；ARGO_FETCH_MOBILE=0 关闭）
  第一级B：TLS 指纹伪造（curl_cffi impersonate，多指纹轮换，免起浏览器）
  第一级C：r.jina.ai 阅读器（keyless 免费层，远端 JS 渲染转 markdown；
            仅公网 URL，ARGO_FETCH_JINA=0 关闭）
  第二级：Chrome CDP 驱动（页面交互/JS 渲染/CF 绕过）
  第三级：内容质量评估（content_ok/page_type/quality_score）

对比 fetch_v2：
- fetch_v2: urllib + Hound subprocess（需要 master_fetch 包）
- fetch_v3: http_client(stdlib) + curl_cffi(可选) + chrome_cdp(stdlib+系统Chrome) → 完全自主

TLS 指纹伪造层（第一级B）：
  指纹检测型反爬（Cloudflare 等）凭 TLS ClientHello 判 bot，urllib 直接 403。
  curl_cffi 可逐字节模拟 Chrome/Safari/Firefox 指纹，免起浏览器即可通过。
  开关：ARGO_FETCH_IMPERSONATE=0 关闭，默认开启。

用法：
    from fetch_v3 import fetch_v3, fetch_page_v3
    result = fetch_v3("https://example.com", actions=[{"click": "#btn"}])
    # 兼容旧接口
    result = fetch_page_v3("https://example.com", max_chars=3000)
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import traceback
import urllib.parse
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# 确保能导入同目录模块
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# 渲染层（第二级A）：TinyFish 直连渲染，独立模块以保持本文件聚焦主链调度
import fetch_render_tinyfish as _render_tinyfish

# 质量信号层（第三级）：来源分类 / 页面类型 / 质量分 / 内容安全
import fetch_quality as _quality

# 本地状态目录唯一来源（env ARGO_STATE_DIR → config cache.db_path 父目录 → 旧路径）
import argo_paths as _paths
from net_proxy import open_url  # 出口调度唯一入口（issue #13 同类修复）
from engine_env import env_flag  # 布尔开关统一判断（见 env_flag 的说明）
from cli_io import dumps


# ─── 内容提取器（复用 fetch.py 的逻辑，增强版）──────────────────────────────

_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"}

class ContentExtractor(HTMLParser):
    """从 HTML 提取正文文本（基于文本密度排序）。"""

    def __init__(self):
        super().__init__()
        self._in_skip = 0
        self._blocks: list[tuple[float, str]] = []
        self._current: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._in_skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._in_skip = max(0, self._in_skip - 1)
        if tag == "title":
            self._in_title = False
        if tag in ("p", "div", "article", "section", "li", "h1", "h2", "h3", "h4", "td", "blockquote"):
            text = "".join(self._current).strip()
            if len(text) > 20:
                density = len(text.replace(" ", "")) / max(len(text), 1)
                self._blocks.append((density, text))
            self._current = []

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_skip == 0:
            self._current.append(data)


def _cut(text: str, limit: int) -> str:
    """按上限裁剪；limit <= 0 表示不限量（取全文）。"""
    if not text:
        return text
    return text[:limit] if limit and limit > 0 else text


def extract_content(html: str, max_chars: int = 8000) -> tuple[str, str]:
    """从 HTML 提取正文和标题。

    P0 增强：readability 密度法为主（链接密度惩罚 + 标签权重 + 容器归并，
    保持文档顺序）；返回空时回退旧密度排序实现（链接列表页等无正文场景
    不至于丢掉旧行为保底的结果）。
    """
    try:
        from readability_extract import extract_readability
        content, title = extract_readability(html, max_chars=max_chars)
        if content.strip():
            return _cut(content, max_chars), title.strip()
    except Exception:
        pass
    ext = ContentExtractor()
    try:
        ext.feed(html)
    except Exception:
        pass
    ext._blocks.sort(key=lambda x: x[0], reverse=True)
    content = "\n\n".join(text for _, text in ext._blocks[:10])
    return _cut(content, max_chars), ext.title.strip()


# ─── 降级检测 ────────────────────────────────────────────────────────────────

# 强特征：与格式无关，任何来源（含 Markdown 正文）出现都说明这是反爬挑战页
_CF_STRONG = re.compile(
    r"checking your browser|cf-browser-verification|cf_chl_opt|ray id|"
    r"challenge-platform|please verify you are a human",
    re.IGNORECASE,
)
# 弱特征：裸词 cloudflare。只在 HTML 里有判别力——Markdown 正文里正常
# 提到 Cloudflare 是家常便饭（blog.cloudflare.com 通篇都是），不能据此判壳
_CF_WEAK = re.compile(r"cloudflare", re.IGNORECASE)
# 壳特征的扫描窗口：只在开头这段里找。
# 挑战页一定在顶部自报家门，而大页面中途总会出现裸词——实测 Wikipedia 的
# HTML 首个命中在第 241,552 字符、Astro 在第 78,009 字符，都在脚本与脚注里。
# 扫全文的后果是「页面越长越像反爬壳」：同一站点把 --max-chars 调大就会
# 悄悄换掉抓取方式（Wikipedia 因此从 http 升级到 Chrome CDP，正文也从
# 32,789 字变成另一份 68,143 字的 CDP 产物），预算改变了内容来源，
# 这正是「预算引发的错误」最难察觉的一种。
_CF_SCAN_CHARS = 32_768
# HTML 路径的壳判据＝强特征 ∪ 弱特征
_CF_MARKERS = re.compile(
    r"checking your browser|cf-browser-verification|cf_chl_opt|ray id|"
    r"challenge-platform|please verify you are a human|cloudflare",
    re.IGNORECASE,
)

_JS_MARKERS = re.compile(
    r"enable javascript|javascript is required|javascript to run this app|"
    r"you need to enable javascript|requires javascript",
    re.IGNORECASE,
)

_EMPTY_SHELL = re.compile(
    r"^[\s\n]*<html[^>]*>[\s\n]*<head>.*?</head>[\s\n]*<body>[\s\n]*</body>[\s\n]*</html>[\s\n]*$",
    re.IGNORECASE | re.DOTALL,
)


# 已产出 Markdown 的抓取方式：内容已由站点或阅读器转好，没有 HTML 壳可言。
_MARKDOWN_METHODS = frozenset({"http_md", "md_variant", "llms_txt",
                               "jina", "jina_reader", "parallel",
                               "parallel_mcp", "tinyfish"})


def _needs_browser(result: dict) -> bool:
    """判断是否需要升级到浏览器抓取。

    长度判据排在 Markdown 豁免之前：「内容过短」是内容量判据，与格式无关，
    安全网必须留着。

    Markdown 豁免只放行**弱**特征（裸词 cloudflare），不放行**强**特征。
    两边的代价不对称：
      - 豁免过宽：读者代理返回的挑战页会被当成正文收下，且不再升级浏览器
        （实测夹具：本应调 browser 拿到真正文，豁免后直接返回「Just a
        moment... Checking your browser」并标 content_ok=True）
      - 豁免过窄：正文里提到 Cloudflare 就被判壳，白跑整条升级链
    强特征（checking your browser / ray id / challenge-platform …）在任何
    来源里都只可能是挑战页，不该被格式豁免掉；弱特征才会误伤正文。
    """
    if not result.get("success"):
        return True
    content = result.get("content", "")
    if not content or len(content.strip()) < 100:
        return True
    if result.get("fetch_method") in _MARKDOWN_METHODS:
        return bool(_CF_STRONG.search(content))
    html = result.get("html", "")
    # 空壳 HTML
    if html and _EMPTY_SHELL.search(html):
        return True
    # CF 挑战：弱特征只看开头窗口，强特征扫全文（强特征在任何位置都只可能是
    # 挑战页，且长度有限，不会因页面变长而误中）
    window = (html or content)[:_CF_SCAN_CHARS]
    if _CF_MARKERS.search(window) or _CF_STRONG.search(content):
        return True
    # JS 要求
    if len(content) < 300 and _JS_MARKERS.search(html):
        return True
    return False


def _impersonate_enabled() -> bool:
    """TLS 指纹伪造层开关：ARGO_FETCH_IMPERSONATE=0 关闭，默认开启。"""
    return env_flag("ARGO_FETCH_IMPERSONATE")


# ─── 第零级：AI 友好变体探测（{url}.md 直出）────────────────────────────────

_MD_SKIP_EXTS = {".html", ".htm", ".php", ".jsp", ".asp", ".aspx", ".shtml",
                 ".pdf", ".xml", ".json", ".png", ".jpg", ".jpeg", ".gif",
                 ".webp", ".svg", ".css", ".js", ".zip", ".gz", ".mp4", ".mp3"}

# ─── Markdown 响应校验（协商与 .md 变体共用同一组判据）────────────────────
#
# 2026-09 实测：声称 text/markdown 的响应里每 5 个就有 1 个是假货——
#   docs.docker.com   200 + text/markdown + 16 字节占位页（正文只有一行标题）
#   docs.gitbook.com  200 + text/markdown + 完整 HTML 错误页（302 Found）
#   supabase / prisma 404 + text/markdown + 错误提示
#   nextjs.org        308 + text/markdown + "Redirecting..."，正文来自别的 URL
# 单看 Content-Type 会把噪声灌进语料，故统一在此收口。

_MD_MIN_CHARS = 200  # 长度地板：低于此视为占位页/跳转页
# 内容协商 Accept：markdown 优先、html 次之、其余兜底。实测 37 个站点，
# 不支持者一律原样返回 HTML，无 406 风险，故可作为主路径的默认请求头。
_ACCEPT_NEGOTIATE = "text/markdown, text/html;q=0.8, */*;q=0.5"
# 「这是一份 HTML 文档」的判据：只看文档级标记。不能用任意标签——
# react.dev 返回的真 Markdown 内嵌 <Intro>/JSX 代码示例，拿 <div 当判据
# 会把真 Markdown 误判成 HTML（实测假阴性）。
_MD_DOC_MARKERS = re.compile(r"<\s*(!doctype|html[\s>]|head[\s>]|body[\s>])",
                             re.IGNORECASE)
# 错误页/占位页正文特征（不区分大小写，允许前置 # 标题号）
_MD_ERROR_PAGE = re.compile(
    r"^\s*(?:#+\s*)?(?:404|403|500|502|not\s+found|redirecting|access\s+denied|"
    r"just\s+a\s+moment|please\s+verify\s+you\s+are\s+a\s+human)\b",
    re.IGNORECASE)


def _md_variant_enabled() -> bool:
    """.md 变体探测开关：ARGO_FETCH_MD_VARIANT=0 关闭，默认开启。"""
    return env_flag("ARGO_FETCH_MD_VARIANT")


def _negotiate_enabled() -> bool:
    """内容协商开关：ARGO_FETCH_MD_NEGOTIATE=0 关闭，默认开启。

    协商不是额外探测——它复用主路径那一次 GET 的请求头，故零额外请求、
    零额外延迟。实测 37 个站点支持者得干净 Markdown、不支持者原样返回
    HTML，无一例失败（不存在 406 风险），因此默认常开。
    """
    return env_flag("ARGO_FETCH_MD_NEGOTIATE")


def _is_markdown_ctype(ctype: str) -> bool:
    """Content-Type 是否可能承载 Markdown。

    text/plain 一并纳入：react.dev/learn 实测返回 text/plain 而正文是真
    Markdown（带 frontmatter），只认 text/markdown 会漏掉它。
    """
    ctype = (ctype or "").lower()
    return "markdown" in ctype or ctype.startswith("text/plain")


def _looks_like_html_document(text: str) -> bool:
    """判断正文是否是一份 HTML 文档（而非内嵌片段的正经 Markdown）。

    只用一条判据：开头窗口里出现文档级标记（`<!doctype`/`<html`/`<head`/`<body>`）。
    真实网页响应一定在开头自报家门，因此这条足够拦住实测到的全部假货
    （gitbook 的 HTML 错误页、Cloudflare 的 HTML 外壳、404 页）。

    先后试过两条更"聪明"的二级判据，都被真实站点证伪，记在这里免得重蹈：
      - 数标签个数：合法 Markdown 内嵌少量 HTML 就会被顶穿（8 行表格＝33 个闭标签）
      - 数标签字符占比：Markdown 里的 JSX 代码示例属性极长，实测 mintlify 的
        单页占比达 51.9%，而它返回的是百分之百的真 Markdown；
        blog.cloudflare.com 同样因此被误拒，从一次 GET 掉到外部阅读器。
    结论：想从「Markdown 里混着 HTML」中分辨「这是一份 HTML 文档」，
    结构统计不可靠；以开头自报家门为准，宁可漏判（退回常规 HTML 路径，只是
    少省一点）也不误收（把 HTML 当正文交出去，结构尽失且不再升级）。
    """
    return bool(_MD_DOC_MARKERS.search(text[:4096]))


def _looks_like_markdown(text: str) -> bool:
    """嗅探响应体是否为可用 Markdown 正文（排除伪装成 .md 的 HTML 壳）。"""
    if not text or len(text.strip()) < 120:
        return False
    if _looks_like_html_document(text):
        return False
    return True


def _tinyfish_enabled() -> bool:
    """tinyfish 免费渲染层开关（直连 api.fetch.tinyfish.ai，需 TINYFISH_API_KEY）：
    ARGO_FETCH_TINYFISH=0 关闭，默认开启。

    仅在http/指纹均失败或命中 JS/反爬壳时启用，结果失败自动回退，
    不改变正常抓取路径。

    实现委托给 fetch_render_tinyfish（渲染层已独立成模块）。
    """
    return _render_tinyfish.enabled()


def _md_variant_url(url: str) -> str | None:
    """返回可探测的 .md 变体 URL；不适合探测时返回 None。

    只对无扩展名路径（文档站页面形态）追加 .md；站点根（/）不放 .md 探测
    （根级 AI 友好文件是 llms.txt，由 _llms_txt_url 负责），已带扩展名或
    带查询串的动态地址跳过，避免无意义请求。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    path = parsed.path or "/"
    ext = os.path.splitext(path)[1].lower()
    if ext or parsed.query or path in ("", "/"):
        return None
    return url.rstrip("/") + ".md"


def _llms_txt_url(url: str) -> str | None:
    """返回站点根 /llms.txt 候选；仅对站点根路径（文档站门户形态）探测。

    llms.txt（2024 起、规范 v1.8.0）是站点自述的 markdown 索引：H1 站名 +
    分节资源清单，头部开发者文档站采用中。非根路径不探测——该文件只存在于
    站点根，对深层页面探测是无意义请求。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.path not in ("", "/") or parsed.query:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/llms.txt"


# llms-full.txt（llms.txt v2 规范伴生文件）明确禁用（2026-09-29）：全站
# 正文合并成单文件，实测最大站点千万 token 量级——一次抓取撑爆 agent 上
# 下文窗口且无索引结构。守卫让「以后有人按规范补探测」直接踩刹车。
_BANNED_AI_VARIANTS = frozenset({"llms-full.txt"})


def _is_banned_ai_variant(url: str) -> bool:
    """候选 URL 是否指向禁用的 AI 变体（按路径末段判，防查询串伪装）。"""
    try:
        path = urlparse(url).path
    except Exception:
        return False
    return path.rstrip("/").rsplit("/", 1)[-1] in _BANNED_AI_VARIANTS


def _ai_variant_candidates(url: str) -> list[tuple[str, str]]:
    """第零级探测候选清单（按优先序）：页面 .md 直出 → 站点根 llms.txt。"""
    out: list[tuple[str, str]] = []
    md = _md_variant_url(url)
    if md:
        out.append((md, "md_variant"))
    llms = _llms_txt_url(url)
    if llms:
        out.append((llms, "llms_txt"))
    # token 炸弹守卫：全站合并类变体永不进候选（见 _BANNED_AI_VARIANTS）
    return [(u, k) for u, k in out if not _is_banned_ai_variant(u)]


def _negotiated_markdown(resp: dict) -> str | None:
    """从一次普通 GET 的响应里取出经校验的 Markdown 正文；不适用返回 None。

    四道门，逐条对应实测到的四类污染源：状态码 200 → 长度地板 → 非 HTML
    文档 → 非错误页特征。任一不过即视为普通 HTML 响应、走原有链路，因此
    不支持的站点行为完全不变。
    """
    if resp.get("status") != 200 or not resp.get("text"):
        return None
    ctype = ""
    for k, v in (resp.get("headers") or {}).items():
        if str(k).lower() == "content-type":
            ctype = str(v)
            break
    if not _is_markdown_ctype(ctype):
        return None
    text = resp["text"]
    if len(text.strip()) < _MD_MIN_CHARS:
        return None
    if _looks_like_html_document(text):
        return None
    if _MD_ERROR_PAGE.match(text.lstrip()):
        return None
    return text


def _markdown_title(content: str) -> str:
    """从 Markdown 正文取标题：先认 frontmatter 的 title，再认首行 H1。

    顺序不能反、范围不能放：全文 search `^#` 会把代码块里的 `# 注释` 当成
    标题——实测一篇 frontmatter 写明 `title: Install Guide` 的文章，标题被
    代码注释覆盖成 `install dependencies`。H1 只认「跳过 frontmatter 后的
    首个非空行」，正是 Markdown 的 H1 语义位置。
    """
    m = re.search(r"^title:\s*(.+)$", content[:2000], re.MULTILINE)
    if m:
        return m.group(1).strip().strip("\"'")[:200]
    body = re.sub(r"\A---\n.*?\n---\n", "", content, count=1, flags=re.DOTALL)
    m = re.match(r"\s*#\s+(.+?)\s*$", body, re.MULTILINE)
    return m.group(1)[:200] if m else ""


def _markdown_result(url: str, text: str, max_chars: int, method: str,
                     final_url: str = "") -> dict:
    """用已就绪的 Markdown 直接构建结果。

    关键：不走 extract_content——Markdown 喂进 HTML 提取器会被当纯文本
    重新切块，结构（标题/表格/围栏）全丢。协商命中的价值正在于保留站点
    自己那份结构，故此处原样透传。
    """
    content = _cut(text, max_chars)
    # 标题从完整正文里取：裁剪窗口小于标题搜索窗（2,000 字）时，
    # 用交付视图取标题会因窗口不足而取不到。
    title = _markdown_title(text)
    result = {
        "url": url,
        "content": content,
        "html": "",
        "title": title,
        "length": len(content),
        "success": True,
        "error": None,
        "fetch_method": method,
    }
    _archive_truncated(url, text, content, "text", result, primary=True)
    if final_url and final_url.rstrip("/") != url.rstrip("/"):
        # 内容来自重定向后的地址：如实标注，避免「URL 是 A、正文是 B」而无声
        result["final_url"] = final_url
    return result


def _md_variant_wanted(result: dict) -> bool:
    """主请求结果是否还值得回探 AI 友好变体。

    已经拿到 Markdown（协商命中）就没有再探的必要——两者的产物同类，
    再探只是白多一次请求。
    """
    if not result.get("success"):
        return True
    return result.get("fetch_method") not in _MARKDOWN_METHODS


def _md_variant_fetch(url: str, max_chars: int = 8000,
                      timeout: float = 8.0) -> dict | None:
    """探测 AI 友好变体（{url}.md 直出 / 站点根 llms.txt）。命中返回结果，未命中 None。

    背景：头部开发者文档站自发提供 .md 直出与 llms.txt，对 Agent 返回干净
    正文（诚实身份即可获取）。探测只多一两个 GET，命中即跳过整个反爬降级链；
    未命中静默放弃，走原有链路。返回的 result.url 保持为原始请求 URL
    （llms.txt 描述的是站点，不是探测端点自身）。
    """
    candidates = _ai_variant_candidates(url)
    if not candidates:
        return None
    for probe_url, kind in candidates:
        try:
            from http_client import HttpClient
            client = HttpClient(timeout=min(timeout, 5.0), max_retries=0,
                                jitter=False)
            resp = client.get(probe_url, extra_headers={
                "User-Agent": "argo-fetch-v3/1.0 (+local-research; md-variant)",
                "Accept": "text/markdown, text/plain;q=0.9, */*;q=0.8",
            })
        except Exception:
            continue
        if resp.get("status", 0) != 200 or not resp.get("text"):
            continue
        headers = resp.get("headers") or {}
        ctype = ""
        for k, v in headers.items():
            if str(k).lower() == "content-type":
                ctype = str(v).lower()
                break
        if "html" in ctype:
            continue
        text = resp["text"]
        if not _looks_like_markdown(text):
            continue
        return _markdown_result(url, text, max_chars, kind)
    return None


# ─── 第一级A2：移动端 UA 分支（客户端形态分流）──────────────────────────────

_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
              "Mobile/15E148 Safari/604.1")


def _mobile_branch_enabled() -> bool:
    """移动端 UA 分支开关：ARGO_FETCH_MOBILE=0 关闭，默认开启。"""
    return env_flag("ARGO_FETCH_MOBILE")


# 客户端形态分流已知站点：这些站对真机 UA 直接返回 SSR 数据，而桌面 UA 首发
# 会触发风控、连坐紧随其后的移动端请求（2026-08 实测冷却 ≥8s），因此必须
# 移动优先而非失败后重试。可用 ARGO_MOBILE_FIRST_HOSTS 追加（逗号分隔）。
_MOBILE_FIRST_HOSTS = ("douyin.com", "iesdouyin.com")


def _mobile_first_host(url: str) -> bool:
    """URL 是否命中「移动优先」主机清单。"""
    extra = [h.strip().lower() for h in os.environ.get(
        "ARGO_MOBILE_FIRST_HOSTS", "").split(",") if h.strip()]
    host = (urlparse(url).hostname or "").lower()
    for marker in list(_MOBILE_FIRST_HOSTS) + extra:
        if host == marker or host.endswith("." + marker):
            return True
    return False


# ─── 身份记忆（per-host 成功档位，跨进程小文件）─────────────────────────────
#
# 首次访问未知分流站：桌面失败 → 移动成功（2 次触碰）；记忆生效后（TTL 24h）
# 直接移动首发（1 次触碰）。实测依据：抖音类站点按 IP 递进限速，请求节奏比
# 身份选择重要一个数量级，省掉的每次试错都是真实的封禁风险。
_IDENTITY_TTL = 86400
_IDENTITY_PATH = os.environ.get(
    "ARGO_IDENTITY_MEMORY",
    str(_paths.state_path("fetch-identity.json")))
_identity_mem: dict[str, float] = {}
_identity_loaded = False
_identity_dirty = False
_identity_last_flush = 0.0
_IDENTITY_FLUSH_INTERVAL = 30.0  # 最多每 30s 写一次文件


def _identity_load() -> None:
    global _identity_loaded, _identity_mem
    if _identity_loaded:
        return
    _identity_loaded = True
    try:
        with open(_IDENTITY_PATH, encoding="utf-8") as f:  # 写侧 atomic_write_text 恒 UTF-8，读侧同编码
            raw = json.load(f)
        now = time.time()
        mem: dict[str, float] = {}
        for h, t in raw.items():
            try:
                exp = float(t)
                if exp > now:
                    mem[str(h)] = exp
            except (TypeError, ValueError):
                continue  # 单条脏数据不拖垮整表（旧版本/手改字段）
        _identity_mem = mem
    except Exception:
        _identity_mem = {}


def _identity_remember_mobile(host: str) -> None:
    """记录「该 host 移动端身份成功过」，最多每 30s 写一次文件。失败静默（纯增益层）。"""
    global _identity_dirty, _identity_last_flush
    if not host:
        return
    _identity_load()
    if len(_identity_mem) >= 512:  # 有界：内存表防长驻进程无限增长（磁盘侧有 TTL）
        _identity_mem.pop(next(iter(_identity_mem)), None)
    _identity_mem[host] = time.time() + _IDENTITY_TTL
    _identity_dirty = True
    now = time.monotonic()
    if _identity_last_flush > 0.0 and now - _identity_last_flush < _IDENTITY_FLUSH_INTERVAL:
        return
    _identity_last_flush = now
    try:
        # 原子写走 argo_paths 唯一来源（唯一 tmp 名）；旧实现固定 `.tmp` 名，
        # 并发抓取进程会互相搬走临时文件，身份记忆静默丢失。
        _paths.atomic_write_json(Path(_IDENTITY_PATH), _identity_mem, indent=None)
        _identity_dirty = False
    except Exception:
        pass


def _identity_is_mobile(host: str) -> bool:
    """该 host 近 24h 内移动身份是否成功过。"""
    if not host:
        return False
    _identity_load()
    return _identity_mem.get(host, 0) > time.time()


def _mobile_http_fetch(url: str, max_chars: int = 8000,
                       timeout: float = 8.0) -> dict:
    """移动端 UA 抓取：客户端形态分流型站点对真机 UA 返回 SSR 数据。

    实测（2026-08）：抖音 iesdouyin 分享页对 iPhone UA 返回含 _ROUTER_DATA
    的服务端渲染数据页；桌面/AI bot UA 一律收到 acrawler 风控 JS 壳。
    该类分流与 TLS 指纹无关，stdlib 即可通过，放在 TLS 伪造层之前。
    """
    try:
        from url_safety import check_url
        ok, reason = check_url(url)
        if not ok:
            return _make_result(url, "", 0, "http_mobile", ok=False,
                                error=f"URL 被 SSRF 防护拦截: {reason}")
    except ImportError:
        pass
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=min(timeout, 5.0), max_retries=1,
                            jitter=False)
        resp = client.get(url, extra_headers={"User-Agent": _MOBILE_UA})
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": _MOBILE_UA})
        try:
            with open_url(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
                return _make_result(url, text, max_chars, "http_mobile")
        except Exception as e:
            return _make_result(url, "", 0, "http_mobile", ok=False,
                                error=str(e)[:100])
    if resp.get("status", 0) >= 400:
        result = _make_result(url, "", 0, "http_mobile", ok=False,
                              error=f"HTTP {resp.get('status')}")
        _mark_stop_signal(result, resp)
        return result
    if not resp.get("text"):
        return _make_result(url, "", 0, "http_mobile", ok=False,
                            error="empty response")
    result = _make_result(url, resp["text"], max_chars, "http_mobile")
    _mark_stop_signal(result, resp)
    return result


def _mark_stop_signal(result: dict, resp: dict) -> dict:
    """把 429/503 明确停止信号记录到结果，供主链检查使用。

    429（速率限制）与 503（服务过载）是服务器明确的「请停止」信号，
    与请求方式（UA/TLS 指纹/浏览器）无关。收到后不应升级重链，
    否则等于无视服务器指示、持续放大目标站点负载。
    """
    status = resp.get("status", 0)
    result["status"] = status
    if status in (429, 503):
        result["stop_signal"] = True
    return result


# ─── 第一级：增强 HTTP ───────────────────────────────────────────────────────

def _http_fetch(url: str, max_chars: int = 8000, timeout: float = 8.0,
                allow_markdown: bool = True) -> dict:
    """使用 http_client（UA 轮换 + Cookie 积累）抓取。

    allow_markdown：是否在这次请求上带内容协商 Accept。协商复用本次 GET，
    **不额外发请求**——命中则直接拿到站点自己的 Markdown（结构完整、跳过
    整个反爬降级链），未命中则该响应就是原本要抓的 HTML，行为与从前一致。
    需要 raw HTML 的调用方（crawl/extract 取链接）置 False。

    Markdown 命中只认「校验通过」的结果（见 _negotiated_markdown 四道门），
    声称 text/markdown 却是占位页/错误页/404 的响应一律按 HTML 处理。
    """
    try:
        from url_safety import check_url
        ok, reason = check_url(url)
        if not ok:
            return _make_result(url, "", 0, "http", ok=False,
                                error=f"URL 被 SSRF 防护拦截: {reason}")
    except ImportError:
        pass
    negotiate = allow_markdown and _negotiate_enabled()
    extra = {"Accept": _ACCEPT_NEGOTIATE} if negotiate else None
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=timeout, max_retries=1, jitter=False)
        resp = client.get(url, extra_headers=extra)
    except ImportError:
        # fallback 到 urllib
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "argo-fetch-v3/1.0 (+local-research)",
        })
        try:
            with open_url(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
                return _make_result(url, text, max_chars, "http")
        except Exception as e:
            return _make_result(url, "", 0, "http", ok=False, error=str(e)[:100])

    if resp.get("status", 0) >= 400:
        result = _make_result(url, "", 0, "http", ok=False,
                              error=f"HTTP {resp.get('status')}")
        _mark_stop_signal(result, resp)
        return result
    if not resp.get("text"):
        return _make_result(url, "", 0, "http", ok=False, error="empty response")

    if negotiate:
        text = _negotiated_markdown(resp)
        if text is not None:
            return _markdown_result(url, text, max_chars, "http_md",
                                    final_url=resp.get("url", ""))

    return _make_result(url, resp["text"], max_chars, "http")


def _tls_spoof_fetch(url: str, max_chars: int = 8000, timeout: float = 8.0) -> dict:
    """TLS 指纹伪造层：curl_cffi impersonate 多指纹轮换抓取。

    针对指纹检测型反爬（Cloudflare 等直接按 TLS ClientHello 判 bot），
    urllib/curl 原生指纹与真实浏览器不同会被 403。本层用 curl_cffi
    逐字节模拟 Chrome/Safari/Firefox 指纹，免起浏览器即可通过。

    指纹轮换顺序：chrome → safari → firefox（safari 对部分站点更友好）。
    """
    try:
        from url_safety import check_url
        ok, reason = check_url(url)
        if not ok:
            return _make_result(url, "", 0, "tls_spoof", ok=False,
                                error=f"URL 被 SSRF 防护拦截: {reason}")
    except ImportError:
        pass

    try:
        from http_client import HttpClient
        client = HttpClient(timeout=timeout, max_retries=1, jitter=False)
        resp = client.get_impersonated(url, timeout=timeout)
    except ImportError:
        return _make_result(url, "", 0, "tls_spoof", ok=False,
                            error="http_client not available")

    if resp.get("status", 0) >= 400:
        result = _make_result(url, "", 0, "tls_spoof", ok=False,
                              error=f"HTTP {resp.get('status')}")
        _mark_stop_signal(result, resp)
        return result
    if not resp.get("text"):
        return _make_result(url, "", 0, "tls_spoof", ok=False,
                            error=resp.get("error", "empty response"))

    result = _make_result(url, resp["text"], max_chars, "tls_spoof")
    result["impersonate"] = resp.get("impersonate", "")
    return result


def _wayback_fetch(url: str, max_chars: int = 8000, timeout: float = 12.0) -> dict:
    """Wayback Machine 快照回退：CDX API 查最新快照 → 抓取。

    用于 HTTP 失败 / 空内容 / 疑似被删页面的保底，返回统一输出格式。
    """
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=timeout, max_retries=1, jitter=False)
        # CDX 查询最新快照
        cdx_url = (
            "https://web.archive.org/cdx/search/cdx"
            f"?url={urllib.parse.quote(url, safe='')}"
            "&output=json&limit=1&sort=reverse"
        )
        resp = client.get(cdx_url)
        if not resp.get("text"):
            return _make_result(url, "", 0, "wayback", ok=False,
                                error="wayback cdx empty")
        data = json.loads(resp["text"])
        if not data or len(data) < 2:
            return _make_result(url, "", 0, "wayback", ok=False,
                                error="wayback no snapshot")
        timestamp = data[1][1]
        snapshot_url = f"https://web.archive.org/web/{timestamp}/{url}"
        snap = client.get(snapshot_url)
        if not snap.get("text"):
            return _make_result(url, "", 0, "wayback", ok=False,
                                error="wayback snapshot empty")
        result = _make_result(url, snap["text"], max_chars, "wayback")
        result["snapshot_url"] = snapshot_url
        result["snapshot_ts"] = timestamp
        return result
    except Exception as e:
        return _make_result(url, "", 0, "wayback", ok=False,
                            error=f"wayback error: {str(e)[:100]}")


def _archive_truncated(url: str, text: str, delivered: str, kind: str,
                       extra: dict, primary: bool = False) -> None:
    """内容被裁时把完整版存进全文存档，并就地补上可回读的字段。

    只裁不存会让「截断」变成不可追溯的数据销毁：默认档 8,000 字抓一个
    十一万字的页面，被裁掉的 93% 既无副本也无线索，事后无法复核结论是否
    落在丢掉的那一段里。存档失败不影响本次交付（见 fulltext_store.save）。

    primary 标识「这条就是交付视图」。`truncated` / `full_length` 只由它写：
    同一次抓取里正文与 HTML 都会各自被裁，若都往同名键上写，后写的 HTML
    会覆盖正文的数字——实测把 32,789 字的正文报成 533,612 字节的 HTML。
    非主视图的截断另起 `_truncated` 后缀的键，不抢占语义。
    """
    cut = bool(text) and len(text) > len(delivered)
    if primary:
        extra["full_length"] = len(text)
        extra["truncated"] = cut
    else:
        extra[f"{kind}_truncated"] = cut
    if not cut:
        return
    try:
        from fulltext_store import save as _save_fulltext
        path = _save_fulltext(url, text, kind)
        if path:
            extra[f"full_{kind}_path"] = path
            if primary:
                extra["full_text_path"] = path
    except Exception:
        pass


def _make_result(url: str, html: str, max_chars: int,
                 method: str, ok: bool = True, error: str | None = None,
                 title: str = "") -> dict:
    """构建统一输出格式。

    取全文 → 裁交付视图 → 被裁部分归档。`content` 仍受 max_chars 约束
    （token 预算不变），但完整正文不再当场销毁。
    """
    full_content, extracted_title = (
        extract_content(html, 0) if ok and html else ("", ""))
    if not title:
        title = extracted_title
    content = _cut(full_content, max_chars)
    extra: dict = {}
    _archive_truncated(url, full_content, content, "text", extra, primary=True)
    full_html = html or ""
    delivered_html = _cut(full_html, max_chars * 2)
    # HTML 同样会被预算裁掉，而结构性提取（表格 / Meta / JSON-LD）全靠它——
    # 裁掉就等于那些表格从此再也取不到。与正文分开存，互不覆盖。
    if full_html:
        _archive_truncated(url, full_html, delivered_html, "html", extra)
    result = {
        "url": url,
        "content": content,
        "html": delivered_html,
        "title": title,
        "length": len(content),
        "success": ok,
        "error": error,
        "fetch_method": method,
    }
    result.update(extra)
    return result


# ─── 第一级C：r.jina.ai 阅读器（keyless 免费层，远端 JS 渲染转 markdown）─────

_JINA_PRIVATE_HOST = re.compile(
    r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.)")


def _jina_enabled() -> bool:
    """r.jina.ai 阅读器级开关：ARGO_FETCH_JINA=0 关闭，默认开启。"""
    return env_flag("ARGO_FETCH_JINA")


def _is_public_host(host: str) -> bool:
    """r.jina.ai 是第三方代理：只把公网 URL 交给它。

    内网/本机/裸 IP 一律跳过——把内部地址送出第三方 = 拓扑泄露；
    域名形 IP（全数字）与含冒号的 IPv6 字面量同判为裸地址。
    """
    if not host:
        return False
    if host == "localhost" or host.endswith((".local", ".internal", ".lan")):
        return False
    if _JINA_PRIVATE_HOST.match(host):
        return False
    if host.replace(".", "").isdigit() or ":" in host:
        return False
    return "." in host


def _jina_reader_fetch(url: str, max_chars: int = 8000,
                       timeout: float = 8.0) -> dict | None:
    """r.jina.ai 阅读器（keyless 免费层）。

    定位：HTTP/TLS 直连全败后的免浏览器快速路径——jina 在远端渲染并返回
    markdown，命中则免去 CDP 冷启动。免费层有速率限制，失败/429 返回 None
    静默放行走 Wayback/浏览器。调用方须先过 _is_public_host（第三方代理边界）。
    """
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=min(timeout, 10.0), max_retries=0,
                            jitter=False)
        resp = client.get(f"https://r.jina.ai/{url}", extra_headers={
            "User-Agent": "argo-fetch-v3/1.0 (+local-research; jina-reader)",
            "Accept": "text/plain",
        })
    except Exception:
        return None
    if resp.get("status", 0) != 200 or not resp.get("text"):
        return None
    raw = resp["text"]
    title = ""
    m = re.search(r"^Title:\s*(.+)$", raw[:2000], re.MULTILINE)
    if m:
        title = m.group(1).strip()[:200]
    if "Markdown Content:" in raw:
        raw = raw.split("Markdown Content:", 1)[1].lstrip()
    if not _looks_like_markdown(raw):
        return None
    result = _make_result(url, "", max_chars, "jina_reader")
    result["content"] = raw[:max_chars]
    result["length"] = len(result["content"])
    result["title"] = title
    return result


# ─── 第一级D：Parallel 免费 MCP web_fetch（keyless 云端渲染+提取）────────────

_PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"


def _parallel_mcp_enabled() -> bool:
    return env_flag("ARGO_FETCH_PARALLEL")


def _parallel_session_id() -> str:
    """免费层限流按 session_id 关联：一次生成、持久复用（官方建议）。

    状态目录不可写时退化为随机 id——仍唯一但不稳定，只损失限流关联的
    连续性，不影响功能。
    """
    try:
        from argo_paths import state_path
        p = state_path("parallel_session_id.txt")
        if p.exists() and p.read_text(encoding="utf-8").strip():
            return p.read_text(encoding="utf-8").strip()
        sid = uuid.uuid4().hex
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sid, encoding="utf-8")
        return sid
    except Exception:
        return uuid.uuid4().hex


def _parallel_mcp_fetch(url: str, max_chars: int = 8000,
                        timeout: float = 25.0) -> dict:
    """Parallel 免费 MCP web_fetch（keyless，full_content markdown）。

    定位：jina 同级的免浏览器快速路径——云端 JS 渲染 + 正文提取，
    full_content=true 拿整页 markdown（官方警告长文可达数万 token，
    故截到 max_chars）。无账号无 key（2026-09-14 实测无状态直调成立），
    免费层限流按 session_id 关联。失败静默返回 success=False 放行后级。
    调用方须先过 _is_public_host（第三方代理边界）。
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "web_fetch", "arguments": {
                "urls": [url],
                "objective": "Extract the main page content",
                "full_content": True,
                "session_id": _parallel_session_id(),
            }}}
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=min(timeout, 25.0), max_retries=0,
                            jitter=False)
        resp = client.post(_PARALLEL_MCP_URL, body=body, extra_headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
    except Exception as e:
        return {"url": url, "title": "", "content": "", "html": "", "length": 0,
                "success": False, "error": f"parallel_mcp: {e}",
                "fetch_method": "parallel_mcp"}
    if resp.get("status", 0) != 200 or not resp.get("text"):
        return {"url": url, "title": "", "content": "", "html": "", "length": 0,
                "success": False, "error": f"parallel_mcp: HTTP {resp.get('status')}",
                "fetch_method": "parallel_mcp"}
    try:
        data = json.loads(resp["text"])
    except (ValueError, TypeError):
        return {"url": url, "title": "", "content": "", "html": "", "length": 0,
                "success": False, "error": "parallel_mcp: 响应非 JSON",
                "fetch_method": "parallel_mcp"}
    # MCP 错误显式记录（fetch 链语义：失败放行后级，但 error 留痕可归因）
    r = data.get("result") or {}
    mcp_err = None
    if r.get("isError"):
        blk = (r.get("content") or [{}])
        mcp_err = (blk[0].get("text", "")[:200] if isinstance(blk[0], dict)
                   else str(blk[0])[:200])
    elif isinstance(data.get("error"), dict):
        mcp_err = str(data["error"].get("message") or data["error"])[:200]
    inner: dict = {}
    for block in (r.get("content") or []):
        text = block.get("text", "") if isinstance(block, dict) else str(block)
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and parsed.get("results"):
            inner = parsed
            break
    items = inner.get("results") or []
    if not items:
        return {"url": url, "title": "", "content": "", "html": "", "length": 0,
                "success": False,
                "error": f"parallel_mcp: {mcp_err or '上游无结果'}",
                "fetch_method": "parallel_mcp"}
    item = items[0]
    # full_content=true → 整页 markdown；缺省（上游变更/未开启）退回 excerpts 拼接
    raw = str(item.get("full_content") or "") or "\n\n".join(
        str(e) for e in (item.get("excerpts") or []))
    result = _make_result(url, "", max_chars, "parallel_mcp")
    result["content"] = raw[:max_chars]
    result["length"] = len(result["content"])
    result["title"] = str(item.get("title") or "")[:200]
    if mcp_err:
        result["parallel_mcp_note"] = mcp_err
    return result


# ─── 第二级A：tinyfish 直连渲染（Markdown 直出，含 JS 执行）─────────────
# 实现见 fetch_render_tinyfish（独立模块）：markdown-only 渲染，
# 不产 raw html，故 need_html 场景由主链跳过本层。

def _tinyfish_fetch(url: str, max_chars: int = 8000, timeout: float = 8.0) -> dict:
    """委托给 fetch_render_tinyfish.fetch（渲染层已拆为独立模块）。"""
    return _render_tinyfish.fetch(url, max_chars=max_chars, timeout=timeout)


# ─── 第二级：Chrome CDP 浏览器 ───────────────────────────────────────────────

def _browser_fetch(url: str, max_chars: int = 8000, timeout: float = 15.0,
                   actions: list[dict] | None = None) -> dict:
    """使用 Chrome CDP 驱动抓取（支持页面交互）。"""
    try:
        from chrome_cdp import ChromeCDP
    except ImportError:
        return _make_result(url, "", 0, "browser", ok=False,
                            error="chrome_cdp not available")

    try:
        cdp = ChromeCDP(auto_start=True)
    except Exception as e:
        return _make_result(url, "", 0, "browser", ok=False,
                            error=f"Chrome failed to start: {str(e)[:100]}")

    try:
        # 导航
        cdp.navigate(url, wait_until="networkidle")

        # 执行页面交互序列（Hound actions 等价能力）
        if actions:
            cdp.execute_actions(actions)

        # 提取内容
        html = cdp.get_html()
        text = cdp.get_text()
        title = cdp.get_title()

        return {
            "url": url,
            "content": text[:max_chars] if text else "",
            "html": html[:max_chars * 2] if html else "",
            "title": title or "",
            "length": len(text) if text else 0,
            "success": bool(text),
            "error": None if text else "empty content",
            "fetch_method": "chrome_cdp",
        }
    except Exception as e:
        return _make_result(url, "", 0, "browser", ok=False,
                            error=f"CDP error: {str(e)[:100]}")
    finally:
        try:
            cdp.stop()
        except Exception:
            pass


# ─── 第三级：质量评估 ─────────────────────────────────────────────────────────
# 实现见 fetch_quality（独立模块）：来源分类 / 页面类型 / 质量分 / 内容安全。

def _assess_quality(result: dict) -> dict:
    """计算内容质量信号（委托 fetch_quality.assess）。"""
    return _quality.assess(result)


# 兼容别名：存量测试与调用方直接引用这些私有名，委托到 fetch_quality 保留。
_detect_page_type = _quality._detect_page_type
_compute_quality = _quality._compute_quality
_classify_domain = _quality._classify_domain


# ─── 主入口 ──────────────────────────────────────────────────────────────────

# RFC 3986 语法型规范化：unreserved 字符集与默认端口
_UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                  "0123456789-._~")
_DEFAULT_PORTS = {"http": "80", "https": "443", "ftp": "21", "ws": "80", "wss": "443"}
_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 §6.2.2.3：移除 . 与 .. 路径段（纯语法，不触碰语义）。"""
    if not path:
        return path
    out: list[str] = []
    for seg in path.split("/"):
        if seg == ".":
            continue
        if seg == "..":
            if out and out[-1] not in ("", ".."):
                out.pop()
            continue
        out.append(seg)
    joined = "/".join(out)
    if path.endswith("/") and not joined.endswith("/"):
        joined += "/"
    if path.startswith("/") and not joined.startswith("/"):
        joined = "/" + joined
    return joined


def normalize_url(url: str) -> str:
    """RFC 3986 §6.2.2 / §6.2.3 语法型与协议型规范化。

    只做**等价改写**（同一个资源的不同写法归一），不做语义改写，因此可以
    安全地用在缓存键与去重上：

      · scheme / host 小写（§6.2.2.1）—— 大小写不同不会访问到不同资源，
        但会让同一页被判成两条，缓存与去重都失效
      · 解码 unreserved 百分号编码（§6.2.2.2）：%7E→~、%41→A
      · 移除点段（§6.2.2.3）：/a/./b/../c → 该去哪去哪
      · 去掉默认端口（§6.2.3）：http:80、https:443
      · 空路径补 /（§6.2.3）：https://x.com 与 https://x.com/ 是同一个资源

    不动的部分：query 与 fragment 原样保留（只有既有的追踪参数清理会碰它们），
    percent 编码中的保留字符不解码——那会改变语义。
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return url
    if not parsed.scheme or not parsed.netloc:
        return url

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc

    # host 小写、userinfo 保留原样、端口按需剥离
    userinfo, _, hostport = netloc.rpartition("@")
    host, sep, port = hostport.partition(":")
    host = host.lower()
    if port and port == _DEFAULT_PORTS.get(scheme):
        port = ""
    netloc = (userinfo + "@" if userinfo else "") + host + (sep + port if port else "")

    # unreserved 解码 + 保留字符的百分号编码统一大写十六进制
    def _fix_pct(m: re.Match) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()

    path = _PCT_RE.sub(_fix_pct, parsed.path)
    path = _remove_dot_segments(path) or "/"
    if not path.startswith("/"):
        path = "/" + path

    return urllib.parse.urlunsplit(
        (scheme, netloc, path, parsed.query, parsed.fragment))


def _optimize_url(url: str) -> str:
    """URL 优化：Reddit 重写、追踪参数清理等。

    - reddit.com → old.reddit.com（7× 更小、无 JS 渲染要求）
    - 清理常见追踪参数（utm_source, fbclid, gclid 等）
    """
    url = normalize_url(url)
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    # Reddit 优化
    if host in ("reddit.com", "www.reddit.com", "new.reddit.com"):
        # 重写为 old.reddit（纯 HTML，无需 JS，体积更小）
        url = url.replace("://www.reddit.com", "://old.reddit.com")
        url = url.replace("://reddit.com", "://old.reddit.com")
        url = url.replace("://new.reddit.com", "://old.reddit.com")

    # 清理追踪参数。**必须在主机重写之后重新解析**：此前用的是重写之前
    # 捕获的 parsed，一旦两者同时命中就会把重写悄悄撤销——实测
    # `https://www.reddit.com/r/x?utm_source=z` 最终仍是 www.reddit.com，
    # 而分享链接几乎都带 utm，等于这条优化在真实场景里大面积失效。
    tracking_params = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                       "utm_content", "fbclid", "gclid", "ref", "ref_src"}
    parsed = urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    filtered = {k: v for k, v in qs.items() if k.lower() not in tracking_params}
    if len(filtered) < len(qs):
        new_qs = urllib.parse.urlencode(filtered, doseq=True)
        url = urllib.parse.urlunparse(parsed._replace(query=new_qs))

    return url


def _cache_content_too_short(hit: dict, max_chars: int) -> bool:
    """缓存正文是否因写入时 max_chars 更小而不够本次使用。

    缓存存的是「写入时按 max_chars 裁剪后」的正文（不是完整副本），所以
    本次请求更大时必须视为 miss 回源，否则调用方会静默拿到被截短的正文
    （实测：--max-chars 60000 命中 8000 字缓存，正文反而比首次更短）。
    与 search 侧 _max_results 柔性命中同构：不够用就 miss 去升级，够用才截断。

    老条目（修复前写入，无 _max_chars）长度不可信，退回按 content 实际
    长度判断；首轮回源后会写入带 _max_chars 的条目，之后即精确判定。
    """
    if max_chars is None:
        return False
    if max_chars <= 0:
        # 0/负数表示「要全文」，而缓存从不存全文（只存裁剪后的副本）→ 必然不够。
        # 早先这里写成 `if not max_chars: return False`，于是要全文的请求
        # 会安安静静拿回一份 8,000 字的缓存副本，长度还显示成 8,000。
        return True
    content = hit.get("content") or ""
    cached_max = hit.get("_max_chars")
    if cached_max is None:
        return max_chars > len(content)
    try:
        return max_chars > int(cached_max)
    except (TypeError, ValueError):
        return max_chars > len(content)


def _strip_html_unless_needed(result: dict, need_html: bool) -> dict:
    """模型/CLI 默认只要正文。html 只在调用方声明 need_html（extract/crawl）时保留。

    写缓存已经丢掉 html；若不在公共返回里剥掉，缓存未命中的第一次会把最多
    max_chars*2 的生 HTML 泄漏给 MCP/CLI。与「内部字段不进模型可见输出」同构。
    """
    if not need_html and isinstance(result, dict):
        result.pop("html", None)
    return result


def fetch_v3(url: str, max_chars: int = 8000, timeout: float = 8.0,
             use_browser_fallback: bool = True,
             actions: list[dict] | None = None,
             force_browser: bool = False,
             skip_cache: bool = False,
             need_html: bool = False) -> dict:
    """多级抓取降级链主函数（逐级升级，受全局 deadline 约束）。

    执行顺序：
      第零级：{url}.md 变体 + 站点根 /llms.txt 探测（AI 友好直出，命中即跳过整条反爬链）
      第一级：增强 HTTP（UA 轮换 + Cookie 积累 + Accept: text/markdown 内容协商，
              协商命中即得站点自己的 Markdown，fetch_method=http_md）；
              客户端形态分流型站点移动 UA 首发
      第一级B：TLS 指纹伪造（curl_cffi impersonate，指纹检测型反爬）
      第一级C：r.jina.ai 阅读器（keyless 免费层，仅公网 URL，markdown-only）
      第一级D：Parallel 免费 MCP web_fetch（keyless 免费层，仅公网 URL，markdown-only）
      第二级A：tinyfish 直连渲染（markdown-only，需 TINYFISH_API_KEY；need_html 或开关关闭时跳过）
      第二级B：Wayback 快照 + Chrome CDP 浏览器（自动降级或 actions 触发）
      第三级：质量评估（content_ok/page_type/quality_score）

    全局 deadline：单次 fetch_v3 总耗时上限 = ARGO_FETCH_DEADLINE_S（默认 60，
    可设 0 关闭）。降级是「延迟换成功率」的交易，延迟必须有一等公民约束——
    逐级独立超时的加法无上限（8+8+8+12+8+15≈59s+），会击穿 MCP 客户端
    工具超时。每级升级前检查剩余预算，耗尽即停链返回当前最优结果
    （失败结果 + deadline_exhausted 标记），不再无限叠加。

    URL 级缓存：无 actions 的成功结果写入 SearchCache（L1+L2），正文按本次
    max_chars 裁剪并记录 _max_chars；后续请求更大 max_chars 时判为 miss 回源
    重抓，避免静默返回被截短的正文。
    need_html：调用方需要原始 HTML（如爬取提取链接）时置 True，会跳过 tinyfish/
    jina/Parallel（仅产 markdown）、停用内容协商，并且**自动绕过 URL 缓存**——
    缓存有意不存 html 大字段（省空间），命中只会拿到空 html，让结构化提取
    静默全空。这条约束放在这里，是因为要求每个调用方自己记得配 skip_cache，
    迟早有人漏；漏了的症状是「开关看着接上了，结果永远为空」。
    """
    try:
        from url_safety import check_url
        ok, reason = check_url(url)
        if not ok:
            return _strip_html_unless_needed({
                "url": url, "title": "", "content": "",
                "html": "", "length": 0, "success": False,
                "error": f"URL 被 SSRF 防护拦截: {reason}",
                "fetch_method": "blocked",
            }, need_html)
    except ImportError:
        pass

    # URL 优化（Reddit 重写、追踪参数清理）
    url = _optimize_url(url)

    # robots.txt 尊重（合规检查）：被禁路径直接拒绝，抓取失败放行
    try:
        from robots_guard import robots_blocked
        if robots_blocked(url, timeout=min(timeout, 5.0)):
            result = _make_result(url, "", 0, "robots_blocked", ok=False,
                                  error="robots.txt 禁止抓取")
            result = _assess_quality(result)
            result["cached"] = False
            return _strip_html_unless_needed(result, need_html)
    except ImportError:
        pass

    # 有 actions → 强制浏览器模式，且不读缓存
    if actions:
        force_browser = True
        skip_cache = True

    # 需原始 HTML → 不读缓存（缓存不存 html，命中必得空 html）。
    # 与 actions 同理：这是取数的前提条件，不是调用方的可选项。
    if need_html:
        skip_cache = True

    # 读 URL 缓存
    if max_chars is not None and max_chars <= 0:
        # 要全文的请求：缓存只存裁剪副本，读写都不该经过它
        skip_cache = True

    if not skip_cache and not force_browser:
        try:
            from cache import SearchCache
            hit = SearchCache().get_fetch(url)
            # 缓存正文是按写入时 max_chars 裁剪的，本次要得更多 → 判 miss 走抓取链
            if hit and hit.get("success") and not _cache_content_too_short(hit, max_chars):
                # 缓存内容可能比本次 max_chars 更长 → 截断
                out = {k: v for k, v in hit.items() if not str(k).startswith("_")}
                content = out.get("content") or ""
                if max_chars and len(content) > max_chars:
                    out["content"] = content[:max_chars]
                    out["length"] = len(out["content"])
                # 评分口径变了 → 条目里的旧分不再成立，就地重算。
                # 正文仍在条目里（它才是贵的那部分），为一个公式改动去重新
                # 联网不值得。html 未被缓存，结构修正项拿不到，故标注口径来源，
                # 让调用方知道这个分与新鲜抓取的分不是同一种计算。
                try:
                    _v = (_quality.QUALITY_FORMULA_VERSION
                          if hasattr(_quality, "QUALITY_FORMULA_VERSION") else 0)
                    if (out.get("quality_breakdown") or {}).get("version") != _v:
                        out = _quality.assess(out)
                        out["quality_basis"] = "rescored"
                except Exception:
                    pass
                out["cached"] = True
                out["cache_level"] = hit.get("_cache_level", "L?")
                out["url"] = url
                return _strip_html_unless_needed(out, need_html)
        except Exception:
            pass

    if force_browser:
        result = _browser_fetch(url, max_chars, timeout=15.0, actions=actions)
    else:
        # 全局 deadline：所有降级升级动作共用的总预算（秒）。
        # ARGO_FETCH_DEADLINE_S=0 关闭；默认 60s（MCP 客户端工具超时的安全下限）。
        try:
            deadline_s = float(os.environ.get("ARGO_FETCH_DEADLINE_S", "60") or 60)
        except ValueError:
            deadline_s = 60.0
        t_chain0 = time.monotonic()
        deadline_hit = {"flag": False}

        def _budget_left() -> float:
            """剩余降级预算；耗尽时置标记并返回 -1（调用方停止升级）。"""
            if deadline_s <= 0:
                return 1.0
            left = deadline_s - (time.monotonic() - t_chain0)
            if left <= 0:
                deadline_hit["flag"] = True
                return -1.0
            return left

        def _level_timeout(requested: float) -> float:
            """本降级级的 timeout：不超过剩余总预算。

            **每一级都必须过这道口**。历史 bug（2026-09-19 复现）：只有
            后加的 jina / Parallel 两级写了 `min(..., max(_budget_left(), 1.0))`，
            而 mobile UA / TLS 指纹 / wayback / tinyfish / browser 五级都拿
            原始的 timeout（8s / 8s / 12s / 8s / 15s）。预算只剩 0.1s 时，
            这五级仍会各自跑满自己的超时，实测**可击穿总预算 34.9s**——
            而 ARGO_FETCH_DEADLINE_S 存在的全部理由就是兜住 MCP 客户端
            的工具超时（默认 60s），击穿等于让这条兜底失效。

            `deadline_s <= 0` 表示 deadline 被显式关闭（ARGO_FETCH_DEADLINE_S=0），
            此时 `_budget_left()` 返回的是「放行」哨兵 1.0 而不是真实剩余量，
            必须原样放行——照抄 min() 会把关闭状态下的每一级都压成 1s。

            下限 1.0s：预算所剩无几时，0 秒超时的请求本身没有意义（连不上
            也读不到），留 1s 让它要么快速成功、要么快速失败。代价是总墙钟
            的上界不是 deadline 本身，而是 **deadline + 5s**（预算耗尽后仍有
            至多 5 个降级级各留 1s 下限）。这个上界是刻意的：比「击穿 34.9s」
            小一个量级，又不必给每一级单独定义「多小的预算算不值得试」。
            """
            if deadline_s <= 0:
                return requested
            return min(requested, max(_budget_left(), 1.0))

        host = (urlparse(url).hostname or "").lower()
        gated = (_mobile_branch_enabled()
                 and (_mobile_first_host(url) or _identity_is_mobile(host)))
        result = None
        # AI 友好变体探测（{url}.md 直出 / 站点根 llms.txt）**排在主请求之后**。
        #
        # 早先它排在最前面，理由是「命中即省下整条反爬链」；接上内容协商之后
        # 这个理由不再成立：协商折在主请求里、不额外花一次往返，而探测无论
        # 命中与否都要先付一次串行请求。实测未命中的站点因此白等 571–1,246 ms
        # （MDN 1,246 / mintlify 719 / astro 571），而它们占多数。
        # 现在改为：主请求先走，只在它没能拿到 Markdown 时才回探变体——
        # 已知的 .md 专有站点（bun / nextjs / ai-sdk / nodejs 这类协商不覆盖的）
        # 仍能拿到，其余站点少一次往返。
        if result is None:
            # 第一级：客户端形态分流型站点（如抖音）直接以移动端 UA 首发——
            # 桌面 UA 首发会触发风控并连坐后续移动请求，顺序不可颠倒。
            if gated:
                result = _mobile_http_fetch(url, max_chars, timeout)
                if result.get("success") and not _needs_browser(result):
                    result["ua_profile"] = "mobile"
                    _identity_remember_mobile(host)
            else:
                # 第一级：HTTP（带内容协商；crawl/extract 要 raw HTML 时关闭）
                result = _http_fetch(url, max_chars, timeout,
                                     allow_markdown=not need_html)

            # 主请求未得到 Markdown → 回探 AI 友好变体。
            # 门控站（抖音一类）跳过：少一次主机触碰，保住单次直连窗口
            # （实测 .md 探测会触发连坐限速）。
            #
            # need_html 一并关掉这条：它和 tinyfish/jina/Parallel 是同一类
            # 「只产 markdown」的通道，而上面 docstring 已把「跳过只产
            # markdown 的通道」写进 need_html 的契约。漏在这里的后果实测过
            # （2026-09-19）：extract 传 need_html=True 抓 bun.sh/docs 这类
            # 站点，结果被 .md 变体替换、html 字段为空，extract.py 再拿
            # markdown 去跑表格/Meta/JSON-LD 正则 → 三项全空却 success=True，
            # 正是 docstring 自己警告的「开关看着接上了、结果永远为空」。
            if (result is not None and not gated and not need_html
                    and _md_variant_enabled() and _md_variant_wanted(result)):
                md = _md_variant_fetch(url, max_chars, timeout)
                if md is not None:
                    md["md_variant"] = True
                    md["http_fallback"] = True
                    result = md

            # 明确停止信号（429/503）→ 不再升级 TLS/wayback/CDP。
            # 限速/过载与请求方式无关，继续升级重链 = 无视服务器指示放大负载。
            if not result.get("stop_signal") and _budget_left() > 0:
                # 第一级A2：移动端 UA 分支（客户端形态分流型反爬）。实测抖音
                # iesdouyin 对真机 UA 返回 SSR 数据、对桌面/AI UA 一律风控壳；
                # 该类分流与 TLS 指纹无关，stdlib 免费尝试即可，命中则免去
                # TLS 伪造与浏览器冷启动。ARGO_FETCH_MOBILE=0 关闭。
                if (_mobile_branch_enabled() and not gated
                        and result.get("fetch_method") != "http_mobile"
                        and (not result.get("success")
                             or _needs_browser(result))):
                    mob = _mobile_http_fetch(url, max_chars,
                                             _level_timeout(timeout))
                    if (mob.get("success") and not mob.get("stop_signal")
                            and not _needs_browser(mob)):
                        mob["ua_profile"] = "mobile"
                        mob["http_fallback"] = True
                        result = mob
                        _identity_remember_mobile(host)

            if not result.get("stop_signal") and _budget_left() > 0:
                # 第一级B：TLS 指纹伪造（HTTP 失败或疑似指纹拦截时）
                # 指纹检测型反爬对 urllib 直接 403，TLS 层免起浏览器即可通过，
                # 避免不必要的 CDP 冷启动。门控站跳过：单次直连原则，
                # 连击直连只会加重按 IP 的递进限速，失败直接交 Wayback/浏览器。
                if (not gated and _impersonate_enabled()
                        and (not result.get("success")
                             or _needs_browser(result))):
                    spoof = _tls_spoof_fetch(url, max_chars,
                                             _level_timeout(timeout))
                    if spoof.get("stop_signal"):
                        result = spoof
                    elif spoof.get("success"):
                        spoof["http_fallback"] = True
                        result = spoof

            if not result.get("stop_signal") and _budget_left() > 0:
                # 第一级C：r.jina.ai 阅读器（keyless 免费层，远端 JS 渲染）。
                # HTTP/TLS 直连全败时的免浏览器快速路径；markdown-only 同
                # tinyfish，need_html 场景跳过；第三方代理只接公网 URL。
                if (not gated and not need_html and _jina_enabled()
                        and _is_public_host(host)
                        and (not result.get("success")
                             or _needs_browser(result))):
                    # 超时受剩余预算约束：deadline 场景本级不得击穿总预算
                    jina = _jina_reader_fetch(
                        url, max_chars, timeout=_level_timeout(timeout))
                    if jina is not None and jina.get("success"):
                        jina["http_fallback"] = True
                        result = jina

            if not result.get("stop_signal") and _budget_left() > 0:
                # 第一级D：Parallel 免费 MCP web_fetch（keyless 云端渲染+提取）。
                # jina 同级的免浏览器快速路径，markdown-only 同 tinyfish，
                # need_html 场景跳过；免费层限流按持久 session_id 关联；
                # full_content 整页较慢，超时受剩余预算约束。ARGO_FETCH_PARALLEL=0 关闭。
                if (not gated and not need_html and _parallel_mcp_enabled()
                        and _is_public_host(host)
                        and (not result.get("success")
                             or _needs_browser(result))):
                    pm = _parallel_mcp_fetch(
                        url, max_chars,
                        timeout=min(_level_timeout(timeout), 25.0))
                    if pm.get("success") and not _needs_browser(pm):
                        pm["http_fallback"] = True
                        result = pm

            if not result.get("stop_signal") and _budget_left() > 0:
                # 第二级：Wayback 快照回退（HTTP 失败 / 内容空 / 疑似被删页面）
                if not result.get("success"):
                    wb = _wayback_fetch(url, max_chars,
                                        timeout=_level_timeout(
                                            min(timeout * 1.5, 12.0)))
                    if wb.get("success"):
                        wb["http_fallback"] = True
                        result = wb

                # 第三级：浏览器降级（HTTP 失败或疑似 CF/JS 壳）；预算耗尽不再升级
                if use_browser_fallback and _needs_browser(result) and _budget_left() > 0:
                    if _tinyfish_enabled() and not need_html:
                        # tinyfish 免费渲染（返回 clean Markdown，含 JS 执行）优先于本地 Chrome；
                        # 只产 markdown 无 raw html，爬取（need_html）跳过，失败自动回退。
                        tf = _tinyfish_fetch(url, max_chars,
                                            _level_timeout(timeout))
                        if tf.get("success") and len(
                                (tf.get("content") or "").strip()) >= 100:
                            tf["http_fallback"] = True
                            result = tf
                    # tinyfish 未命中（关闭/缺 key/失败/短内容）才起本地 Chrome——
                    # 内容过短的成功响应与失败同样需要继续降级
                    if result.get("fetch_method") != _render_tinyfish.TINYFISH_METHOD:
                        browser_result = _browser_fetch(
                            url, max_chars, timeout=_level_timeout(15.0))
                        if browser_result.get("success") or not result.get("success"):
                            browser_result["http_fallback"] = True
                            result = browser_result

        if deadline_hit["flag"]:
            result["deadline_exhausted"] = True

    # 第三级：质量评估
    result = _assess_quality(result)

    # 写 URL 缓存（不存大块 html，省空间）
    if not skip_cache and result.get("success"):
        try:
            from cache import SearchCache, FETCH_DEFAULT_TTL, FETCH_EVIDENCE_KEY
            to_store = {
                k: v for k, v in result.items()
                if k not in ("html",) and not str(k).startswith("_")
            }
            # 记录本次裁剪上限：后续请求更大 max_chars 时据此判缓存不够用
            # （读缓存侧 _cache_content_too_short）。下划线字段读时会被过滤，
            # 不会泄漏给调用方。
            to_store["_max_chars"] = max_chars
            # 按内容类型粗略 TTL：新闻短、文档长
            ttl = FETCH_DEFAULT_TTL
            st = (result.get("source_type") or result.get("page_type") or "")
            if st in ("news", "realtime"):
                ttl = 600
            elif st in ("docs", "documentation", "reference"):
                ttl = 86400
            # 证据分与正文写进**同一个条目、同一个 try 块**。
            # 分成两处写过（独立 kind、各自 try）会失配：登录态内容被
            # assert_cacheable 拒绝时正文没落盘，证据却照样写成功，于是
            # 「已核验」标记挂在一份不存在的正文上，verify 从此永久跳过它。
            try:
                from evidence_loop import extract_fetch_evidence
                ev = extract_fetch_evidence(result)
                if ev:
                    to_store[FETCH_EVIDENCE_KEY] = {
                        k: v for k, v in ev.items() if k != "url"}
            except Exception:
                pass
            SearchCache().set_fetch(url, to_store, ttl=ttl)
        except Exception:
            pass

    result["cached"] = False
    return _strip_html_unless_needed(result, need_html)


def fetch_page_v3(url: str, max_chars: int = 3000,
                  timeout: int = 8, raw: bool = False) -> dict:
    """兼容 fetch.py 的 fetch_page() 签名，支持透明替换。

    raw=True 时跳过 URL 缓存：缓存写入时有意丢弃 html 大字段（省空间），
    缓存命中只会返回空 html，结构化提取（tables/meta/jsonld）会全空。
    需要原始 HTML 的场景必须重新抓取，才能拿到完整页面。
    """
    result = fetch_v3(url, max_chars=max_chars, timeout=float(timeout),
                      skip_cache=raw, need_html=raw)
    out = {
        "url": result["url"],
        "content": result["content"],
        "length": result["length"],
        "success": result["success"],
        "error": result.get("error", ""),
    }
    if raw:
        out["html"] = result.get("html", "")
    return out


# ─── 聚焦提取（--focus：BM25 段落聚焦，省 token）──────────────────────────────
# 语义来源在 focus_extract.apply_focus（CLI 与 MCP 的 argo_fetch 共用同一份
# 裁剪契约），此处只做接线。历史 bug：文档（SKILL.md / references/usage.md）
# 一直写着 `argo fetch URL --focus 关键词`，但本文件的 CLI 没有该参数，
# 调用方拿到的是 argparse 的 unrecognized arguments——文档承诺的能力只在
# MCP 侧存在。加了参数还不够，两处必须走同一实现，否则迟早再次分叉。

def _focus_fetch_chars(max_chars: int, query: str) -> int:
    """聚焦场景的抓取额度（契约在 focus_extract，此处只做容错接线）。"""
    try:
        from focus_extract import focus_fetch_chars
    except ImportError:
        return max_chars
    return focus_fetch_chars(max_chars, query)


def _apply_focus_to_result(result: dict, query: str, top_k: int = 5,
                           max_chars: int | None = None) -> dict:
    """对成功结果做 BM25 聚焦裁剪（失败结果不动，语义与 MCP 侧一致）。"""
    if not query or not result.get("success"):
        return result
    try:
        from focus_extract import apply_focus
    except ImportError:
        return result
    return apply_focus(result, query, top_k=top_k, max_chars=max_chars)


# ─── 全文回读（--full / --offset / --limit）──────────────────────────────────

def _read_archived(url: str) -> str | None:
    """读本地全文存档，不联网。"""
    try:
        from fulltext_store import load as _load_fulltext
        return _load_fulltext(url, "text")
    except Exception:
        return None


def _full_view(url: str, args) -> dict:
    """取全文视图：优先用存档（本地、零延迟），没有存档才按全量重新抓一次。

    存在的意义：Agent 的上下文放不下整篇，但结论可能落在被裁掉的那一段。
    有了这条通道，「先看 8,000 字摘要、需要时再翻全文」才成立——否则想复核
    就只能重新联网抓一遍，而截断发生过的页面往往连抓法都不一样。
    """
    from_archive = True
    text = _read_archived(url)
    if text is None:
        from_archive = False
        r = fetch_v3(url, max_chars=0, timeout=args.timeout,
                     force_browser=args.browser,
                     use_browser_fallback=not args.no_fallback)
        text = r.get("content", "") or ""
    else:
        r = {"success": True, "error": None, "title": ""}
    start = max(args.offset or 0, 0)
    end = start + args.limit if args.limit and args.limit > 0 else None
    out = dict(r)
    out.update({
        "url": url,
        "content": text[start:end],
        "length": len(text[start:end]),
        "total_length": len(text),
        "offset": start,
        "full_source": "archive" if from_archive else "refetch",
        "fetch_method": "fulltext" if from_archive else r.get("fetch_method", "full"),
    })
    return out


# ─── CLI ─────────────────────────────────────────────────────────────────────

# 人类可读输出的默认显示上限（字符）。--limit 管的是交付窗口，这里是显示上限；
# 显式 --full / --limit 时按调用方要求放开，截断时打印可翻页的提示。
_DISPLAY_CHARS = 2000


def build_parser():
    """CLI 参数表（独立成函数以便测试校验旗标契约）。

    `--use-browser` 与 `--browser` 同义：文档两处都写过前者，入口若只认后者，
    用户照文档敲命令即报错。两个名字都收，避免再出现「文档有、代码无」。
    """
    import argparse
    p = argparse.ArgumentParser(description="Argo fetch v3 — 四级抓取（零依赖）")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--max-chars", type=int, default=8000)
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--browser", "--use-browser", dest="browser",
                   action="store_true",
                   help="强制使用浏览器（--use-browser 同义）")
    p.add_argument("--no-fallback", action="store_true", help="禁用浏览器降级")
    p.add_argument("--actions", type=str,
                   help="页面交互 JSON（如 '[{\"click\":\"#btn\"}]'）")
    p.add_argument("--focus", type=str, default="",
                   help="BM25 聚焦关键词：只返回相关段落，省 token")
    p.add_argument("--focus-top", type=int, default=5,
                   help="--focus 无段落超阈值时的回退保留段落数（默认 5）")
    p.add_argument("--full", action="store_true",
                   help="取全文：优先读本地全文存档，没有则按抓取全量取回，"
                        "不受 --max-chars 限制（用于复核被截断的部分）")
    p.add_argument("--offset", type=int, default=0,
                   help="从正文第 N 个字符开始输出（配合 --limit 翻页读长文）")
    p.add_argument("--limit", type=int, default=0,
                   help="本次输出字符数上限；0 表示不额外限制")
    p.add_argument("--json", action="store_true",
                   help="只输出 JSON 摘要（缺省还会附人类可读的正文段；"
                        "Usage 一直把 --json 列为 Common flags，此前却报"
                        " unrecognized arguments）")
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()

    actions = None
    if args.actions:
        actions = json.loads(args.actions)

    # 聚焦时按放大额度抓取——否则 focus 只能在文档头部选段（见
    # focus_extract.focus_fetch_chars），选完再裁回 args.max_chars。
    if args.full or args.offset or args.limit:
        r = _full_view(args.url, args)
    else:
        r = fetch_v3(args.url,
                     max_chars=_focus_fetch_chars(args.max_chars, args.focus),
                     timeout=args.timeout,
                     force_browser=args.browser,
                     use_browser_fallback=not args.no_fallback,
                     actions=actions)

    focus_requested = bool(args.focus)
    pre_focus_len = r.get("length", 0)
    r = _apply_focus_to_result(r, args.focus, top_k=args.focus_top,
                               max_chars=args.max_chars)

    # 输出摘要
    summary = {k: r[k] for k in ("success", "fetch_method", "content_ok",
                                  "quality_score", "page_type", "source_type",
                                  "is_official", "length", "url") if k in r}
    # 截断必须可见：只给 8,000 字却不说明「还有多少没给」，调用方会当全文用
    for k in ("truncated", "full_length", "full_text_path", "total_length",
              "offset", "full_source", "final_url"):
        if k in r:
            summary[k] = r[k]
    if focus_requested:
        # 显式回报聚焦是否真的生效：正文过短时 focus_applied=False，
        # 不谎报「已省 token」
        summary["focus_applied"] = bool(r.get("focus_applied"))
    if r.get("error"):
        summary["error"] = r["error"]
    print(dumps(summary))
    # __main__ 块是模块级代码，不能用 return 短路——用 if 包住人类可读段
    if not args.json:
        if r.get("title"):
            print(f"\nTitle: {r['title']}")
        if focus_requested:
            print(f"\n[focus] query={args.focus!r} applied={bool(r.get('focus_applied'))} "
                  f"chars={pre_focus_len} → {r.get('length', 0)}")
        print(f"\n--- CONTENT ({r['length']} chars) ---")
        # 默认档保持 2000 字显示上限（护住 agent 上下文），但显式 --full /
        # --limit 时必须按调用方要求显示完整交付窗口：此前这行硬编码
        # [:2000]，与 --limit 的语义（本次输出字符数上限，0 表示不限）冲突，
        # 且表头写的是交付长度，于是「说 8000 字、只给 2000 字、不说明还剩
        # 多少」——调用方会把截断正文当全文用（证据核验场景尤其危险）。
        delivered = r.get("content", "") or ""
        show_all = bool(args.full) or bool(args.limit and args.limit > 0)
        shown = delivered if show_all else delivered[:_DISPLAY_CHARS]
        print(shown)
        if len(shown) < len(delivered):
            # 两个字段是不同代码路径写的同一事实：fetch_v3 路径给 full_length，
            # _full_view 路径给 total_length，故这里按可用者兜底。
            full_len = r.get("full_length") or r.get("total_length") or len(delivered)
            if focus_requested:
                # --focus 之后 content 是重排后的段落抽取，不再是正文的连续窗口，
                # 按「本页起点 + 本页长度」推 --offset 会指向无关区间。宁可不给
                # 翻页建议，也不能给错的。
                print(f"\n[truncated] 已显示 {len(shown)}/{len(delivered)} 字"
                      f"（页面全文 {full_len} 字）。当前为 --focus 抽取结果，"
                      f"非正文连续窗口；要完整正文请用 --full")
            else:
                # --offset 是相对正文全文的绝对偏移（见 _full_view），所以下一页
                # 要从「本页起点 + 本页长度」继续，否则已翻过页的调用方会原地打转。
                next_offset = int(r.get("offset") or 0) + len(shown)
                print(f"\n[truncated] 已显示 {len(shown)}/{len(delivered)} 字"
                      f"（页面全文 {full_len} 字）。"
                      f"继续读：--offset {next_offset} --limit {_DISPLAY_CHARS}；"
                      f"全文：--full")

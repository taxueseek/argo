#!/usr/bin/env python3
"""crawl_frontier.py — 爬取前沿的质量规则。

爬虫的价值不在于抓得多，而在于少抓同一页的不同写法、少抓根本不是页面的东西。
这里只做三件事，都是抓取前的纯函数判断，不碰网络：

  1. URL 归一：复用 url_canon 的单一来源（追踪参数、fragment、参数序、尾斜杠），
     另加会话 ID 参数（;jsessionid=、PHPSESSID 等）。
  2. 明显地不该抓的 URL：图片/音视频/压缩包/字体/脚本样式等静态资产，
     以及路径段连续重复三次以上、层级过深、URL 过长的坏链接。
  3. 范围判定：同主机 + 起始路径前缀。auto 模式在起始页是叶子页时上提一层，
     让「从一篇博客进入」也能覆盖同栏目的其他文章。

不做模板陷阱学习与 DUST 改写规则（需要跨页统计，属第二阶段）；这里保持纯函数，
调用方自己持有已访问集合与内容哈希。
"""

from __future__ import annotations

import html as _html
import re
from urllib.parse import urldefrag, urljoin, urlparse

try:
    from url_canon import canonical_url
except ImportError:  # pragma: no cover - 脚本目录不在 sys.path 时退回原样
    def canonical_url(url: str, **_kwargs) -> str:  # type: ignore
        return url


# 静态资产后缀：fetch_v3 的正文提取对这些没有收益，抓了只会浪费一次请求。
_ASSET_SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".tif", ".tiff", ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus",
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".m3u8", ".mpd",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".exe", ".dmg", ".pkg", ".deb", ".rpm", ".apk", ".ipa",
    ".css", ".js", ".mjs", ".cjs", ".map",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".epub",
})

# 会话 ID 参数：值通常随机且每次不同，会把同一页拆成无限多个 URL。
_SESSION_PARAMS = frozenset({
    "jsessionid", "phpsessid", "sessionid", "session_id", "zenid", "oscsid",
    "cfid", "cftoken",
})

_MAX_URL_LEN = 2048
_MAX_DEPTH = 8
_MAX_QUERY_LEN = 512


def canonical(url: str, base: str = "") -> str:
    """绝对化 + 归一化。解析失败时原样返回（fail-open，不让坏 URL 中断整次爬取）。

    fold_scheme=False：归一化的结果会被当成实际抓取地址，http 折成 https 会
    悄悄改变请求协议（有些站只有 http）。去重仍能覆盖追踪参数、参数序、
    www/mobile、尾斜杠这些真正的重复写法。
    """
    if base:
        url = urljoin(base, url)
    url, _fragment = urldefrag(url)
    try:
        return canonical_url(url, fold_scheme=False)
    except Exception:
        return url


def strip_session_params(url: str) -> str:
    """删掉路径里的 ;jsessionid=... 与会话类查询参数。"""
    parsed = urlparse(url)
    path = re.sub(r";(?:%s)=[^/]*" % "|".join(_SESSION_PARAMS), "", parsed.path,
                  flags=re.IGNORECASE)
    if not parsed.query:
        return parsed._replace(path=path).geturl() if path != parsed.path else url
    kept = []
    for part in parsed.query.split("&"):
        name = part.split("=", 1)[0].lower()
        if name in _SESSION_PARAMS:
            continue
        kept.append(part)
    query = "&".join(kept)
    return parsed._replace(path=path, query=query).geturl()


def is_asset(url: str) -> bool:
    """路径后缀是静态资产即判定为不该按页面抓取。"""
    path = urlparse(url).path.lower()
    dot = path.rfind(".")
    if dot < 0:
        return False
    suffix = path[dot:]
    return suffix in _ASSET_SUFFIXES


def is_pathological(url: str) -> bool:
    """坏链接的确定性判据：重复路径段、层级过深、URL 过长。

    重复段规则取自 Heritrix 的 PathologicalPath：坏掉的相对链接会把 `more/`
    越叠越长，`/a/more/more/more` 这类 URL 永远抓不完。
    """
    if len(url) > _MAX_URL_LEN:
        return True
    parsed = urlparse(url)
    if len(parsed.query) > _MAX_QUERY_LEN:
        return True
    path = parsed.path or "/"
    if re.search(r"(/[^/]+)\1{2,}", path):
        return True
    depth = len([seg for seg in path.split("/") if seg])
    return depth > _MAX_DEPTH


def looks_html(html: str) -> bool:
    """没有可靠的 Content-Type 时按内容嗅探：只要出现标签就算 HTML。

    fetch_v3 的批量入口不返回响应头，扩展名过滤之后仍有大量无后缀的
    `/api/...`、`/download/...`；这里只排除明确的非 HTML（XML/JSON/PDF/空响应），
    片段（`<a href=...>`）也当页面——旧版测试与真实站的局部渲染都会给片段。
    """
    head = (html or "")[:4096].lstrip().lower()
    if not head:
        return False
    if head.startswith(("<?xml", "{", "[", "%pdf")):
        return False
    return "<" in head and ">" in head


def extract_links(html: str, base: str) -> list[str]:
    """提取 href，绝对化后按归一化 URL 去重；不做资产/范围判断（调用方分层）。"""
    out: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"""href\s*=\s*["']([^"'#]+)""", html or "",
                             flags=re.IGNORECASE):
        raw = _html.unescape(match.group(1).strip())
        if not raw or raw.lower().startswith(("javascript:", "mailto:", "tel:",
                                              "data:", "javascript:")):
            continue
        absolute = urljoin(base, raw)
        scheme = urlparse(absolute).scheme.lower()
        if scheme not in ("http", "https"):
            continue
        key = canonical(strip_session_params(absolute))
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def scope_prefix(start_url: str, scope: str = "auto") -> str:
    """返回路径前缀；`/` 表示整个主机。

    auto：把末段当作叶子页（有无扩展名都算），上提到所在目录；目录型种子
    （以 / 结尾）保持原样。这样从一篇博客进入能覆盖同栏目，而不是只抓这一篇。
    path：严格按起始路径前缀，末段不带 / 时补上，等于只抓该路径下面的页。
    """
    path = urlparse(start_url).path or "/"
    if scope == "domain":
        return "/"
    segments = [seg for seg in path.split("/") if seg]
    if scope == "path":
        return path if path.endswith("/") else path + "/"
    # auto
    if path.endswith("/"):
        return path
    if not segments:
        return "/"
    # 叶子页（/blog/post.html 或 /blog/post）上提到它所在的栏目目录，而不是
    # 直接退到根：否则从一篇博客进入会把整站都当成范围。和 PageSift 的
    # --scope auto「叶子种子扩到栏目」同义。
    parent = "/" + "/".join(segments[:-1]) if len(segments) > 1 else ""
    return (parent + "/") if parent else "/"


class Frontier:
    """持有一段爬取的起始主机、路径前缀与子域开关。"""

    def __init__(self, start_url: str, scope: str = "auto",
                 subdomains: bool = False):
        parsed = urlparse(start_url)
        self.start_url = start_url
        self.host = parsed.netloc.lower()
        self.root_host = parsed.hostname.lower() if parsed.hostname else ""
        self.prefix = scope_prefix(start_url, scope)
        self.scope = scope
        self.subdomains = subdomains

    def _host_ok(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host == self.host:
            return True
        if not self.subdomains:
            return False
        name = parsed.hostname or ""
        return bool(self.root_host) and (name == self.root_host
                                         or name.endswith("." + self.root_host))

    def _path_ok(self, url: str) -> bool:
        if self.prefix == "/":
            return True
        path = urlparse(url).path or "/"
        return path == self.prefix.rstrip("/") or path.startswith(self.prefix)

    def accept(self, url: str) -> tuple[bool, str]:
        """返回 (是否接受, 拒绝原因)。原因用于汇总统计，不进上下文。"""
        if not self._host_ok(url):
            return False, "offsite"
        if is_asset(url):
            return False, "asset"
        if is_pathological(url):
            return False, "pathological"
        if not self._path_ok(url):
            return False, "out_of_scope"
        return True, ""

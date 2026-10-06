"""jump_resolver — 不透明跳转壳解析（baidu/sogou/so /link 30x Location）。

baidu/sogou 结果链接是 token 型跳转（与 DDG uddg 的可离线还原不同），聚合层
P0（evidence.is_serp_or_jump_url）把这类 URL 整批滤掉——引擎不解析就永远
0 条。本模块提供引擎级解析：单跳请求读 Location，失败返回空串由调用方丢弃
（与 _unwrap_ddg_link 同哲学：不可核验的跳转壳不进结果）。

两条落点形态（2026-10-06 实测补第二类）：
  - 30x → 直接读 Location（baidu /link 之类）；
  - 200 + 短页 → 页面里 window.location.replace("真实URL") 一类 JS 跳转。
    搜狗对浏览器指纹客户端走这条（~234 字节小页）；对裸指纹/无会话客户端
    302 弹回首页 Location: /，同源弹回一律不救回。
"""

from __future__ import annotations

import html as _html
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

JUMP_MARKERS = ("baidu.com/link", "baidu.com/baidu.php",
                "sogou.com/link", "so.com/link")

# 跳转页里的 JS/meta 落点（按优先级排列，先命中先赢）
_JS_TARGET_RES = (
    re.compile(r"window\.location\.replace\(\s*[\"']([^\"']+)[\"']\s*\)"),
    re.compile(r"window\.location\.href\s*=\s*[\"']([^\"']+)[\"']"),
    re.compile(r"location\.href\s*=\s*[\"']([^\"']+)[\"']"),
    re.compile(r"location\.replace\(\s*[\"']([^\"']+)[\"']\s*\)"),
    re.compile(r"""<meta[^>]+http-equiv=["']refresh["'][^>]+
                content=["'][^"']*url=([^"';]+)""", re.IGNORECASE | re.VERBOSE),
)

# 200 响应只有落在这么短以内才当跳转页读：真实正文页不该进来白耗解析
_MAX_REDIRECT_PAGE_BYTES = 8192

# 批量解析的总量与并发上限：最坏墙钟 = ceil(cap/workers) × timeout
_BATCH_CAP = 16
_BATCH_WORKERS = 8


def is_jump_url(u: str) -> bool:
    return any(m in (u or "") for m in JUMP_MARKERS)


def _extract_js_target(body: str) -> str:
    """从 JS/meta 跳转页提取落点；没有则空串。"""
    for rx in _JS_TARGET_RES:
        m = rx.search(body)
        if m:
            return _html.unescape(m.group(1).strip())
    return ""


def _is_usable_target(target: str, source_url: str) -> bool:
    """落点基本可用性：非绝对 URL / 同源弹回（搜狗无会话时 Location: /，
    经 urljoin 后是同 origin 的首页）不算可用落点。"""
    if not target or not target.lower().startswith(("http://", "https://")):
        return False
    try:
        src_origin = urllib.parse.urlsplit(source_url).netloc.lower()
        tgt_origin = urllib.parse.urlsplit(target).netloc.lower()
    except ValueError:
        return False
    return bool(tgt_origin) and tgt_origin != src_origin


def _fetch_no_redirect(url: str, timeout: float,
                       profiles: list[str]) -> tuple[int, dict[str, str], str]:
    """请求且不跟随跳转，返回 (status, headers, body)。

    curl_cffi 缺席时回落 urllib（只覆盖 302 场景，JS 页提取不了）。
    网络层异常向上抛，由调用方统一 fail-open。
    """
    try:
        from curl_cffi import requests as cr
    except ImportError:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
        })
        opener = urllib.request.build_opener(_NoRedirectHandler)
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), ""
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), ""

    proxies = None
    try:
        from net_proxy import resolve_proxy
        purl = resolve_proxy(url)
        if purl:
            proxies = {"http": purl, "https": purl}
    except Exception:
        pass
    last_err: Exception | None = None
    for fp in profiles:
        try:
            resp = cr.get(url, timeout=timeout, allow_redirects=False,
                          proxies=proxies, impersonate=fp)
        except Exception as e:
            last_err = e
            continue
        status = int(resp.status_code)
        body = ""
        if status == 200 and len(resp.content or b"") <= _MAX_REDIRECT_PAGE_BYTES:
            body = resp.text or ""
        return status, dict(resp.headers), body
    if last_err is not None:
        raise last_err
    return 0, {}, ""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 拦下 3xx，把 Location 留给调用方


def _resolve_core(url: str, timeout: float, profiles: list[str]) -> str:
    """单条解析本体：30x 读 Location，200 短页提取 JS 落点；失败空串。"""
    try:
        status, headers, body = _fetch_no_redirect(url, timeout, profiles)
    except Exception:
        return ""
    try:
        if 300 <= status < 400:
            target = urllib.parse.urljoin(url, (headers.get("Location") or "").strip())
        elif status == 200 and body:
            target = urllib.parse.urljoin(url, _extract_js_target(body))
        else:
            return ""
    except Exception:
        return ""
    return target if _is_usable_target(target, url) else ""


def resolve_jump_url(url: str, profiles: list[str] | None = None,
                     timeout: float = 8.0) -> str:
    """跳转壳 → 真实 URL（单跳 Location / JS 落点）。失败返回空串。"""
    return _resolve_core(url, timeout, profiles or ["chrome131"])


def resolve_jump_urls(urls: list[str], timeout: float = 2.5,
                      profiles: list[str] | None = None,
                      acceptable=None) -> dict[str, str]:
    """批量并行解析，返回 {原URL: 落点URL}（只含解析成功的）。

    `acceptable`：可选落点复核回调（主链传 serp_guard 判据——落点若仍是
    搜索页/跳转页则不救回）；缺省只做同源弹回检查。超过 _BATCH_CAP 的
    部分不解析（调用方按重要性排序给，前头的优先保住）；单条失败只丢
    自己那条，不影响其余（fail-open）。
    """
    uniq = [u for u in dict.fromkeys(urls) if u][:_BATCH_CAP]
    if not uniq:
        return {}
    profiles = profiles or ["chrome131"]
    workers = max(1, min(_BATCH_WORKERS, len(uniq)))
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="jump-resolve") as ex:
        targets = dict(zip(uniq, ex.map(lambda u: _resolve_core(u, timeout,
                                                                profiles),
                                        uniq)))
    out: dict[str, str] = {}
    for src, target in targets.items():
        if not target:
            continue
        if acceptable is not None and not acceptable(target):
            continue
        out[src] = target
    return out

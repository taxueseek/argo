"""jump_resolver — 不透明跳转壳解析（baidu/sogou/so /link 30x Location）。

baidu/sogou 结果链接是 token 型跳转（与 DDG uddg 的可离线还原不同），聚合层
P0（evidence.is_serp_or_jump_url）把这类 URL 整批滤掉——引擎不解析就永远
0 条。本模块提供引擎级解析：单跳请求读 Location，失败返回空串由调用方丢弃
（与 _unwrap_ddg_link 同哲学：不可核验的跳转壳不进结果）。
"""

from __future__ import annotations

JUMP_MARKERS = ("baidu.com/link", "baidu.com/baidu.php",
                "sogou.com/link", "so.com/link")


def is_jump_url(u: str) -> bool:
    return any(m in (u or "") for m in JUMP_MARKERS)


def resolve_jump_url(url: str, profiles: list[str] | None = None,
                     timeout: float = 8.0) -> str:
    """跳转壳 → 真实 URL（单跳 Location）。失败返回空串。"""
    try:
        from curl_cffi import requests as cr
        proxies = None
        try:
            from net_proxy import resolve_proxy
            purl = resolve_proxy(url)
            if purl:
                proxies = {"http": purl, "https": purl}
        except Exception:
            pass
        for fp in (profiles or ["chrome131"]):
            try:
                resp = cr.get(url, timeout=timeout, allow_redirects=False,
                              proxies=proxies, impersonate=fp)
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location") or ""
                    if loc.startswith("http"):
                        return loc
            except Exception:
                continue
        return ""
    except Exception:
        return ""

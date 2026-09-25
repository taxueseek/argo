#!/usr/bin/env python3
"""net_proxy.py — 出口调度：代理解析与按域规则（issue #13，2026-09-14）。

问题本质：进程默认直连。受限网络下部分域（github.com 等）直连不可达必须经
代理，而另一些域（国内源）走代理反而更慢或触发风控。本模块是 argo 全部
命令行网络层的唯一出口决策点，http_client / engines_base.http_open 共用。

## 解析优先级（越具体越优先）

  1. 调用方显式 override（proxy= 参数；"direct" 表示强制直连）
  2. config.yaml `network.proxy.rules` 域名后缀命中（值=代理 URL 或 "direct"）
  3. ARGO_PROXY 环境变量（argo 全局；"direct" 表示强制直连）
  4. config.yaml `network.proxy.url`（argo 全局）
  5. 标准环境变量（HTTPS_PROXY / HTTP_PROXY / ALL_PROXY + NO_PROXY，
     urllib.request.getproxies 语义，与 curl/浏览器直觉一致）
  6. 无 → 直连

## 典型环境三档（本地化适配）

  - TUN/全局代理（Clash TUN 等）：什么都不用配，直连即已可达。
  - 本地混合端口：`ARGO_PROXY=http://127.0.0.1:7890` 或 config 里 url 填上。
  - 按源分流：rules 里给需要代理的域配 URL、给直连更优的域配 "direct"。

注意：https 目标经代理走 CONNECT 隧道（set_tunnel）；代理自身按 http://
明文连接（https 代理极少见，暂不支持）。
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅注解引用（ParseResult/HTTPConnection）；运行时按需惰性导入
    from http.client import HTTPConnection
    from urllib.parse import ParseResult

# urllib 栈（http.client+urllib.request≈26ms）不进模块级导入链：本模块被
# 13 处导入，其中 health_check.is_available（route 热路径）、job/fetch 等场景
# 导入了却未必真发请求；urllib 推迟到各函数体内首次调用时加载（进程内仅一次）。

logger = logging.getLogger("unified_search.net_proxy")

_cfg_cache: dict[str, Any] | None = None
_cfg_stamp: float | None = None


def _network_cfg() -> dict[str, Any]:
    """读 config.yaml 的 network.proxy 段；config 不可用时按空配置处理。"""
    global _cfg_cache, _cfg_stamp
    try:
        from config import config_stamp, load_config
        stamp = config_stamp()
        if _cfg_cache is not None and _cfg_stamp == stamp:
            return _cfg_cache
        cfg = load_config() or {}
        net = (cfg.get("network") or {}) if isinstance(cfg, dict) else {}
        proxy = (net.get("proxy") or {}) if isinstance(net, dict) else {}
        _cfg_cache = {
            "url": str(proxy.get("url") or "").strip(),
            "rules": proxy.get("rules") or {},
        }
        _cfg_stamp = stamp
        return _cfg_cache
    except Exception:
        return {"url": "", "rules": {}}


def resolve_proxy(url: str, override: str | None = None,
                  include_standard_env: bool = True) -> str | None:
    """返回该 URL 应使用的代理 URL；None=直连。优先级见模块 docstring。

    include_standard_env=False 供 urllib 类传输层使用——urlopen 原生认标准
    环境变量，调用方只需 argo 级增量配置；重复接管反而改变 mock 契约与
    失败路径。http.client 类传输层（HttpClient）必须 True（它自己不认 env）。
    """
    import urllib.parse
    import urllib.request

    if override is not None:
        return None if str(override).strip().lower() == "direct" else override

    host = (urllib.parse.urlparse(url if "//" in url else "https://" + url)
            .hostname or "").lower()

    # 2) 按域规则（域名后缀匹配：github.com 命中 api.github.com / www.github.com）
    cfg = _network_cfg()
    for domain, val in (cfg.get("rules") or {}).items():
        d = str(domain).lower().strip()
        if d and (host == d or host.endswith("." + d)):
            v = str(val).strip()
            return None if v.lower() == "direct" else v

    # 3) argo 全局 env
    argo = os.environ.get("ARGO_PROXY", "").strip()
    if argo:
        return None if argo.lower() == "direct" else argo

    # 4) argo 全局 config
    cu = str(cfg.get("url") or "").strip()
    if cu:
        return None if cu.lower() == "direct" else cu

    # 5) 标准环境变量（含 NO_PROXY 尊重；getproxies 已处理大小写变体）
    if not include_standard_env:
        return None
    try:
        scheme = urllib.parse.urlparse(
            url if "//" in url else "https://" + url).scheme or "https"
        proxies = urllib.request.getproxies()
        if proxies.get(scheme) and not urllib.request.proxy_bypass(host):
            return proxies[scheme]
    except Exception:
        pass
    return None


def open_url(req: Any, timeout: float = 10.0):
    """代理感知的 `urllib.request.urlopen` 替身——urllib 类出口的唯一入口。

    为什么需要它：`urlopen` 原生只认标准 `HTTP(S)_PROXY` 环境变量，**不认**
    argo 在 `config.yaml` 的 `network.proxy` 里配置的 url/rules。凡走 urllib
    的抓取都应经本函数，否则在「必须经代理才能出网」的环境里会一直连不上、
    把预算耗光后返回空（issue #13 的形态：抓 GitHub 挂到 deadline_exhausted）。

    issue #13 修复时只覆盖了 `http_open`（引擎侧），其余脚本里的 urlopen
    仍直接调用、不认这项配置；本函数把这条通道统一到一处，供所有 urllib
    出口复用。

    失败语义与 `urlopen` 完全一致：原样抛出，调用方既有的 `except` 分支
    （含 `urllib.error.HTTPError` / `URLError`）不受影响。
    """
    import urllib.parse
    import urllib.request

    if isinstance(req, str):
        req = urllib.request.Request(req)
    url = getattr(req, "full_url", "") or ""
    try:
        # include_standard_env=False：urlopen 原生认标准环境变量，这里只补
        # argo 级增量配置，避免重复接管改变既有 mock/失败语义。
        px = resolve_proxy(url, include_standard_env=False)
    except Exception as e:
        # 静默直连是最坏的选择：本模块存在的理由就是「必须经代理才能出网」的
        # 环境里直连会一直连不上，而解析失败后表现为完全一样的症状——用户看到
        # 「一直连不上」，日志里什么都没有，只能猜。留一条 debug 给排查。
        logger.debug("代理解析失败（%s），本次按直连处理：%r", url, e)
        px = None
    if not px:
        return urllib.request.urlopen(req, timeout=timeout)
    scheme = urllib.parse.urlparse(url).scheme or "https"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({scheme: px}))
    return opener.open(req, timeout=timeout)


def open_connection(parsed: ParseResult, timeout: float,
                    proxy_url: str | None) -> tuple[HTTPConnection, bool]:
    """按是否走代理构造 http.client 连接。返回 (conn, via_proxy)。

    https 目标 + 代理 → 到代理的 HTTPSConnection + set_tunnel（CONNECT 隧道）；
    http 目标 + 代理 → 到代理的 HTTPConnection，request 须用绝对 URL
    （见 request_selector）。
    """
    import http.client
    import urllib.parse

    target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not proxy_url:
        cls = (http.client.HTTPSConnection if parsed.scheme == "https"
               else http.client.HTTPConnection)
        return cls(parsed.hostname, target_port, timeout=timeout), False
    p = urllib.parse.urlparse(proxy_url)
    proxy_port = p.port or (443 if p.scheme == "https" else 80)
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(p.hostname, proxy_port, timeout=timeout)
        conn.set_tunnel(parsed.hostname, target_port)
        return conn, True
    return http.client.HTTPConnection(p.hostname, proxy_port, timeout=timeout), True


def request_selector(parsed: ParseResult, path: str,
                     via_proxy: bool) -> str:
    """http 经代理时请求行必须是绝对 URL（RFC 7230 5.3.2）；https 隧道用相对。"""
    if via_proxy and parsed.scheme == "http":
        port = "" if parsed.port in (None, 80) else f":{parsed.port}"
        return f"http://{parsed.hostname}{port}{path}"
    return path

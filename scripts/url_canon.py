#!/usr/bin/env python3
"""url_canon.py — URL 规范化唯一来源。

背景：本仓此前有 4 份各自实现的「URL 归一」——search.py:_canonical_url、
plan.py:canonicalize_url、candidate_envelope.py:canonicalize_url、
research_dossier.py:canonical_url。四份的追踪参数表、大小写处理、尾斜杠
规则各不相同，导致同一条链接在不同阶段归一成不同键：融合层已合并的结果，
进了 dossier 又被当成两条。归一逻辑必须只有一个来源，否则「去重」取决于
你从哪个入口看。

本模块的目标：同一条内容的不同 URL 变体（追踪参数、www、默认端口、
尾斜杠、fragment、参数顺序、百分号转义大小写、移动站域名）折叠成同一个键。

明确不做（避免误合并）：
  - 不改 path 大小写（多数服务器路径大小写敏感，/Wiki/Foo ≠ /wiki/foo）
  - 不删有语义的查询参数（只删确认的追踪参数）
  - 不把 http/https 折叠进不同键时丢信息——统一提升为 https 仅用于比较语义，
    输出保留调用方需要的形态由 canonical_url 统一决定

验证：
    python3 -m pytest tests/test_url_canon.py -q
"""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

__all__ = ["canonical_url", "TRACKING_PARAMS", "is_tracking_param"]


# 追踪/营销参数（小写比较）。来源：SearXNG url 处理插件、EasyList 派生表、
# 各平台分享链接实测。只收「确认只用于归因、删掉不改变页面内容」的参数。
TRACKING_PARAMS: frozenset[str] = frozenset({
    # Google / 通用营销
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_cid", "utm_reader", "utm_referrer",
    "utm_social", "utm_social_type", "utm_brand", "utm_adgroup",
    "gclid", "gclsrc", "dclid", "wbraid", "gbraid", "gad_source",
    "fbclid", "msclkid", "yclid", "twclid", "ttclid", "igshid", "igsh",
    "mc_cid", "mc_eid", "_ga", "_gl", "_hsenc", "_hsmi", "vero_id",
    "oly_anon_id", "oly_enc_id", "s_kwcid", "ef_id", "cmpid", "campaign",
    # 中文平台分享/统计
    "spm", "scm", "share_token", "share_medium", "share_source", "share_plat",
    "share_tag", "share_session_id", "share_from", "share_scene",
    "from_source", "from_spmid", "from_spmid2", "vd_source", "unique_k",
    "buvid", "buvid3", "buvid4", "up_id", "seid", "spmid", "timestamp",
    "wxshare", "weibo_id", "share_referer", "share_object_id",
    # 通用归因
    "ref", "ref_src", "ref_url", "refer", "referer", "referrer",
    "source", "from", "src", "cmp", "camp", "srsltid",
    "clickid", "click_id", "clicktime", "scid", "scene", "sessionid",
    "trace", "trace_id", "track", "tracking_id", "trk", "trkcampaign",
})

# 移动站/AMP 子域前缀（fold_mobile）与 www 前缀（fold_www）分开——
# 原先合成一个交替式，两个开关任一为真就会把两类前缀都剥掉，开关名不符实。
_MOBILE_PREFIX_RE = re.compile(r"^(?:m|mobile|amp)\.", re.I)
_WWW_PREFIX_RE = re.compile(r"^www\d*\.", re.I)
# 百分号转义（用于把 %3a 规范成 %3A）
_PCT_RE = re.compile(r"%[0-9a-fA-F]{2}")
# 非默认端口判断用
_DEFAULT_PORTS = {"http": "80", "https": "443", "": ""}


def is_tracking_param(name: str) -> bool:
    """参数名是否属于追踪参数（大小写不敏感）。"""
    if not name:
        return False
    low = name.lower()
    if low in TRACKING_PARAMS:
        return True
    # utm_* 前缀族（utm_ 后接任意子键一律视为归因）
    return low.startswith("utm_")


def _normalize_pct_escape(value: str) -> str:
    """把百分号转义统一为大写十六进制，并将非保留字符解码回字面量。

    两种变体合并：`%7euser` 与 `~user`、`%3a` 与 `%3A`。
    只解码 RFC 3986 的 unreserved 集合（A-Za-z0-9-._~），保留字符不动，
    避免把 `%2F` 解成 `/` 而改变路径语义。
    """
    if "%" not in value:
        return value
    # 先把 %xx 折成大写
    value = _PCT_RE.sub(lambda m: m.group(0).upper(), value)
    # 再把 unreserved 的转义还原（只还原 unreserved，保留字符保持转义）
    keep = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "%" and i + 2 < len(value):
            hexpart = value[i + 1:i + 3]
            try:
                decoded = chr(int(hexpart, 16))
            except ValueError:
                out.append(ch)
                i += 1
                continue
            if decoded in keep:
                out.append(decoded)
            else:
                out.append("%" + hexpart)
            i += 3
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _collapse_slashes(path: str) -> str:
    """折叠路径中的重复斜杠（保留前导单个斜杠）。`//a///b` → `/a/b`。"""
    if not path or "//" not in path:
        return path
    return re.sub(r"/{2,}", "/", path)


@lru_cache(maxsize=4096)
def canonical_url(
    url: str,
    *,
    fold_scheme: bool = True,
    fold_www: bool = True,
    fold_mobile: bool = True,
    drop_tracking: bool = True,
    drop_fragment: bool = True,
    strip_trailing_slash: bool = True,
    sort_query: bool = True,
    drop_query: bool = False,
) -> str:
    """把 URL 归一为稳定的去重键。

    折叠规则（默认全开）：
      1. scheme 小写；http/https 统一视为 https（fold_scheme）
      2. host 小写、去默认端口、去 userinfo
      3. 去 wwwN. / m. / mobile. / amp. 前缀（要求剩余 ≥2 段，防把 m.co 削成 co）
      4. 删确认的追踪参数（含 utm_* 前缀族）
      5. 删 fragment
      6. 查询参数按键排序（?a=1&b=2 ≡ ?b=2&a=1）
      7. 百分号转义统一大写，unreserved 字符还原
      8. 折叠重复斜杠、去尾斜杠（根路径保留）

    任何解析失败都原样返回输入（fail-open：规范化的失败不该让结果消失）。

    纯函数 + 热路径，故加 lru_cache：result_to_candidate → _candidate_id
    每条结果要算 4–5 次（候选 ID、去重、minhash、RRF 各若干），urlparse +
    2 次 regex sub + parse_qsl + urlencode 无记忆化时约 15–40 µs/次 × 5 ×
    20 条 ≈ 1.5–4 ms/次搜索。键空间 = 去重前 URL 数（有界），4096 覆盖一次
    搜索的全部结果；同一 URL 的二次查询天然命中。入参全是 str/bool，可哈希。
    """
    if not url:
        return ""
    raw = str(url).strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
    except Exception:
        return raw

    # 非 http(s)（file://, ftp:// 等）不做内容级折叠，只做最小归一
    orig_scheme = (p.scheme or "").lower()
    if orig_scheme not in ("http", "https", ""):
        return raw
    # 无 scheme 且无 netloc 的「伪 URL」（如 "not a url"、"%%"）原样返回：
    # 这类串多是标题或坏数据，强行补 scheme 会造出 `https:///not a url`，
    # 反而让两个不同的坏值撞成同一个键。
    if not orig_scheme and not (p.netloc or ""):
        return raw
    scheme = orig_scheme
    if fold_scheme:
        scheme = "https"
    elif not scheme:
        scheme = "https"

    host = (p.netloc or "").lower()
    # 去 userinfo（安全：凭据不进去重键，也不进对外产物）
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    # 去默认端口：按**原始 scheme** 判默认端口——否则 http://x:80 会因
    # scheme 已折成 https 而被误判为「非默认端口」残留 :80。
    if ":" in host and not host.startswith("["):
        h, _, port = host.rpartition(":")
        if port.isdigit() and _DEFAULT_PORTS.get(orig_scheme, "") == port:
            host = h
    # 每个开关只管自己的前缀；逐个剥离而非互斥（www.m.example.com → example.com），
    # 但每步都保留「剩余 ≥2 段」的守卫，防把 m.co 削成 co。
    for enabled_flag, prefix_re in ((fold_www, _WWW_PREFIX_RE),
                                    (fold_mobile, _MOBILE_PREFIX_RE)):
        if not enabled_flag:
            continue
        stripped = prefix_re.sub("", host)
        if stripped != host and stripped.count(".") >= 1:
            host = stripped

    path = p.path or ""
    path = _collapse_slashes(path)
    path = _normalize_pct_escape(path)
    if strip_trailing_slash and path not in ("", "/") and path.endswith("/"):
        path = path.rstrip("/")

    query = ""
    if not drop_query and p.query:
        pairs = parse_qsl(p.query, keep_blank_values=True)
        if drop_tracking:
            pairs = [(k, v) for k, v in pairs if not is_tracking_param(k)]
        if sort_query:
            pairs.sort(key=lambda kv: (kv[0], kv[1]))
        norm = [(_normalize_pct_escape(k), _normalize_pct_escape(v)) for k, v in pairs]
        query = urlencode(norm)

    fragment = "" if drop_fragment else (p.fragment or "")

    return urlunparse((scheme, host, path, p.params or "", query, fragment))

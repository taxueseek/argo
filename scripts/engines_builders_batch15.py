#!/usr/bin/env python3
"""批次十五构建器：腾讯元宝搜索（2026-10-21）。

  yuanbao   腾讯元宝搜索（api.wsa.cloud.tencent.com/SearchPro，需 key）

为什么需要独立 builder（声明式 type: http 表达不了）：

  1. **Pages 是 JSON 字符串数组**——`Response.Pages[i]` 本身是 JSON 文本，
     要二次解析；output_map 只能走普通嵌套对象。
  2. **错误走 HTTP 200**——鉴权失败/欠费/限流全部返回 200 + `Response.Error`。
     声明式解析会把它当「0 结果」，把「key 错了」上报成「网上没有这个信息」
     （与 2026-09-15 `--engine you` 的 SSL 超时同族，那条踩过一次）。
  3. **日期是 `YYYY/MM/DD HH:MM:SS`**——不是 argo 的 ISO 约定，要归一。
  4. **Cnt 只接受 10/20/30/50**——传别的值上游直接回 `illegal Cnt` 且 0 结果。

接口事实（2026-10-21 本机实测，非文档抄录）：

  POST /SearchPro
  Authorization: Bearer <key>；Content-Type: application/json; charset=utf-8
  body  {"Query": "...", "Cnt": N}
  返回  Response.{RequestId, Query, Version, Pages[], Error?}

  单条 Pages[i] 解析后：
    {passage, score, date, title, url, content, site, favicon, pics}
    （文档「输出示例」写的是 images，实测字段名是 pics；README 与文档
     不一致时以实测为准。）

  错误码 → argo 失败类别：
    UnauthorizedOperation / ResourceNotFound  → auth        （key 错 / 服务未开通）
    ResourceUnavailable                       → quota_exhausted（账号欠费）
    RequestLimitExceeded                      → rate_limited
    InvalidParameter                          → upstream    （接口契约变了）
    InternalError / 其他                       → unknown
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from engines_http import UA
from engines_base import http_open, note_failure, safe_search

_UA = UA
_YUANBAO_URL = "https://api.wsa.cloud.tencent.com/SearchPro"
# 腾讯侧公布的中间价/新闻口径都按北京时间；本机可能不在东八区，日期归一
# 必须显式指定偏移，否则跨时区机器上「今天」会错一天。
_CST = timezone(timedelta(hours=8))

# key 候选链（真源在 engine_env.KNOWN_ENV_ALIASES，这里只声明本引擎要用哪条）。
# ⚠️ 与下方 get_env 调用里的**字面量列表**必须一致——状态层的变体感知契约由
# tests/test_env_declaration_consistency.py 静态扫描，它只认写死在调用处的
# ast.Constant/List/Tuple；写成 `get_env(_KEY_ENV)` 会被判成「声明了却没读到」。
# tests/test_yuanbao_engine.py 有一条把两者钉在一起。
_KEY_ENV = ("ARGO_YUANBAO_API_KEY", "TENCENTCLOUD_WSA_APIKEY", "YUANBAO_API_KEY")

# Cnt 实测只接受 10/20/30/50（1/2/5/100 均回 "illegal Cnt"）。
_CNT_ALLOWED = (10, 20, 30, 50)

_ERROR_MAP: dict[str, tuple[str, str]] = {
    "UnauthorizedOperation": ("auth", "key 无效或未授权"),
    "ResourceNotFound": ("auth", "服务未开通"),
    "ResourceUnavailable": ("quota_exhausted", "账号欠费或资源不可用"),
    "RequestLimitExceeded": ("rate_limited", "超出频率限制"),
    "InvalidParameter": ("upstream", "参数被拒（接口契约可能已变）"),
    "InternalError": ("unknown", "上游内部错误"),
}

_TAG_RE = re.compile(r"</?[a-zA-Z][^>]{0,40}>")


def yuanbao_api_key() -> str:
    """按候选链取 key（os.environ 优先，~/.config/argo/env 热读保底）。

    候选链以字面量落在 get_env 调用里（原因见 _KEY_ENV 上方注释）。
    """
    try:
        from engine_env import get_env
    except ImportError:
        import os
        for name in _KEY_ENV:
            v = (os.environ.get(name) or "").strip()
            if v:
                return v
        return ""
    return (get_env(["ARGO_YUANBAO_API_KEY", "TENCENTCLOUD_WSA_APIKEY",
                     "YUANBAO_API_KEY"]) or "").strip()


def yuanbao_cnt(n: int) -> int:
    """把请求条数夹到上游允许的档位。

    取「能覆盖 n 的最小合法档」而不是最大档：额度按次计，多要不产生额外
    结果价值（本地还会按 n 截断），少要则可能不够。n 超出最大档时给最大档，
    由本地截断兜底。
    """
    try:
        want = int(n)
    except (TypeError, ValueError):
        want = 5
    if want <= 0:
        want = 5
    for v in _CNT_ALLOWED:
        if want <= v:
            return v
    return _CNT_ALLOWED[-1]


def _clean(text: Any, limit: int | None = None) -> str:
    out = _TAG_RE.sub("", str(text or "")).strip()
    if limit is not None and len(out) > limit:
        out = out[:limit].rstrip()
    return out


def _norm_date(raw: Any) -> str:
    """`YYYY/MM/DD HH:MM:SS`（东八区）→ ISO 带偏移；无法解析返回空串。

    交出去空串而不是原样字符串：argo 的 freshness/时间窗按 ISO 或年月解析，
    把 `2023/06/08 00:00:00` 原样当 published_at 会落到「有字段但读不出日期」
    那一类——比没有更糟（时间窗会当它无日期剔除，但浏览者会以为有日期）。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=_CST).isoformat(
                timespec="seconds")
        except ValueError:
            continue
    return ""


def _load_page(raw: Any) -> dict[str, Any] | None:
    """Pages[i] 是 JSON 文本（实测），兼容上游改成对象的情形。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def parse_yuanbao_payload(data: Any, n: int, eng: str = "yuanbao"
                          ) -> list[dict[str, Any]]:
    """响应体 → argo 结果条目（纯函数，门禁直接打到）。

    错误路径**必须**留痕：上游把 4xx/5xx 语义全塞进 HTTP 200 的
    `Response.Error`，不留痕就会退化成 `no-results`，把「欠费」报成
    「网上没有」。分类交给 retry_policy 的既有映射，不在此另立一套。
    """
    resp = data.get("Response") if isinstance(data, dict) else None
    if not isinstance(resp, dict):
        note_failure(eng, "upstream", "no-response", str(data)[:160])
        return []

    err = resp.get("Error")
    if isinstance(err, dict):
        code = str(err.get("Code") or "").strip()
        category, reason = _ERROR_MAP.get(code, ("unknown", code or "unknown"))
        note_failure(eng, category, reason,
                     f"{code}: {err.get('Message') or ''}")
        return []

    pages = resp.get("Pages")
    if not isinstance(pages, list):
        note_failure(eng, "upstream", "pages-not-list",
                     f"Pages 类型 {type(pages).__name__}")
        return []

    limit = max(1, int(n or 5))
    out: list[dict[str, Any]] = []
    for raw in pages:
        if len(out) >= limit:
            break
        item = _load_page(raw)
        if not item:
            continue
        url = str(item.get("url") or "").strip()
        title = _clean(item.get("title"), 160)
        if not url or not title:
            continue
        # passage 是摘要档（实测 ~800 字），content 是更长正文（~2000 字）。
        # 搜索层只交摘要：正文按需走 fetch/verify 链，与 DeepSeek 引擎同口径。
        snippet = _clean(item.get("passage")) or _clean(item.get("content"), 300)
        row: dict[str, Any] = {
            "title": title,
            "url": url,
            "snippet": snippet[:600],
            "source": eng,
        }
        site = _clean(item.get("site"), 60)
        if site:
            row["site_name"] = site
        published = _norm_date(item.get("date"))
        if published:
            row["published_at"] = published
        score = item.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            row["upstream_score"] = float(score)
        out.append(row)
    return out


def _build_yuanbao_engine(spec: dict[str, Any]) -> Any:
    timeout = spec.get("timeout", 12)
    eng = spec.get("_name", "yuanbao")
    endpoint = spec.get("url") or _YUANBAO_URL

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None,
                depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        q = (query or "").strip()
        if not q:
            return []
        key = yuanbao_api_key()
        if not key:
            # 缺 key 与「配置错了」要分开：这条是缺配置，路由层已有 env 拦截，
            # 走到这里说明是显式 --engine 调用，留痕让错误可行动。
            note_failure(eng, "auth", "missing-key",
                         "未配置 " + " / ".join(_KEY_ENV))
            return []
        body = json.dumps({"Query": q, "Cnt": yuanbao_cnt(n)},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": _UA,
                "Accept": "application/json",
            },
        )
        try:
            with http_open(req, timeout=to, engine=eng) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            note_failure(eng, "network", "exception", str(e)[:160])
            return []
        return parse_yuanbao_payload(data, n, eng)

    return _engine

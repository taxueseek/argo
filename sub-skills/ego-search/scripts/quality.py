#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态抓取质量信号：空页 / 登录墙 / 验证码提示。

不替代 content_security；只给 Agent 是否「登录态是否还有效」的可操作信号。
"""
from __future__ import annotations

import re
from typing import Any

# 中英常见登录墙 / 验证码信号（低噪声关键词）
_AUTH_WALL_RE = re.compile(
    r"(请先登录|立即登录|登录后查看|sign\s*in\s*to\s*continue|log\s*in\s*to\s*continue|"
    r"create\s*an\s*account|验证码|captcha|cloudflare|access\s*denied|"
    r"403\s*forbidden|请完成人机验证|扫码登录|登录以继续)",
    re.I,
)
_EMPTY_MIN_CHARS = 80


def assess_body(payload: dict[str, Any]) -> dict[str, Any]:
    """对 fetch/detail 类 payload 附加 quality 字段。"""
    if not isinstance(payload, dict):
        return payload
    content = payload.get("content")
    if content is None and isinstance(payload.get("detail"), dict):
        # act 结构：评估 detail
        d = payload["detail"]
        if isinstance(d, dict):
            d = dict(d)
            d["quality"] = _score_text(d.get("content") or "", d.get("title") or "")
            payload = dict(payload)
            payload["detail"] = d
            # 顶层也挂一份摘要
            payload["quality"] = d["quality"]
        return payload

    text = content if isinstance(content, str) else ""
    title = payload.get("title") or ""
    q = _score_text(text, str(title))
    out = dict(payload)
    out["quality"] = q
    return out


def _score_text(text: str, title: str = "") -> dict[str, Any]:
    blob = f"{title}\n{text}"
    n = len(text.strip())
    auth = bool(_AUTH_WALL_RE.search(blob))
    empty = n < _EMPTY_MIN_CHARS
    # 登录态是否仍像可用：有正文且无登录墙信号
    login_likely_ok = (not empty) and (not auth)
    return {
        "content_chars": n,
        "empty_or_thin": empty,
        "auth_wall_suspected": auth,
        "login_likely_ok": login_likely_ok,
        "hint": (
            "可能掉登录或撞验证码，请在对应运行时人工登录后用 --keep-space 重试"
            if auth or empty
            else "body_ok"
        ),
    }

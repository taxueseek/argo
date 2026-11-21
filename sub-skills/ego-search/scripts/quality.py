#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态抓取质量信号：空页 / 登录墙 / 验证码提示。

不替代 content_security；只给 Agent 是否「登录态是否还有效」的可操作信号。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

# 优先复用主仓 scripts/engine_states.py 单真源；子技能独立运行时回落同值常量
_ARGO_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if _ARGO_SCRIPTS.is_dir() and str(_ARGO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ARGO_SCRIPTS))
try:
    from engine_states import (
        EGO_AUTH_COOLDOWN_STATES,
        EGO_AUTH_FAILED,
        EGO_AUTH_HEALTHY,
        EGO_AUTH_OK,
        EGO_AUTH_RESTRICTED,
        EGO_AUTH_TO_PAGE_AUTH,
        EGO_AUTH_UNKNOWN,
    )
except ImportError:  # pragma: no cover - 独立部署兜底
    EGO_AUTH_OK, EGO_AUTH_HEALTHY = "ok", "healthy"
    EGO_AUTH_FAILED, EGO_AUTH_RESTRICTED, EGO_AUTH_UNKNOWN = (
        "auth_failed", "account_restricted", "unknown",
    )
    EGO_AUTH_COOLDOWN_STATES = frozenset({EGO_AUTH_FAILED, EGO_AUTH_RESTRICTED})
    EGO_AUTH_TO_PAGE_AUTH = {
        EGO_AUTH_OK: "ready",
        EGO_AUTH_HEALTHY: "ready",
        EGO_AUTH_FAILED: "signed_out",
        EGO_AUTH_RESTRICTED: "search_restricted",
    }

# 中英常见登录墙 / 验证码信号（低噪声关键词）
_AUTH_WALL_RE = re.compile(
    r"(请先登录|立即登录|登录后查看|sign\s*in\s*to\s*continue|log\s*in\s*to\s*continue|"
    r"create\s*an\s*account|验证码|captcha|cloudflare|access\s*denied|"
    r"403\s*forbidden|请完成人机验证|扫码登录|登录以继续)",
    re.I,
)
_EMPTY_MIN_CHARS = 80


def assess_body(payload: dict[str, Any]) -> dict[str, Any]:
    """对 fetch/detail/api 类 payload 附加 quality 与 Cookie 鉴权归因字段。"""
    if not isinstance(payload, dict):
        return payload
    cookie_names = payload.get("cookie_names")
    strict_cookie = bool(payload.get("cookie_names_complete", False))
    if payload.get("content") is None and isinstance(payload.get("detail"), dict):
        # act 结构：评估 detail
        d = payload["detail"]
        if isinstance(d, dict):
            d = dict(d)
            d_url = str(d.get("url") or payload.get("url") or "")
            d_cookies = d.get("cookie_names") if d.get("cookie_names") is not None else cookie_names
            d["quality"] = assess_auth_with_cookies(
                _score_text(d.get("content") or "", d.get("title") or ""),
                url=d_url,
                present_cookie_names=d_cookies,
                strict_cookie_check=strict_cookie,
            )
            payload = dict(payload)
            payload["detail"] = d
            # 顶层也挂一份摘要
            payload["quality"] = d["quality"]
            if d["quality"].get("handoff_required"):
                payload["handoff_required"] = True
                payload["login_url"] = d["quality"].get("login_url") or ""
        return payload

    if payload.get("content") is None and payload.get("api_url"):
        # api 结构：评估同源 API 返回 + Cookie 存在性 + 静默限权探针
        api_err = payload.get("data_type") == "error" or payload.get("data") in (None, "", {}, [])
        raw_preview = str(payload.get("data") or "")[:600]
        base = {
            "content_chars": len(raw_preview),
            "empty_or_thin": api_err,
            "auth_wall_suspected": bool(_AUTH_WALL_RE.search(raw_preview)),
            "login_likely_ok": not api_err and not bool(_AUTH_WALL_RE.search(raw_preview)),
            "hint": "api_error_or_empty" if api_err else "api_ok",
        }
        target_url = str(payload.get("api_url") or payload.get("page_url") or "")
        q = assess_auth_with_cookies(
            base,
            url=target_url,
            present_cookie_names=cookie_names,
            strict_cookie_check=strict_cookie,
        )
        # 若携带懒触发的 health_probe 响应或接口本身内联返回限权码，优先判定 account_restricted
        probe_res = assess_health_probe(
            payload.get("health_probe") if payload.get("health_probe") is not None else payload.get("data"),
            url=target_url,
        )
        if probe_res.get("auth_state") in EGO_AUTH_COOLDOWN_STATES:
            q.update(probe_res)
        out = dict(payload)
        out["quality"] = q
        if q.get("handoff_required"):
            out["handoff_required"] = True
            out["login_url"] = q.get("login_url") or ""
            out["handoff"] = build_handoff(q, url=target_url)
        return out

    text = payload.get("content") if isinstance(payload.get("content"), str) else ""
    title = payload.get("title") or ""
    target_url = str(payload.get("url") or "")
    q = assess_auth_with_cookies(
        _score_text(text, str(title)),
        url=target_url,
        present_cookie_names=cookie_names,
        strict_cookie_check=strict_cookie,
    )
    out = dict(payload)
    out["quality"] = q
    if q.get("handoff_required"):
        out["handoff_required"] = True
        out["login_url"] = q.get("login_url") or ""
        out["handoff"] = build_handoff(q, url=target_url)
    return out


def assess_health_probe(probe_data: Any, *, url: str = "", site: str = "") -> dict[str, Any]:
    """判定静默限权（Shadow Restriction）探针或内联接口响应。

    核心区分：
      - account_restricted：Cookie 仍在但账号被平台暗中风控限权（如小红书 code=300011），
        重登同账号无效，需换活跃小号或退回 L0 公开检索。
      - auth_failed：凭证过期或未登录（如 HTTP 401 / isLogin=false / code=-101）。
      - healthy：探针正常。
    """
    import site_profiles

    profile = site_profiles.get_profile(site) if site else site_profiles.match_site(url)
    if profile is None or probe_data is None:
        return {"auth_state": EGO_AUTH_UNKNOWN}

    blob = ""
    code: int | None = None
    if isinstance(probe_data, dict):
        raw_code = probe_data.get("code")
        if isinstance(raw_code, int):
            code = raw_code
        elif isinstance(raw_code, str) and raw_code.lstrip("-").isdigit():
            code = int(raw_code)
        # B 站 nav 接口 data.isLogin=False 明确表未登录
        data_obj = probe_data.get("data")
        if isinstance(data_obj, dict) and data_obj.get("isLogin") is False:
            return {
                "site": profile.name,
                "login_url": profile.login_url,
                "auth_state": EGO_AUTH_FAILED,
                "login_likely_ok": False,
                "handoff_required": True,
                "hint": f"{profile.name} 身份探针返回未登录（isLogin=false），请在 {profile.login_url} 登录后重试",
            }
        blob = f"{probe_data.get('msg', '')} {probe_data.get('message', '')} {probe_data.get('error', '')}"
    else:
        blob = str(probe_data)[:500]

    hit_code = code is not None and code in profile.restricted_codes
    hit_kw = any(kw.lower() in blob.lower() for kw in profile.restricted_keywords)
    if hit_code or hit_kw:
        # B 站 -101 属于未登录，其余限权码归 account_restricted
        is_unauth_only = (profile.name == "bilibili" and code == -101)
        state = EGO_AUTH_FAILED if is_unauth_only else EGO_AUTH_RESTRICTED
        hint = (
            f"{profile.name} 未登录（code={code}），请在 {profile.login_url} 登录后重试"
            if is_unauth_only
            else (
                f"检测到 {profile.name} 账号被平台静默限权（code={code}），重登同账号通常无效；"
                "建议切换正常活跃小号，或退回 L0 公开检索（argo search）。"
            )
        )
        return {
            "site": profile.name,
            "login_url": profile.login_url,
            "auth_state": state,
            "restricted_code": code,
            "login_likely_ok": False,
            "handoff_required": True,
            "hint": hint,
        }
    return {
        "site": profile.name,
        "auth_state": EGO_AUTH_HEALTHY,
        "login_likely_ok": True,
        "handoff_required": False,
    }


def build_handoff(quality_info: dict[str, Any], *, url: str = "") -> dict[str, Any]:
    """构造统一的 L3 人工接管契约载荷（含 L0 公开降级建议，绝不绕过反爬/验证码）。"""
    import site_profiles

    site_name = str(quality_info.get("site") or "")
    profile = site_profiles.get_profile(site_name) if site_name else site_profiles.match_site(url)
    auth_state = str(quality_info.get("auth_state") or EGO_AUTH_FAILED)
    login_url = str(quality_info.get("login_url") or (profile.login_url if profile else ""))
    scopes = list(profile.public_web_scopes) if profile else []
    scope_expr = " OR ".join(f"site:{s}" for s in scopes) if scopes else ""
    return {
        "handoff_required": True,
        "auth_state": auth_state,
        "site": profile.name if profile else (site_name or "unknown"),
        "login_url": login_url,
        "clear_cooldown_cmd": (
            f"argo ego-search status --clear-cooldown {profile.domains[0]}"
            if profile and profile.domains
            else "argo ego-search status --clear-cooldown"
        ),
        "public_fallback": {
            "tier": "L0",
            "available": bool(scopes),
            "site_scopes": scopes,
            "suggested_query_filter": f"({scope_expr})" if scope_expr else "",
        },
        "action_required": (
            "切换活跃小号或在浏览器人工完成登录/验证后，再清除冷却重试；"
            "不尝试绕过验证码或风控。"
        ),
    }


def assess_auth_with_cookies(
    base_quality: dict[str, Any],
    *,
    url: str = "",
    present_cookie_names: list[str] | set[str] | None = None,
    strict_cookie_check: bool = False,
) -> dict[str, Any]:
    """结合站点声明的登录 Cookie 名，精准区分登录失效与普通内容异常。

    规则（只查键名存在性，绝不接触 Cookie 值）：
      - 未匹配到站点画像：只保留文本启发式嫌疑，不升级 auth_failed
        （无 login_url 的 handoff 与冷却都是死路）。
      - 匹配到画像但未采集到 Cookie 列表：正文/API 异常（auth_wall_suspected
        或 empty_or_thin）即升级 auth_failed + handoff_required——画像自带
        login_url，交接可执行。
      - strict_cookie_check=True（如 CDP 全量含 HttpOnly 键名）：关键 Cookie 全部缺失时
        即使页面返回 200 壳子也判定为 auth_failed + handoff_required。
      - strict_cookie_check=False（如页面 document.cookie，看不到 HttpOnly）：仅在
        正文/API 已出现异常（auth_wall_suspected 或 empty_or_thin）且关键 Cookie 未命中时，
        才升级为 auth_failed + handoff_required，避免误杀 HttpOnly 登录态。
    """
    import site_profiles

    q = dict(base_quality)
    profile = site_profiles.match_site(url)
    if profile is None:
        # 无画像站点只保留文本启发式嫌疑（auth_wall_suspected + hint），不升级
        # auth_failed / handoff_required：没有站点身份就没有 login_url，升级后
        # 触发的 1h 站点冷却是条死路，而正文含 cloudflare/captcha 等词并不代表
        # 真登录墙（2026-11-21 探针 5 实证：误封类第三分支）。
        return q

    q["site"] = profile.name
    q["login_url"] = profile.login_url

    if present_cookie_names is None:
        if q.get("auth_wall_suspected"):
            q["auth_state"] = EGO_AUTH_FAILED
            q["handoff_required"] = True
        return q

    present = {str(n).strip() for n in present_cookie_names if str(n).strip()}
    expected = list(profile.cookie_names)
    matched = [c for c in expected if c in present]
    missing = [c for c in expected if c not in present]

    q["cookie_probe"] = {
        "site": profile.name,
        "matched_count": len(matched),
        "missing_cookies": missing,
    }
    all_missing = bool(expected) and not matched
    body_suspect = bool(q.get("auth_wall_suspected") or q.get("empty_or_thin"))

    # 判据与 docstring 对齐：Cookie 命中即登录态证据，正文嫌疑只作辅助——
    # 旧第三子句让 matched 非空也被误判 auth_failed 并自动记 1h 站点冷却
    # （2026-11-21 审计：误封锁站）。
    if (strict_cookie_check and all_missing) or (all_missing and body_suspect):
        q["auth_state"] = EGO_AUTH_FAILED
        q["login_likely_ok"] = False
        q["handoff_required"] = True
        q["hint"] = (
            f"检测到 {profile.name} 登录态失效（缺少关键 Cookie: {', '.join(missing) or 'auth'}），"
            f"请在浏览器打开 {profile.login_url} 人工登录后重试"
        )
    else:
        q["auth_state"] = EGO_AUTH_OK
        q["handoff_required"] = False
    return q


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

#!/usr/bin/env python3
"""
plan.py — 离线搜索计划（吸纳 yichen-unified-search 契约）

目标：
  - 不联网，只产出 status / authorization / steps / limitations
  - 分流 input_kind：keyword | url-seed | known-url
  - 与 route_query 对齐，供 deep/research 路径挂载元数据

执行分层（产品纪律，禁止日常「先确认再搜」）：
  - daily：fast/auto/budget + 非 deep depth → 直搜，不挂 plan，不等用户确认
  - professional：mode=deep 或 depth=deep → 直搜同时附加 plan 元数据 + 核验提示
  - deep_research：research.py → 先离线 plan 一次（非循环），再拆子查询采集

反死循环保证：
  - build_plan 永不 import/调用 super_search 或 deep_research
  - 仅依赖 route_query（纯决策，无网络）
  - 调用方最多 build_plan 一次，不得 plan→search→plan 递归

用法：
  python3 scripts/plan.py "检索词" --mode auto --json
  python3 scripts/plan.py "https://example.com/a" --input-kind known-url --json
  python3 scripts/plan.py "搜索引用 https://example.com/a 的报道" --input-kind url-seed --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Literal
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "ref", "fbclid", "gclid", "mc_cid", "mc_eid",
}

ExecutionTier = Literal["daily", "professional", "deep_research"]


def execution_tier(
    mode: str = "auto",
    depth: str = "fast",
    context: str = "search",
) -> ExecutionTier:
    """判定执行分层。

    context:
      - search：普通 argo_search / search.py
      - research：deep_research / argo_research
    """
    ctx = (context or "search").lower()
    if ctx in ("research", "deep_research", "argo_research"):
        return "deep_research"
    m = (mode or "auto").lower()
    d = (depth or "fast").lower()
    if m == "deep" or d == "deep":
        return "professional"
    return "daily"


def should_attach_plan(
    mode: str = "auto",
    depth: str = "fast",
    context: str = "search",
    plan_only: bool = False,
) -> bool:
    """是否在 super_search 响应中附加完整 plan 对象。

    - plan_only：显式只要计划 → True
    - daily：False（避免热路径噪音与「先确认」暗示）
    - professional（mode/depth=deep）：True（元数据，不阻断执行）
    - deep_research：False（由 research.py 顶层挂一次 plan，子查询不重复）
    """
    if plan_only:
        return True
    tier = execution_tier(mode, depth, context)
    return tier == "professional"


def requires_user_confirmation(
    mode: str = "auto",
    depth: str = "fast",
    context: str = "search",
    input_kind: str = "keyword",
    plan_status: str | None = None,
) -> bool:
    """日常搜索永不要求用户确认。

    仅下列情况可能为 True（且仍非交互阻塞，只是 status 提示）：
      - needs_authorization（显式站内登录敏感查询）
    known-url handoff 是工具分流，不是「请确认后搜索」。
    """
    if plan_status == "needs_authorization":
        return True
    # daily / professional / research 的 keyword 与 url-seed 均不弹确认
    _ = (mode, depth, context, input_kind)
    return False


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    return list(dict.fromkeys(_URL_RE.findall(text)))


def is_pure_url(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(re.fullmatch(r"https?://[^\s]+", t, flags=re.I))


def canonicalize_url(url: str) -> str:
    """去掉明确追踪参数；失败则原样返回。"""
    if not url:
        return url
    try:
        p = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), ""))
    except Exception:
        return url


def classify_input_kind(query: str, explicit: str = "auto") -> str:
    """keyword | url-seed | known-url。

    auto 规则（安全默认）：
      - 纯 URL → known-url（应 fetch/归档，不进多引擎热搜）
      - 含 URL 且含「搜索/引用/讨论/关联」等发现意图 → url-seed
      - 含 URL 其余 → known-url（安全 handoff）
      - 无 URL → keyword
    """
    if explicit and explicit != "auto":
        return explicit
    urls = extract_urls(query)
    if not urls:
        return "keyword"
    if is_pure_url(query):
        return "known-url"
    discover = re.search(
        r"(搜索|检索|引用|讨论|关联|提到|关于该|关于此|related|discuss|mention|site:)",
        query or "",
        re.I,
    )
    return "url-seed" if discover else "known-url"


def build_plan(
    query: str,
    mode: str = "auto",
    depth: str = "fast",
    max_results: int = 5,
    engine: str = "auto",
    input_kind: str = "auto",
    login_approved: bool = False,
    context: str = "search",
) -> dict[str, Any]:
    """离线计划。永不调用搜索后端；永不回调 super_search/research。"""
    kind = classify_input_kind(query, input_kind)
    urls = extract_urls(query)
    limitations: list[str] = []
    steps: list[dict[str, Any]] = []
    tier = execution_tier(mode, depth, context)

    # ── known-url：交接 fetch，不联网搜索 ──
    if kind == "known-url":
        target = urls[0] if urls else query.strip()
        return {
            "schema_version": "1.0",
            "status": "handoff_required",
            "authorization": "not_applicable",
            "input_kind": kind,
            "query": query,
            "execution_tier": tier,
            "requires_confirmation": False,
            "handoff": {
                "action": "fetch_or_archive",
                "url": target,
                "canonical_url": canonicalize_url(target),
                "suggested_tools": ["argo_fetch", "argo_pdf", "argo_extract"],
            },
            "route": None,
            "steps": [
                {
                    "action": "handoff_fetch",
                    "url": target,
                    "reason": "known-url is not a multi-engine search query",
                }
            ],
            "limitations": [
                "Known URL reading/download/archive is outside multi-engine search.",
                "Use argo_fetch / argo_pdf for content; use --input-kind url-seed only to discover related public pages.",
            ],
            "mode": mode,
            "depth": depth,
        }

    # ── url-seed：URL 仅作发现线索 ──
    search_query = query
    if kind == "url-seed":
        limitations.append(
            "URL is a discovery seed only; this plan does not read/download/archive the seed URL itself."
        )
        # 若用户未写 site: 且是纯发现句，可提示但默认保留原 query 交给引擎
        if urls and "site:" not in query.lower():
            host = urlparse(urls[0]).netloc
            if host:
                limitations.append(
                    f"Consider site-index discovery around host={host}; not equal to native full-site index."
                )

    # ── 社交/登录敏感词：仅声明授权需求，不阻断公共引擎 ──
    ql = (query or "").lower()
    needs_login = any(
        s in ql for s in ("小红书站内", "抖音站内", "chrome 登录", "登录态")
    )
    if needs_login and not login_approved:
        return {
            "schema_version": "1.0",
            "status": "needs_authorization",
            "authorization": "explicit_current_turn_login_required",
            "input_kind": kind,
            "query": query,
            "execution_tier": tier,
            "requires_confirmation": True,
            "route": {
                "platform": "social_native",
                "backend": "social_engines",
                "mode": mode,
                "reason": "native_login_gated_search",
                "login_state_used": True,
            },
            "steps": [],
            "limitations": [
                "State platform, exact query, and limit before requesting login authorization.",
                "One approval does not extend to other platforms or later turns.",
            ],
            "mode": mode,
            "depth": depth,
        }

    # ── 正常 keyword / url-seed：对齐 route_query（离线，无网络）──
    try:
        from route import route_query
        decision = route_query(
            search_query, engine_override=engine, mode=mode,
            depth=depth, context=context,
        )
    except Exception as e:
        return {
            "schema_version": "1.0",
            "status": "invalid_request",
            "authorization": "not_applicable",
            "input_kind": kind,
            "query": query,
            "execution_tier": tier,
            "requires_confirmation": False,
            "route": None,
            "steps": [],
            "limitations": [f"route_query failed offline: {type(e).__name__}"],
            "mode": mode,
            "depth": depth,
        }

    engines = decision.get("engines_combo") or decision.get("engines") or []
    primary = decision.get("engine") or (engines[0] if engines else "auto")
    domain = decision.get("domain")

    steps.append({
        "action": "route",
        "engine": primary,
        "engines_combo": engines,
        "domain": domain,
        "reason": decision.get("reason"),
    })
    steps.append({
        "action": "search",
        "query": search_query,
        "max_results": max_results,
        "mode": mode,
        "depth": depth,
        "engines": engines,
    })
    # 仅 professional / deep_research 追加核验步骤（日常不暗示「先确认」）
    if tier in ("professional", "deep_research"):
        steps.append({
            "action": "optional_verify_top_k",
            "k": min(3, max_results),
            "note": "snippet is clue only; fetch before hard claims",
        })
        if tier == "deep_research":
            steps.insert(0, {
                "action": "decompose_then_collect",
                "note": "research path: offline plan once → sub-queries → synthesize; no plan loop",
            })
            limitations.append(
                "Deep research: plan is offline metadata only; execution continues without user confirmation."
            )
    else:
        limitations.append(
            "Daily tier: direct search; plan attach skipped; no user confirmation gate."
        )

    # 常见局限
    if any(e.startswith("local_") for e in engines):
        limitations.append("Local engines parse public pages; may be blocked by CAPTCHA/anti-bot.")
    if "octen" in engines:
        limitations.append("octen requires OCTEN_API_KEY; missing key yields empty results for that engine.")
    if decision.get("parallel"):
        limitations.append("Parallel multi-engine; early-stop may skip later engines when primary is sufficient.")
    if max_results > 10:
        limitations.append("Some backends clamp max_results ≤10; request may be truncated per engine.")

    return {
        "schema_version": "1.0",
        "status": "ready",
        "authorization": "not_required",
        "input_kind": kind,
        "query": query,
        "search_query": search_query,
        "execution_tier": tier,
        "requires_confirmation": False,
        "route": {
            "platform": "web",
            "backend": primary,
            "engines_combo": engines,
            "domain": domain,
            "mode": mode,
            "depth": depth,
            "reason": decision.get("reason"),
            "login_state_used": False,
            "confidence": decision.get("confidence"),
            "parallel": decision.get("parallel"),
            "tfidf_scores": decision.get("tfidf_scores") or [],
        },
        "steps": steps,
        "limitations": limitations,
        "decision": {
            "engine": primary,
            "engines_combo": engines,
            "domain": domain,
            "elapsed_ms": decision.get("elapsed_ms"),
        },
        "mode": mode,
        "depth": depth,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Argo 离线搜索计划（不联网）")
    p.add_argument("query", help="查询词或 URL")
    p.add_argument("--mode", default="auto", choices=("fast", "auto", "deep", "budget"))
    p.add_argument("--depth", default="fast", choices=("fast", "balanced", "deep"))
    p.add_argument("--max-results", "-n", type=int, default=5)
    p.add_argument("--engine", "-e", default="auto")
    p.add_argument(
        "--input-kind",
        default="auto",
        choices=("auto", "keyword", "url-seed", "known-url"),
    )
    p.add_argument("--login-approved", action="store_true")
    p.add_argument(
        "--context",
        default="search",
        choices=("search", "research"),
        help="search=日常/专业分层；research=深度研究分层",
    )
    p.add_argument("--json", action="store_true", dest="json_output", default=True)
    args = p.parse_args()
    plan = build_plan(
        args.query,
        mode=args.mode,
        depth=args.depth,
        max_results=args.max_results,
        engine=args.engine,
        input_kind=args.input_kind,
        login_approved=args.login_approved,
        context=args.context,
    )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if plan.get("status") in ("ready", "handoff_required", "needs_authorization") else 1


if __name__ == "__main__":
    raise SystemExit(main())

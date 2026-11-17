#!/usr/bin/env python3
"""fetch_jina.py — r.jina.ai 阅读器层（fetch_v3 第一级C）。

从 fetch_v3.py 拆出（2026-11-17）：与 fetch_jsrun、fetch_render_tinyfish
同构，独立成模块让 fetch_v3 行数回到祖父文件上限内。本层只负责「请求 Jina
并解析出 markdown」，公网边界（is_public_host）由调用方保证。

定位：HTTP/TLS 直连全败后的免浏览器快速路径——jina 在远端渲染并返回
markdown，命中则免去 CDP 冷启动。有 Key 用 Key（ARGO_JINA_API_KEY），
无 Key 走免 Key 匿名档兜底。失败返回 None，由 fetch_v3 主链降级到
Parallel/Wayback/浏览器。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import engine_states
from engine_env import env_flag, get_env


def enabled() -> bool:
    """r.jina.ai 阅读器级开关：ARGO_FETCH_JINA=0 关闭，默认开启。"""
    return env_flag("ARGO_FETCH_JINA")


def fetch(url: str, max_chars: int = 8000, timeout: float = 8.0) -> dict | None:
    """r.jina.ai 阅读器（有 Key 用 Key，无 Key 走免 Key 兜底）。

    Key 档与免 Key 档的差异（2026-11-17 实测）：免 Key 匿名池按 IP 信誉
    间歇返回 401 AuthenticationRequiredError（bad IP reputation），同机
    4 次请求 3 次被拒；带 ARGO_JINA_API_KEY 稳定 200、500 RPM。故优先带
    Key，缺失时仍走匿名档兜底（有总比没有强，失败由上层降级）。
    """
    import fetch_v3  # 延迟导入：复用 _make_result / _looks_like_markdown，避免循环引用

    try:
        from http_client import HttpClient
        client = HttpClient(timeout=min(timeout, 10.0), max_retries=0,
                            jitter=False)
        headers = {
            "User-Agent": "argo-fetch-v3/1.0 (+local-research; jina-reader)",
            "Accept": "text/plain",
            # 显式声明返回格式，避免默认格式漂移（官方可选 markdown/html/text/...）
            "X-Respond-With": "markdown",
            # 远端渲染等待上限对齐本级超时（官方上限 180s），避免默认值白等
            "X-Timeout": str(int(min(max(timeout, 1.0), 180.0))),
            # 输出 token 预算：超预算会被 Jina 以 409 BudgetExceededError
            # 拒绝（2026-11-17 实测：8000 字符的 openai 页实际需 5806 token，
            # 比例约 0.73 token/字符，含 Title/URL/Published 等 frontmatter）。
            # 按 max_chars * 1.5 留余量，下限 1024 防短页被误拒。
            "X-Token-Budget": str(max(1024, int(max_chars * 1.5))),
        }
        api_key = get_env("ARGO_JINA_API_KEY").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = client.get(f"https://r.jina.ai/{url}", extra_headers=headers)
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
    if not fetch_v3._looks_like_markdown(raw):
        return None
    result = fetch_v3._make_result(url, "", max_chars,
                                   engine_states.FETCH_JINA_READER)
    result["content"] = raw[:max_chars]
    result["length"] = len(result["content"])
    result["title"] = title
    # 额度可见化：Reader 响应头带 x-ratelimit-*（RPM 档）与 x-usage-tokens
    # （本次消耗）。10M 免费 token 是共享钱包，落进结果便于观测消耗。
    resp_headers = {str(k).lower(): v
                    for k, v in (resp.get("headers") or {}).items()}
    quota = {k: v for k, v in resp_headers.items()
             if k.startswith(("x-ratelimit", "x-usage"))}
    if quota:
        result["jina_quota"] = quota
        remaining = resp_headers.get("x-ratelimit-remaining")
        if remaining and str(remaining).strip().isdigit() and int(remaining) < 50:
            result["jina_low_quota"] = True
    return result

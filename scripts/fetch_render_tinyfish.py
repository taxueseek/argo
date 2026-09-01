#!/usr/bin/env python3
"""fetch_render_tinyfish.py — TinyFish 直连渲染层（fetch_v3 第二级A）。

从 fetch_v3 拆出：该层自包含（直连 HTTP、不依赖 fetch_v3 内部状态），
独立成模块可让 fetch_v3 保持在主链编排职责上，避免文件继续膨胀。

定位：markdown-only 渲染。返回 clean markdown 作为 content，不产 raw html
——因此调用方若需要原始 HTML（爬取提取链接）必须显式跳过本层。
失败一律返回 success=False，由 fetch_v3 主链继续降级到 Wayback/浏览器。
"""

from __future__ import annotations

import json
import os
import urllib.request

# fetch_method 标识。fetch_v3 主链用它判断是否已命中渲染层
# （命中则不再冷启动本地 Chrome）。集中为常量避免拼写漂移。
TINYFISH_METHOD = "tinyfish"

_ENDPOINT = "https://api.fetch.tinyfish.ai"
_MAX_ERR_CHARS = 100


def _result(url: str, *, success: bool, error: str | None = None,
            content: str = "", title: str = "") -> dict:
    """统一的渲染层结果构造（消除重复的 dict 字面量）。"""
    return {
        "url": url,
        "content": content,
        "html": "",          # 本层只产 markdown，无 raw html
        "title": title,
        "length": len(content),
        "success": success,
        "error": error,
        "fetch_method": TINYFISH_METHOD,
    }


def enabled() -> bool:
    """渲染层开关：ARGO_FETCH_TINYFISH=0 关闭，默认开启。"""
    return os.environ.get("ARGO_FETCH_TINYFISH", "1").strip() not in (
        "0", "false", "False", "no")


def _api_key() -> str:
    """引擎密钥统一走 engine_env：os.environ 优先 + ~/.config/argo/env 热读兜底，
    与 search 引擎的 {TINYFISH_API_KEY} 占位符解析同一真源——否则会出现
    「search 能用、fetch 渲染层静默禁用」的分裂（密钥只写在 env 文件时）。"""
    try:
        from engine_env import get_env
        return get_env("TINYFISH_API_KEY")
    except ImportError:
        return os.environ.get("TINYFISH_API_KEY", "")


def fetch(url: str, max_chars: int = 8000, timeout: float = 8.0) -> dict:
    """TinyFish 免费渲染（直连 HTTP，无外部 CLI 依赖）。

    POST https://api.fetch.tinyfish.ai，认证用 X-API-Key
    （读 TINYFISH_API_KEY 环境变量）。
    key 缺失或端点失败时返回非 success，由上层继续走 Wayback/浏览器。
    """
    api_key = _api_key().strip()
    if not api_key:
        return _result(url, success=False,
                       error="TINYFISH_API_KEY not configured")

    body = json.dumps({"urls": [url], "format": "markdown"}).encode("utf-8")
    req = urllib.request.Request(
        _ENDPOINT,
        data=body,
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
    except Exception as e:
        return _result(url, success=False, error=str(e)[:_MAX_ERR_CHARS])

    results = data.get("results") or []
    # 真实 /fetch 返回形如 {"results":[...], "errors":[...]}；errors 非空时应
    # 优先透出，避免「取不到却报 empty」这种模糊错误掩盖真实原因。
    errors = data.get("errors") or []
    err_msg = ""
    if errors:
        e0 = errors[0]
        err_msg = (e0.get("message") or e0.get("error")
                   if isinstance(e0, dict) else str(e0))

    # 畸形 payload 守卫：results 为空或首项非 dict 时按失败处理（第三方返回
    # 结构不受我方控制，任何形状都不能让异常逃出 fetch() 的 success=False 契约）。
    item = results[0] if results and isinstance(results[0], dict) else {}
    if not item:
        return _result(url, success=False,
                       error=(err_msg or "tinyfish empty result")[:_MAX_ERR_CHARS])

    text = (item.get("text") or "").strip()
    if not text:
        return _result(url, success=False,
                       error=(err_msg or "tinyfish empty content")[:_MAX_ERR_CHARS])

    content = text[:max_chars]
    return _result(url, success=True, content=content,
                   title=item.get("title") or "")

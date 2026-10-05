#!/usr/bin/env python3
"""fetch_jsrun.py — fetch 降级链第一级E：V8 JS 执行（js-run 轻量车道）。

从 fetch_v3.py 拆出（2026-10-05）：jsrun 段独立成模块，fetch_v3.py
行数回到祖父文件上限内。职责：用 V8 沙箱执行「环境探测 + 纯计算」型
挑战页 JS，尝试获取 clearance cookie 后重试请求。失败时返回空结果，
由调用方降级到 tinyfish/CDP。
"""

from __future__ import annotations

import sys
from pathlib import Path

from engine_env import env_flag


def _jsrun_enabled() -> bool:
    """js-run 车道开关：ARGO_FETCH_JSRUN=0 关闭，默认开启。"""
    return env_flag("ARGO_FETCH_JSRUN")


def _jsrun_challenge_fetch(url: str, html: str, max_chars: int = 8000,
                           timeout: float = 8.0) -> dict:
    """用 js-run 执行挑战页 JS，尝试获取 clearance cookie 后重试请求。

    只处理「环境探测 + 纯计算」型挑战脚本（v0 面）。失败时返回空结果，
    由调用方降级到 tinyfish/CDP。

    重试请求用 curl_cffi Chrome 指纹 impersonate（站点按 TLS 指纹风控，
    裸 Python 指纹被静默拦截）。退避重试 3 次（5s/10s/15s）。
    """
    import re as _re
    import time as _time

    # 提取 <script> 内容（挑战页的通行证计算脚本）
    scripts = _re.findall(r'<script[^>]*>(.*?)</script>', html, _re.DOTALL | _re.IGNORECASE)
    if not scripts:
        return {}

    # 只取含环境探测/计算特征的脚本（过滤掉统计/广告等无关脚本）
    challenge_scripts = []
    for s in scripts:
        if _re.search(r'navigator|document\.cookie|btoa|atob|setTimeout|__INITIAL_STATE__|challenge', s, _re.IGNORECASE):
            challenge_scripts.append(s)
    if not challenge_scripts:
        return {}

    try:
        _skill_dir = Path(__file__).parent.parent / "sub-skills" / "js-run" / "scripts"
        if str(_skill_dir) not in sys.path:
            sys.path.insert(0, str(_skill_dir))
        from jsrun import JsRun
    except (ImportError, OSError):
        return {}

    try:
        with JsRun() as jr:
            for script in challenge_scripts:
                try:
                    jr.run(script, timeout_ms=3000)
                except Exception:
                    continue
            # 推进逻辑时间（挑战脚本常用 setTimeout 延迟发通行证）
            jr.advance(5000)
            cookie = jr.get_cookie()

        if not cookie or len(cookie) < 10:
            return {}

        # 用 clearance cookie 重试原请求（curl_cffi Chrome 指纹 + 退避重试）
        from curl_cffi import requests as _cr

        session = _cr.Session(impersonate="chrome")
        last_err = ""
        for attempt in range(1, 4):
            try:
                resp = session.get(
                    url,
                    headers={"Cookie": cookie},
                    timeout=timeout,
                )
                if resp.status_code == 200 and len(resp.text.strip()) >= 100:
                    return {
                        "url": url,
                        "content": resp.text[:max_chars],
                        "html": "",
                        "title": "",
                        "length": len(resp.text),
                        "success": True,
                        "error": None,
                        "fetch_method": "jsrun_challenge",
                        "jsrun_cookie": cookie[:100],
                    }
                last_err = f"HTTP {resp.status_code}, {len(resp.text)} 字节"
            except Exception as e:
                last_err = str(e)[:200]
            if attempt < 3:
                _time.sleep(5 * attempt)  # 5s, 10s
        return {}
    except Exception:
        pass
    return {}

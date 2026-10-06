#!/usr/bin/env python3
"""fetch_browser.py — fetch 降档链第二级B：Chrome CDP 浏览器档

从 fetch_v3 拆出（fetch_v3 在模块体量门禁的祖父清单里，只能减不能增；
浏览器档是自洽的一级，与 chrome_cdp / browser_auth 同域）。

职责：
  - 系统 Chrome（headless）渲染取页 + Hound 风格 actions 交互
  - 登录态车道（A2）：auth_profile 传入 browser_auth 持久 profile 时带会话
    抓取，结果标 login_state_used / cache_eligible=False——cache.assert_cacheable
    据此拒绝该载荷进公共缓存；cookie 由 Chrome 自持加密，argo 不读取
"""

from __future__ import annotations


def _fail(url: str, error: str) -> dict:
    """失败结果（与 fetch_v3._make_result 的空内容形状逐字段一致）。"""
    return {
        "url": url, "content": "", "html": "", "title": "",
        "length": 0, "success": False, "error": error,
        "fetch_method": "browser",
    }


def browser_fetch(url: str, max_chars: int = 8000, timeout: float = 15.0,
                  actions: list[dict] | None = None,
                  auth_profile: str | None = None) -> dict:
    """使用 Chrome CDP 驱动抓取（支持页面交互）。

    auth_profile：browser_auth 的持久 profile 路径。传入时 Chrome 带登录态
    启动（user_data_dir 指向持久目录；chrome_cdp 对持久 profile 不做清理）。
    """
    try:
        from chrome_cdp import ChromeCDP
    except ImportError:
        return _fail(url, "chrome_cdp not available")

    try:
        cdp = ChromeCDP(auto_start=True, user_data_dir=auth_profile)
    except Exception as e:
        err = f"Chrome failed to start: {str(e)[:100]}"
        if auth_profile:
            err += "（登录态 profile 启动失败：关掉占用它的 Chrome，或 argo auth logout 后重登）"
        return _fail(url, err)

    try:
        # 导航（真 networkidle：CDP Network 事件计数；SPA 永不 idle 时超时放行）
        cdp.navigate(url, wait_until="networkidle")

        # 执行页面交互序列（Hound actions 等价能力）
        if actions:
            cdp.execute_actions(actions)

        # 提取内容
        html = cdp.get_html()
        text = cdp.get_text()
        title = cdp.get_title()

        result = {
            "url": url,
            "content": text[:max_chars] if text else "",
            "html": html[:max_chars * 2] if html else "",
            "title": title or "",
            "length": len(text) if text else 0,
            "success": bool(text),
            "error": None if text else "empty content",
            "fetch_method": "chrome_cdp",
        }
        if auth_profile:
            # 登录态 provenance：与 ego-browser 通道的 envelope 语义同源
            # （candidate_envelope：login_state_used → visibility=authenticated）
            result["login_state_used"] = True
            result["cache_eligible"] = False
            try:
                from browser_auth import touch
                touch(url)
            except Exception:
                pass
        return result
    except Exception as e:
        return _fail(url, f"CDP error: {str(e)[:100]}")
    finally:
        try:
            cdp.stop()
        except Exception:
            pass

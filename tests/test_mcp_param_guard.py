#!/usr/bin/env python3
"""test_mcp_param_guard.py — 数值参数类型校验：非法类型显式 -32602。

## 背景（2026-10-02 审计）

timeout 系列此前是裸 `int(arguments.get("timeout", N))`，传 "10s" 直接
ValueError，被 execute_tool 兜底 except 包装成 -32000 内部错误——调用方
看不出是参数给错了。与 _required 缺参 -32602 同一契约：参数级错误走
参数级错误码，不与引擎内部故障混在一起。
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from mcp_handlers import execute_tool  # noqa: E402


def _error_code(resp):
    """统一取错误码：协议级取 error.code；工具级取 content[0].text 内嵌的。"""
    if not isinstance(resp, dict):
        return None
    if isinstance(resp.get("error"), dict):
        return resp["error"].get("code")
    try:
        return (json.loads(resp["content"][0]["text"]).get("error") or {}).get("code")
    except Exception:
        return None


class TestNumericParamTypes:
    def test_timeout_string_gets_param_error(self):
        resp = execute_tool("argo_search", {"query": "x", "timeout": "10s"})
        assert _error_code(resp) == -32602, (
            f"非法 timeout 应报 -32602，得到：{str(resp)[:200]}")
        text = resp["content"][0]["text"]
        assert "timeout" in text, f"错误消息应点名参数：{text[:200]}"

    def test_bad_type_fails_fast_before_execution(self):
        """校验在干活之前：坏 timeout 不应真的发起抓取/搜索。"""
        resp = execute_tool("argo_fetch", {"url": "https://example.com/",
                                           "timeout": "soon"})
        assert _error_code(resp) == -32602

    def test_all_timeout_tools_reject_bad_type(self):
        # 只收真正读 arguments.timeout 的工具（schema 有无该键是另一回事：
        # crawl 读 timeout 但 schema 未声明——向后兼容的历史参数）。
        # screenshot/pdf 不读 timeout，多余键按忽略处理，不是校验对象。
        cases = (
            ("argo_crawl", {"url": "https://x", "timeout": "soon"}),
            ("argo_fetch", {"url": "https://x", "timeout": "soon"}),
            ("argo_article", {"url": "https://x", "timeout": "soon"}),
        )
        for tool, args in cases:
            resp = execute_tool(tool, args)
            assert _error_code(resp) == -32602, f"{tool}: {str(resp)[:150]}"

    def test_valid_int_still_passes_required_guard_order(self):
        """合法 timeout + 缺 query：先命中的是缺参 -32602（守卫次序不变）。"""
        resp = execute_tool("argo_search", {"timeout": 10})
        text = resp["content"][0]["text"]
        assert "Missing required parameter(s): query" in text

    def test_max_chars_string_gets_param_error(self):
        """max_chars 各处收口（2026-10-03 补齐）：坏类型报参数错误，不再掉进
        各分支的兜底 except——argo_fetch 的裸值会流进 fetch 函数炸成
        TypeError→-32000，argo_local_read 会包成 -32000 内部错误。"""
        cases = (
            ("argo_fetch", {"url": "https://example.com/", "max_chars": "8k"}),
            ("argo_local_read", {"path": "/tmp/x.txt", "max_chars": "8k"}),
        )
        for tool, args in cases:
            resp = execute_tool(tool, args)
            code = _error_code(resp)
            assert code == -32602, f"{tool}: 应 -32602，得到 {code}：{str(resp)[:150]}"
            text = resp["content"][0]["text"]
            assert "max_chars" in text, f"{tool}: 错误消息应点名参数：{text[:200]}"

    def test_article_max_chars_guard_after_stubbed_fetch(self):
        """argo_article 的 max_chars 校验在抓取成功之后（其 URL 白名单先拦）——
        打桩 fetch_article 免网络直达校验点。"""
        import article
        from unittest.mock import patch

        with patch.object(article, "fetch_article",
                          return_value={"ok": True, "content": "x" * 500}):
            resp = execute_tool("argo_article", {
                "url": "https://mp.weixin.qq.com/s/abc", "max_chars": "8k"})
        assert _error_code(resp) == -32602, str(resp)[:200]
        text = resp["content"][0]["text"]
        assert "max_chars" in text, text[:200]

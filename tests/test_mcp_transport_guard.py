#!/usr/bin/env python3
"""test_mcp_transport_guard.py — tools/call 入口对畸形 arguments 的守卫。

## 背景（2026-10-02 实测）

宿主发 `"arguments": null`（或字符串）时，旧实现把值原样传给
execute_tool，mcp_handlers 顶层 `arguments.get("pretty", ...)` 直接
AttributeError，逃到 run_stdio 的兜底 except——响应变成：

    {"jsonrpc":"2.0","id":null,
     "error":{"code":-32000,
              "message":"Internal error: 'NoneType' object has no
                        attribute 'get'"}}

两个问题：① 错误类型错了（应是 -32602 参数级，不是 -32000 内部错误）；
② id 从 7 变 null，宿主无法对账是哪个请求失败。MCP 规范里 arguments
是 object，畸形时按空对象处理即可。
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import mcp_transport  # noqa: E402


def _error_code(resp):
    """统一取错误码：协议级取 error.code；工具级（isError + content[0].text
    内嵌 JSON）取内嵌 error.code。都不是则返回 None。"""
    if not isinstance(resp, dict):
        return None
    if isinstance(resp.get("error"), dict):
        return resp["error"].get("code")
    try:
        payload = json.loads(resp["content"][0]["text"])
        return (payload.get("error") or {}).get("code")
    except Exception:
        return None


class TestToolsCallArgumentsGuard:
    def test_arguments_null_returns_param_error(self):
        resp = mcp_transport.handle_rpc(
            "tools/call", {"name": "argo_search", "arguments": None})
        assert _error_code(resp) == -32602, (
            f"应按空对象走缺参 -32602，得到：{resp!r}")

    def test_arguments_non_dict_treated_as_empty(self):
        for bad in ("oops", 42, ["a"]):
            resp = mcp_transport.handle_rpc(
                "tools/call", {"name": "argo_search", "arguments": bad})
            assert _error_code(resp) == -32602, (
                f"arguments={bad!r} 应按空对象处理：{resp!r}")

    def test_missing_arguments_key_still_works(self):
        resp = mcp_transport.handle_rpc("tools/call", {"name": "argo_search"})
        assert _error_code(resp) == -32602

    def test_valid_arguments_still_execute(self):
        """守卫不得误伤正常调用：带合法 query 的请求不走缺参错误。"""
        resp = mcp_transport.handle_rpc(
            "tools/call", {"name": "argo_unknown_tool_xyz",
                           "arguments": {"query": "x"}})
        # 未知工具走工具级 -32601（Unknown tool），说明参数已通过入口守卫
        assert _error_code(resp) == -32601, resp


class TestEofHardExit:
    """EOF 后必须硬退：在途引擎线程（社会搜并行池，非 daemon）会让解释器
    退出时的 atexit join 拖到引擎内部超时（实测最长 ~15s），宿主侧表现为
    「会话已关、argo 进程残留」。run_stdio 退出点直接 os._exit(0)——
    响应已全部 flush，盘上状态（quota/缓存/熔断）都是处理期间同步落盘的。
    """

    def test_eof_hard_exits_despite_live_worker(self, monkeypatch):
        import io
        import os as _os
        import sys
        import threading

        exited = []
        monkeypatch.setattr(_os, "_exit", lambda code=0: exited.append(code))

        # 模拟一个仍在跑的非 daemon 线程（在途引擎调用）
        gate = threading.Event()
        worker = threading.Thread(target=gate.wait)
        worker.start()

        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
        mcp_transport.run_stdio()  # 旧实现：正常 return，exited 保持为空

        gate.set()
        worker.join()
        assert exited == [0], "EOF 后未硬退——在途线程会拖住进程退出最长 ~15s"


if __name__ == "__main__":
    import unittest  # noqa: F401  — pytest 收集，保留 main 以便单独直跑

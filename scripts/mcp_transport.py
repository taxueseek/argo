#!/usr/bin/env python3
"""mcp_transport.py — MCP JSON-RPC 分发 + stdio 帧协议（P2-4 拆分自 mcp_server.py）。

handle_rpc 按方法分发（initialize / tools/list / tools/call / ping）；
run_stdio 支持 Content-Length 帧与 NDJSON 两种输入格式。
工具 schema 在 mcp_tools.py，执行逻辑在 mcp_handlers.py。
"""

from __future__ import annotations

import json
import sys

from mcp_handlers import _warm_core_async, execute_tool
from mcp_tools import TOOLS

_response_format = "content-length"  # 根据客户端请求自动切换


def handle_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """处理 JSON-RPC 请求。"""
    if method == "initialize":
        _warm_core_async()
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "argo",
                "version": "2.7.1"
            },
            # 短指令：降 tools 上下文；细节在 tool schema
            "instructions": (
                "Argo：日常用 argo_search（默认精简 JSON，信源在 sources）；"
                "深度研究只用 argo_research（内建 academic/finance topic，不调用外部 skill）；"
                "核验 argo_evidence；消歧 argo_clarify；正文 argo_fetch（mode=extract 可提取表格/元数据/JSON-LD）。"
                "社交用 argo_social_search（mode=sentiment 做舆情聚合）。缓存+RRF+成本路由已内建。"
                "本地文件/记录搜索用 argo_local_search（搜本机，非联网，与 argo_search 互补）。"
            ),
        }

    elif method == "tools/list":
        _warm_core_async()
        return {"tools": TOOLS}

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        return execute_tool(tool_name, arguments)

    elif method == "ping":
        return {}

    elif method.startswith("notifications/"):
        # 通知消息无需回复
        return None

    else:
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def run_stdio():
    """运行 MCP stdio 服务。MCP 帧协议：Content-Length: N\\r\\n\\r\\n{json}"""
    import sys, os, time as _time

    global _response_format

    sys.stderr.write("[argo-mcp] ready, waiting for stdin\n")
    sys.stderr.flush()

    while True:
        try:
            # 读取 Content-Length 头
            header = sys.stdin.buffer.readline()
            if not header:
                sys.stderr.write("[argo-mcp] EOF on stdin, exiting\n")
                sys.stderr.flush()
                break  # EOF
            header_str = header.decode("utf-8", errors="replace").strip()
            if not header_str:
                continue
            if not header_str.startswith("Content-Length:"):
                # NDJSON 格式（Kimix 等）
                _response_format = "ndjson"
                try:
                    request = json.loads(header_str)
                except json.JSONDecodeError:
                    _send_error(None, -32700, "Parse error")
                    continue
            else:
                _response_format = "content-length"
                length = int(header_str.split(":")[1].strip())
                sys.stdin.buffer.readline()  # skip blank line
                body = sys.stdin.buffer.read(length).decode("utf-8")
                request = json.loads(body)

            method = request.get("method", "")
            params = request.get("params", {})
            request_id = request.get("id")

            response = handle_rpc(method, params)

            # 通知消息无需回复
            if response is None:
                continue

            if request_id is not None:
                _send_response({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": response
                })

        except json.JSONDecodeError:
            _send_error(None, -32700, "Parse error")
        except Exception as e:
            _send_error(None, -32000, f"Internal error: {e}")


def _send_response(response: dict):
    """发送 MCP 响应，根据客户端请求格式自动选择。紧凑 separators 省传输体积。"""
    data = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    if _response_format == "ndjson":
        sys.stdout.write(data + "\n")
    else:
        encoded = data.encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded)
    sys.stdout.flush()


def _send_error(request_id, code: int, message: str):
    """发送 JSON-RPC 错误响应。"""
    resp = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message}
    }
    _send_response(resp)


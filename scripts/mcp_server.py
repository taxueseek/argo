#!/usr/bin/env python3
"""
mcp_server.py — unified-search MCP 服务层

将 research/evidence/clarify 三个工具暴露为 MCP tool，
通过 JSON-RPC over stdio 与 Grok/Claude 等客户端通信。

用法：
  python3 mcp_server.py                    # 启动 MCP stdio 服务
  python3 mcp_server.py --test             # 本地测试模式
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)


# ── 工具定义（MCP schema） ────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "unified_research",
        "description": "深度研究：将复杂查询分解为子问题，多源并行采集，输出综合报告+引用+知识缺口。适用于学术综述、事实核查、竞品分析、技术选型等需要多步搜索的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "研究查询（可以是复杂的、多步骤的问题）"
                },
                "num_sub_queries": {
                    "type": "integer",
                    "description": "子查询数量（默认4，最大8）",
                    "default": 4,
                    "minimum": 2,
                    "maximum": 8
                },
                "max_results": {
                    "type": "integer",
                    "description": "每个子查询最大结果数（默认5）",
                    "default": 5
                },
                "depth": {
                    "type": "string",
                    "enum": ["fast", "balanced", "deep"],
                    "description": "搜索深度（默认balanced）",
                    "default": "balanced"
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "auto", "deep", "budget"],
                    "description": "预算模式（默认auto）",
                    "default": "auto"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "unified_evidence",
        "description": "来源可信度评估：对搜索结果进行权威性+时效性+交叉验证的综合评分，输出每个结果的可信度分解。适用于事实核查、高后果决策、学术引用等需要评估来源可靠性的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询词（用于交叉验证）"
                },
                "results_json": {
                    "type": "string",
                    "description": "搜索结果 JSON 字符串（格式：{\"results\": [{\"title\": \"...\", \"url\": \"...\", \"snippet\": \"...\", \"source\": \"...\", \"score\": 0.8}]}）"
                }
            },
            "required": ["query", "results_json"]
        }
    },
    {
        "name": "unified_clarify",
        "description": "意图消歧：分析查询中的歧义词、多义实体，给出意图分类和推荐搜索策略。适用于查询含歧义词（如「苹果」=公司/水果、「Python」=语言/蛇）或意图不明确的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "需要消歧的搜索查询"
                }
            },
            "required": ["query"]
        }
    },
]


# ── 工具执行 ──────────────────────────────────────────────────────────────────

def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行 MCP 工具。"""
    try:
        if name == "unified_research":
            from research import deep_research
            result = deep_research(
                query=arguments["query"],
                num_sub_queries=arguments.get("num_sub_queries", 4),
                max_results=arguments.get("max_results", 5),
                depth=arguments.get("depth", "balanced"),
                mode=arguments.get("mode", "auto"),
            )
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        elif name == "unified_evidence":
            from evidence import compute_credibility
            results_data = json.loads(arguments["results_json"])
            results = results_data.get("results", [])
            result = compute_credibility(results, arguments["query"])
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        elif name == "unified_clarify":
            from clarify import analyze_query, recommend_routing
            analysis = analyze_query(arguments["query"])
            routing = recommend_routing(analysis)
            analysis["routing"] = routing
            return {"content": [{"type": "text", "text": json.dumps(analysis, ensure_ascii=False, indent=2)}]}

        else:
            return {"error": {"code": -32601, "message": f"Unknown tool: {name}"}}

    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
            "isError": True
        }


# ── MCP JSON-RPC 处理 ────────────────────────────────────────────────────────

def handle_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """处理 JSON-RPC 请求。"""
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "unified-search",
                "version": "2.0.0"
            },
            "instructions": "unified-search MCP 提供 3 个工具：unified_research（深度研究）、unified_evidence（可信度评估）、unified_clarify（意图消歧）。底层使用 47 个搜索引擎的统一搜索基础设施。"
        }

    elif method == "tools/list":
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
    import sys
    while True:
        try:
            # 读取 Content-Length 头
            header = sys.stdin.buffer.readline()
            if not header:
                break  # EOF
            header_str = header.decode("utf-8", errors="replace").strip()
            if not header_str:
                continue
            if not header_str.startswith("Content-Length:"):
                # 兼容行模式（某些客户端不发 Content-Length）
                try:
                    request = json.loads(header_str)
                except json.JSONDecodeError:
                    _send_error(None, -32700, "Parse error")
                    continue
            else:
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
                response["jsonrpc"] = "2.0"
                response["id"] = request_id
                _send_response(response)

        except json.JSONDecodeError:
            _send_error(None, -32700, "Parse error")
        except Exception as e:
            _send_error(None, -32000, f"Internal error: {e}")


def _send_response(response: dict):
    """发送 MCP 帧响应。"""
    data = json.dumps(response, ensure_ascii=False)
    encoded = data.encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode())
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _send_error(request_id, code: int, message: str):
    """发送 JSON-RPC 错误响应。"""
    resp = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message}
    }
    _send_response(resp)


# ── 测试模式 ──────────────────────────────────────────────────────────────────

def test_mode():
    """本地测试。"""
    print("=== unified-search MCP 工具测试 ===\n")

    # 测试 clarify
    print("--- clarify 测试 ---")
    result = execute_tool("unified_clarify", {"query": "Python 吞苹果 兼容吗"})
    print(result["content"][0]["text"][:500])
    print()

    # 测试 research（快速模式）
    print("--- research 测试（fast模式）---")
    result = execute_tool("unified_research", {
        "query": "React Server Components 2025 生产环境案例",
        "num_sub_queries": 2,
        "max_results": 3,
        "depth": "fast",
        "mode": "fast",
    })
    text = result["content"][0]["text"]
    # 只打印前 500 字符
    print(text[:500])
    print()

    # 测试 evidence
    print("--- evidence 测试 ---")
    sample_results = json.dumps({
        "results": [
            {"title": "Python docs", "url": "https://docs.python.org", "snippet": "Official Python documentation", "source": "wikipedia", "score": 0.9},
            {"title": "Some blog", "url": "https://random-blog.com/python", "snippet": "Python tutorial", "source": "duckduckgo", "score": 0.6},
        ]
    })
    result = execute_tool("unified_evidence", {"query": "Python tutorial", "results_json": sample_results})
    print(result["content"][0]["text"][:500])


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--test" in sys.argv:
        test_mode()
    else:
        run_stdio()

#!/usr/bin/env python3
"""
mcp_server.py — Argo MCP 服务入口（P2-4 拆分后为聚合薄壳）

原 970 行单文件已拆为三模块，本文件只做聚合与兼容导出：
  mcp_tools.py      工具 schema 唯一真源（10 个工具）
  mcp_handlers.py   路径引导 + 延迟导入 + 结果压缩 + execute_tool + 预热
  mcp_transport.py  JSON-RPC 分发（handle_rpc）+ stdio 帧协议（run_stdio）

对外行为不变：`python3 mcp_server.py` 启动 stdio 服务，
`--test` 走本地测试；`from mcp_server import TOOLS/execute_tool/handle_rpc`
等历史 import 面照常可用。

用法：
  python3 mcp_server.py                    # 启动 MCP stdio 服务
  python3 mcp_server.py --test             # 本地测试模式
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: F401  — 兼容面：测试 monkeypatch mcp_server.subprocess.run
import sys

from mcp_handlers import (
    _module_cache,
    _compact_research_result,
    _compact_search_result,
    _dumps,
    _resolve_research_profile,
    _search_social_platforms,
    execute_tool,
)
from mcp_tools import TOOLS
from mcp_transport import handle_rpc, run_stdio

__all__ = [
    "TOOLS",
    "execute_tool",
    "handle_rpc",
    "run_stdio",
    "_module_cache",
    "_compact_search_result",
    "_compact_research_result",
    "_dumps",
    "_resolve_research_profile",
    "_search_social_platforms",
]


def test_mode():
    """本地测试。"""
    print("=== Argo MCP 工具测试 ===\n")

    # 测试 search
    print("--- argo_search 测试（fast模式）---")
    result = execute_tool("argo_search", {
        "query": "Python async best practices",
        "max_results": 3,
        "depth": "fast",
        "mode": "fast",
    })
    print(result["content"][0]["text"][:500])
    print()

    # 测试 local search（本地文件）
    print("--- argo_local_search 测试 ---")
    result = execute_tool("argo_local_search", {
        "query": "数据抓取",
        "path": os.path.expanduser("~/.agents/skills"),
        "max_results": 3,
    })
    print(result["content"][0]["text"][:500])
    print()

    # 测试 clarify
    print("--- clarify 测试 ---")
    result = execute_tool("argo_clarify", {"query": "Python 吞苹果 兼容吗"})
    print(result["content"][0]["text"][:500])
    print()

    # 测试 research（快速模式）
    print("--- research 测试（fast模式）---")
    result = execute_tool("argo_research", {
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
    result = execute_tool("argo_evidence", {"query": "Python tutorial", "results_json": sample_results})
    print(result["content"][0]["text"][:500])


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--test" in sys.argv:
        test_mode()
    else:
        run_stdio()

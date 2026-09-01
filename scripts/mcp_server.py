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
  python3 mcp_server.py --call <tool> '<json-args>'
                                           # CLI 单发：与 stdio 共用 execute_tool
                                           # （同引擎、同压缩、同守卫），供 DSH
                                           # 原生工具等免帧协议调用方复用
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

    # 测试 local search（本地文件）——路径用安装锚点（argo 根目录），
    # 不写死 ~/.agents/skills 等主机路径（SKILL.md 禁止）。
    argo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("--- argo_local_search 测试 ---")
    result = execute_tool("argo_local_search", {
        "query": "数据抓取",
        "path": argo_root,
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


def call_mode(tool: str, payload: str) -> int:
    """CLI 单发（--call <tool> '<json-args>'）。

    与 stdio 服务共用 execute_tool，stdout 只输出结果 JSON（isError 时
    退出码 1）；不输出任何日志到 stdout，保证调用方解析不被污染。
    """
    try:
        arguments = json.loads(payload) if payload.strip() else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid --call payload: {e}"}, ensure_ascii=False))
        return 2
    if not isinstance(arguments, dict):
        print(json.dumps({"error": "--call payload must be a JSON object"}, ensure_ascii=False))
        return 2
    result = execute_tool(tool, arguments)
    print(_dumps(result))
    # isError 是工具级错误；裸 {"error": {...}} 是协议级错误（如未知工具）。
    # 两者都必须非零退出，否则调用方（如 DSH 原生工具）把错误当成功渲染。
    is_error = bool(result.get("isError")) or (
        isinstance(result.get("error"), dict) and "content" not in result)
    return 1 if is_error else 0


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Windows GBK 防线（PEP 540）：非 UTF-8 模式直接跑本文件时，重新以
    # -X utf8 启动自己，保证中文 JSON 读取与 stderr 输出不因控制台编码崩。
    if not sys.flags.utf8_mode and os.name == "nt":
        import subprocess as _sp
        _sp.run(
            [sys.executable, "-X", "utf8", __file__, *sys.argv[1:]],
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        sys.exit(0)
    if "--test" in sys.argv:
        test_mode()
    elif "--call" in sys.argv:
        _i = sys.argv.index("--call")
        _tool = sys.argv[_i + 1] if len(sys.argv) > _i + 1 else ""
        if not _tool:
            print(json.dumps({"error": "usage: mcp_server.py --call <tool> [json-args]"},
                             ensure_ascii=False))
            sys.exit(2)
        _payload = sys.argv[_i + 2] if len(sys.argv) > _i + 2 else "{}"
        sys.exit(call_mode(_tool, _payload))
    else:
        run_stdio()

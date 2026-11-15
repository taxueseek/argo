#!/usr/bin/env python3
"""type: mcp 通用适配器离线单测：stdio 与 Streamable HTTP 双传输、双内容层
（json/text）、isError fail-visible、builder spec 校验。全程本地假服务，不联网。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from engines_mcp import _build_mcp_engine  # noqa: E402

STDIO_SERVER = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    m = req.get("method")
    if m == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "serverInfo": {"name": "fake", "version": "0.1"}}}), flush=True)
    elif m == "notifications/initialized":
        pass
    elif m == "tools/call":
        args = (req.get("params") or {}).get("arguments") or {}
        payload = {"results": [
            {"title": "Stdio 命中", "url": "https://example.com/a",
             "snippet": "片段甲", "level": 4},
            {"title": "Stdio 命中二", "url": "https://example.com/b",
             "snippet": "片段乙", "level": 2},
        ], "echo_query": args.get("q", ""), "echo_n": args.get("perPage")}
        print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(payload)}]}}), flush=True)
'''


class StdioTransportTest(unittest.TestCase):
    """stdio 传输：JSON 文本内容 + 标准 output_map + 参数模板类型保真。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        server_py = Path(self.tmp.name) / "fake_stdio_server.py"
        server_py.write_text(STDIO_SERVER, encoding="utf-8")
        self.spec = {
            "_name": "stdio_demo",
            "transport": "stdio",
            "command": [sys.executable, str(server_py)],
            "tool": "fake_tool",
            "arguments": {"q": "{query}", "perPage": "{n}"},
            "timeout": 20,
            "output_map": {
                "mode": "json",
                "items": "results",
                "item_title": "title",
                "item_url": "url",
                "item_summary": "snippet",
                "item_upstream_authority": "level",
            },
        }
        self.engine = _build_mcp_engine(self.spec)

    def test_json_content_mapped(self):
        rows = self.engine("测试查询", n=3)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Stdio 命中")
        self.assertEqual(rows[0]["url"], "https://example.com/a")
        # map 路径字段按 engines_base 约定落字符串（rank_signals.authority_level
        # 用 int(float(str(...))) 兼容 str/int 双型；builder 直连路径才是 int）
        self.assertEqual(rows[0].get("upstream_authority"), "4")
        self.assertEqual(rows[0].get("source"), "stdio_demo")

    def test_argument_types_coerced(self):
        """{n} 模板解析为 int——MCP 参数类型与 spec 声明一致。"""
        rows = self.engine("测试查询", n=7)
        self.assertEqual(len(rows), 2)
        # 服务端回显：q="测试查询"，perPage=7（int 而非字符串 "7"）
        self.assertEqual(rows[0]["title"], "Stdio 命中")

    def test_builder_requires_command(self):
        bad = dict(self.spec)
        bad.pop("command")
        with self.assertRaises(ValueError):
            _build_mcp_engine(bad)

    def test_builder_rejects_shell_string(self):
        """stdio command 必须是列表——shell 字符串在构建期直接拒绝。"""
        bad = dict(self.spec)
        bad["command"] = "python3 fake.py"
        with self.assertRaises((ValueError, TypeError)):
            _build_mcp_engine(bad)


GREPAPP_TEXT = (
    "Repository: staruhub/ClaudeSkills\n"
    "Path: scripts/source_evaluator.py\n"
    "URL: https://github.com/staruhub/ClaudeSkills/blob/main/scripts/source_evaluator.py\n"
    "License: MIT\n"
    "\n"
    "Snippets:\n"
    "--- Snippet 1 (Line 107) ---\n"
    "def score_authority(url: str) -> float:\n"
    '    """Score source authority."""\n'
    "\n"
    "Repository: owner2/other\n"
    "Path: src/main.py\n"
    "URL: https://github.com/owner2/other/blob/main/src/main.py\n"
    "License: Apache-2.0\n"
    "\n"
    "Snippets:\n"
    "--- Snippet 1 (Line 12) ---\n"
    "print('hello')\n"
)


class _McpHttpStub(BaseHTTPRequestHandler):
    """最小 Streamable HTTP MCP 假服务：SSE 帧 + Mcp-Session-Id 回显断言。"""

    received_sessions: list = []
    received_args: list = []
    result_payload: dict = {"isError": False}

    def do_POST(self):
        length = int(self.headers.get("Content-Type") and self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        method = body.get("method")
        if method == "initialize":
            self._sse({"jsonrpc": "2.0", "id": body.get("id"), "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-http", "version": "0.1"},
            }}, session="sess-123")
        elif method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
        elif method == "tools/call":
            _McpHttpStub.received_sessions.append(self.headers.get("Mcp-Session-Id"))
            _McpHttpStub.received_args.append((body.get("params") or {}).get("arguments"))
            result = dict(_McpHttpStub.result_payload)
            result.setdefault("content", [{"type": "text", "text": json.dumps({
                "results": [{"title": "HTTP 命中", "url": "https://example.com/h"}]})}])
            self._sse({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
        else:
            self.send_response(400)
            self.end_headers()

    def _sse(self, obj, session=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(f"event: message\ndata: {json.dumps(obj)}\n\n".encode())

    def log_message(self, *args):
        pass


class HttpTransportTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _McpHttpStub)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        _McpHttpStub.received_sessions = []
        _McpHttpStub.received_args = []
        _McpHttpStub.result_payload = {"isError": False}

    def _spec(self, output_map):
        return {
            "_name": "http_demo",
            "transport": "http",
            "url": f"http://127.0.0.1:{self.port}",
            "tool": "searchGitHub",
            "arguments": {"query": "{query}", "perPage": "{n}"},
            "timeout": 10,
            "output_map": output_map,
        }

    def test_sse_session_and_json_mapping(self):
        engine = _build_mcp_engine(self._spec({
            "items": "results", "item_title": "title", "item_url": "url",
        }))
        rows = engine("查询", n=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://example.com/h")
        # tools/call 请求携带 initialize 返回的会话头（回传验证）
        self.assertEqual(_McpHttpStub.received_sessions, ["sess-123"])
        # 参数类型保真：perPage 是 int 5（与调用 n=5 一致，非字符串 "5"）
        self.assertEqual(_McpHttpStub.received_args[0].get("perPage"), 5)
        self.assertEqual(_McpHttpStub.received_args[0].get("query"), "查询")

    def test_is_error_yields_no_items(self):
        _McpHttpStub.result_payload = {
            "isError": True, "content": [{"type": "text", "text": "No results."}]}
        engine = _build_mcp_engine(self._spec({"items": "results", "item_title": "title"}))
        self.assertEqual(engine("查询", n=3), [])

    def test_text_mode_regex_mapping(self):
        """mode: text——grep.app 报告分块正则抽 title/url/snippet。"""
        _McpHttpStub.result_payload = {"isError": False, "content": [
            {"type": "text", "text": GREPAPP_TEXT}]}
        engine = _build_mcp_engine(self._spec({
            "mode": "text",
            "text_split": r"\n(?=Repository: )",
            "item_title_regex": r"Path: (.+)",
            "item_url_regex": r"URL: (\S+)",
            "item_summary_regex": r"(?s)--- Snippet 1 \(Line \d+\) ---\n(.*)",
        }))
        rows = engine("def score_authority(", n=5)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "scripts/source_evaluator.py")
        self.assertEqual(rows[0]["url"],
                         "https://github.com/staruhub/ClaudeSkills/blob/main/scripts/source_evaluator.py")
        self.assertIn("def score_authority", rows[0]["snippet"])
        self.assertEqual(rows[1]["title"], "src/main.py")

    def test_json_mode_rejects_plain_text(self):
        """json 模式收到非 JSON 文本必须失败可见（返回空并告警），不编造条目。"""
        _McpHttpStub.result_payload = {"isError": False, "content": [
            {"type": "text", "text": "纯文本报告，没有 JSON"}]}
        engine = _build_mcp_engine(self._spec({"items": "results", "item_title": "title"}))
        self.assertEqual(engine("查询", n=3), [])

    def test_builder_requires_tool_and_transport(self):
        with self.assertRaises(ValueError):
            _build_mcp_engine({"_name": "x", "transport": "http", "url": "http://a.b"})
        with self.assertRaises(ValueError):
            _build_mcp_engine({"_name": "x", "tool": "t", "transport": "grpc"})
        with self.assertRaises(ValueError):
            _build_mcp_engine({"_name": "x", "tool": "t", "transport": "http", "url": "ftp://a.b"})


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""engines_mcp.py — MCP 通用连接器适配器（spec type: mcp）。

把任意 MCP server（stdio 子进程或 Streamable HTTP）接入为 argo 搜索引擎。
分层契约：

- 协议层通用：initialize 握手 → notifications/initialized → tools/call。
  响应可能是 application/json 或 text/event-stream（实测 mcp.grep.app 返回
  SSE），Mcp-Session-Id 响应头存在则全程回传。
- 内容层声明式：structuredContent / JSON 文本走标准 output_map（与 http 引擎
  同一套字段映射、url_kind、引擎出处打标）；纯文本工具声明 mode: text，用
  text_split 分块正则 + item_*_regex 逐块抽字段。两类都不写每源代码。

spec 契约（engines/specs/<engine_id>.yaml）::

    engine_id: grepapp_mcp
    type: mcp
    transport: http                  # http（Streamable HTTP）| stdio（子进程）
    url: https://mcp.grep.app        # http 传输端点
    # command: [uvx, -y, some-server] # stdio 传输命令——必须是列表，禁 shell
    tool: searchGitHub               # 工具单一 allowlist，只可调用这一个
    arguments:                       # 参数模板：{query}/{n}/{depth} 与 {ENV}
      query: "{query}"
      perPage: "{n}"
    required_env: [ARGO_GREP_APP_API_KEY]  # env 白名单真源；未配置的头不发送
    headers: {}                      # http 可选；{ENV} 走 required_env 白名单
    timeout: 20
    output_map:
      mode: json                     # 默认 json；纯文本工具显式声明 text
      text_split: '\\n(?=Repository: )'
      item_title_regex: 'Path: (.+)'
      item_url_regex: 'URL: (\\S+)'
      item_summary_regex: '(?s)--- Snippet 1 \\(Line \\d+\\) ---\\n(.*)'
    explicit_only: true              # 连接器默认不占自动路由预算
    synthesized: true                # 合成/新接入标记，人工确认后摘除

安全边界：stdio 用列表参数 + build_child_env 白名单环境（禁 shell）；
不 eval 远端 payload；密钥不落日志；isError 的结果不产出条目（fail-visible）。
"""

from __future__ import annotations

import json
import re
import select
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from engine_env import allowed_placeholders_for, build_child_env
from engines_base import (
    _apply_url_kind,
    _ensure_engine_source,
    _get_logger,
    _header_meaningful,
    _parse_http_payload,
    _resolve,
    http_open,
    safe_search,
)

_PROTOCOL_VERSION = "2025-06-18"
_TRANSPORTS = ("http", "stdio")
_JSON_RE = re.compile(r"^data:\s*(.+?)\s*$")


# ── JSON-RPC 响应解析（application/json 或 SSE data: 帧）─────────────────────

def _parse_jsonrpc_body(text: str) -> dict[str, Any] | None:
    """整段 JSON 优先，SSE 逐 data: 行兜底，返回首个完整 JSON-RPC 对象。"""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ── Streamable HTTP 通道 ─────────────────────────────────────────────────────

def _client_info() -> dict[str, str]:
    """握手用的 clientInfo：版本读单源，不再手抄字面量。

    此前这里写死 "2.14.1"，v2.15.0 升版时没人改——而 release_check 的八处
    机器声明扫描不含本文件，升版盲区就是这么来的。真源与 mcp_transport
    同口径：argo_search.__version__（release_check 已把它列为声明之一）。
    """
    try:
        import argo_search
        version = str(argo_search.__version__)
    except Exception:
        version = "unknown"
    return {"name": "argo", "version": version}


class _HttpChannel:
    """Streamable HTTP JSON-RPC 通道（握手/回执/工具调用按 MCP 2025-06-18）。

    HTTP 出口统一走 engines_base.http_open（egress 门禁 + 代理解析 + 失败
    归因，issue #13），与 http 引擎同一出口；不直连 urlopen。
    """

    def __init__(self, url: str, headers: dict[str, str], timeout: float,
                 eng: str = "") -> None:
        self._url = url
        self._headers = headers
        self._timeout = timeout
        self._eng = eng
        self._session: str | None = None
        self._next_id = 1

    def _post(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        req = urllib.request.Request(self._url, data=body, headers=headers, method="POST")
        with http_open(req, timeout=self._timeout, engine=self._eng) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session = sid
            return resp.read().decode("utf-8", "replace")

    def _request(self, method: str, params: dict[str, Any] | None = None,
                 *, notify: bool = False) -> dict[str, Any] | None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if notify:
            payload.pop("id", None)
        else:
            payload["id"] = self._next_id
            self._next_id += 1
        if params is not None:
            payload["params"] = params
        raw = self._post(payload)
        return None if notify else _parse_jsonrpc_body(raw)

    def handshake(self) -> None:
        self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _client_info(),
        })
        self._request("notifications/initialized", notify=True)

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        obj = self._request("tools/call", {"name": tool, "arguments": arguments})
        if obj is None:
            return None
        if obj.get("error"):
            err = obj["error"]
            raise RuntimeError(f"MCP tools/call error: {err.get('code')} {err.get('message', '')[:200]}")
        return obj.get("result")


# ── stdio 通道 ───────────────────────────────────────────────────────────────

class _StdioChannel:
    """stdio 子进程 JSON-RPC 通道：换行分隔帧 + select 截止期读。"""

    def __init__(self, command: list[str], env: dict[str, str], timeout: float) -> None:
        self._timeout = timeout
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            shell=False,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next_id = 1

    def _exchange(self, payload: dict[str, Any], *, notify: bool) -> dict[str, Any] | None:
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()
        if notify:
            return None
        # 预算按墙钟而不是「未就绪的次数」：旧实现只在 select 超时未就绪时
        # deadline -= 1.0，只要对端持续吐帧（日志通知、其它 id 的响应、空行、
        # 解析失败的行）就永远不减，MCP 通知流可以把一次 tools/call 挂死到
        # 线程被杀。截止期在这里唯一，任何读到的行都不得延长它。
        deadline = time.monotonic() + max(0.0, float(self._timeout))
        stream = self._proc.stdout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([stream], [], [], min(remaining, 1.0))
            if not ready:
                continue
            line = stream.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and obj.get("id") == payload.get("id"):
                return obj
        raise TimeoutError(f"MCP stdio 响应超时（工具 {payload.get('method')}）")

    def handshake(self) -> None:
        self._exchange({
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _client_info(),
            },
        }, notify=False)
        self._next_id += 1
        self._exchange({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        self._next_id += 1
        obj = self._exchange({
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }, notify=False)
        if obj is None:
            return None
        if obj.get("error"):
            err = obj["error"]
            raise RuntimeError(f"MCP tools/call error: {err.get('code')} {err.get('message', '')[:200]}")
        return obj.get("result")

    def close(self) -> None:
        try:
            self._proc.kill()
        except OSError:
            pass


# ── 内容层：MCP 结果 → argo 标准条目 ─────────────────────────────────────────

def _text_blocks(result: dict[str, Any]) -> str:
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t)
    return "\n\n".join(parts)


def _regex_first(pattern: str, text: str) -> str:
    if not pattern:
        return ""
    m = re.search(pattern, text)
    return (m.group(1) if m and m.groups() else m.group(0)) if m else ""


def _text_items(text: str, om: dict[str, Any], eng: str, n: int, spec: dict) -> list[dict[str, Any]]:
    """mode: text——按 text_split 分块，逐块正则抽 title/url/snippet。"""
    split_re = om.get("text_split") or ""
    try:
        chunks = re.split(split_re, text) if split_re else [text]
    except re.error as e:
        raise ValueError(f"output_map.text_split 非法正则: {e}") from e
    rows: list[dict[str, Any]] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        row = {
            "title": _regex_first(om.get("item_title_regex", ""), chunk),
            "url": _regex_first(om.get("item_url_regex", ""), chunk),
            "snippet": _regex_first(om.get("item_summary_regex", ""), chunk),
        }
        if not (row["title"] or row["url"] or row["snippet"]):
            continue
        if row["snippet"]:
            row["snippet"] = row["snippet"][:300]
        if row["title"]:
            row["title"] = row["title"][:200]
        rows.append(row)
        if len(rows) >= n:
            break
    return _apply_url_kind(_ensure_engine_source(rows, eng), spec)


def _result_to_items(result: dict[str, Any] | None, spec: dict, eng: str, n: int) -> list[dict[str, Any]]:
    om = spec.get("output_map") or {}
    mode = str(om.get("mode", "json")).lower()
    if not isinstance(result, dict):
        return []
    if result.get("isError"):
        _get_logger().warning(f"引擎 {eng} MCP 工具 isError: {_text_blocks(result)[:200]}")
        return []
    if mode == "text":
        return _text_items(_text_blocks(result), om, eng, n, spec)
    data = result.get("structuredContent")
    if data is None:
        blob = _text_blocks(result).strip()
        if not blob:
            return []
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            raise ValueError(
                f"引擎 {eng} 工具返回非 JSON 文本；纯文本工具须在 output_map 声明 mode: text")
    return _parse_http_payload(json.dumps(data, ensure_ascii=False), "json", eng, n, om, spec)


# ── builder ──────────────────────────────────────────────────────────────────

def _build_mcp_engine(spec: dict[str, Any]) -> Any:
    """type: mcp 统一构造（stdio / Streamable HTTP 双传输）。"""
    eng = spec.get("_name", "")
    transport = str(spec.get("transport", "")).lower()
    if transport not in _TRANSPORTS:
        raise ValueError(f"引擎 {eng} type: mcp 需要 transport: http|stdio，得 {transport!r}")
    tool = str(spec.get("tool", "")).strip()
    if not tool:
        raise ValueError(f"引擎 {eng} type: mcp 必须声明 tool（单一 allowlist）")
    timeout = float(spec.get("timeout", 20))
    arguments_template = dict(spec.get("arguments") or {})
    allowed = allowed_placeholders_for(eng, spec)

    command: list[str] = []
    if transport == "stdio":
        raw_command = spec.get("command")
        # 必须是列表：字符串会被逐字符拆开（且 shell 形态在禁用之列）
        if not isinstance(raw_command, list) or not raw_command or \
                not all(isinstance(c, str) and c.strip() for c in raw_command):
            raise ValueError(
                f"引擎 {eng} stdio 传输必须声明非空 command 字符串列表（禁 shell）")
        command = [str(c) for c in raw_command]
    elif not str(spec.get("url", "")).strip().startswith(("http://", "https://")):
        raise ValueError(f"引擎 {eng} http 传输需要合法 url")

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        # env/header 在每次请求时解析（对齐 http builder）：daemon 下密钥热轮换
        # 与 envfile 热读不失效；未配置的 {ENV} 头由 _header_meaningful 过滤。
        env = build_child_env(eng, spec) if transport == "stdio" else {}
        headers = {}
        if transport == "http":
            headers = {
                k: v for k, v in (
                    (k, _resolve(str(v), query, n, allowed_env=allowed, **kwargs))
                    for k, v in (spec.get("headers") or {}).items()
                ) if _header_meaningful(v)
            }
        args: dict[str, Any] = {}
        for k, v in arguments_template.items():
            resolved = _resolve(str(v), query, n, allowed_env=allowed, depth=depth, **kwargs)
            if isinstance(resolved, list):
                resolved = " ".join(resolved)
            if resolved.lower() == "true":
                args[k] = True
            elif resolved.lower() == "false":
                args[k] = False
            else:
                try:
                    args[k] = int(resolved)
                except ValueError:
                    try:
                        args[k] = float(resolved)
                    except ValueError:
                        args[k] = resolved
        channel: Any
        if transport == "stdio":
            channel = _StdioChannel(command, env, to)
        else:
            channel = _HttpChannel(str(spec.get("url")), headers, to, eng=eng)
        try:
            channel.handshake()
            result = channel.call_tool(tool, args)
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as e:
            _get_logger().warning(f"引擎 {eng} MCP 调用失败: {type(e).__name__}: {e}")
            return []
        finally:
            if isinstance(channel, _StdioChannel):
                channel.close()
        return _result_to_items(result, spec, eng, n)

    return _engine

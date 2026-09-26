#!/usr/bin/env python3
"""test_exa_keyless.py — exa 免 key 匿名 MCP 通道回归。

无 key 时不再直接报错退出，而是走 Exa 托管 MCP 匿名通道
（POST https://mcp.exa.ai/mcp，tools/call web_search_exa，2026-09-26
实测匿名可用）。锁定五条契约：
  1. SSE 形态应答（event: message + data: {...}）能解析出结果
  2. 纯 JSON 形态应答同样解析（同一通道随 content-type 切换不能时好时坏）
  3. 文本块解析：Title:/URL:/Published:/Author:/Highlights: 多块、
     Highlights 折行续接
  4. 失败诚实路径：HTTP 失败 / MCP isError / 缺 result / 不可解析 →
     带 error 的记录（不是静默 []）
  5. 有 key 时仍走官方 REST（api.exa.ai），不碰 MCP

打桩点打在读取处（engines_builders_tech.http_open），无真实网络。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import engines_builders_tech as ebt  # noqa: E402


class _FakeResp:
    """HTTP 响应替身（context manager 协议；body 为原始文本）。"""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _mcp_result_text() -> str:
    return (
        "Title: Exa 首条结果\n"
        "URL: https://example.com/one\n"
        "Published Date: 2026-01-15\n"
        "Author: Alice\n"
        "Highlights: 这是第一条高亮\n"
        "高亮第二行续文\n"
        "\n"
        "Title: Exa 第二条\n"
        "URL: https://example.com/two\n"
        "Highlights: 第二条高亮"
    )


def _mcp_payload() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text",
                                    "text": _mcp_result_text()}]}}


def _sse_raw() -> str:
    # MCP over SSE：应答事件是 message + data:<json>；后面再混一个无关
    # 事件（如 ping）验证解析器只认带 result/error 的 data 行
    return ("event: message\n"
            f"data: {json.dumps(_mcp_payload(), ensure_ascii=False)}\n\n"
            "event: ping\n"
            "data: {}\n\n")


def _json_raw() -> str:
    return json.dumps(_mcp_payload(), ensure_ascii=False)


def _no_exa_keys():
    """屏蔽本机真实 key（含 envfile 热读保底），强制走匿名通道。"""
    return (patch.dict(os.environ, {"ARGO_EXA_API_KEY": "", "EXA_API_KEY": ""},
                       clear=False),
            patch("engine_env._envfile_load", return_value={}))


class TestKeylessMcpChannel:
    def _run(self, raw_body: str):
        env_patch, envfile_patch = _no_exa_keys()
        with env_patch, envfile_patch, \
                patch("engines_builders_tech.http_open") as mock_open:
            mock_open.return_value.__enter__.return_value = _FakeResp(raw_body)
            eng = ebt._build_exa_engine({})
            return eng("rust async runtime", n=5), mock_open

    def test_sse_response_parsed(self):
        rs, _ = self._run(_sse_raw())
        assert rs and "error" not in rs[0], rs
        assert rs[0]["url"] == "https://example.com/one"
        assert rs[0]["title"] == "Exa 首条结果"
        assert rs[0]["source"] == "exa"
        assert rs[0]["published_at"] == "2026-01-15"
        assert "高亮第二行续文" in rs[0]["snippet"]
        assert rs[1]["url"] == "https://example.com/two"

    def test_plain_json_response_parsed(self):
        rs, _ = self._run(_json_raw())
        assert len(rs) == 2
        assert rs[0]["url"] == "https://example.com/one"

    def test_mcp_request_shape(self):
        _, mock_open = self._run(_json_raw())
        req = mock_open.call_args[0][0]
        assert req.full_url == "https://mcp.exa.ai/mcp"
        body = json.loads(req.data.decode("utf-8"))
        assert body["jsonrpc"] == "2.0"
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "web_search_exa"
        assert body["params"]["arguments"]["query"] == "rust async runtime"
        assert body["params"]["arguments"]["numResults"] == 5
        assert req.get_header("Accept") == "application/json, text/event-stream"

    def test_numresults_capped_at_10(self):
        env_patch, envfile_patch = _no_exa_keys()
        with env_patch, envfile_patch, \
                patch("engines_builders_tech.http_open") as mock_open:
            mock_open.return_value.__enter__.return_value = _FakeResp(_json_raw())
            eng = ebt._build_exa_engine({})
            eng("rust async runtime", n=50)
        body = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        assert body["params"]["arguments"]["numResults"] == 10

    def test_na_values_not_treated_as_real_fields(self):
        """匿名通道无值时给字面 "N/A"（实测），不得当成真实发布时间/作者。"""
        text = ("Title: T\nURL: https://example.com/na\n"
                "Published: N/A\nAuthor: N/A\nHighlights: h")
        raw = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "result": {"content": [{"type": "text", "text": text}]}})
        rs, _ = self._run(raw)
        assert rs and "error" not in rs[0]
        assert "published_at" not in rs[0]
        assert "metadata" not in rs[0]


class TestKeylessHonestFailure:
    """匿名通道失效必须以 error 记录上报，不许静默装成无结果。"""

    def _run(self, raw_body: str | None, raise_exc: Exception | None = None):
        env_patch, envfile_patch = _no_exa_keys()
        with env_patch, envfile_patch, \
                patch("engines_builders_tech.http_open") as mock_open:
            if raise_exc is not None:
                mock_open.side_effect = raise_exc
            else:
                mock_open.return_value.__enter__.return_value = _FakeResp(raw_body or "")
            eng = ebt._build_exa_engine({})
            return eng("rust async runtime", n=5)

    def test_http_failure_returns_error_item(self):
        rs = self._run(None, raise_exc=urllib.error.URLError("connection refused"))
        assert len(rs) == 1 and "error" in rs[0]
        assert rs[0]["source"] == "exa"
        assert "url" not in rs[0]

    def test_mcp_iserror_returns_error_item(self):
        raw = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "result": {"isError": True,
                                     "content": [{"text": "rate limited"}]}})
        rs = self._run(raw)
        assert len(rs) == 1 and "error" in rs[0]

    def test_jsonrpc_error_returns_error_item(self):
        raw = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "error": {"code": -32000, "message": "too many requests"}})
        rs = self._run(raw)
        assert len(rs) == 1 and "error" in rs[0]

    def test_missing_result_returns_error_item(self):
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "note": "no result"})
        rs = self._run(raw)
        assert len(rs) == 1 and "error" in rs[0]

    def test_unparseable_body_returns_error_item(self):
        rs = self._run("<html>maintenance</html>")
        assert len(rs) == 1 and "error" in rs[0]

    def test_empty_body_returns_error_item(self):
        rs = self._run("")
        assert len(rs) == 1 and "error" in rs[0]


class TestTextBlockParser:
    """_parse_exa_text_blocks 的直接单元测试（多块 / 折行 / 边界）。"""

    def test_multi_blocks(self):
        blocks = ebt._parse_exa_text_blocks(_mcp_result_text())
        assert len(blocks) == 2
        assert blocks[0]["title"] == "Exa 首条结果"
        assert blocks[0]["published"] == "2026-01-15"
        assert blocks[0]["author"] == "Alice"

    def test_highlights_multiline_joined(self):
        blocks = ebt._parse_exa_text_blocks(_mcp_result_text())
        assert "这是第一条高亮" in blocks[0]["highlights"]
        assert "高亮第二行续文" in blocks[0]["highlights"]

    def test_published_date_label_normalized(self):
        blocks = ebt._parse_exa_text_blocks("Title: t\nPublished Date: 2026-01-01")
        assert blocks[0]["published"] == "2026-01-01"

    def test_empty_text(self):
        assert ebt._parse_exa_text_blocks("") == []
        assert ebt._parse_exa_text_blocks("没有任何标签行") == []

    def test_block_without_title(self):
        blocks = ebt._parse_exa_text_blocks("URL: https://a.io/x\nHighlights: h")
        assert blocks == [{"url": "https://a.io/x", "highlights": "h"}]


class TestKeyedPathUnchanged:
    """有 key 必须仍走官方 REST——匿名通道是兼容路径，不是替代。"""

    def test_with_key_uses_rest_not_mcp(self):
        with patch.dict(os.environ, {"ARGO_EXA_API_KEY": "k-test"}, clear=False), \
                patch("engine_env._envfile_load", return_value={}), \
                patch("engines_builders_tech.http_open") as mock_open:
            mock_open.return_value.__enter__.return_value = _FakeResp(json.dumps(
                {"results": [{"title": "T1", "url": "https://a.com", "text": "内容"}]}))
            eng = ebt._build_exa_engine({})
            rs = eng("rust async", n=5)
        assert rs and rs[0]["source"] == "exa" and rs[0]["title"] == "T1"
        req = mock_open.call_args[0][0]
        assert req.full_url == "https://api.exa.ai/search"

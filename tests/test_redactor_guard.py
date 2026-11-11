#!/usr/bin/env python3
"""test_secret_guard.py — 输出流自动脱敏拦截器与设置项覆盖测试。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from secret_guard import redact_text, redact_data, get_active_secrets
import settings_page


def test_redact_known_active_secrets():
    """测试已知真实活跃密钥在文本中的自动脱敏。"""
    fake_secret = "mock_secret_" + "abcdefg123456"
    with patch.dict(os.environ, {"ARGO_CUSTOM_KEY": fake_secret}):
        text = f"Warning: failed with key {fake_secret} on endpoint."
        redacted = redact_text(text)
        assert fake_secret not in redacted
        assert "[REDACTED_API_KEY]" in redacted


def test_redact_generic_key_patterns():
    """测试通用标准格式高熵 API Key（sk-..., tvly-..., ghp_...）的签名脱敏。"""
    fake_sk = "sk-" + "proj" + ("a" * 32)
    text1 = f"Error message: invalid bearer token: {fake_sk}"
    assert "sk-" not in redact_text(text1)
    assert "[REDACTED_SECRET]" in redact_text(text1)

    fake_tvly = "tvly-" + ("test" * 8)
    text2 = f"Using Tavily search with {fake_tvly}"
    assert "tvly-" not in redact_text(text2)
    assert "[REDACTED_SECRET]" in redact_text(text2)

    fake_ghp = "ghp_" + ("1234567890" * 3) + "123456"
    text3 = f"GitHub PAT leaked: {fake_ghp}"
    assert "ghp_" not in redact_text(text3)
    assert "[REDACTED_SECRET]" in redact_text(text3)


def test_redact_url_params():
    """测试 URL 查询参数中的敏感凭据清洗。"""
    secret_val = "topsecret" + "987654321"
    url = f"https://example.com/v1/search?q=machine+learning&api_key={secret_val}&page=1"
    redacted = redact_text(url)
    assert secret_val not in redacted
    assert "api_key=[REDACTED]" in redacted
    assert "q=machine+learning" in redacted
    assert "page=1" in redacted


def test_redact_data_recursive():
    """测试递归嵌套数据结构（dict / list / str）的深度脱敏。"""
    fake_live = "sk-" + "live" + ("1" * 20)
    payload = {
        "results": [
            {
                "title": "API Documentation",
                "url": "https://api.org/data?token=super_secret_token_456",
                "snippet": f"Contact support if {fake_live} fails.",
                "meta": {
                    "debug_logs": ["Auth Bearer 1234567890abcdef1234567890abcdef"],
                },
            }
        ]
    }
    redacted = redact_data(payload)
    item = redacted["results"][0]
    assert "token=[REDACTED]" in item["url"]
    assert "sk-" not in item["snippet"]
    assert "[REDACTED_SECRET]" in item["snippet"]
    assert "1234567890abcdef1234567890abcdef" not in item["meta"]["debug_logs"][0]


def test_settings_page_lists_all_services():
    """测试设置面板完整收录核心服务、商业数据源与可选增强引擎。"""
    data = settings_page.collect()
    key_rows = data.get("key_rows") or []
    assert len(key_rows) > 0

    kinds = {r.get("kind") for r in key_rows}
    assert "service" in kinds
    assert "engine" in kinds
    assert "optional" in kinds

    engines_listed = {r["engine"] for r in key_rows}
    # 核心服务
    assert "jev" in engines_listed
    assert "semantic_scholar" in engines_listed
    # 可选增强
    assert "github" in engines_listed
    assert "exa" in engines_listed
    # 商业引擎
    assert "bocha" in engines_listed


# ── 出口覆盖：MCP 与 --list-engines ──────────────────────────────────────────

def test_mcp_output_is_redacted():
    """MCP 工具返回必须经过脱敏——它是「结果交给 Agent 与 LLM」的那条路。

    回归（2026-11-11 审计）：a559fda 新增 secret_guard，但只在
    search_cli.py 一处调用；mcp_handlers._ok（19 个工具的唯一返回出口）
    里 redact 出现 0 次。secret_guard 文件头写的目标是「防止真实凭据反射
    给 Agent 与 LLM」，而 MCP 恰是那条通道，覆盖却只有 CLI 一侧。

    判据走真实出口 execute_tool，不直接测 _ok——否则把 _ok 换成别的返回
    形态（如某个分支自己拼 content）就绕过了。
    """
    import mcp_handlers

    leaked = "sk-" + "mcp" + ("9" * 28)
    with patch.object(mcp_handlers, "_lazy_cached") as mock_lazy:
        fake = MagicMock()
        fake.super_search.return_value = {
            "query": "q",
            "results": [{"title": "t", "url": "https://x.example/a",
                         "snippet": f"auth with {leaked} failed",
                         "source": "anysearch", "score": 0.9}],
        }
        mock_lazy.return_value = fake
        resp = mcp_handlers.execute_tool("argo_search", {"query": "q"})

    text = resp["content"][0]["text"]
    assert leaked not in text, "MCP 出口泄漏了 API Key"
    assert "REDACTED" in text


def test_list_engines_output_is_redacted(monkeypatch, capsys):
    """`--list-engines` 出口必须脱敏。

    为什么单列：引擎状态行含 runtime.failure.evidence 与 admission 等自由
    文本字段，是 stdout 上唯一能带回真实凭据的出口（历史上引擎把 401 响应体
    原样记进 failure.evidence）。而 --list-engines 在 search 之前就 return，
    走不到 search_cli 文件底部那处脱敏——它是脱敏的盲区。
    """
    import search_cli

    leaked = "sk-" + "list" + ("7" * 28)
    row = {
        "engine_id": "probe_engine", "status": "blocked",
        "cost_tier": "api", "type": "http", "routable": False,
        "env_ready": False, "missing_env": [],
        "runtime": {"failure": {"category": "auth",
                                "evidence": f"upstream said {leaked}"}},
    }
    monkeypatch.setattr("engine_status.list_engines_detail", lambda **kw: [row])
    monkeypatch.setattr("engine_status.compact_engine_row", lambda r: r)
    monkeypatch.setattr("engine_status.format_engines_table",
                        lambda rows: "\n".join(
                            str(r.get("runtime", {}).get("failure", {}).get("evidence"))
                            for r in rows))
    monkeypatch.setattr(sys, "argv", ["search.py", "--list-engines", "--detail",
                                      "--engine", "probe_engine"])
    search_cli.main()
    out = capsys.readouterr().out
    assert leaked not in out, "--list-engines 出口泄漏了 failure.evidence 里的凭据"

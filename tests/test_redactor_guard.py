#!/usr/bin/env python3
"""test_secret_guard.py — 输出流自动脱敏拦截器与设置项覆盖测试。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

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

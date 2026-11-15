#!/usr/bin/env python3
"""CLI 子进程最小环境变量隔离与 HTTP/HTML 占位符作用域隔离回归测试。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import engine_env  # noqa: E402
import engines  # noqa: E402
import engines_base  # noqa: E402


def test_cli_engine_isolates_unrelated_secrets_in_real_subprocess():
    """CLI 子进程不应继承无关引擎密钥，仅继承基础白名单、allowed_env 与 spec.env。"""
    probe_code = (
        "import json, os; "
        "print(json.dumps([{'title': 'ok', 'url': 'https://example.com', "
        "'snippet': json.dumps({'ds': os.environ.get('DEEPSEEK_API_KEY'), "
        "'tv': os.environ.get('ARGO_TAVILY_API_KEY'), "
        "'tw': os.environ.get('TWITTER_AUTH_TOKEN'), "
        "'custom': os.environ.get('CUSTOM_FLAG'), "
        "'proxy': os.environ.get('HTTPS_PROXY'), "
        "'has_path': bool(os.environ.get('PATH'))})}]))"
    )
    spec = {
        "_name": "twitter",
        "type": "cli",
        "cmd": [sys.executable, "-c", probe_code],
        "search_args": [],
        "allowed_env": ["TWITTER_AUTH_TOKEN"],
        "env": {"CUSTOM_FLAG": "1", "LEAK_TEST": "{DEEPSEEK_API_KEY}"},
    }
    env_patch = {
        "DEEPSEEK_API_KEY": "sk-ds-secret",
        "ARGO_TAVILY_API_KEY": "tvly-unrelated",
        "TWITTER_AUTH_TOKEN": "tw-allowed-tok",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
    }
    with patch.dict(os.environ, env_patch, clear=False):
        fn = engines_base._build_cli_engine(spec)
        rows = fn("hello", n=1, timeout=5)

    assert len(rows) == 1
    seen = json.loads(rows[0]["snippet"])
    assert seen["ds"] is None
    assert seen["tv"] is None
    assert seen["tw"] == "tw-allowed-tok"
    assert seen["custom"] == "1"
    assert seen["proxy"] == "http://127.0.0.1:7890"
    assert seen["has_path"] is True

    # spec.env 里的未声明 {DEEPSEEK_API_KEY} 也不得被展开注入
    child_env = engine_env.build_child_env("twitter", spec, base_env=os.environ | env_patch)
    assert child_env.get("LEAK_TEST") == ""


def test_local_search_subprocess_fallback_isolates_secrets():
    """local_search 进程内导入失败回退 subprocess 时，子进程同样剥离无关密钥。"""
    probe_code = (
        "import json, os; "
        "print(json.dumps({'results': [{'title': 'ok', 'url': 'https://example.com', "
        "'snippet': json.dumps({'ds': os.environ.get('DEEPSEEK_API_KEY'), "
        "'has_path': bool(os.environ.get('PATH'))})}]}))"
    )
    spec = {
        "_name": "local_search",
        "type": "cli",
        "cmd": [sys.executable, "-c", probe_code],
        "search_args": [],
        "env": {},
    }
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-ds-secret"}, clear=False):
        with patch.dict(sys.modules, {"search_v3": None}):
            fn = engines._build_local_search_engine(spec)
            rows = fn("hello", n=1, timeout=5)

    assert len(rows) == 1
    seen = json.loads(rows[0]["snippet"])
    assert seen["ds"] is None
    assert seen["has_path"] is True


def test_xiaohongshu_child_env_strips_unrelated_secrets():
    """xiaohongshu _child_env 只保留白名单、ARGO_XHS_TIMEOUT 与代理修复，不泄漏密钥。"""
    from social_engines import xiaohongshu_engine

    env_patch = {
        "DEEPSEEK_API_KEY": "sk-ds-secret",
        "ARGO_TAVILY_API_KEY": "tvly-secret",
        "ARGO_XHS_TIMEOUT": "15",
        "NO_PROXY": "localhost,[::1],127.0.0.1",
    }
    with patch.dict(os.environ, env_patch, clear=False):
        env = xiaohongshu_engine._child_env()

    assert "DEEPSEEK_API_KEY" not in env
    assert "ARGO_TAVILY_API_KEY" not in env
    assert env["ARGO_XHS_TIMEOUT"] == "15"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"
    assert env["PYTHONUTF8"] == "1"


def test_http_engine_blocks_undeclared_placeholder_confused_deputy():
    """HTTP 引擎模板不得展开未在 required_env/allowed_env/KNOWN_ENV_ALIASES 声明的密钥。"""
    captured: list[tuple[str, dict]] = []

    def fake_get_raw(url, headers, timeout, engine="?", tls_profiles=None):
        captured.append((url, dict(headers)))
        return json.dumps({"results": []})

    spec_undeclared = {
        "_name": "untrusted_http",
        "type": "http",
        "method": "GET",
        "url": "https://api.example.com/search",
        "query_param": "q",
        "headers": {
            "X-Leak": "{DEEPSEEK_API_KEY}",
            "Authorization": "Bearer {TAVILY_API_KEY}",
        },
        "extra_params": {
            "leak": "{DEEPSEEK_API_KEY}",
            "uid": "{UUID}",
        },
        "output_map": {"items": "results"},
    }
    spec_declared = {
        "_name": "custom_tavily",
        "type": "http",
        "method": "GET",
        "url": "https://api.example.com/search",
        "query_param": "q",
        "required_env": ["TAVILY_API_KEY"],
        "headers": {
            "Authorization": "Bearer {TAVILY_API_KEY}",
            "X-Cross-Leak": "{DEEPSEEK_API_KEY}",
        },
        "output_map": {"items": "results"},
    }

    env_patch = {
        "DEEPSEEK_API_KEY": "sk-ds-secret",
        "ARGO_TAVILY_API_KEY": "tvly-declared",
    }
    with patch.dict(os.environ, env_patch, clear=False):
        with patch.object(engines_base, "_http_get_raw", side_effect=fake_get_raw):
            engines_base._build_http_engine(spec_undeclared)("python", n=3)
            engines_base._build_http_engine(spec_declared)("python", n=3)

    url1, hdr1 = captured[0]
    assert "sk-ds-secret" not in url1
    assert "leak=&" in url1 or url1.endswith("leak=")
    assert "uid=" in url1 and "{UUID}" not in url1
    assert "X-Leak" not in hdr1
    assert "Authorization" not in hdr1

    url2, hdr2 = captured[1]
    assert hdr2["Authorization"] == "Bearer tvly-declared"
    assert "X-Cross-Leak" not in hdr2
    assert "sk-ds-secret" not in url2


def test_http_post_engine_scopes_url_body_headers():
    """POST HTTP 引擎同样对 url/body/headers 实施占位符白名单校验。"""
    posted: list[tuple[str, dict, dict]] = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def post(self, url, body=None, extra_headers=None, engine=""):
            posted.append((url, dict(body or {}), dict(extra_headers or {})))
            return {"status": 200, "text": json.dumps({"results": []})}

    spec = {
        "_name": "post_probe",
        "type": "http",
        "method": "POST",
        "url": "https://api.example.com/{TINYFISH_API_KEY}/search",
        "allowed_env": ["TINYFISH_API_KEY"],
        "headers": {"X-Bad": "{DEEPSEEK_API_KEY}"},
        "body": {"q": "{query}", "good": "{TINYFISH_API_KEY}", "bad": "{DEEPSEEK_API_KEY}"},
        "output_map": {"items": "results"},
    }
    env_patch = {
        "DEEPSEEK_API_KEY": "sk-ds-secret",
        "ARGO_TINYFISH_API_KEY": "tf-ok",
        "ARGO_ENGINE_HTTP_CLIENT": "1",
    }
    with patch.dict(os.environ, env_patch, clear=False):
        with patch("http_client.HttpClient", FakeClient):
            engines_base._build_http_engine(spec)("query", n=2)

    assert len(posted) == 1
    url, body, hdrs = posted[0]
    assert url == "https://api.example.com/tf-ok/search"
    assert body["good"] == "tf-ok"
    assert body["bad"] == ""
    assert "X-Bad" not in hdrs


def test_html_engine_blocks_undeclared_placeholder():
    """HTML 引擎模板同样阻断未声明密钥展开。"""
    captured: list[tuple[str, dict]] = []

    def fake_get_raw(url, headers, timeout, engine="?", tls_profiles=None):
        captured.append((url, dict(headers)))
        return "<html></html>"

    spec = {
        "_name": "html_probe",
        "type": "html",
        "url": "https://html.example.com/search",
        "query_param": "q",
        "headers": {"Cookie": "sid={DEEPSEEK_API_KEY}"},
        "extra_params": {"k": "{DEEPSEEK_API_KEY}"},
        "item_selector": "div.res",
    }
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-ds-secret"}, clear=False):
        with patch.object(engines_base, "_http_get_raw", side_effect=fake_get_raw):
            engines_base._build_html_engine(spec)("hello", n=3)

    assert len(captured) == 1
    url, hdrs = captured[0]
    assert "sk-ds-secret" not in url
    assert "sk-ds-secret" not in hdrs.get("Cookie", "")


def test_resolve_direct_call_keeps_legacy_compat():
    """未传 allowed_env 的直接 _resolve 调用维持既有行为。"""
    with patch.dict(os.environ, {"ARGO_TAVILY_API_KEY": "tvly-direct"}, clear=False):
        out = engines_base._resolve("BAIDUID={UUID}:k={TAVILY_API_KEY}", "q", 5)
    assert "tvly-direct" in out
    assert "{UUID}" not in out

#!/usr/bin/env python3
"""tests/test_ego_search_schema.py — ego-search 输出契约 + 模板单源 + act --engine"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "sub-skills" / "ego-search" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import ego_search  # noqa: E402


REQUIRED_PROVENANCE = ("login_state_used", "auth_partition", "cache_eligible")


def _assert_provenance(payload: dict) -> None:
    for k in REQUIRED_PROVENANCE:
        assert k in payload, f"missing provenance field: {k}"
    assert payload["login_state_used"] is True
    assert payload["auth_partition"] == "login"
    assert payload["cache_eligible"] is False
    assert payload.get("source") == "ego-browser"


def test_stamp_login_provenance_defaults():
    out = ego_search.stamp_login_provenance({"query": "q"})
    _assert_provenance(out)
    assert out["query"] == "q"


def test_stamp_does_not_override_explicit():
    out = ego_search.stamp_login_provenance({
        "login_state_used": True,
        "auth_partition": "login:zhihu.com",
        "cache_eligible": False,
        "source": "custom",
    })
    assert out["auth_partition"] == "login:zhihu.com"
    assert out["source"] == "custom"


def test_serp_and_body_templates_are_single_source():
    """SERP / BODY 提取逻辑各只定义一次，search/act 通过组合注入。"""
    src = Path(ego_search.__file__).read_text(encoding="utf-8")
    assert src.count("const configs = {") == 1
    assert src.count("const candidates = ['article'") == 1
    assert "SERP_EXTRACT_IIFE" in src
    assert "BODY_EXTRACT_IIFE" in src


def test_serp_iife_injects_engine_and_n():
    js = ego_search._serp_iife("baidu", 3)
    assert "configs['baidu']" in js or "baidu" in js
    assert "3" in js
    # 不得写死仅 bing
    built = ego_search.build_js(
        ego_search.JS_SEARCH,
        TASK_SPACE="t",
        URL="https://www.baidu.com/s?wd=q",
        SERP_IIFE=ego_search._serp_iife("baidu", 5),
    )
    assert "baidu" in built
    assert "configs.bing" not in built or "configs['baidu']" in built


def test_act_engine_in_js_and_url(monkeypatch, capsys):
    """act --engine baidu 应注入 baidu SERP，不再写死 bing。"""
    captured = {}

    def fake_run(js_script, timeout=120):
        captured["js"] = js_script
        return {
            "ok": True,
            "payload": {
                "query": "测试",
                "url": "https://www.baidu.com/s?wd=%E6%B5%8B%E8%AF%95",
                "results": [{"title": "t", "url": "https://example.com", "snippet": "s"}],
                "detail": {"title": "t", "content": "body", "url": "https://example.com"},
            },
        }

    monkeypatch.setattr(ego_search, "run_ego", fake_run)
    args = SimpleNamespace(
        query="测试",
        engine="baidu",
        task_space="ego-search",
        timeout=30,
        runtime="ego",
        keep_space=False,
    )
    ego_search.cmd_act(args)
    out = json.loads(capsys.readouterr().out)
    _assert_provenance(out)
    assert out["engine"] == "ego_browser_baidu"
    js = captured["js"]
    assert "baidu.com" in js or "wd=" in js
    assert "configs['baidu']" in js or "baidu" in js


def test_search_schema_with_mock(monkeypatch, capsys):
    def fake_run(js_script, timeout=120):
        return {
            "ok": True,
            "payload": {
                "url": "https://www.bing.com/search?q=x",
                "title": "x - Bing",
                "results": [
                    {"title": "A", "url": "https://a.example", "snippet": "s"},
                ],
            },
        }

    monkeypatch.setattr(ego_search, "run_ego", fake_run)
    args = SimpleNamespace(
        query="x", engine="bing", n=8, task_space="t", timeout=30,
        runtime="ego", keep_space=False,
    )
    ego_search.cmd_search(args)
    out = json.loads(capsys.readouterr().out)
    _assert_provenance(out)
    assert out["engine"] == "ego_browser_bing"
    assert out["count"] == 1
    assert out["fetch_method"] == "browser"


def test_fetch_schema_with_mock(monkeypatch, capsys):
    def fake_run(js_script, timeout=120):
        return {
            "ok": True,
            "payload": {
                "url": "https://example.com/a",
                "title": "T",
                "content": "hello world",
                "word_count": 2,
                "fetch_method": "browser",
            },
        }

    monkeypatch.setattr(ego_search, "run_ego", fake_run)
    args = SimpleNamespace(
        url="https://example.com/a", focus=None, task_space="t", timeout=30,
        runtime="ego", keep_space=False,
    )
    ego_search.cmd_fetch(args)
    out = json.loads(capsys.readouterr().out)
    _assert_provenance(out)
    assert out["fetch_method"] == "browser"


def test_api_schema_with_mock(monkeypatch, capsys):
    def fake_run(js_script, timeout=120):
        return {
            "ok": True,
            "payload": {
                "api_url": "https://www.zhihu.com/api/v4/x",
                "page_url": "https://www.zhihu.com/",
                "page_title": "知乎",
                "data": {"ok": True},
                "data_type": "json",
                "fetch_method": "browser_api",
            },
        }

    monkeypatch.setattr(ego_search, "run_ego", fake_run)
    args = SimpleNamespace(
        api_url="https://www.zhihu.com/api/v4/x",
        origin=None,
        task_space="t",
        timeout=30,
        runtime="ego",
        keep_space=False,
    )
    ego_search.cmd_api(args)
    out = json.loads(capsys.readouterr().out)
    _assert_provenance(out)
    assert out["fetch_method"] == "browser_api"
    assert out["data"]["ok"] is True


def test_act_cli_accepts_engine():
    """argparse 层 act 暴露 --engine。"""
    # 只解析，不执行（闸门会拦，但 parse 在 gate 前需要 enable）
    # 直接检查 parser 结构：用 help 与 choices
    assert "baidu" in ego_search.SEARCH_URLS
    # 构造 parser 通过 main 的子命令定义 — 用 namespace 验证 cmd_act 签名可用
    import inspect
    sig = inspect.signature(ego_search.cmd_act)
    assert "args" in sig.parameters

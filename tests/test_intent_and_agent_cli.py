#!/usr/bin/env python3
"""tests/test_intent_and_agent_cli.py — 方案二契约门禁（--agent 单开关、Fail-Fast 冲突与 --intent 路由）。

覆盖四项核心契约：
  1. --agent 单开关：等价于 --json --fields agent，输出瘦身且保留关键证据字段；
  2. Fail-Fast 互斥：--envelope 与 --agent / --fields agent 入口直接报错退出（不触发搜索）；
     --agent 与 --fields full 同样入口直接报错；
  3. --intent 高层意图路由：news / academic / social / image-reverse / calc 的正常调度与
     缺密钥/超预算/mode=fast 下的透明降级与 warnings 注入；
  4. 零回归守卫：未传 --intent 时不附加 intent_resolution 字段、不改变默认路由。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import intent_router  # noqa: E402
import search_cli  # noqa: E402


def _mock_engines() -> dict[str, dict]:
    return {
        "deepseek_search": {"enabled": True, "explicit_only": True, "required_env": ["DEEPSEEK_API_KEY"]},
        "animetrace": {"enabled": True, "explicit_only": True},
        "tracemoe": {"enabled": True, "explicit_only": True},
        "saucenao": {"enabled": True, "explicit_only": True, "required_env": ["ARGO_SAUCENAO_API_KEY"]},
        "google_lens": {"enabled": True, "explicit_only": True, "required_env": ["ARGO_SERPAPI_API_KEY"]},
        "wolframalpha": {"enabled": True, "explicit_only": True, "required_env": ["WOLFRAM_APP_ID"]},
    }


# ── 1. --intent 路由逻辑单测（全离线、确定性） ───────────────────────────────

def test_intent_news_uses_deepseek_when_ready():
    res = intent_router.resolve_intent(
        "news",
        mode="auto",
        get_engines_fn=_mock_engines,
        check_env_fn=lambda _e, _s: [],
        check_quota_fn=lambda _e, _m: True,
    )
    assert res["engine"] == "deepseek_search"
    assert res["degraded"] is False
    assert res["warnings"] == []


def test_intent_news_degrades_cleanly_on_missing_key():
    res = intent_router.resolve_intent(
        "news",
        mode="auto",
        get_engines_fn=_mock_engines,
        check_env_fn=lambda eng, _s: ["DEEPSEEK_API_KEY"] if eng == "deepseek_search" else [],
        check_quota_fn=lambda _e, _m: True,
    )
    assert res["engine"] == "auto"
    assert res["domain"] == "news"
    assert res["degraded"] is True
    assert any("DEEPSEEK_API_KEY" in w for w in res["warnings"])


def test_intent_news_degrades_on_quota_exhausted_or_fast_mode():
    res_quota = intent_router.resolve_intent(
        "news",
        mode="auto",
        get_engines_fn=_mock_engines,
        check_env_fn=lambda _e, _s: [],
        check_quota_fn=lambda _e, _m: False,
    )
    assert res_quota["engine"] == "auto"
    assert res_quota["degraded"] is True
    assert any("配额" in w or "预算" in w for w in res_quota["warnings"])

    res_fast = intent_router.resolve_intent(
        "news",
        mode="fast",
        get_engines_fn=_mock_engines,
        check_env_fn=lambda _e, _s: [],
        check_quota_fn=lambda _e, _m: True,
    )
    assert res_fast["engine"] == "auto"
    assert res_fast["domain"] == "news"
    assert res_fast["degraded"] is True
    assert any("fast" in w for w in res_fast["warnings"])


def test_intent_academic_and_social():
    acad = intent_router.resolve_intent("academic", depth="fast")
    assert acad["domain"] == "academic"
    assert acad["depth"] == "deep"
    assert acad["engine"] == "auto"
    assert acad["degraded"] is False

    soc = intent_router.resolve_intent("social")
    assert soc["domain"] == "social"
    assert soc["engine"] == "auto"
    assert soc["degraded"] is False


def test_intent_image_reverse_selects_available_and_caps_two():
    res = intent_router.resolve_intent(
        "image-reverse",
        get_engines_fn=_mock_engines,
        check_env_fn=lambda eng, _s: ["ARGO_SAUCENAO_API_KEY"] if eng == "saucenao" else [],
        check_quota_fn=lambda _e, _m: True,
    )
    assert res["engine"] == "animetrace,tracemoe"
    assert res["degraded"] is False

    res_none = intent_router.resolve_intent(
        "image-reverse",
        get_engines_fn=_mock_engines,
        check_env_fn=lambda _e, _s: ["MISSING_KEY"],
        check_quota_fn=lambda _e, _m: True,
    )
    assert res_none["engine"] == "auto"
    assert res_none["degraded"] is True
    assert res_none["warnings"]


def test_intent_calc_and_explicit_engine_override():
    calc_ok = intent_router.resolve_intent(
        "calc",
        get_engines_fn=_mock_engines,
        check_env_fn=lambda _e, _s: [],
        check_quota_fn=lambda _e, _m: True,
    )
    assert calc_ok["engine"] == "wolframalpha"
    assert calc_ok["degraded"] is False

    calc_miss = intent_router.resolve_intent(
        "calc",
        get_engines_fn=_mock_engines,
        check_env_fn=lambda _e, _s: ["WOLFRAM_APP_ID"],
        check_quota_fn=lambda _e, _m: True,
    )
    assert calc_miss["engine"] == "auto"
    assert calc_miss["degraded"] is True

    over = intent_router.resolve_intent(
        "news",
        engine="exa",
        get_engines_fn=_mock_engines,
        check_env_fn=lambda _e, _s: [],
        check_quota_fn=lambda _e, _m: True,
    )
    assert over["engine"] == "exa"
    assert over["overridden_by_engine"] is True
    assert any("--engine exa" in w for w in over["warnings"])


# ── 2. CLI 集成契约：--agent 单开关 + Fail-Fast 互斥 + 零回归 ─────────────────

def _stub_super_search(called_box: dict):
    def _fake(**kwargs):
        called_box.update(kwargs)
        return {
            "query": kwargs["query"],
            "engine": kwargs["engine"],
            "domain": kwargs.get("engine_domain") or "general",
            "mode": kwargs["mode"],
            "depth": kwargs["depth"],
            "status": "completed",
            "fetch_required": False,
            "results": [
                {
                    "title": "Sample",
                    "url": "https://example.invalid/1",
                    "snippet": "sample snippet",
                    "source": "stub",
                    "score": 0.9,
                }
            ],
            "tfidf_scores": {"general": 0.5},  # 应被 agent 档剥离的遥测字段
            "warnings": [],
        }
    return _fake


def test_cli_agent_flag_implies_json_and_strips_telemetry(monkeypatch, capsys):
    called: dict = {}
    monkeypatch.setattr(search_cli, "super_search", _stub_super_search(called))
    monkeypatch.setattr(sys, "argv", ["search.py", "python asyncio", "--agent"])
    search_cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["query"] == "python asyncio"
    assert "results" in out and len(out["results"]) == 1
    assert "tfidf_scores" not in out, "--agent 必须启用 agent 档剥离遥测字段"
    assert "intent_resolution" not in out, "未传 --intent 时不得附加 intent_resolution"
    assert called["quiet"] is True


def test_cli_envelope_and_agent_fail_fast(monkeypatch, capsys):
    called = {"hit": False}

    def _must_not_run(**_kwargs):
        called["hit"] = True
        return {}

    monkeypatch.setattr(search_cli, "super_search", _must_not_run)

    for argv in (
        ["search.py", "q", "--agent", "--envelope"],
        ["search.py", "q", "--json", "--fields", "agent", "--envelope"],
        ["search.py", "q", "--agent", "--fields", "full"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            search_cli.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "互斥" in err
        assert called["hit"] is False, f"{argv} 应在入口 Fail-Fast，不得触发搜索"


def test_cli_agent_with_intent_propagates_resolution_and_warnings(monkeypatch, capsys):
    called: dict = {}
    monkeypatch.setattr(search_cli, "super_search", _stub_super_search(called))
    monkeypatch.setattr(
        intent_router,
        "resolve_intent",
        lambda intent, **_kw: {
            "intent": intent,
            "engine": "auto",
            "domain": "news",
            "depth": "fast",
            "degraded": True,
            "reason": "缺少 DEEPSEEK_API_KEY，已降级",
            "overridden_by_engine": False,
            "warnings": ["[intent:news] 缺少 DEEPSEEK_API_KEY，已降级"],
        },
    )
    monkeypatch.setattr(sys, "argv", ["search.py", "AI 政策", "--agent", "--intent", "news"])
    search_cli.main()
    out = json.loads(capsys.readouterr().out)
    assert called["engine_domain"] == "news"
    assert out["intent_resolution"]["intent"] == "news"
    assert out["intent_resolution"]["degraded"] is True
    assert any("DEEPSEEK_API_KEY" in w for w in out["warnings"])

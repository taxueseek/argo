#!/usr/bin/env python3
"""test_pacing_and_auth.py — ego-search 并发槽位、同站节奏锁、冷却期与 Cookie 精准鉴权单测。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

EGO_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(EGO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(EGO_SCRIPTS))

import pacing  # noqa: E402
import quality  # noqa: E402
import site_profiles  # noqa: E402


def test_browser_slot_enforces_site_limit(tmp_path):
    # 单站上限=1 时，第二个并发请求在 wait=0.05s 内应触发 SlotBusyError
    with pacing.browser_slot("zhihu.com", wait=1.0, site_slots=1, browser_slots=4, root=tmp_path) as m1:
        assert m1["site"] == "zhihu.com"
        assert m1["wait_ms"] >= 0
        with pytest.raises(pacing.SlotBusyError):
            with pacing.browser_slot("www.zhihu.com", wait=0.05, site_slots=1, browser_slots=4, root=tmp_path):
                pass
        # 其他站点不受单站槽位阻塞
        with pacing.browser_slot("xueqiu.com", wait=0.5, site_slots=1, browser_slots=4, root=tmp_path) as m2:
            assert m2["site"] == "xueqiu.com"


def test_pace_site_enforces_min_interval(tmp_path):
    w1 = pacing.pace_site("zhihu.com", min_interval=0.15, root=tmp_path)
    assert w1 == 0.0
    t0 = time.monotonic()
    w2 = pacing.pace_site("zhihu.com", min_interval=0.15, root=tmp_path)
    elapsed = time.monotonic() - t0
    assert w2 > 0.05
    assert elapsed >= 0.08


def test_cooldown_record_check_and_clear(tmp_path):
    assert pacing.check_cooldown("zhihu.com", root=tmp_path) is None
    pacing.record_cooldown(
        "https://www.zhihu.com/question/1",
        "auth_failed",
        seconds=120,
        login_url="https://www.zhihu.com/signin",
        root=tmp_path,
    )
    cd = pacing.check_cooldown("zhihu.com", root=tmp_path)
    assert cd is not None
    assert cd["reason"] == "auth_failed"
    assert cd["retry_after_seconds"] > 0
    assert "zhihu.com" in pacing.active_cooldowns(root=tmp_path)

    cleared = pacing.clear_cooldown("zhihu.com", root=tmp_path)
    assert cleared == ["zhihu.com"]
    assert pacing.check_cooldown("zhihu.com", root=tmp_path) is None


def test_site_profiles_match_and_preset_api():
    p = site_profiles.match_site("https://www.zhihu.com/api/v4/search_v3?q=test")
    assert p is not None and p.name == "zhihu"
    assert "z_c0" in p.cookie_names

    spec = site_profiles.resolve_preset_api("zhihu", "AI 搜索", n=5)
    assert spec["origin"] == "https://www.zhihu.com"
    assert "limit=5" in spec["api_url"]
    assert "AI" in spec["api_url"]

    with pytest.raises(ValueError):
        site_profiles.resolve_preset_api("unknown_site_xyz", "q")


def test_cookie_auth_probe_strict_vs_document_cookie():
    # 场景 1：CDP 全量键名（strict=True）下，即使页面返回了 200 推荐壳子（>80字），缺 z_c0 也必须翻转 auth_failed
    shell_payload = {
        "url": "https://www.zhihu.com/explore",
        "title": "发现 - 知乎",
        "content": "知乎推荐内容流" * 30,
        "cookie_names": ["_xsrf", "d_c0"],
        "cookie_names_complete": True,
    }
    out = quality.assess_body(shell_payload)
    assert out["handoff_required"] is True
    assert out["login_url"] == "https://www.zhihu.com/signin"
    assert out["quality"]["auth_state"] == "auth_failed"
    assert out["quality"]["cookie_probe"]["missing_cookies"] == ["z_c0"]

    # 场景 2：页面 document.cookie（strict=False）看不到 HttpOnly z_c0，但正文健康 → 不应误杀
    ok_payload = {
        "url": "https://www.zhihu.com/question/123",
        "title": "某个问题 - 知乎",
        "content": "这是一段完整的知乎高赞回答正文内容，讨论架构设计与搜索系统原理。" * 10,
        "cookie_names": ["_xsrf"],
        "cookie_names_complete": False,
    }
    out_ok = quality.assess_body(ok_payload)
    assert out_ok.get("handoff_required") is not True
    assert out_ok["quality"]["auth_state"] == "ok"

    # 场景 3：同源 API 返回空/错且缺登录 Cookie → 精准升级 handoff_required
    api_fail = {
        "api_url": "https://xueqiu.com/query/v1/symbol/search/status.json?q=test",
        "page_url": "https://xueqiu.com/",
        "data": None,
        "data_type": "error",
        "cookie_names": ["device_id"],
    }
    out_api = quality.assess_body(api_fail)
    assert out_api["handoff_required"] is True
    assert out_api["quality"]["auth_state"] == "auth_failed"
    assert out_api["login_url"] == "https://xueqiu.com/"
    assert out_api["handoff"]["public_fallback"]["tier"] == "L0"
    assert "xueqiu.com" in out_api["handoff"]["public_fallback"]["site_scopes"]


def test_shadow_restriction_probe_distinguishes_account_restricted_from_auth_failed():
    # 小红书静默限权：Cookie 完整、接口 200 但 items=[]，懒探针返回 code=300011 → account_restricted
    xhs_shadow = {
        "api_url": "https://www.xiaohongshu.com/search_result?keyword=test",
        "page_url": "https://www.xiaohongshu.com/",
        "data": {"code": 0, "items": []},
        "data_type": "json",
        "cookie_names": ["web_session", "a1"],
        "health_probe": {"code": 300011, "msg": "当前账号存在异常，请切换账号后重试"},
    }
    out = quality.assess_body(xhs_shadow)
    assert out["handoff_required"] is True
    assert out["quality"]["auth_state"] == "account_restricted"
    assert out["quality"]["restricted_code"] == 300011
    assert out["handoff"]["auth_state"] == "account_restricted"
    assert out["handoff"]["public_fallback"]["available"] is True

    # B 站 nav 探针 isLogin=False → 明确归 auth_failed（非限权）
    bili_unauth = quality.assess_health_probe(
        {"code": -101, "data": {"isLogin": False}},
        site="bilibili",
    )
    assert bili_unauth["auth_state"] == "auth_failed"
    assert bili_unauth["handoff_required"] is True

    # 正常探针 → healthy
    bili_ok = quality.assess_health_probe(
        {"code": 0, "data": {"isLogin": True, "uname": "ok"}},
        site="bilibili",
    )
    assert bili_ok["auth_state"] == "healthy"
    assert bili_ok["handoff_required"] is False
